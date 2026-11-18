"""Parse and check the agent's final answer before it is rendered into steps.

evaluate_answer() returns (flag, report_md). On FAIL the report goes back to the model once
as a reflection message; a second FAIL falls back to the rules engine's answer.
"""

import json
import re

from deletion_audit.context import CaseView
from deletion_audit.decisions import Decision, decide
from deletion_audit.planner import render, validate
from deletion_audit.systems import DATA_FLOW, system_for

PROVENANCE_KINDS = ("evidence", "catalog", "activity", "timing", "repopulates")
FIXED_CATEGORY = {"integration_service": DATA_FLOW, "backup_service": "BACKUP_ARCHIVE"}
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_answer(text: str) -> tuple[dict | None, str]:
    """The last fenced or bare JSON object in the final text, or (None, reason)."""
    text = text or ""
    candidates = _FENCED.findall(text)
    if not candidates:
        start = text.find("{")
        candidates = [text[start : text.rfind("}") + 1]] if start >= 0 else []
    for raw in reversed(candidates):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, ""
    return (
        None,
        "the final message contains no parseable JSON object"
        if text.strip()
        else "the final message is empty",
    )


def known_resource(view: CaseView, rid: str) -> bool:
    """The resource appears in evidence or activity this case may see."""
    ctx = view.ctx
    return any(view.visible(o.captured_at) for o in ctx.store.by_resource.get(rid, ())) or any(
        a.ts <= view.audit_at for a in ctx.activity_by_resource.get(rid, ())
    )


def build_decisions(view: CaseView, answer: dict) -> tuple[list[Decision], list[str]]:
    """Decisions for the answer's resources: the model's category, the evidence's state."""
    decisions, notes = [], []
    for res in answer.get("resources") or []:
        rid = res.get("resource_id")
        d = decide(view, rid, list(res.get("provenance") or []), view.rules, category=res.get("category"))
        if d is None:
            notes.append(f"skipped {rid}: unknown system or category")
            continue
        decisions.append(d)
        if d.state_note:
            notes.append(d.state_note)
        cited = res.get("evidence_id")
        if cited and cited != d.evidence_id:
            notes.append(f"{rid}: agent cited {cited}, state evidence is {d.evidence_id}")
    return decisions, notes


def evaluate_answer(view: CaseView, answer: dict | None, parse_error: str = "") -> tuple[bool, str]:
    """(passed, report_md). The report lists every problem found, for the reflection turn."""
    problems = []
    if answer is None:
        return False, f"- {parse_error or 'no answer'}"
    if not isinstance(answer.get("authorized"), bool):
        problems.append("`authorized` must be true or false")
    resources = answer.get("resources")
    if not isinstance(resources, list):
        problems.append("`resources` must be a list")
        resources = []
    if answer.get("authorized") is False and resources:
        problems.append("a request that does not authorize deletion must have `resources: []`")
    if answer.get("authorized") is True and view.subject is not None and not resources:
        problems.append(f"the request email resolves to {view.subject.user_id}, but no resources were listed")
    seen = set()
    for i, res in enumerate(resources, 1):
        if not isinstance(res, dict):
            problems.append(f"resources[{i}] is not an object")
            continue
        rid = res.get("resource_id") or ""
        label = f"resources[{i}] ({rid or 'no id'})"
        if rid in seen:
            problems.append(f"{label}: listed twice")
        seen.add(rid)
        sys_ = system_for(rid)
        if sys_ is None or not known_resource(view, rid):
            problems.append(f"{label}: not a resource id that any visible evidence or activity mentions")
            continue
        if not view.observations(resource_id=rid) and not view.activity(rid):
            problems.append(
                f"{label}: nothing in the case window [requested_at, audit_at] concerns it "
                "(resource_state in_case_window is false), so its state at audit_at is unknown "
                "and it is outside this audit"
            )
        cat = res.get("category")
        if cat not in view.rules and cat != DATA_FLOW:
            problems.append(f"{label}: category {cat!r} is not a deletion_rules category or DATA_FLOW")
        elif sys_ in FIXED_CATEGORY and cat != FIXED_CATEGORY[sys_]:
            problems.append(f"{label}: {sys_} resources use category {FIXED_CATEGORY[sys_]}")
        elif cat in FIXED_CATEGORY.values() and sys_ not in FIXED_CATEGORY:
            problems.append(
                f"{label}: category {cat} is only for " + ("connectors" if cat == DATA_FLOW else "backups")
            )
        ev = res.get("evidence_id")
        if ev:
            when = view.ctx.evidence_time(ev)
            if when is None:
                problems.append(f"{label}: evidence_id {ev!r} does not exist")
            elif when > view.audit_at:
                problems.append(f"{label}: evidence_id {ev} is dated {when}, after audit_at")
        prov = res.get("provenance")
        if not isinstance(prov, list) or not prov:
            problems.append(f"{label}: needs at least one provenance entry")
            continue
        for p in prov:
            kind, _, ref = str(p).partition(":")
            if kind not in PROVENANCE_KINDS or not ref:
                problems.append(
                    f"{label}: provenance {p!r} must be one of "
                    + ", ".join(f"{k}:<id>" for k in PROVENANCE_KINDS)
                )
                continue
            when = view.ctx.evidence_time(ref)
            if when is None:
                problems.append(f"{label}: provenance {p} names an unknown id")
            elif when > view.audit_at:
                problems.append(f"{label}: provenance {p} is dated after audit_at")
    if not problems and answer.get("authorized"):
        decisions, _ = build_decisions(view, answer)
        status, steps = render(decisions, view.rules)
        problems.extend(f"rendered steps: {e}" for e in validate(view, status, steps))
    if problems:
        return False, "\n".join(f"- {p}" for p in problems)
    return True, ""
