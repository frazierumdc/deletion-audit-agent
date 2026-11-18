"""Time-rule check: later evidence must not change any answer.

For every distinct audit_at among the target cases, re-run the rules engine with every evidence
file and activity row dated after that time removed, and compare those cases' rows with the
full run. Any difference means data from after audit_at reached the answer.

  python -m deletion_audit.evaluation.leak_check [--requests FILE] [--workers 8]
"""

import argparse
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

from deletion_audit.context import AuditContext
from deletion_audit.contract import case_sort_key, serialize_steps
from deletion_audit.data import load_requests
from deletion_audit.engine_rules import run_rules
from deletion_audit.paths import DATASET


def pool_requests(target_path):
    pool = {}
    for path in (DATASET / "deletion_requests.csv", DATASET / "sample_deletion_requests.csv", target_path):
        for r in load_requests(path):
            pool.setdefault(r.case_id, r)
    return list(pool.values())


def _rows(ctx, targets, pool, workers=1):
    return {
        r.case_id: (r.status, serialize_steps(r.steps))
        for r in run_rules(ctx, targets, pool, workers=workers)
    }


def _check(job):
    cutoff, case_ids, target_path = job
    pool = pool_requests(target_path)
    targets = [r for r in pool if r.case_id in case_ids]
    return cutoff, _rows(AuditContext.load(drop_after=cutoff), targets, pool)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requests", default=str(DATASET / "deletion_requests.csv"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    targets = load_requests(args.requests)
    pool = pool_requests(args.requests)
    full = _rows(AuditContext.load(), targets, pool, args.workers)
    by_cutoff = defaultdict(set)
    for r in targets:
        by_cutoff[r.audit_at].add(r.case_id)
    jobs = [(t, ids, args.requests) for t, ids in sorted(by_cutoff.items())]
    leaks = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for _cutoff, rows in ex.map(_check, jobs):
            leaks += [cid for cid, row in rows.items() if row != full[cid]]
    leaks.sort(key=case_sort_key)
    print(
        f"{len(targets)} cases, {len(jobs)} distinct audit_at cutoffs: "
        + (
            "no answer changes when later data is removed"
            if not leaks
            else f"{len(leaks)} answers depend on later data: {', '.join(leaks)}"
        )
    )
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
