"""Output checks.

python -m deletion_audit.evaluation.regression --validate output.csv
    Contract validator: exact header, one row per request in sorted order, valid JSON steps,
    every row passes the contract, and every cited evidence id exists and is dated at or
    before that case's audit_at.
python -m deletion_audit.evaluation.regression --compare a.csv b.csv
    Byte-compare two outputs; on a difference, list the case ids whose rows differ.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

from deletion_audit.contract import OUTPUT_HEADER, case_sort_key, validate_row
from deletion_audit.data import load_activity, load_audit_log, load_manifest, load_requests
from deletion_audit.paths import DATASET


def validate_file(path, requests_path=DATASET / "deletion_requests.csv") -> list[str]:
    requests = {r.case_id: r for r in load_requests(requests_path)}
    times = {f.file_id: f.captured_at for f in load_manifest()}
    times.update((a.activity_id, a.ts) for a in load_activity())
    times.update((c.audit_id, c.event_time) for c in load_audit_log())
    errors = []
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    if "\r\n" in raw:
        errors.append("file uses CRLF line endings")
    rows = list(csv.reader(raw.splitlines()))
    if not rows or tuple(rows[0]) != OUTPUT_HEADER:
        return errors + [f"header must be {','.join(OUTPUT_HEADER)}"]
    ids = [r[0] for r in rows[1:]]
    if sorted(ids) != sorted(requests):
        missing, extra = set(requests) - set(ids), set(ids) - set(requests)
        errors.append(
            f"case ids differ from the requests: {len(missing)} missing, {len(extra)} extra, "
            f"{len(ids) - len(set(ids))} duplicated"
        )
    if ids != sorted(ids, key=case_sort_key):
        errors.append("rows are not sorted by case_id")
    for row in rows[1:]:
        if len(row) != 3:
            errors.append(f"{row[:1]}: expected 3 columns, got {len(row)}")
            continue
        cid, status, steps_json = row
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as exc:
            errors.append(f"{cid}: steps_json is not valid JSON ({exc})")
            continue
        errors.extend(f"{cid}: {e}" for e in validate_row(status, steps))
        req = requests.get(cid)
        for i, s in enumerate(steps if isinstance(steps, list) else [], 1):
            ev = s.get("evidence_id") if isinstance(s, dict) else None
            if ev and ev not in times:
                errors.append(f"{cid}: step {i} cites unknown evidence {ev}")
            elif ev and req and times[ev] > req.audit_at:
                errors.append(f"{cid}: step {i} cites {ev} dated {times[ev]}, after audit_at {req.audit_at}")
    return errors


def compare_files(a, b) -> list[str]:
    if Path(a).read_bytes() == Path(b).read_bytes():
        return []
    rows_a = {r[0]: r for r in csv.reader(Path(a).read_text(encoding="utf-8").splitlines())}
    rows_b = {r[0]: r for r in csv.reader(Path(b).read_text(encoding="utf-8").splitlines())}
    diff = sorted({c for c in set(rows_a) | set(rows_b) if rows_a.get(c) != rows_b.get(c)}, key=case_sort_key)
    return (
        [f"{len(diff)} rows differ: {', '.join(diff[:20])}" + (" …" if len(diff) > 20 else "")]
        if diff
        else ["files differ in bytes only (line endings or quoting)"]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", metavar="CSV")
    ap.add_argument(
        "--requests",
        default=str(DATASET / "deletion_requests.csv"),
        help="requests file the output answers (default: <data>/deletion_requests.csv)",
    )
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    if not (args.validate or args.compare):
        ap.error("give --validate or --compare")
    errors = []
    if args.validate:
        errors += validate_file(args.validate, args.requests)
        print(f"validate {args.validate}: " + ("OK" if not errors else f"{len(errors)} problems"))
    if args.compare:
        diff = compare_files(*args.compare)
        print("compare: " + ("identical" if not diff else diff[0]))
        errors += diff
    for e in errors[:50]:
        print("  " + e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
