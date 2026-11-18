"""Loaders for the CSV files in the data directory (paths.DATASET).

These return raw rows as dataclasses with normalised timestamps. Identity resolution,
evidence parsing and linking live in their own modules. sample_output.csv is never
loaded here: only the evaluation harness may read it.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from deletion_audit.paths import DATASET
from deletion_audit.timeutil import ts_norm


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Identity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    ts: str
    event_type: str
    source: str
    target: str
    resource_id: str
    result: str
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    file_path: str  # relative to the data directory, as in the manifest
    origin: str
    captured_at: str

    @property
    def abs_path(self) -> Path:
        return DATASET / self.file_path

    @property
    def extension(self) -> str:
        return Path(self.file_path).suffix.lower().lstrip(".")


def _rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_requests(path=DATASET / "deletion_requests.csv") -> list[Request]:
    return [
        Request(
            r["case_id"],
            r["email"].strip(),
            ts_norm(r["requested_at"]),
            r["reported_status"].strip(),
            ts_norm(r["reported_at"]),
            ts_norm(r["audit_at"]),
            r["request_text"],
        )
        for r in _rows(path)
    ]


def load_identities(dataset=DATASET) -> list[Identity]:
    return [
        Identity(r["user_id"], r["name"], r["email"], r["phone"], r["email_hash"], ts_norm(r["updated_at"]))
        for r in _rows(Path(dataset) / "user_identities.csv")
    ]


def load_audit_log(dataset=DATASET) -> list[AuditChange]:
    return [
        AuditChange(
            r["audit_id"],
            r["user_id"],
            ts_norm(r["event_time"]),
            r["field_name"],
            r["old_value"],
            r["new_value"],
        )
        for r in _rows(Path(dataset) / "user_audit_log.csv")
    ]


def load_activity(dataset=DATASET) -> list[Activity]:
    return [
        Activity(
            r["activity_id"],
            ts_norm(r["event_time"]),
            r["event_type"],
            r["source_system"],
            r["target_system"],
            r["resource_id"],
            r["reported_result"],
            r["details"],
        )
        for r in _rows(Path(dataset) / "system_activity.csv")
    ]


def load_rules(dataset=DATASET) -> dict[str, Rule]:
    """Rules keyed by data_category."""
    return {
        r["data_category"]: Rule(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
        for r in _rows(Path(dataset) / "deletion_rules.csv")
    }


def load_manifest(dataset=DATASET) -> list[EvidenceFile]:
    return [
        EvidenceFile(r["file_id"], r["file_path"], r["origin"], ts_norm(r["captured_at"]))
        for r in _rows(Path(dataset) / "evidence_files.csv")
    ]
