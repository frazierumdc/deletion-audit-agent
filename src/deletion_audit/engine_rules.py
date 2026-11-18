"""The deterministic rules engine: intent -> identity -> links -> state -> decisions -> steps.

Cases are linked jointly, because unowned workflow rows are shared out between all
known requests by the global timing matcher; only the target cases are returned.
"""

from deletion_audit.context import AuditContext, CaseView
from deletion_audit.data import Request
from deletion_audit.decisions import CaseResult, decide
from deletion_audit.intent import rule_classify
from deletion_audit.linking.direct import add_link, direct_links
from deletion_audit.linking.repopulation import repopulation_links
from deletion_audit.linking.timing import match_timing
from deletion_audit.planner import render, validate


def link_cases(views: list[CaseView], use_phase: bool = True) -> dict[str, dict[str, list[str]]]:
    """case_id -> {resource_id: provenance} for every authorised case with a resolved subject."""
    active = [v for v in views if rule_classify(v.request.request_text).authorized and v.subject is not None]
    links = {v.case_id: direct_links(v) for v in active}
    for cid, cands in match_timing(active, links, use_phase=use_phase).items():
        for c in cands:
            add_link(links[cid], c.resource_id, f"timing:{c.activity_id}")
    for v in active:
        repopulation_links(v, links[v.case_id])
    return links


def audit_case_rules(view: CaseView, links: dict[str, list[str]] | None) -> CaseResult:
    intent = rule_classify(view.request.request_text)
    if not intent.authorized:
        return CaseResult(view.case_id, "NOT_APPLICABLE", [], [], "rules", [intent.reason])
    notes = [intent.reason]
    if view.subject is None:
        notes.append("request email matches no known identity")
    decisions = []
    for rid in sorted(links or {}):
        d = decide(view, rid, links[rid], view.rules)
        if d is None:
            notes.append(f"skipped {rid}: unknown system or category")
            continue
        decisions.append(d)
        if d.state_note:
            notes.append(d.state_note)
    status, steps = render(decisions, view.rules)
    notes.extend(f"contract: {e}" for e in validate(view, status, steps))
    return CaseResult(view.case_id, status, steps, decisions, "rules", notes)


def _link_at(
    ctx: AuditContext, cutoff: str, targets: list[Request], pool: list[Request], use_phase: bool
) -> list[CaseResult]:
    """Results for `targets` (all with audit_at == cutoff), linked against data dated <= cutoff only.

    Every pool request submitted by the cutoff competes for workflow rows, but it competes with what
    was known at the cutoff, so another case's later evidence cannot reach these answers.
    """
    tctx = ctx.truncated(cutoff)
    by_id = {r.case_id: r for r in pool if r.requested_at <= cutoff}
    by_id.update((r.case_id, r) for r in targets)
    views = {cid: CaseView(tctx, r) for cid, r in by_id.items()}
    links = link_cases(list(views.values()), use_phase=use_phase)
    results = [audit_case_rules(views[r.case_id], links.get(r.case_id)) for r in targets]
    for res in results:
        res.timing_candidates = views[res.case_id].timing_candidates
    return results


_WORKER_CTX = None


def _worker_job(job):
    global _WORKER_CTX
    if _WORKER_CTX is None:
        _WORKER_CTX = AuditContext.load()
    return _link_at(_WORKER_CTX, *job)


def run_rules(
    ctx: AuditContext,
    targets: list[Request],
    pool: list[Request],
    use_phase: bool = True,
    strict_ownership: bool = True,
    workers: int = 1,
) -> list[CaseResult]:
    """Audit `targets`; `pool` holds every known request that competes for activity rows.

    Default: cases are grouped by audit_at and each group is linked against a context truncated at
    that time (see _link_at). strict_ownership=False is an ablation only: one global run in which
    ownership may use evidence captured after a case's audit_at, breaking the time rule.
    """
    if not strict_ownership:
        by_id = {r.case_id: r for r in pool}
        by_id.update((r.case_id, r) for r in targets)
        views = {cid: CaseView(ctx, r, strict_ownership=False) for cid, r in by_id.items()}
        links = link_cases(list(views.values()), use_phase=use_phase)
        return [audit_case_rules(views[r.case_id], links.get(r.case_id)) for r in targets]
    groups = {}
    for r in targets:
        groups.setdefault(r.audit_at, []).append(r)
    jobs = [(cutoff, group, pool, use_phase) for cutoff, group in sorted(groups.items())]
    if workers > 1 and len(jobs) > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as ex:
            batches = list(ex.map(_worker_job, jobs))
    else:
        batches = [_link_at(ctx, *job) for job in jobs]
    results = {res.case_id: res for batch in batches for res in batch}
    return [results[r.case_id] for r in targets]
