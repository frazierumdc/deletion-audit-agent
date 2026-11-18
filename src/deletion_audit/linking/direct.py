"""Links grounded in content: evidence carrying the subject's identifiers, data-catalog rows, and
workflow rows on resources that visible evidence ties to the subject. Everything goes through
CaseView, so only information available by audit_at is used."""

from deletion_audit.context import CaseView, catalog_tokens

WORKFLOW_SOURCE = "deletion_worker"


def add_link(links: dict, resource_id: str, provenance: str) -> None:
    prov = links.setdefault(resource_id, [])
    if provenance not in prov:
        prov.append(provenance)


def direct_links(view: CaseView) -> dict[str, list[str]]:
    """resource_id -> provenance list for one authorised case with a resolved subject."""
    subject = view.subject
    links = {}
    if subject is None:
        return links
    # 1. Evidence observations in the case window that carry one of the subject's identifiers.
    for tok in sorted(subject.identifiers):
        for o in view.observations(key=tok):
            add_link(links, o.resource_id, f"evidence:{o.evidence_id}")
    # 2. Data-catalog rows in the window that name the subject.
    identity = view.ctx.identity
    for a in view.ctx.catalog_rows:
        if view.in_window(a.ts) and any(
            subject.user_id in identity.owners(t, view.audit_at) for t in catalog_tokens(a.details)
        ):
            add_link(links, a.resource_id, f"catalog:{a.activity_id}")
    # 3. Workflow rows in the window on resources that visible evidence ties to the subject.
    for a in view.activity():
        if a.source == WORKFLOW_SOURCE and subject.user_id in view.owners(a.resource_id, a.ts):
            add_link(links, a.resource_id, f"activity:{a.activity_id}")
    return links
