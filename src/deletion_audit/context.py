"""AuditContext (everything loaded once) and CaseView (the only door to data for one case).

CaseView enforces the time rule: nothing captured after the case's audit_at is visible, and
state observations and activity are limited to the case window [requested_at, audit_at].
Identity and resource ownership are resolved with the same cutoff, so a later file or catalog
row cannot tie a record to the subject.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from deletion_audit.data import (
    Activity,
    AuditChange,
    EvidenceFile,
    Identity,
    Request,
    Rule,
    load_activity,
    load_audit_log,
    load_identities,
    load_manifest,
    load_rules,
)
from deletion_audit.evidence.loader import load_evidence
from deletion_audit.evidence.model import Coverage, EvidenceStore, Observation
from deletion_audit.identifiers import norm_identifier
from deletion_audit.identity import IdentityIndex, Subject
from deletion_audit.timeutil import minutes_between

OWNERSHIP_DAYS = 30  # resource ids are reused months apart; only trust nearby ownership evidence
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_USER_RE = re.compile(r"\buser_\d+\b")


@dataclass(frozen=True)
class OwnershipClaim:
    """Identifier tokens tying a resource to someone, from one evidence item or catalog row."""

    resource_id: str
    tokens: frozenset
    ts: str  # when the link was observed
    visible_at: str  # when it became available (evidence captured_at, or the catalog row time)
    source: str  # "evidence:<file_id>" or "catalog:<activity_id>"


def catalog_tokens(details: str) -> frozenset:
    """Identifiers named in a LINK_RESOURCE_IDENTITY row ('... linked X (label) to a@example.com and user_11.')."""
    toks = {f"u:{u}" for u in _USER_RE.findall(details)}
    toks |= {t for e in _EMAIL_RE.findall(details) if (t := norm_identifier(e.rstrip(".")))}
    return frozenset(toks)


@dataclass
class AuditContext:
    identities: list[Identity]
    audit_log: list[AuditChange]
    activity: list[Activity]
    rules: dict[str, Rule]
    manifest: dict[str, EvidenceFile]
    store: EvidenceStore
    identity: IdentityIndex
    activity_by_resource: dict = field(default_factory=dict)
    claims_by_resource: dict = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls, drop_after: str | None = None) -> "AuditContext":
        """Load everything. drop_after removes evidence files and activity dated after that time."""
        manifest = load_manifest()
        identities, audit_log = load_identities(), load_audit_log()
        ctx = cls(
            identities=identities,
            audit_log=audit_log,
            activity=load_activity(),
            rules=load_rules(),
            manifest={f.file_id: f for f in manifest},
            store=load_evidence(manifest),
            identity=IdentityIndex(identities, audit_log),
        )
        ctx._index()
        return ctx.truncated(drop_after) if drop_after else ctx

    def truncated(self, cutoff: str) -> "AuditContext":
        """A copy holding only evidence captured, and activity dated, at or before `cutoff`.

        The rules engine links all cases that share an audit_at against this copy, so that no
        competing case can bring later information into the global timing match.
        """
        store = EvidenceStore(
            reports=self.store.reports,
            media={k: m for k, m in self.store.media.items() if m["captured_at"] <= cutoff},
        )
        for o in self.store.observations:
            if o.captured_at <= cutoff:
                store.add(o)
        for c in self.store.coverage:
            if c.captured_at <= cutoff:
                store.add_coverage(c)
        ctx = AuditContext(
            identities=self.identities,
            audit_log=self.audit_log,
            activity=[a for a in self.activity if a.ts <= cutoff],
            rules=self.rules,
            manifest={k: f for k, f in self.manifest.items() if f.captured_at <= cutoff},
            store=store,
            identity=self.identity,
        )
        ctx._index()
        return ctx

    def _index(self) -> None:
        for a in self.activity:
            self.activity_by_resource.setdefault(a.resource_id, []).append(a)
        for o in self.store.observations:
            if o.keys:
                self.claims_by_resource[o.resource_id].append(
                    OwnershipClaim(o.resource_id, o.keys, o.ts, o.captured_at, f"evidence:{o.evidence_id}")
                )
        for a in self.catalog_rows:
            self.claims_by_resource[a.resource_id].append(
                OwnershipClaim(
                    a.resource_id, catalog_tokens(a.details), a.ts, a.ts, f"catalog:{a.activity_id}"
                )
            )

    @cached_property
    def _evidence_times(self) -> dict[str, str]:
        times = {f.file_id: f.captured_at for f in self.manifest.values()}
        times.update((a.activity_id, a.ts) for a in self.activity)
        times.update((c.audit_id, c.event_time) for c in self.audit_log)
        return times

    def evidence_time(self, evidence_id: str) -> str | None:
        """When a citable item (file_id, activity_id or audit_id) became available."""
        return self._evidence_times.get(evidence_id)

    @cached_property
    def catalog_rows(self) -> list[Activity]:
        return [a for a in self.activity if a.event_type == "LINK_RESOURCE_IDENTITY"]


class CaseView:
    """Read-only view of the context for one request, cut off at its audit_at."""

    def __init__(self, ctx: AuditContext, request: Request, strict_ownership: bool = True):
        self.ctx = ctx
        self.strict_ownership = strict_ownership  # False only for the lenient-ownership ablation
        self.request = request
        self.case_id = request.case_id
        self.requested_at = request.requested_at
        self.audit_at = request.audit_at
        self.timing_candidates = []  # set by the global timing matcher (linking/timing.py)

    # --- identity and ownership -------------------------------------------------------------
    @cached_property
    def subject(self) -> Subject | None:
        return self.ctx.identity.resolve(self.request.email, self.audit_at)

    def ownership(self, resource_id: str, near_ts: str, days: int = OWNERSHIP_DAYS) -> list[tuple[str, str]]:
        """(user_id, source) pairs tying a resource to users, from evidence visible by audit_at and
        observed within `days` of near_ts."""
        out = []
        cutoff = self.audit_at if self.strict_ownership else "9999"
        for c in self.ctx.claims_by_resource.get(resource_id, ()):
            if c.visible_at > cutoff or abs(minutes_between(c.ts, near_ts)) > days * 1440:
                continue
            for tok in sorted(c.tokens):
                out.extend((uid, c.source) for uid in sorted(self.ctx.identity.owners(tok, cutoff)))
        return out

    def owners(self, resource_id: str, near_ts: str) -> set[str]:
        return {uid for uid, _ in self.ownership(resource_id, near_ts)}

    # --- time rule -------------------------------------------------------------------------
    def visible(self, captured_at: str | None) -> bool:
        """Evidence captured at or before audit_at may be used for this case."""
        return bool(captured_at) and captured_at <= self.audit_at

    def in_window(self, ts: str | None) -> bool:
        return bool(ts) and self.requested_at <= ts <= self.audit_at

    # --- evidence ---------------------------------------------------------------------------
    def observations(
        self, resource_id: str | None = None, key: str | None = None, window: bool = True
    ) -> list[Observation]:
        """Observations captured by audit_at, filtered by resource and/or identifier token.

        window=True also requires the observed time to fall in [requested_at, audit_at] (state
        after the request). window=False keeps earlier observations, for linking records to people.
        """
        store = self.ctx.store
        if resource_id is not None:
            pool = store.by_resource.get(resource_id, [])
        elif key is not None:
            pool = store.by_key.get(key, [])
        else:
            pool = store.observations
        return [
            o
            for o in pool
            if self.visible(o.captured_at)
            and (resource_id is None or o.resource_id == resource_id)
            and (key is None or key in o.keys)
            and (not window or self.in_window(o.ts))
        ]

    def coverage(self, system: str | None = None) -> list[Coverage]:
        """Full-snapshot coverage captured by audit_at and taken within the case window."""
        return [
            c
            for c in self.ctx.store.coverage
            if (system is None or c.system == system) and self.visible(c.captured_at) and self.in_window(c.ts)
        ]

    def evidence_file(self, evidence_id: str) -> EvidenceFile | None:
        """Manifest row for an evidence id, only if it was captured by audit_at."""
        f = self.ctx.manifest.get(evidence_id)
        return f if f is not None and self.visible(f.captured_at) else None

    # --- activity ---------------------------------------------------------------------------
    def activity(self, resource_id: str | None = None) -> list[Activity]:
        """Activity rows within [requested_at, audit_at], optionally for one resource."""
        pool = self.ctx.activity_by_resource.get(resource_id, []) if resource_id else self.ctx.activity
        return [a for a in pool if self.in_window(a.ts)]

    @property
    def rules(self) -> dict[str, Rule]:
        return self.ctx.rules
