"""The output contract: operations, step ordering, serialisation, validation and output.csv."""

import csv
import json
import re
from pathlib import Path

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
OUTPUT_HEADER = ("case_id", "deletion_status", "steps_json")
STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")

# (phase, operation, state) in the order steps are sorted within a phase.
OPERATIONS = (
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
)
OP_PHASE = {op: phase for phase, op, _ in OPERATIONS}
OP_STATE = {op: state for _, op, state in OPERATIONS}
OP_RANK = {op: i for i, (_, op, _) in enumerate(OPERATIONS)}
PHASE_RANK = {phase: i for i, phase in enumerate(PHASES)}


def step_sort_key(step: dict) -> tuple:
    """Phase, then operation in table order, then system_id, then resource_id (plain string sort)."""
    op = step["operation"]
    return (PHASE_RANK[OP_PHASE[op]], OP_RANK[op], step["system_id"], step["resource_id"])


def make_step(operation: str, system_id: str, resource_id: str, evidence_id: str = "") -> dict:
    """An unnumbered step; phase and state are derived from the operation."""
    if operation not in OP_PHASE:
        raise ValueError(f"unknown operation: {operation}")
    return {
        "phase": OP_PHASE[operation],
        "system_id": system_id,
        "resource_id": resource_id,
        "operation": operation,
        "state": OP_STATE[operation],
        "evidence_id": evidence_id or "",
    }


def render_steps(steps) -> list[dict]:
    """Sort, de-duplicate and number steps, emitting exactly STEP_KEYS in contract order.

    Duplicates (same operation, system and resource) keep the first one given.
    """
    seen, unique = set(), []
    for s in steps:
        key = (s["operation"], s["system_id"], s["resource_id"])
        if key not in seen:
            seen.add(key)
            unique.append(
                make_step(s["operation"], s["system_id"], s["resource_id"], s.get("evidence_id", ""))
            )
    unique.sort(key=step_sort_key)
    return [{"step_no": i, **s} for i, s in enumerate(unique, 1)]


def serialize_steps(steps) -> str:
    return json.dumps(list(steps), separators=(",", ":"), ensure_ascii=False)


def case_sort_key(case_id: str) -> tuple:
    """Numeric order of case ids (case_33 before case_100), as in the input files."""
    m = re.fullmatch(r"(.*?)(\d+)", case_id)
    return (m.group(1), int(m.group(2))) if m else (case_id, -1)


def validate_row(status: str, steps: list) -> list[str]:
    """Contract errors for one output row (empty list if it is valid)."""
    errors = []
    if status not in STATUSES:
        return [f"invalid status {status!r}"]
    if not isinstance(steps, list):
        return ["steps_json is not a JSON array"]
    if status == "NOT_APPLICABLE":
        return [] if not steps else ["NOT_APPLICABLE must have steps_json=[]"]
    if not steps:
        errors.append(f"{status} must have at least one step")
    for i, s in enumerate(steps, 1):
        where = f"step {i}"
        if not isinstance(s, dict) or tuple(sorted(s)) != tuple(sorted(STEP_KEYS)):
            errors.append(f"{where}: keys must be exactly {', '.join(STEP_KEYS)}")
            continue
        if s["step_no"] != i:
            errors.append(f"{where}: step_no is {s['step_no']!r}")
        op = s["operation"]
        if op not in OP_PHASE:
            errors.append(f"{where}: unknown operation {op!r}")
            continue
        if s["phase"] != OP_PHASE[op] or s["state"] != OP_STATE[op]:
            errors.append(f"{where}: {op} needs phase {OP_PHASE[op]} and state {OP_STATE[op]}")
        if not s["system_id"] or not s["resource_id"]:
            errors.append(f"{where}: empty system_id or resource_id")
        phase, ev = OP_PHASE[op], s["evidence_id"]
        if status == "COMPLETE":
            if phase != "VERIFICATION":
                errors.append(f"{where}: COMPLETE allows only VERIFICATION steps")
            elif not ev:
                errors.append(f"{where}: COMPLETE verification must cite evidence")
        elif phase == "FINDING" and not ev:
            errors.append(f"{where}: a finding must cite evidence")
        elif phase != "FINDING" and ev:
            errors.append(f"{where}: INCOMPLETE {phase.lower()} steps use an empty evidence_id")
    valid = [
        s
        for s in steps
        if isinstance(s, dict)
        and s.get("operation") in OP_PHASE
        and isinstance(s.get("system_id"), str)
        and isinstance(s.get("resource_id"), str)
    ]
    keys = [step_sort_key(s) for s in valid]
    if keys != sorted(keys):
        errors.append("steps are not in phase/operation/system/resource order")
    if status == "INCOMPLETE" and not any(OP_PHASE[s["operation"]] == "FINDING" for s in valid):
        errors.append("INCOMPLETE needs at least one finding")
    return errors


def write_output(rows, path) -> None:
    """Write output.csv: rows of (case_id, status, steps) sorted by numeric case id, LF line endings."""
    rows = sorted(rows, key=lambda r: case_sort_key(r[0]))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(OUTPUT_HEADER)
        for case_id, status, steps in rows:
            w.writerow([case_id, status, serialize_steps(steps)])
