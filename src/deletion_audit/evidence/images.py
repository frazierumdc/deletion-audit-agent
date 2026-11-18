"""PNG and PDF evidence: map cached vision extractions (audit/agent/vision.py) to Observations.

This module never calls a model. It reads the vision cache for the current model and prompt (or the
dataset's hand-written fixture); a file with neither yields no observations and is reported as such.

Mapping rules (code decisions, not the model's):
- a record with a printed resource_id is an Observation of that resource, present=True;
- a record without one is matched to a data-catalog row through its labels (and, for the page's
  subject record, its user_ids), using the label in parentheses, e.g. "object_42
  (delivery_note_42.pdf)"; the observation only becomes visible once both the file and
  the catalog row exist;
- exposed: the record shows names, emails, phones or hashes (plus the page subject's, when the
  record is part of the page subject); analytics records are also exposed by a user_id;
- flow and restore flags come from state hints; a backup with no restore_blocked hint is restorable;
- events (activity feeds, query logs) never prove presence; they are kept for read_evidence.
"""

import json
import re
from functools import lru_cache

from deletion_audit.evidence.model import Observation, clip
from deletion_audit.identifiers import norm_identifier
from deletion_audit.systems import system_for

MISSING_NOTE = "no cached vision extraction or fixture (run: python -m deletion_audit --vision)"
_CATALOG_RE = re.compile(r"\b(?:linked|confirmed) (\S+) \(([^)]+)\)")
IDENTIFIER_KEYS = ("names", "emails", "phones", "user_ids", "hashes")
EXPOSING = ("names", "emails", "phones", "hashes")


@lru_cache(maxsize=1)
def catalog_labels() -> dict:
    """label -> [(resource_id, activity_id, ts)] from LINK_RESOURCE_IDENTITY rows."""
    from deletion_audit.data import load_activity

    out = {}
    for a in load_activity():
        if a.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        m = _CATALOG_RE.search(a.details)
        if m and m.group(1) == a.resource_id and m.group(2) != a.resource_id:
            out.setdefault(m.group(2), []).append((a.resource_id, a.activity_id, a.ts))
    return out


def _ident(d) -> dict:
    d = d if isinstance(d, dict) else {}
    return {k: [v for v in d.get(k) or [] if isinstance(v, str) and v.strip()] for k in IDENTIFIER_KEYS}


def _merge(a: dict, b: dict) -> dict:
    return {k: a[k] + [v for v in b[k] if v not in a[k]] for k in IDENTIFIER_KEYS}


def _tokens(ident: dict) -> frozenset:
    toks = set()
    for k in ("emails", "phones", "hashes", "user_ids"):
        for v in ident[k]:
            tok = norm_identifier(v)
            if tok:
                toks.add(tok)
    return frozenset(toks)


def record_to_observation(rec: dict, subject: dict | None, eid: str, captured: str) -> Observation | None:
    ident = _ident(rec.get("identifiers"))
    if rec.get("part_of_page_subject") and subject:
        ident = _merge(ident, _ident(subject))
    rid, visible_from, via = rec.get("resource_id"), captured, ""
    if not rid:
        refs = list(rec.get("labels") or [])
        if rec.get("where") == "header" or rec.get("part_of_page_subject"):
            # A user_id names the record itself only when the record is the page's subject (a
            # reference registry entry); a register row that mentions a user is a different record.
            refs += _ident(rec.get("identifiers"))["user_ids"]
        hits = sorted({h for ref in refs for h in catalog_labels().get(ref, ())})
        if len({h[0] for h in hits}) != 1:
            return None  # no catalog match, or an ambiguous one: keep it unlinked
        rid, via, ts = hits[0]
        visible_from = max(captured, ts)
    system = system_for(rid)
    if system is None:
        return None
    hints = set(rec.get("state_hints") or [])
    exposed = any(ident[k] for k in EXPOSING)
    if system == "analytics_platform":
        exposed = exposed or bool(ident["user_ids"])
    flow = restore = None
    if system == "integration_service":
        flow = (
            True
            if hints & {"active", "continuous_sync"}
            else False
            if "paused_or_disabled" in hints
            else None
        )
    if system == "backup_service":
        restore = "restore_blocked" not in hints
    present = not ("deleted" in hints and system != "identity_service")
    source = f"vision:{rec.get('where') or 'record'}" + (f" via catalog:{via}" if via else "")
    return Observation(
        eid,
        visible_from,
        captured,
        system,
        rid,
        present=present,
        exposed=exposed if present else None,
        flow_enabled=flow,
        restore_allowed=restore,
        keys=_tokens(ident),
        source=source,
        raw=clip(rec),
    )


def parse_media(store, eid, captured, path) -> str:
    """Add observations for one PNG/PDF file from its cached extraction; returns a note for the file report."""
    from deletion_audit.agent.vision import load_extraction

    entry = load_extraction(path)
    if entry is None:
        return MISSING_NOTE
    if entry.get("status") != "ok":
        return "vision extraction failed: " + "; ".join(entry.get("errors", [[]])[-1])[:300]
    ext = entry["extraction"]
    subject = ext["artifact"].get("page_subject")
    added = 0
    for rec in ext.get("records", []):
        added += store.add(record_to_observation(rec, subject, eid, captured))
    store.media[eid] = {
        "captured_at": captured,
        "artifact": ext.get("artifact"),
        "events": ext.get("events", []),
        "unreadable": ext.get("unreadable", []),
    }
    return "" if added else "vision: no record maps to a resource id"


def media_summary(store, eid) -> str:
    """Events and page facts of a media file, for read_evidence."""
    m = store.media.get(eid)
    return clip(json.dumps(m, ensure_ascii=False, sort_keys=True)) if m else ""
