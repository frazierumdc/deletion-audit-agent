"""Decisions -> (status, steps). Deterministic rendering, so format and order can't drift.

Every finding is followed by its remediation and its post-remediation verification; compliant
records get only a verification step citing the evidence that proves their state.
"""

from deletion_audit.context import CaseView
from deletion_audit.contract import make_step, render_steps, validate_row
from deletion_audit.decisions import Decision
from deletion_audit.systems import DATA_FLOW

TREATMENT_OPS = {
    "REDACT": ("REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
}


def required_action(category: str, rules: dict) -> str:
    rule = rules.get(category)
    return rule.required_action if rule else "DELETE"


def render(decisions: list[Decision], rules: dict) -> tuple[str, list[dict]]:
    findings, fixes, checks = [], [], []  # checks: (operation, decision)
    for d in sorted(decisions, key=lambda d: d.resource_id):
        sys_, rid, ev = d.system_id, d.resource_id, d.evidence_id
        if d.category == DATA_FLOW:
            if d.flow_enabled:
                findings.append(make_step("REPORT_ACTIVE_DATA_FLOW", sys_, rid, ev))
                fixes.append(make_step("DISABLE_DATA_FLOW", sys_, rid))
            checks.append(("VERIFY_DATA_FLOW_DISABLED", d))
            continue
        if d.category == "BACKUP_ARCHIVE":
            if d.restore_allowed:
                findings.append(make_step("REPORT_RESTORE_RISK", sys_, rid, ev))
                fixes.append(make_step("BLOCK_RESTORE", sys_, rid))
            checks.append(("VERIFY_RESTORE_BLOCKED", d))
            continue
        action = required_action(d.category, rules)
        if action == "RETAIN":
            checks.append(("VERIFY_RECORD_PRESENT", d))
        elif d.present is False:
            checks.append(("VERIFY_RECORD_ABSENT", d))
        elif action == "DELETE":
            findings.append(make_step("REPORT_REMAINING_RECORD", sys_, rid, ev))
            fixes.append(make_step("DELETE_RECORD", sys_, rid))
            checks.append(("VERIFY_RECORD_ABSENT", d))
        elif action in TREATMENT_OPS:
            fix_op, verify_op = TREATMENT_OPS[action]
            if d.exposed:
                findings.append(make_step("REPORT_EXPOSED_PII", sys_, rid, ev))
                fixes.append(make_step(fix_op, sys_, rid))
            checks.append((verify_op, d))
    status = "INCOMPLETE" if findings else "COMPLETE"
    verify = [
        make_step(op, d.system_id, d.resource_id, d.evidence_id if status == "COMPLETE" else "")
        for op, d in checks
    ]
    return status, render_steps(findings + fixes + verify)


def validate(view: CaseView, status: str, steps: list[dict]) -> list[str]:
    """Contract errors plus evidence checks: every cited id exists and was available by audit_at."""
    errors = validate_row(status, steps)
    for i, s in enumerate(steps, 1):
        ev = s.get("evidence_id") if isinstance(s, dict) else None
        if not ev:
            continue
        when = view.ctx.evidence_time(ev)
        if when is None:
            errors.append(f"step {i}: unknown evidence id {ev!r}")
        elif when > view.audit_at:
            errors.append(f"step {i}: {ev} is dated {when}, after audit_at {view.audit_at}")
    return errors
