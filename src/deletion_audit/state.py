"""State of one resource at audit_at, from in-window evidence and workflow check rows.

Workflow results are claims and never set state. The exceptions are the live observations a
check row reports ("... remains enabled", "... can still be restored") and repopulation rows.
"""

from dataclasses import dataclass

from deletion_audit.context import CaseView
from deletion_audit.systems import SYSTEM_CATEGORY, system_for

DELETE_HINT_EVENTS = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}


@dataclass(frozen=True)
class ResourceState:
    system: str
    resource_id: str
    category: str | None
    present: bool | None
    exposed: bool | None
    flow_enabled: bool | None
    restore_allowed: bool | None
    evidence_id: str  # evidence proving the latest state ("" if none)
    observed: bool  # any in-window observation at all


def category_for(rid, obs, activities) -> str | None:
    cats = [o.category for o in obs if o.category]
    if cats:
        return cats[-1]  # latest statement wins
    sys_ = system_for(rid)
    for a in activities:
        hint = DELETE_HINT_EVENTS.get(a.event_type)
        if sys_ == "support_platform" and hint == "DELETE":
            return "SUPPORT_ATTACHMENT"
        if sys_ == "support_platform" and hint == "REDACT":
            return "SUPPORT_MESSAGE"
        if sys_ == "analytics_platform" and hint == "DELETE":
            return "SECONDARY_IDENTIFIER"
    if sys_ == "support_platform":
        return "SUPPORT_ATTACHMENT"
    return SYSTEM_CATEGORY.get(sys_)


def _any(values):
    vals = [v for v in values if v is not None]
    return any(vals) if vals else None


def resource_state(view: CaseView, rid: str) -> ResourceState:
    obs = sorted(view.observations(resource_id=rid), key=lambda o: (o.ts, o.captured_at, o.evidence_id))
    acts = view.activity(rid)
    present = exposed = flow = restore = None
    evidence = ""
    if obs:
        # The latest evidence item decides; replicas in one snapshot are combined, so a copy left
        # in any replica keeps the record present.
        latest = obs[-1].evidence_id
        same = [o for o in obs if o.evidence_id == latest]
        present = _any(o.present for o in same)
        exposed = _any(o.exposed for o in same if o.present)
        flow = _any(o.flow_enabled for o in same)
        restore = _any(o.restore_allowed for o in same)
        evidence = latest
    latest_ts = obs[-1].ts if obs else ""
    for a in acts:
        if a.ts < latest_ts:
            continue
        kind = None
        if a.event_type == "CHECK_DATA_FLOW" and "remains enabled" in a.details:
            kind = "flow"
        elif a.event_type == "CHECK_RESTORE_ACCESS" and "can still be restored" in a.details:
            kind = "restore"
        elif "repopulated downstream" in a.details:
            kind = "flow" if system_for(rid) == "integration_service" else "restore"
        if kind == "flow" and flow is None:
            flow, evidence, present = True, evidence or a.activity_id, True
        elif kind == "restore" and restore is None:
            restore, evidence, present = True, evidence or a.activity_id, True
    return ResourceState(
        system_for(rid),
        rid,
        category_for(rid, obs, acts),
        present,
        exposed,
        flow,
        restore,
        evidence,
        bool(obs),
    )


def absence_proof(view: CaseView, rid: str) -> str:
    """Latest in-window full snapshot of the resource's system that does not contain it."""
    sys_ = system_for(rid)
    best = None
    for cov in view.coverage(sys_):
        if rid in cov.resource_ids:
            continue
        key = (cov.ts, cov.captured_at, cov.evidence_id)
        if best is None or key > best:
            best = key
    return best[2] if best else ""
