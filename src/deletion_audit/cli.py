"""Deletion-audit command line.

    python -m deletion_audit --engine rules                  # deterministic, no key
    python -m deletion_audit --engine agent --offline        # agent pipeline, scripted model, no key
    python -m deletion_audit --engine agent                  # Claude (needs ANTHROPIC_API_KEY)
    python -m deletion_audit --engine agent --replay         # cached Claude answers only, no key

Data comes from data/synthetic/ unless AUDIT_DATA_DIR points elsewhere.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from deletion_audit.paths import DATASET, OUTPUT_CSV, ROOT, RUNS_DIR

DEFAULT_MODEL = "anthropic:claude-opus-5-5"


def load_env() -> None:
    """Load an uncommitted .env if python-dotenv is installed; a real environment variable wins."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Audit deletion requests and write output.csv.")
    p.add_argument("--engine", choices=("rules", "agent"), default="rules",
                   help="rules: deterministic, no API key needed. agent: Claude via aisuite.")  # fmt: skip
    p.add_argument("--model", default=os.environ.get("AGENT_MODEL", DEFAULT_MODEL),
                   help="provider:model for the agent engine (default: %(default)s)")  # fmt: skip
    p.add_argument("--concurrency", type=int, default=8,
                   help="worker threads (agent engine) or processes (rules engine)")  # fmt: skip
    p.add_argument("--offline", action="store_true",
                   help="agent engine: replace Claude with a scripted stand-in (no key, no network)")  # fmt: skip
    p.add_argument("--replay", action="store_true",
                   help="agent engine: use only cached Claude answers (no key needed); error on a miss")  # fmt: skip
    p.add_argument("--allow-fallback", action="store_true",
                   help="agent engine: use the rules engine when the key is missing or a case fails")  # fmt: skip
    p.add_argument("--requests", default=str(DATASET / "deletion_requests.csv"),
                   help="requests to audit (default: <data>/deletion_requests.csv)")  # fmt: skip
    p.add_argument("--output", default=str(OUTPUT_CSV), help="output CSV (default: out/output.csv)")
    p.add_argument("--no-phase", action="store_true",
                   help="ablation: turn off the calibrated run-phase timing signal")  # fmt: skip
    p.add_argument("--lenient-ownership", action="store_true",
                   help="ablation only: ownership may use evidence after audit_at (breaks the time rule)")  # fmt: skip
    p.add_argument("--evidence-report", action="store_true", help="print per-file parse counts and exit")
    p.add_argument("--vision", action="store_true",
                   help="transcribe PNG/PDF evidence with Claude (replaces fixtures; needs the key)")  # fmt: skip
    return p.parse_args(argv)


def main(argv=None) -> int:
    load_env()
    args = parse_args(argv)
    os.environ["AGENT_MODEL"] = args.model  # the vision cache lookup keys on it
    if args.evidence_report:
        from deletion_audit.evidence.loader import load_evidence, report_lines

        print("\n".join(report_lines(load_evidence())))
        return 0
    if args.vision:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set; see README", file=sys.stderr)
            return 2
        return run_vision_step(args, use_fixtures=False)
    engine = args.engine
    live = engine == "agent" and not (args.replay or args.offline)
    if live and not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.allow_fallback:
            print("ANTHROPIC_API_KEY is not set; see README (or use --offline)", file=sys.stderr)
            return 2
        print("ANTHROPIC_API_KEY is not set; falling back to the rules engine", file=sys.stderr)
        engine = "rules"
    if engine == "agent":
        return run_agent_engine(args)
    return run_rules_engine(args)


def pool_requests(target_path):
    """Every known request: they all compete for the same unowned workflow rows."""
    from deletion_audit.data import load_requests

    pool = {}
    for path in (DATASET / "deletion_requests.csv", DATASET / "sample_deletion_requests.csv", target_path):
        for r in load_requests(path):
            pool.setdefault(r.case_id, r)
    return list(pool.values())


def run_vision_step(args, use_fixtures: bool = True) -> int:
    """Extract PNG/PDF files that have no cached extraction (one SDK call each, plus one retry)."""
    from deletion_audit.agent.vision import missing_extractions, run_vision
    from deletion_audit.data import load_manifest
    from deletion_audit.usage import UsageLog

    todo = missing_extractions(load_manifest(), args.model, use_fixtures=use_fixtures)
    usage_log = UsageLog()
    entries = run_vision(todo, usage_log, model=args.model, workers=args.concurrency) if todo else {}
    failed = sorted(fid for fid, e in entries.items() if e["status"] != "ok")
    print(
        f"vision: {len(entries)} file(s) extracted now, {len(failed)} failed"
        + (f" ({', '.join(failed)})" if failed else "")
        + f"; usage run_id {usage_log.run_id}"
    )
    return 1 if failed else 0


def warn_missing_vision(args) -> list:
    from deletion_audit.agent.vision import missing_extractions
    from deletion_audit.data import load_manifest

    missing = missing_extractions(load_manifest(), args.model)
    if missing:
        print(
            f"note: {len(missing)} PNG/PDF file(s) have no extraction and add no evidence "
            f"({', '.join(f.file_id for f in missing)}); run --vision",
            file=sys.stderr,
        )
    return missing


def _report(results, args, extra: str = "") -> int:
    from collections import Counter

    from deletion_audit.contract import write_output

    write_output([(r.case_id, r.status, r.steps) for r in results], args.output)
    counts = Counter(r.status for r in results)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"wrote {len(results)} rows to {args.output}: {summary}{extra}")
    bad = [r for r in results if any(n.startswith("contract:") for n in r.notes)]
    for r in bad:
        print(
            f"  {r.case_id}: " + "; ".join(n for n in r.notes if n.startswith("contract:")), file=sys.stderr
        )
    return 1 if bad else 0


def run_rules_engine(args) -> int:
    from deletion_audit.context import AuditContext
    from deletion_audit.data import load_requests
    from deletion_audit.engine_rules import run_rules

    warn_missing_vision(args)
    results = run_rules(
        AuditContext.load(),
        load_requests(args.requests),
        pool_requests(args.requests),
        use_phase=not args.no_phase,
        strict_ownership=not args.lenient_ownership,
        workers=args.concurrency,
    )
    return _report(results, args)


def run_agent_engine(args) -> int:
    from deletion_audit.agent.audit_agent import AgentRunFailed, ReplayMiss, engine_counts, run_agent
    from deletion_audit.context import AuditContext
    from deletion_audit.data import load_requests
    from deletion_audit.usage import UsageLog

    if args.replay or args.offline:
        if warn_missing_vision(args) and args.replay:
            print("--replay: vision extractions are missing", file=sys.stderr)
            return 4
    else:
        code = run_vision_step(args)  # one call per file with no cache and no fixture
        if code:
            return code
    usage_log = UsageLog(path=None) if args.offline else UsageLog()
    started = utc_now()
    try:
        results = run_agent(
            AuditContext.load(),
            load_requests(args.requests),
            pool_requests(args.requests),
            workers=args.concurrency,
            model=args.model,
            replay=args.replay,
            allow_fallback=args.allow_fallback,
            usage_log=usage_log,
            use_phase=not args.no_phase,
            offline=args.offline,
        )
    except ReplayMiss as exc:
        print(f"--replay: {exc}", file=sys.stderr)
        return 4
    except AgentRunFailed as exc:
        for cid, err in sorted(exc.failures.items()):
            print(f"  {cid}: {err}", file=sys.stderr)
        print(f"{exc}; output not written (rerun, or pass --allow-fallback)", file=sys.stderr)
        return 5
    mode = (
        " (offline: scripted model)" if args.offline else " (replay: no model calls)" if args.replay else ""
    )
    code = _report(results, args, f"; engines: {engine_counts(results)}{mode}")
    if not (args.replay or args.offline):  # the usage report describes live runs only
        record_agent_run(args, usage_log.run_id, started, results)
    return code


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rel(path) -> str:
    from pathlib import Path

    p = Path(path).resolve()
    return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p.name


def record_agent_run(args, run_id: str, started: str, results) -> None:
    """Append the run manifest to runs/agent_runs.jsonl and write runs/usage_report.md."""
    import hashlib
    import json
    from pathlib import Path

    from deletion_audit.agent.audit_agent import engine_counts
    from deletion_audit.agent.llm import EFFORT, MAX_TOKENS
    from deletion_audit.evaluation.usage_report import write_report
    from deletion_audit.usage import redact

    run = {
        "run_id": run_id,
        "started": started,
        "finished": utc_now(),
        "command": redact("python -m deletion_audit " + " ".join(sys.argv[1:])).strip(),
        "requests": _rel(args.requests),
        "output": _rel(args.output),
        "output_sha256": hashlib.sha256(Path(args.output).read_bytes()).hexdigest(),
        "cases": len(results),
        "engines": engine_counts(results),
        "model": args.model,
        "backend": "aisuite 0.2.0 tool loop (investigate, reflect); Anthropic SDK (vision)",
        "effort": EFFORT,
        "max_tokens": MAX_TOKENS,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with (RUNS_DIR / "agent_runs.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(run) + "\n")
    print(f"wrote {_rel(write_report(run))}")


if __name__ == "__main__":
    sys.exit(main())
