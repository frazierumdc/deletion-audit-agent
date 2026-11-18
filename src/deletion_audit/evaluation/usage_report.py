"""Write runs/usage_report.md for a live agent run (default: the latest one).
Reads only the usage log, the run manifest and the vision cache.

  python -m deletion_audit.evaluation.usage_report [--run-id RUN_ID] [--output PATH]
"""

import argparse
import json
import sys
from pathlib import Path

from deletion_audit.paths import RUNS_DIR
from deletion_audit.usage import load_usage, render_usage_report

RUNS_LOG = RUNS_DIR / "agent_runs.jsonl"
REPORT = RUNS_DIR / "usage_report.md"


def load_runs() -> list[dict]:
    if not RUNS_LOG.exists():
        return []
    return [json.loads(line) for line in RUNS_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def vision_rows(model: str) -> list[dict]:
    """Usage of the calls that produced the cached extractions this run used."""
    from deletion_audit.agent.vision import load_cached, media_files
    from deletion_audit.data import load_manifest

    rows = []
    for f in media_files(load_manifest()):
        entry = load_cached(f.abs_path, model)
        rows.extend((entry or {}).get("usage", []))
    return rows


def write_report(run: dict, path: Path = REPORT) -> Path:
    md = render_usage_report(run, load_usage(run_ids={run["run_id"]}), vision_rows(run["model"]))
    Path(path).write_text(md, encoding="utf-8", newline="\n")
    return Path(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id")
    ap.add_argument("--output", default=str(REPORT))
    args = ap.parse_args()
    runs = load_runs()
    if args.run_id:
        runs = [r for r in runs if r["run_id"] == args.run_id]
    if not runs:
        print("no matching agent run in " + str(RUNS_LOG), file=sys.stderr)
        return 1
    print(f"wrote {write_report(runs[-1], Path(args.output))} for run {runs[-1]['run_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
