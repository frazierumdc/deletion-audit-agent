"""Global timing matcher for workflow rows that no visible evidence ties to anyone.

Most deletion-worker rows carry no user identifier. A case's own rows sit in its
[requested_at, audit_at] window, clustered around reported_at. Each unowned row is scored against
every authorised case whose window contains it, then rows are assigned greedily (best score first),
at most one row per (case, system) and one case per resource.

Cutoff-aware: when scoring a row for a case, ownership and shared-file signals use only evidence
visible to that case (captured by its audit_at). The competition between cases is still global.

DATASET-SPECIFIC CALIBRATION: the template offsets and the sub-minute "run phase" slots below
were calibrated on the challenge's sample workflows (the synthetic data follows the same pattern). They describe the activity stream,
not any answer, but they are tuned to this dataset. `use_phase=False` turns the phase signal off
for an ablation.
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass

from deletion_audit.context import CaseView
from deletion_audit.linking.direct import WORKFLOW_SOURCE
from deletion_audit.systems import system_for
from deletion_audit.timeutil import minutes_between

TIMED_EVENTS = {
    "DELETE_RECORD",
    "ANONYMIZE_RECORD",
    "REDACT_RECORD",
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "CHECK_DATA_FLOW",
    "CHECK_RESTORE_ACCESS",
}
# (detail pattern, expected minutes between the row and reported_at)
TEMPLATE_OFFSETS = (
    ("reported the requested operation as completed", 0),
    ("stopped after the target system returned an error", 0),
    ("completed some internal work", 0),
    ("submitted primary profile", -60),
    ("Policy action completed", -20),
    ("received the final result", 75),
    ("Connector status check", 45),
    ("Restore-access check", 45),
)
WINDOW_BEFORE, WINDOW_AFTER = 100, 170  # minutes around reported_at
REQUEST_HINTS = {
    "analytics_platform": r"analytic",
    "search_service": r"search",
    "support_platform": r"support|conversation",
    "export_service": r"export|download|archive",
    "integration_service": r"integration|sync|connector",
    "backup_service": r"backup|restore",
}
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

# Sub-minute clock phase of a workflow run relative to its profile-deletion row: each run stamps
# its rows at fixed half-minute slots plus one run-specific jitter.
PHASE_SLOT = {
    "submitted primary profile": 0.0,
    "Policy action completed": -19.5,
    "received the final result": -8.5,
    "Connector status check": -8.5,
    "Restore-access check": -8.5,
}
FAMILY_A_SLOT = -6.5
PHASE_TOLERANCE = 2.5
PHASE_WEIGHT = 20.0


@dataclass(frozen=True)
class Candidate:
    activity_id: str
    resource_id: str
    score: float
    reasons: tuple
    assigned: bool = False


def _template(details):
    for pattern, offset in TEMPLATE_OFFSETS:
        if pattern in details:
            return pattern, offset
    return details.split(" ")[0], 0


def run_phase(a):
    sec = int(a.ts[17:19])
    if system_for(a.resource_id) == "account_service":
        return float(sec % 30)
    for pattern, slot in PHASE_SLOT.items():
        if pattern in a.details:
            return (sec - slot) % 30
    if any(
        p in a.details
        for p in (
            "reported the requested operation",
            "stopped after the target",
            "completed some internal work",
        )
    ):
        return (sec - FAMILY_A_SLOT) % 30
    return None


def _circular_mean(values, period=30.0):
    s = sum(math.sin(2 * math.pi * v / period) for v in values)
    c = sum(math.cos(2 * math.pi * v / period) for v in values)
    return (math.atan2(s, c) * period / (2 * math.pi)) % period


def _phase_distance(a, b):
    d = abs(a - b) % 30
    return min(d, 30 - d)


def match_timing(views: list[CaseView], direct: dict, use_phase: bool = True, top_n: int = 10) -> dict:
    """Assign unowned workflow rows to cases. Returns {case_id: [assigned Candidate]}.

    `direct` maps case_id -> {resource_id: provenance} for the cases to match (authorised, with a
    subject). Each view's `timing_candidates` is set to its best-scoring candidates for the agent.
    """
    by_id = {v.case_id: v for v in views}
    rows = (
        [a for a in views[0].ctx.activity if a.source == WORKFLOW_SOURCE and a.event_type in TIMED_EVENTS]
        if views
        else []
    )

    def files(view, rid):
        return {o.evidence_id for o in view.observations(resource_id=rid, window=False)}

    case_files, phases = {}, defaultdict(list)
    for v in views:
        rids = direct[v.case_id]
        case_files[v.case_id] = set().union(*(files(v, r) for r in rids)) if rids else set()
        for rid in rids:
            for a in v.activity(rid):
                if a.source == WORKFLOW_SOURCE and run_phase(a) is not None:
                    phases[v.case_id].append(run_phase(a))

    pairs = []  # (score, ts, case_id, activity_id, row, reasons)
    for v in sorted(views, key=lambda v: (v.request.reported_at, v.case_id)):
        req = v.request
        for a in rows:
            if not v.in_window(a.ts):
                continue
            delta = minutes_between(a.ts, req.reported_at)
            if delta < -WINDOW_BEFORE or delta > WINDOW_AFTER:
                continue
            if v.owners(a.resource_id, a.ts):  # tied to someone by evidence this case can see
                continue
            sys_ = system_for(a.resource_id)
            pattern, offset = _template(a.details)
            # Follow-up rows sit at fixed offsets; profile-deletion rows wander within the hour.
            weight = 0.3 if pattern == "submitted primary profile" else 1.0
            score = weight * abs(delta - offset)
            reasons = [f"offset {delta:+.0f}m vs expected {offset:+d}m"]
            hint = REQUEST_HINTS.get(sys_)
            if hint and re.search(hint, req.request_text.lower()):
                score -= 15
                reasons.append("request text mentions the system")
            if files(v, a.resource_id) & case_files[v.case_id]:
                score -= 10
                reasons.append("resource appears in the case's evidence files")
            if sys_ == "account_service" and STATUS_RESULT.get(req.reported_status) == a.result:
                score -= 25  # the reported status mirrors the profile-deletion result
                reasons.append(f"result {a.result} mirrors reported_status {req.reported_status}")
            pairs.append((score, a.ts, v.case_id, a.activity_id, a, tuple(reasons)))
    pairs.sort(key=lambda p: p[:4])

    taken_rows, taken_slots = set(), set()
    for cid, rids in direct.items():
        taken_slots.update((cid, system_for(r)) for r in rids)
    assigned = defaultdict(list)
    while True:
        best, chosen = None, None
        for p in pairs:
            base, ts, cid, aid, a, reasons = p
            if a.resource_id in taken_rows or (cid, system_for(a.resource_id)) in taken_slots:
                continue
            score = base
            rp = run_phase(a) if use_phase else None
            if phases[cid] and rp is not None:
                d = _phase_distance(_circular_mean(phases[cid]), rp)
                if d > PHASE_TOLERANCE:
                    continue
                score += PHASE_WEIGHT * d
            key = (score, ts, cid, aid)
            if best is None or key < best:
                best, chosen = key, p
        if chosen is None:
            break
        score, _, cid, aid, a, reasons = best[0], *chosen[1:]
        taken_rows.add(a.resource_id)
        taken_slots.add((cid, system_for(a.resource_id)))
        if run_phase(a) is not None:
            phases[cid].append(run_phase(a))
        assigned[cid].append(Candidate(aid, a.resource_id, round(score, 2), reasons, True))

    chosen_ids = {(cid, c.activity_id) for cid, cs in assigned.items() for c in cs}
    per_case = defaultdict(list)
    for score, _, cid, aid, a, reasons in pairs:
        per_case[cid].append(
            Candidate(aid, a.resource_id, round(score, 2), reasons, (cid, aid) in chosen_ids)
        )
    for cid, v in by_id.items():
        v.timing_candidates = per_case.get(cid, [])[:top_n]
    return dict(assigned)
