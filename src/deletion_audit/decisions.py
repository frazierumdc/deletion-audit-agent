"""Decision: what the audit concluded about one resource. Both engines produce these."""

from dataclasses import dataclass, field, replace

from deletion_audit.context import CaseView
from deletion_audit.state import absence_proof, resource_state
from deletion_audit.systems import DATA_FLOW


@dataclass
class Decision:
    system_id: str
    resource_id: str
    category: str  # deletion_rules category, or DATA_FLOW
    present: bool | None
    exposed: bool | None
    flow_enabled: bool | None
    restore_allowed: bool | None
    evidence_id: str  # proves the current state
    provenance: list[str] = field(default_factory=list)  # why the resource belongs to the case
    state_note: str = ""  # set when no evidence establishes the state


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict]
    decisions: list[Decision]
    engine: str  # "agent" | "agent-retry" | "rules-fallback" | "rules"
    notes: list[str] = field(default_factory=list)
    timing_candidates: list = field(default_factory=list)  # the global matcher's view, for the agent


def decide(
    view: CaseView, rid: str, provenance: list[str], rules: dict, category: str | None = None
) -> Decision | None:
    """Decision for one linked resource, or None when its system or category is unknown.

    category overrides the evidence-derived category (the agent chooses it); state always
    comes from the evidence.
    """
    st = resource_state(view, rid)
    if category:
        st = replace(st, category=category)
    if st.system is None or st.category is None:
        return None
    present, evidence = st.present, st.evidence_id
    action = rules[st.category].required_action if st.category in rules else None
    retained = st.category in (DATA_FLOW, "BACKUP_ARCHIVE") or action == "RETAIN"
    if not retained and (present is False or (present is None and not st.observed)):
        # Absent: prove it with the latest full snapshot lacking the record, else the latest
        # observation, else the workflow row (a claim, used only when nothing else exists).
        acts = view.activity(rid)
        evidence = absence_proof(view, rid) if present is not False else evidence
        evidence = evidence or st.evidence_id or (acts[-1].activity_id if acts else "")
        present = False
    note = ""
    if not evidence:
        # Nothing visible observes this resource (typically a timing-linked row, or a record seen
        # only in a PNG/PDF). Cite the row that linked it, and say that the state is unproven.
        acts = view.activity(rid)
        evidence = acts[-1].activity_id if acts else provenance[0].split(":", 1)[-1] if provenance else ""
        note = f"state unproven: no evidence observes {rid}; citing {evidence or 'nothing'}"
    return Decision(
        st.system,
        rid,
        st.category,
        present,
        st.exposed,
        st.flow_enabled,
        st.restore_allowed,
        evidence,
        list(provenance),
        note,
    )
