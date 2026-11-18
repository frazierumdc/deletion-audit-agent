"""Evidence types: Observation, Coverage and the EvidenceStore that indexes them."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from deletion_audit.identifiers import (
    LOOKUP_FIELDS,
    PII_FIELDS,
    REF_FIELDS,
    is_null,
    norm_identifier,
    parse_bool,
)
from deletion_audit.systems import system_for

RAW_LIMIT = 2000  # characters of source text kept per observation for read_evidence


@dataclass(frozen=True)
class Observation:
    """One statement about one resource from one evidence item."""

    evidence_id: str
    captured_at: str  # when the evidence item was captured (cutoff check)
    ts: str  # when the state was observed
    system: str
    resource_id: str
    present: bool | None = None
    exposed: bool | None = None  # direct identifiers still visible
    flow_enabled: bool | None = None
    restore_allowed: bool | None = None
    category: str | None = None  # deletion_rules data_category when the source says so
    keys: frozenset = frozenset()  # identifier tokens linking it to a person
    source: str = ""  # table, collection or log event it came from
    raw: str = ""  # the record or log lines behind it (truncated)


@dataclass(frozen=True)
class Coverage:
    """A full snapshot of one system's collection: a record missing from it is absent."""

    evidence_id: str
    captured_at: str
    ts: str
    system: str
    resource_ids: frozenset


@dataclass
class FileReport:
    evidence_id: str
    file_path: str
    fmt: str
    observations: int = 0
    coverage: int = 0
    note: str = ""  # e.g. "no records", "pending vision extraction", or an error


@dataclass
class EvidenceStore:
    observations: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)  # evidence_id -> FileReport
    by_resource: dict = field(default_factory=lambda: defaultdict(list))
    by_key: dict = field(default_factory=lambda: defaultdict(list))
    by_evidence: dict = field(default_factory=lambda: defaultdict(list))
    media: dict = field(default_factory=dict)  # evidence_id -> page facts and events from the vision step

    def add(self, o: Observation | None) -> bool:
        """Index an observation. Ones without a resource id or a known system are dropped."""
        if o is None or not o.resource_id or not o.system:
            return False
        self.observations.append(o)
        self.by_resource[o.resource_id].append(o)
        self.by_evidence[o.evidence_id].append(o)
        for k in o.keys:
            self.by_key[k].append(o)
        return True

    def add_coverage(self, c: Coverage) -> None:
        if c.system:
            self.coverage.append(c)


def clip(text) -> str:
    text = (
        text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, sort_keys=True, default=str)
    )
    return text if len(text) <= RAW_LIMIT else text[:RAW_LIMIT] + " …[truncated]"


_EMAIL_IN_TEXT = re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", re.IGNORECASE)


def record_observation(
    evidence_id, captured_at, ts, system, rec, category=None, present=True, extra_keys=(), source=""
) -> Observation | None:
    """Turn a structured record (dict with a resource_id) into an Observation."""
    rid = rec.get("resource_id")
    if not rid:
        return None
    keys, exposed = set(), False
    for k, v in rec.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 in PII_FIELDS and not is_null(v2):
                    exposed = True
                tok = norm_identifier(v2) if isinstance(v2, str) else None
                if tok and (k2 in PII_FIELDS or k2 in REF_FIELDS):
                    keys.add(tok)
            continue
        if k in PII_FIELDS and not is_null(v):
            exposed = True
        if k in PII_FIELDS or k in REF_FIELDS or k in LOOKUP_FIELDS:
            tok = norm_identifier(v)
            if tok:
                keys.add(tok)
        if k == "message_text" and isinstance(v, str) and not is_null(v):
            found = _EMAIL_IN_TEXT.findall(v)
            if found:
                exposed = True
                keys.update("e:" + e.lower().rstrip(".") for e in found)
    for raw in extra_keys:
        tok = norm_identifier(raw)
        if tok:
            keys.add(tok)
    system = system or system_for(rid)
    if system == "analytics_platform":
        # analytics must carry no user reference at all, not just no direct identifiers
        exposed = exposed or not is_null(rec.get("user_id")) or not is_null(rec.get("customer_reference"))
    if "download_available" in rec and parse_bool(rec.get("download_available")) is False:
        present = False
    return Observation(
        evidence_id,
        captured_at,
        ts or captured_at,
        system,
        rid,
        present=present,
        exposed=exposed,
        flow_enabled=parse_bool(rec.get("enabled")) if "enabled" in rec else None,
        restore_allowed=parse_bool(rec.get("restore_allowed")) if "restore_allowed" in rec else None,
        category=category,
        keys=frozenset(keys),
        source=source,
        raw=clip(rec),
    )


# result -> (present, exposed, flow_enabled, restore_allowed)
RESULT_STATE = {
    "FOUND": (True, None, None, None),
    "NOT_FOUND": (False, None, None, None),
    "IDENTIFIABLE": (True, True, None, None),
    "ANONYMIZED": (True, False, None, None),
    "REDACTED": (True, False, None, None),
    "ACTIVE": (True, None, True, True),
    "DISABLED": (True, None, False, None),
    "BLOCKED": (True, None, None, False),
}


def lookup_observation(
    evidence_id, captured_at, ts, system, rid, record_type, result, key, source, raw=""
) -> Observation | None:
    """A lookup result line: '<record_type> <resource_id> -> FOUND/NOT_FOUND/...' keyed on an identifier."""
    if not rid:
        return None
    present, exposed, enabled, restore = RESULT_STATE.get((result or "").upper(), (None, None, None, None))
    tok = norm_identifier(key)
    return Observation(
        evidence_id,
        captured_at,
        ts,
        system or system_for(rid),
        rid,
        present=present,
        exposed=exposed,
        flow_enabled=enabled if record_type == "DATA_FLOW" else None,
        restore_allowed=restore if record_type == "BACKUP_ARCHIVE" else None,
        category=record_type or None,
        keys=frozenset({tok} if tok else ()),
        source=source,
        raw=clip(raw),
    )
