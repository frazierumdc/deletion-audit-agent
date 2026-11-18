"""The agent engine: Claude investigates each case with read-only tools; code renders the steps.

Per case: run_tools() with the investigator prompt -> final JSON answer -> evaluate_answer().
A FAIL gets one reflection turn on the same transcript; a second FAIL (or a turn-limit hit) uses
the rules engine's answer (engine "rules-fallback"). The model chooses the resources and their
categories; their state always comes from the evidence (decide()).

Every answer is cached under CACHE_DIR/agent/ with the usage of the calls that produced it, so
--replay reproduces a run without an API key and errors on a miss. Offline runs (a scripted
stand-in for Claude, see offline.py) cache under CACHE_DIR/agent-offline/ so they can never be
replayed as model answers.
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from deletion_audit.agent.evaluate import build_decisions, evaluate_answer, parse_answer
from deletion_audit.agent.tools import make_tools
from deletion_audit.context import AuditContext, CaseView
from deletion_audit.data import Request
from deletion_audit.decisions import CaseResult
from deletion_audit.engine_rules import run_rules
from deletion_audit.paths import CACHE_DIR, DATASET, PACKAGE_DIR, PROMPTS_DIR
from deletion_audit.planner import render, validate
from deletion_audit.usage import UsageLog, redact

DEFAULT_MODEL = "anthropic:claude-opus-5-5"
PROMPT_FILE = PROMPTS_DIR / "investigator_system.md"
AGENT_CACHE = CACHE_DIR / "agent"
MAX_TURNS = 24
REFLECT_SUFFIX = "\n\nFix these problems and return the corrected JSON."
# Inputs whose bytes decide what the tools return; any change invalidates cached answers.
DATA_FILES = (
    "deletion_requests.csv",
    "sample_deletion_requests.csv",
    "user_identities.csv",
    "user_audit_log.csv",
    "system_activity.csv",
    "deletion_rules.csv",
    "evidence_files.csv",
)
TOOL_SOURCES = (
    "agent/tools.py",
    "state.py",
    "context.py",
    "identity.py",
    "identifiers.py",
    "linking/timing.py",
    "evidence",
)


class MissingApiKey(RuntimeError):
    pass


class ReplayMiss(RuntimeError):
    pass


class AgentRunFailed(RuntimeError):
    """Model calls failed for some cases and --allow-fallback was not given."""

    def __init__(self, failures: dict[str, str]):
        self.failures = failures
        super().__init__(f"{len(failures)} case(s) failed: " + ", ".join(sorted(failures)))


def system_prompt(rules: dict) -> str:
    """The investigator prompt with the rules table filled in. Identical for every case (cache prefix)."""
    table = ["| rule_id | data_category | condition | required_action |", "|---|---|---|---|"]
    table += [
        f"| {r.rule_id} | {r.data_category} | {r.condition} | {r.required_action} |" for r in rules.values()
    ]
    table.append(
        "| (connectors) | DATA_FLOW | A connector or sync can copy or recreate the data | must end disabled |"
    )
    return PROMPT_FILE.read_text(encoding="utf-8").replace("{{DELETION_RULES}}", "\n".join(table))


def _sha(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(hashlib.sha256(p).digest())
    return h.hexdigest()


def data_version() -> str:
    """Hash of everything the tools read: the CSVs, the evidence files listed in the manifest, the
    vision extractions and the tool code. Independent of where the checkout lives, so --replay
    finds the cache in any copy (including an unzipped code.zip)."""
    from deletion_audit.agent.vision import extraction_digest
    from deletion_audit.data import load_manifest

    parts = [(DATASET / name).read_bytes() for name in DATA_FILES]
    for f in sorted(load_manifest(), key=lambda f: f.file_id):
        parts.append(f.file_id.encode() + f.abs_path.read_bytes())
    parts.append(extraction_digest(load_manifest()).encode())  # vision results feed the tools too
    for rel in TOOL_SOURCES:
        p = PACKAGE_DIR / rel
        for f in sorted(p.rglob("*.py")) if p.is_dir() else [p]:
            parts.append(f.read_bytes())
    return _sha(*parts)


def cache_key(model: str, prompt: str, request: Request, timing: list, data_ver: str) -> str:
    case = {"request": asdict(request), "timing": [asdict(c) for c in timing]}
    return _sha(model.encode(), prompt.encode(), json.dumps(case, sort_keys=True).encode(), data_ver.encode())


def user_message(request: Request) -> str:
    return (
        "Audit this deletion request. The request row:\n```json\n"
        + json.dumps(asdict(request), indent=2)
        + "\n```"
    )


class AgentRunner:
    def __init__(
        self,
        ctx: AuditContext,
        model: str = DEFAULT_MODEL,
        replay: bool = False,
        usage_log: UsageLog | None = None,
        client=None,
        cache_dir=AGENT_CACHE,
    ):
        self.ctx, self.model, self.replay = ctx, model, replay
        self.prompt = system_prompt(ctx.rules)
        self.data_ver = data_version()
        self.usage_log = usage_log or UsageLog()
        self.cache_dir = cache_dir
        self.client = client
        if client is None and not replay:
            from deletion_audit.agent.llm import make_client

            self.client = make_client(
                model
            )  # initialise before the thread pool (lazy init is not thread-safe)

    # --- cache ------------------------------------------------------------------------------
    def _cache_path(self, key):
        return self.cache_dir / f"{key}.json"

    def _load(self, key):
        p = self._cache_path(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _store(self, key, entry):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path(key).with_suffix(".tmp")
        tmp.write_text(redact(json.dumps(entry, indent=1, ensure_ascii=False)) + "\n", encoding="utf-8")
        tmp.replace(self._cache_path(key))

    # --- one case ---------------------------------------------------------------------------
    def _investigate(self, view: CaseView) -> dict:
        """Live model calls for one case. Returns the cache entry (answers, outcome, usage)."""
        from deletion_audit.agent.llm import TurnLimitExceeded, run_tools

        usage = []

        def logger(purpose):
            def on_call(turn, u, seconds):
                row = self.usage_log.append(
                    case_id=view.case_id,
                    purpose=purpose,
                    model=self.model,
                    turn=turn,
                    usage=u,
                    seconds=seconds,
                    outcome="ok",
                )
                usage.append(row)

            return on_call

        entry = {"case_id": view.case_id, "model": self.model, "answers": [], "reports": [], "usage": usage}
        tools = make_tools(view)
        try:
            text, transcript = run_tools(
                self.client,
                self.model,
                self.prompt,
                user_message(view.request),
                tools,
                max_turns=MAX_TURNS,
                on_call=logger("investigate"),
            )
            answer, err = parse_answer(text)
            entry["answers"].append(answer if answer is not None else {"unparsed": text[-2000:]})
            ok, report = evaluate_answer(view, answer, err)
            if ok:
                entry["engine"] = "agent"
                return entry
            entry["reports"].append(report)
            text, _ = run_tools(
                self.client,
                self.model,
                self.prompt,
                report + REFLECT_SUFFIX,
                tools,
                max_turns=MAX_TURNS,
                on_call=logger("reflect"),
                transcript=transcript,
            )
            answer, err = parse_answer(text)
            entry["answers"].append(answer if answer is not None else {"unparsed": text[-2000:]})
            ok, report = evaluate_answer(view, answer, err)
            if ok:
                entry["engine"] = "agent-retry"
                return entry
            entry["reports"].append(report)
            entry["engine"] = "rules-fallback"
        except TurnLimitExceeded as exc:
            entry["reports"].append(f"- turn limit reached ({exc}) without a final answer")
            entry["engine"] = "rules-fallback"
        return entry

    def audit(self, request: Request, rules_result: CaseResult) -> CaseResult:
        view = CaseView(self.ctx, request)
        view.timing_candidates = rules_result.timing_candidates
        key = cache_key(self.model, self.prompt, request, view.timing_candidates, self.data_ver)
        entry = self._load(key)
        if entry is None:
            if self.replay:
                raise ReplayMiss(f"{request.case_id}: no cached answer (run live first)")
            entry = self._investigate(view)
            self._store(key, entry)
        return self.result_from_entry(view, entry, rules_result)

    def result_from_entry(self, view: CaseView, entry: dict, rules_result: CaseResult) -> CaseResult:
        engine = entry["engine"]
        notes = [f"agent report: {r}" for r in entry.get("reports", [])]
        if engine == "rules-fallback":
            return CaseResult(
                view.case_id,
                rules_result.status,
                rules_result.steps,
                rules_result.decisions,
                engine,
                notes + rules_result.notes,
                rules_result.timing_candidates,
            )
        answer = entry["answers"][-1]
        notes.append(f"intent: {answer.get('intent', '')}")
        if not answer.get("authorized"):
            return CaseResult(view.case_id, "NOT_APPLICABLE", [], [], engine, notes)
        decisions, more = build_decisions(view, answer)
        status, steps = render(decisions, view.rules)
        notes.extend(more)
        notes.extend(f"contract: {e}" for e in validate(view, status, steps))
        if answer.get("gaps"):
            notes.append(f"gaps: {answer['gaps']}")
        return CaseResult(view.case_id, status, steps, decisions, engine, notes)


def run_agent(
    ctx: AuditContext,
    targets: list[Request],
    pool: list[Request],
    workers: int = 8,
    model: str = DEFAULT_MODEL,
    replay: bool = False,
    allow_fallback: bool = False,
    usage_log: UsageLog | None = None,
    client=None,
    use_phase: bool = True,
    cache_dir=AGENT_CACHE,
    offline: bool = False,
) -> list[CaseResult]:
    """Audit `targets` with the agent. The rules engine runs first: it supplies the global timing
    candidates and the fallback answer for each case.

    offline=True swaps Claude for a scripted stand-in (no key, no network); see offline.py.
    """
    if not offline and not replay and client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        raise MissingApiKey("ANTHROPIC_API_KEY is not set; see README")
    rules_results = {
        r.case_id: r for r in run_rules(ctx, targets, pool, use_phase=use_phase, workers=workers)
    }
    if offline:
        from deletion_audit.agent.offline import offline_client

        client = offline_client(rules_results)
        cache_dir = AGENT_CACHE.parent / "agent-offline" if cache_dir == AGENT_CACHE else cache_dir
        usage_log = usage_log or UsageLog(path=None)
    runner = AgentRunner(
        ctx, model=model, replay=replay, usage_log=usage_log, client=client, cache_dir=cache_dir
    )
    failures = {}

    def job(req):
        try:
            return runner.audit(req, rules_results[req.case_id])
        except ReplayMiss:
            raise
        except Exception as exc:  # API errors: never retried here (llm.py retries 429/5xx)
            failures[req.case_id] = redact(f"{type(exc).__name__}: {exc}")
            rr = rules_results[req.case_id]
            return CaseResult(
                req.case_id,
                rr.status,
                rr.steps,
                rr.decisions,
                "rules-fallback",
                [f"agent error: {failures[req.case_id]}"] + rr.notes,
                rr.timing_candidates,
            )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(job, targets))
    if failures and not allow_fallback:
        raise AgentRunFailed(failures)
    return results


def engine_counts(results: list[CaseResult]) -> dict:
    counts = {}
    for r in results:
        counts[r.engine] = counts.get(r.engine, 0) + 1
    return dict(sorted(counts.items()))
