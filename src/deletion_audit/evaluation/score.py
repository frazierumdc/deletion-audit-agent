"""Evaluation harness: score an engine against the sample answers and the hand-built cases.

  python -m deletion_audit.evaluation.score                          # rules engine, samples
  python -m deletion_audit.evaluation.score --engine both --custom --offline
  python -m deletion_audit.evaluation.score --predictions out.csv    # score an existing file
  python -m deletion_audit.evaluation.score --verbose                # show every mismatch

This is the only code that reads sample_output.csv. Engines solve the requests first and never
see it; the answers are compared afterwards.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from deletion_audit.contract import case_sort_key
from deletion_audit.data import DATASET, load_requests

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


class EngineUnavailable(RuntimeError):
    pass


# --- metrics ---------------------------------------------------------------------------------


def step_key(step) -> tuple:
    return tuple(step[f] for f in FIELDS)


def score(pred: dict, gold: dict) -> dict:
    """pred/gold: {case_id: (status, steps)}. Every gold case must have a prediction."""
    n = len(gold)
    status_ok = row_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits, field_total = Counter(), 0
    confusion = Counter()
    for cid, (g_status, g_steps) in gold.items():
        p_status, p_steps = pred[cid]
        confusion[(g_status, p_status)] += 1
        g_seq, p_seq = [step_key(s) for s in g_steps], [step_key(s) for s in p_steps]
        status_ok += p_status == g_status
        seq_ok += p_seq == g_seq
        row_ok += p_status == g_status and p_seq == g_seq
        g_count, p_count = Counter(g_seq), Counter(p_seq)
        tp += sum((g_count & p_count).values())
        fp += sum((p_count - g_count).values())
        fn += sum((g_count - p_count).values())
        for i, gs in enumerate(g_steps):  # position-aligned with the gold sequence
            field_total += 1
            ps = p_steps[i] if i < len(p_steps) else None
            for f in FIELDS:
                field_hits[f] += int(ps is not None and ps[f] == gs[f])
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n if n else 0.0,
        "row_accuracy": row_ok / n if n else 0.0,
        "sequence_accuracy": seq_ok / n if n else 0.0,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "steps_gold": tp + fn,
        "steps_pred": tp + fp,
        "steps_matched": tp,
        "field_accuracy": {f: field_hits[f] / field_total if field_total else 0.0 for f in FIELDS},
        "field_accuracy_all": sum(field_hits.values()) / (field_total * len(FIELDS)) if field_total else 0.0,
        "confusion": confusion,
    }


def print_report(title: str, m: dict) -> None:
    print(f"## {title}")
    print(f"cases                  : {m['cases']}")
    print(f"status accuracy        : {m['status_accuracy']:.3f}")
    print(f"row accuracy (exact)   : {m['row_accuracy']:.3f}")
    print(f"sequence accuracy      : {m['sequence_accuracy']:.3f}")
    print(
        f"step precision / recall: {m['step_precision']:.3f} / {m['step_recall']:.3f}  (F1 {m['step_f1']:.3f}; "
        f"{m['steps_matched']} of {m['steps_gold']} gold steps matched, {m['steps_pred']} predicted)"
    )
    print(
        f"field accuracy         : overall {m['field_accuracy_all']:.3f}; "
        + ", ".join(f"{k}={v:.3f}" for k, v in m["field_accuracy"].items())
    )
    present = [s for s in STATUSES if any(s in k for k in m["confusion"])]
    print("status confusion       : gold \\ predicted " + " ".join(f"{s[:10]:>10}" for s in present))
    for g in present:
        print(
            f"                         {g:<19}" + " ".join(f"{m['confusion'][(g, p)]:>10}" for p in present)
        )
    print()


def diff_case(label: str, pred, gold) -> None:
    print(f"--- {label}: predicted {pred[0]}, expected {gold[0]}")
    p, g = [step_key(s) for s in pred[1]], [step_key(s) for s in gold[1]]
    for s in g:
        print(("   ok    " if s in p else "   MISS  ") + " ".join(s))
    for s in p:
        if s not in g:
            print("   EXTRA " + " ".join(s))


# --- engines ---------------------------------------------------------------------------------


def pool_requests():
    pool = {}
    for path in (DATASET / "deletion_requests.csv", DATASET / "sample_deletion_requests.csv"):
        for r in load_requests(path):
            pool.setdefault(r.case_id, r)
    return pool


_CTX = None


def _context():
    global _CTX
    if _CTX is None:
        from deletion_audit.context import AuditContext

        _CTX = AuditContext.load()
    return _CTX


# --live calls Claude; --offline uses the scripted stand-in; otherwise cached answers only.
AGENT_MODE = {"live": False, "offline": False}


def run_engine(engine: str, targets: list, pool: list, workers: int) -> dict:
    """{case_id: (status, steps)} for the targets."""
    if engine == "rules":
        from deletion_audit.engine_rules import run_rules

        results = run_rules(_context(), targets, pool, workers=workers)
    elif engine == "agent":
        from deletion_audit.agent.audit_agent import MissingApiKey, ReplayMiss, run_agent

        try:
            results = run_agent(
                _context(),
                targets,
                pool,
                workers=workers,
                offline=AGENT_MODE["offline"],
                replay=not (AGENT_MODE["live"] or AGENT_MODE["offline"]),
            )
        except (MissingApiKey, ReplayMiss) as exc:
            raise EngineUnavailable(f"{exc}; pass --offline, or --live to call the model") from None
    else:
        raise ValueError(engine)
    return {r.case_id: (r.status, r.steps) for r in results}


# --- data ------------------------------------------------------------------------------------


def load_gold(path=DATASET / "sample_output.csv") -> dict:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def load_predictions(path) -> dict:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def expected_answer(case: dict, sample_gold: dict):
    exp = case["expected"]
    if "same_as_sample" in exp:
        return sample_gold[exp["same_as_sample"]]
    return exp["deletion_status"], [dict(zip(FIELDS, s, strict=False)) for s in exp["steps"]]


# --- runs ------------------------------------------------------------------------------------


def evaluate_samples(engine: str, workers: int, verbose: bool, predictions=None) -> dict:
    gold = load_gold()
    if predictions:
        pred = load_predictions(predictions)
        missing = sorted(set(gold) - set(pred))
        if missing:
            raise SystemExit(f"{predictions} has no row for {len(missing)} sample cases, e.g. {missing[:5]}")
        title = f"Samples: {Path(predictions).name}"
    else:
        pool = pool_requests()
        targets = load_requests(DATASET / "sample_deletion_requests.csv")
        pred = run_engine(engine, targets, list(pool.values()), workers)
        title = f"Samples: {engine} engine"
    m = score(pred, gold)
    print_report(title, m)
    if verbose:
        for cid in sorted(gold, key=case_sort_key):
            if pred[cid][0] != gold[cid][0] or [step_key(s) for s in pred[cid][1]] != [
                step_key(s) for s in gold[cid][1]
            ]:
                diff_case(cid, pred[cid], gold[cid])
        print()
    return m


def evaluate_custom(engine: str, workers: int, verbose: bool) -> dict:
    from deletion_audit.evaluation.custom_cases import CASES

    sample_gold = load_gold()
    pool = pool_requests()
    pred, gold = {}, {}
    for case in CASES:
        base = pool[case["base_case"]]
        rewritten = replace(base, **case["overrides"])
        case_pool = dict(pool)
        case_pool[base.case_id] = rewritten  # the rewrite replaces its base request
        out = run_engine(engine, [rewritten], list(case_pool.values()), workers)
        pred[case["id"]] = out[base.case_id]
        gold[case["id"]] = expected_answer(case, sample_gold)
    m = score(pred, gold)
    print_report(f"Hand-built cases: {engine} engine", m)
    for case in CASES:
        cid = case["id"]
        status_ok = pred[cid][0] == gold[cid][0]
        exact = [step_key(s) for s in pred[cid][1]] == [step_key(s) for s in gold[cid][1]]
        print(f"  {cid:32} status {'ok ' if status_ok else 'BAD'}  steps {'exact' if exact else 'differ'}")
        if verbose and not (status_ok and exact):
            diff_case(cid, pred[cid], gold[cid])
    print()
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=("rules", "agent", "both"), default="rules")
    ap.add_argument("--custom", action="store_true", help="also run the hand-built cases")
    ap.add_argument(
        "--predictions", help="score this predictions CSV on the samples instead of running an engine"
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--live",
        action="store_true",
        help="agent engine: call the model for cases with no cached answer (costs money; needs a key)",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="agent engine: scripted stand-in for the model (no key, no network)",
    )
    args = ap.parse_args()
    AGENT_MODE["live"] = args.live
    AGENT_MODE["offline"] = args.offline
    if args.live:
        from dotenv import load_dotenv

        from deletion_audit.paths import ROOT

        load_dotenv(ROOT / ".env", override=False)  # the key stays in the environment, never printed
    if args.predictions:
        evaluate_samples("file", args.workers, args.verbose, args.predictions)
        return 0
    engines = ("rules", "agent") if args.engine == "both" else (args.engine,)
    unavailable = []
    for engine in engines:
        try:
            evaluate_samples(engine, args.workers, args.verbose)
            if args.custom:
                evaluate_custom(engine, args.workers, args.verbose)
        except EngineUnavailable as exc:
            print(f"## {engine} engine: not scored ({exc})\n")
            unavailable.append(engine)
    return 1 if unavailable and len(unavailable) == len(engines) else 0


if __name__ == "__main__":
    sys.exit(main())
