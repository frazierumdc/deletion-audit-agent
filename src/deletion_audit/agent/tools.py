"""Read-only investigation tools for one case, built as closures over its CaseView.

Everything goes through the view, so the agent sees exactly what the time rule allows: nothing
captured or dated after audit_at. aisuite builds each tool's schema from its signature and
Google-style docstring, so keep the first line and the Args: entries accurate. Tools return
small dicts (never JSON strings) and report their own errors instead of raising.
"""

import functools

from deletion_audit.context import CaseView, catalog_tokens
from deletion_audit.identifiers import norm_identifier
from deletion_audit.linking.direct import WORKFLOW_SOURCE
from deletion_audit.state import absence_proof
from deletion_audit.state import resource_state as compute_state
from deletion_audit.systems import DATA_FLOW, system_for
from deletion_audit.timeutil import minutes_between

MAX_ITEMS = 60  # rows per tool result; the rest are counted, not sent
RAW_CHARS = 1500  # characters of raw source text per record in read_evidence
NEAR_BEFORE, NEAR_AFTER = 100, 170  # minutes around reported_at for the case's workflow rows
ANY_DAYS = 100000  # ownership links at any distance, for display


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # a tool failure must never kill the case
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def _capped(items: list, key: str) -> dict:
    out = {key: items[:MAX_ITEMS]}
    if len(items) > MAX_ITEMS:
        out["omitted"] = len(items) - MAX_ITEMS
    return out


def _token(identifier: str) -> str | None:
    """Typed token for a raw identifier, or the token itself if already typed (e:/p:/h:/u:)."""
    s = (identifier or "").strip()
    if len(s) > 2 and s[1] == ":" and s[0] in "ephu":
        return s
    return norm_identifier(s)


def _obs_row(view: CaseView, o) -> dict:
    return {
        "evidence_id": o.evidence_id,
        "captured_at": o.captured_at,
        "observed_at": o.ts,
        "in_window": view.in_window(o.ts),
        "system": o.system,
        "resource_id": o.resource_id,
        "source": o.source,
        "category": o.category,
        "present": o.present,
        "exposed": o.exposed,
        "flow_enabled": o.flow_enabled,
        "restore_allowed": o.restore_allowed,
    }


def _act_row(a) -> dict:
    return {
        "activity_id": a.activity_id,
        "ts": a.ts,
        "event_type": a.event_type,
        "source": a.source,
        "resource_id": a.resource_id,
        "result": a.result,
        "details": a.details,
    }


def make_tools(view: CaseView) -> list:
    """The agent's tools for one case. Each closure only reads through `view`."""
    ctx = view.ctx

    @_safe
    def resolve_identity(email: str) -> dict:
        """Find the user who holds an email (current or historical) and all identifiers they held by audit_at.

        Args:
            email: The email address to resolve, exactly as written.
        """
        subject = ctx.identity.resolve(email, view.audit_at)
        if subject is None:
            return {"user_id": None, "note": "no identity holds this email by audit_at (exact match only)"}
        changes = [
            {
                "audit_id": c.audit_id,
                "event_time": c.event_time,
                "field": c.field_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
            }
            for c in ctx.identity.changes(subject.user_id, view.audit_at)
        ]
        return {
            "user_id": subject.user_id,
            "matched_on": subject.matched_on,
            "identifiers": sorted(subject.identifiers),
            "identity_changes": changes,
            "also_held_by": list(subject.ambiguous),
            "note": "identifier prefixes: e: email, p: phone, h: email hash, u: user_id",
        }

    @_safe
    def search_evidence(identifier: str) -> dict:
        """Find evidence observations and data-catalog rows that carry an identifier or a resource id.

        Args:
            identifier: An email, phone, email hash, user_id, typed token (e.g. "e:a@example.com"), or a resource id.
        """
        identifier = (identifier or "").strip()
        if system_for(identifier):  # a resource id
            obs = view.observations(resource_id=identifier, window=False)
            cat = [a for a in ctx.catalog_rows if a.resource_id == identifier and a.ts <= view.audit_at]
        else:
            tok = _token(identifier)
            if tok is None:
                return {"error": f"not a recognised identifier or resource id: {identifier!r}"}
            obs = view.observations(key=tok, window=False)
            cat = [a for a in ctx.catalog_rows if a.ts <= view.audit_at and tok in catalog_tokens(a.details)]
        rows = [_obs_row(view, o) for o in sorted(obs, key=lambda o: (o.ts, o.evidence_id, o.resource_id))]
        out = _capped(rows, "observations")
        out["catalog_rows"] = [_act_row(a) for a in cat][:MAX_ITEMS]
        return out

    @_safe
    def read_evidence(evidence_id: str, resource_id: str = "") -> dict:
        """Read the raw records or log lines behind an evidence file's observations.

        Args:
            evidence_id: The file_id, e.g. evidence_12.
            resource_id: Optional; only return records about this resource.
        """
        f = view.evidence_file(evidence_id)
        if f is None:
            return {"error": f"{evidence_id} is unknown or was captured after audit_at"}
        obs = [
            o
            for o in ctx.store.by_evidence.get(evidence_id, [])
            if not resource_id or o.resource_id == resource_id
        ]
        report = ctx.store.reports.get(evidence_id)
        records = [
            {"resource_id": o.resource_id, "observed_at": o.ts, "source": o.source, "raw": o.raw[:RAW_CHARS]}
            for o in obs
        ]
        out = {"evidence_id": evidence_id, "captured_at": f.captured_at, **_capped(records, "records")}
        if report is not None and report.note:
            out["note"] = report.note
        media = ctx.store.media.get(evidence_id)
        if media is not None:  # PNG/PDF: page facts and events transcribed by the vision step
            out["page"] = {
                "artifact": media["artifact"],
                "events": media["events"][:MAX_ITEMS],
                "unreadable": media["unreadable"],
            }
        return out

    @_safe
    def list_case_activity(resource_id: str = "") -> dict:
        """List activity rows in the case window [requested_at, audit_at].

        Args:
            resource_id: A resource id for its full in-window history; empty for the deletion-workflow rows near reported_at (they may belong to other people).
        """
        if resource_id:
            rows = view.activity(resource_id)
        else:
            rows = [
                a
                for a in view.activity()
                if a.source == WORKFLOW_SOURCE
                and -NEAR_BEFORE <= minutes_between(a.ts, view.request.reported_at) <= NEAR_AFTER
            ]
        return _capped([_act_row(a) for a in rows], "activity")

    @_safe
    def timing_candidates() -> dict:
        """Unowned workflow rows that the global timing matcher scored for this case (lower score is better).

        assigned=true means the matcher gave the row to this case rather than to a competing request.
        """
        return {
            "candidates": [
                {
                    "activity_id": c.activity_id,
                    "resource_id": c.resource_id,
                    "score": c.score,
                    "assigned": c.assigned,
                    "reasons": list(c.reasons),
                }
                for c in view.timing_candidates
            ],
            "note": "a timing link is weaker than direct evidence; cite it as timing:<activity_id>",
        }

    @_safe
    def resource_state(resource_id: str) -> dict:
        """Code-computed state of a resource at audit_at, its category hint, rule, and who evidence ties it to.

        owners counts only ownership links observed within OWNERSHIP_DAYS of the resource's latest
        in-window activity (resource ids are reused); ownership_links lists every visible link.

        Args:
            resource_id: The resource id, e.g. account_123.
        """
        st = compute_state(view, resource_id)
        if st.system is None:
            return {"error": f"unknown resource id prefix: {resource_id!r}"}
        rule = ctx.rules.get(st.category) if st.category else None
        acts = view.activity(resource_id)
        near = acts[-1].ts if acts else view.audit_at
        owners = sorted({uid for uid, _ in view.ownership(resource_id, near)})
        counted = set(view.ownership(resource_id, near))
        links = [
            {"user_id": uid, "source": src, "counted": (uid, src) in counted}
            for uid, src in sorted(set(view.ownership(resource_id, near, days=ANY_DAYS)))
        ]
        in_window = view.observations(resource_id=resource_id)
        return {
            "resource_id": resource_id,
            "system": st.system,
            "category_hint": st.category,
            "rule": (
                {
                    "rule_id": rule.rule_id,
                    "condition": rule.condition,
                    "required_action": rule.required_action,
                }
                if rule
                else ("connectors must end disabled" if st.category == DATA_FLOW else None)
            ),
            "present": st.present,
            "exposed": st.exposed,
            "flow_enabled": st.flow_enabled,
            "restore_allowed": st.restore_allowed,
            "state_evidence": st.evidence_id or None,
            "absence_proof": absence_proof(view, resource_id) or None,
            "in_window_evidence": sorted({o.evidence_id for o in in_window}),
            "activity_rows": len(acts),
            "in_case_window": bool(in_window or acts),
            "owners": owners,
            "ownership_links": links[:MAX_ITEMS],
            "subject_user_id": view.subject.user_id if view.subject else None,
        }

    @_safe
    def deletion_rules() -> dict:
        """The deletion rules table: required action for each data category."""
        return {
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "data_category": r.data_category,
                    "condition": r.condition,
                    "required_action": r.required_action,
                }
                for r in ctx.rules.values()
            ]
        }

    return [
        resolve_identity,
        search_evidence,
        read_evidence,
        list_case_activity,
        timing_candidates,
        resource_state,
        deletion_rules,
    ]
