"""Vision step: one Claude call per PNG/PDF evidence file, transcribing it to vision_extraction/v1 JSON.

Calls go through the Anthropic SDK directly (llm.media_call), because aisuite 0.2.0 can't send
PDF document blocks. Only the file block is sent: no file name, path or manifest row, so the
model can't guess from names. The JSON is validated in code; a failure gets one retry with the
error list, and a second failure records the file as failed (no observations).

Each result is cached with its usage in CACHE_DIR/vision/<sha256(model + prompt bytes + file bytes)>.json,
so editing the prompt invalidates the cache. Mapping extractions to Observations is code, in
evidence/images.py, which reads this cache only (never the API).

With no cached extraction, a hand-written fixture in <data>/vision_fixtures/<file name>.json is
used instead, so the synthetic dataset runs end to end without an API key. `--vision` replaces
fixtures with real Claude transcriptions (cached, not written over the fixture).
"""

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from deletion_audit.paths import CACHE_DIR, DATASET, PROMPTS_DIR
from deletion_audit.usage import redact

DEFAULT_MODEL = "anthropic:claude-opus-5-5"
PROMPT_FILE = PROMPTS_DIR / "vision_extraction.md"
VISION_CACHE = CACHE_DIR / "vision"
MEDIA_EXTENSIONS = ("png", "pdf")
SCHEMA = "vision_extraction/v1"
STATE_HINTS = {
    "active",
    "continuous_sync",
    "paused_or_disabled",
    "ready_for_download",
    "in_progress",
    "expired",
    "retained",
    "restore_blocked",
    "restored",
    "deleted",
    "redacted",
    "anonymized",
}
RESOURCE_ID_RE = re.compile(
    r"^(account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"legal_hold|marketing|object|cache|email_delivery)_\d+$"
)
IDENTIFIER_KEYS = ("names", "emails", "phones", "user_ids", "hashes")
RETRY_MESSAGE = (
    "Your JSON has these problems:\n{errors}\n\nReturn the corrected JSON object only, following the "
    "same instructions. Transcribe only what the file shows."
)


def vision_model() -> str:
    return os.environ.get("AGENT_MODEL", DEFAULT_MODEL)


def cache_path(path: Path, model: str | None = None, cache_dir: Path = VISION_CACHE) -> Path:
    h = hashlib.sha256()
    for part in ((model or vision_model()).encode(), PROMPT_FILE.read_bytes(), Path(path).read_bytes()):
        h.update(hashlib.sha256(part).digest())
    return cache_dir / f"{h.hexdigest()}.json"


def load_cached(path: Path, model: str | None = None, cache_dir: Path = VISION_CACHE) -> dict | None:
    p = cache_path(path, model, cache_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def fixture_path(path: Path) -> Path:
    return DATASET / "vision_fixtures" / f"{Path(path).name}.json"


def load_extraction(path: Path, model: str | None = None, cache_dir: Path = VISION_CACHE) -> dict | None:
    """The cached Claude extraction for a file, else its hand-written fixture, else None."""
    entry = load_cached(path, model, cache_dir)
    if entry is None and fixture_path(path).exists():
        entry = json.loads(fixture_path(path).read_text(encoding="utf-8"))
    return entry


def parse_json_object(text: str) -> dict | None:
    """The JSON object in the reply (bare, or inside a fence despite the instructions)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _check_ids(where: str, ids, errors: list) -> None:
    for rid in ids or []:
        if not isinstance(rid, str) or not RESOURCE_ID_RE.match(rid):
            errors.append(
                f"{where}: {rid!r} is not a resource id (prefix + digits); put other references in labels"
            )


def _check_identifiers(where: str, ident, errors: list) -> None:
    if not isinstance(ident, dict) or any(not isinstance(ident.get(k), list) for k in IDENTIFIER_KEYS):
        errors.append(f"{where}: identifiers must have the list keys {', '.join(IDENTIFIER_KEYS)}")


def _check_hints(where: str, hints, errors: list) -> None:
    for h in hints or []:
        if h not in STATE_HINTS:
            errors.append(f"{where}: state hint {h!r} is not in the allowed list")


def validate_extraction(obj: dict | None) -> list[str]:
    """Schema problems in one extraction; [] when it is usable."""
    if obj is None:
        return ["the reply is not a JSON object"]
    errors = []
    if obj.get("schema") != SCHEMA:
        errors.append(f'"schema" must be "{SCHEMA}"')
    for key, kind in (("artifact", dict), ("records", list), ("events", list), ("unreadable", list)):
        if not isinstance(obj.get(key), kind):
            errors.append(f'"{key}" must be a {kind.__name__}')
    if errors:
        return errors
    art = obj["artifact"]
    if not isinstance(art.get("tables", []), list):
        errors.append('"artifact.tables" must be a list')
    for t in art.get("tables") or []:
        if not isinstance(t, dict) or not isinstance(t.get("rows_extracted"), int):
            errors.append(f"artifact.tables: {t!r} needs an integer rows_extracted")
    subject = art.get("page_subject")
    if subject is not None:
        _check_identifiers("artifact.page_subject", subject, errors)
        if isinstance(subject, dict):
            _check_ids("artifact.page_subject.resource_ids", subject.get("resource_ids"), errors)
    for i, rec in enumerate(obj["records"], 1):
        where = f"records[{i}]"
        if not isinstance(rec, dict):
            errors.append(f"{where} is not an object")
            continue
        if rec.get("resource_id") is not None:
            _check_ids(where + ".resource_id", [rec["resource_id"]], errors)
        _check_identifiers(where, rec.get("identifiers"), errors)
        _check_hints(where, rec.get("state_hints"), errors)
        if not isinstance(rec.get("labels", []), list):
            errors.append(f"{where}.labels must be a list")
    for i, ev in enumerate(obj["events"], 1):
        where = f"events[{i}]"
        if not isinstance(ev, dict):
            errors.append(f"{where} is not an object")
            continue
        _check_ids(where + ".resource_ids", ev.get("resource_ids"), errors)
        _check_identifiers(where, ev.get("identifiers"), errors)
        _check_hints(where, ev.get("state_hints"), errors)
    return errors


def extract_file(
    file, usage_log, model: str | None = None, sdk_client=None, cache_dir: Path = VISION_CACHE
) -> dict:
    """Extract one evidence file (an EvidenceFile), using the cache when present."""
    from deletion_audit.agent.llm import media_call

    model = model or vision_model()
    target = cache_path(file.abs_path, model, cache_dir)
    if target.exists():
        return json.loads(target.read_text(encoding="utf-8"))
    system = PROMPT_FILE.read_text(encoding="utf-8")
    entry = {
        "schema": SCHEMA,
        "model": model,
        "status": "failed",
        "extraction": None,
        "errors": [],
        "usage": [],
    }
    followups = []
    for attempt in (1, 2):
        started = time.monotonic()
        text, usage, stop = media_call(
            file.abs_path, system, model=model, sdk_client=sdk_client, followups=followups
        )
        seconds = time.monotonic() - started
        obj = parse_json_object(text)
        errors = validate_extraction(obj)
        if stop == "max_tokens":
            errors.insert(0, "the reply was cut off at max_tokens")
        entry["usage"].append(
            usage_log.append(
                case_id=file.file_id,
                purpose="vision",
                model=model,
                turn=attempt,
                usage=usage,
                seconds=seconds,
                outcome="ok" if not errors else "invalid",
                backend="anthropic",
            )
        )
        if not errors:
            entry.update(status="ok", extraction=obj, errors=[])
            break
        entry["errors"].append(errors)
        followups = [
            {"role": "assistant", "content": text or "(empty)"},
            {"role": "user", "content": RETRY_MESSAGE.format(errors="\n".join(f"- {e}" for e in errors))},
        ]
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(redact(json.dumps(entry, indent=1, ensure_ascii=False)) + "\n", encoding="utf-8")
    tmp.replace(target)
    return entry


def media_files(manifest) -> list:
    return [f for f in manifest if f.extension in MEDIA_EXTENSIONS]


def missing_extractions(
    manifest, model: str | None = None, cache_dir: Path = VISION_CACHE, use_fixtures: bool = True
) -> list:
    """Media files with no cached extraction (and, when use_fixtures, no fixture either)."""
    return [
        f
        for f in media_files(manifest)
        if not cache_path(f.abs_path, model, cache_dir).exists()
        and not (use_fixtures and fixture_path(f.abs_path).exists())
    ]


def run_vision(
    files,
    usage_log,
    model: str | None = None,
    workers: int = 8,
    sdk_client=None,
    cache_dir: Path = VISION_CACHE,
) -> dict:
    """Extract the given PNG/PDF files (cached ones are reused). Returns {file_id: entry}."""
    import anthropic

    files = media_files(files)
    sdk_client = sdk_client or anthropic.Anthropic()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        entries = list(ex.map(lambda f: extract_file(f, usage_log, model, sdk_client, cache_dir), files))
    return {f.file_id: e for f, e in zip(files, entries, strict=False)}


def extraction_digest(manifest, model: str | None = None, cache_dir: Path = VISION_CACHE) -> str:
    """Hash of the cached extractions in use, so agent answers are re-derived when they change."""
    h = hashlib.sha256()
    for f in media_files(manifest):
        entry = load_extraction(f.abs_path, model, cache_dir)
        h.update(f.file_id.encode() + (json.dumps(entry, sort_keys=True).encode() if entry else b"missing"))
    return h.hexdigest()
