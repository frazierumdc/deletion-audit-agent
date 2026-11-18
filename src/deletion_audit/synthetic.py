"""Synthetic dataset generator.

Writes a small, fully fictional dataset in the layout the audit reads (requests, identities,
identity history, activity stream, deletion rules, an evidence manifest and evidence files in
SQL, JSON, log, text, PDF and PNG form), plus expected answers for the sample requests.

Every person, email, phone number and resource id here is invented. Emails use the reserved
example.com / example.net / example.org domains and phones use the fictional 555-01xx range.

Each request follows one scenario, and the scenario states the ground truth (what really
happened to each record) before any evidence is written. The expected answers come from that
ground truth, not from running the audit, so the evaluation is not circular.

    python -m deletion_audit.synthetic                 # rewrite data/synthetic/
    python -m deletion_audit.synthetic --out other/dir  # write somewhere else

The PNG needs Pillow (a development dependency); everything else is the standard library.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deletion_audit.contract import make_step, render_steps, serialize_steps

START = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)
SLOT_DAYS = 14  # each request gets its own two-week slot, so case windows never overlap

RULES = (
    ("rule_01", "USER_PROFILE", "Primary customer profile", "DELETE"),
    ("rule_02", "SECONDARY_IDENTIFIER", "Extra contact point stored outside the profile", "DELETE"),
    ("rule_03", "SEARCH_INDEX_ENTRY", "Customer document in a search index", "DELETE"),
    ("rule_04", "CACHE_ENTRY", "Cached copy of profile data", "DELETE"),
    ("rule_05", "SUPPORT_MESSAGE", "Conversation also holds agent or order context", "REDACT"),
    ("rule_06", "SUPPORT_ATTACHMENT", "File uploaded by the customer", "DELETE"),
    ("rule_07", "ANALYTICS_PROFILE", "Behavioural profile kept for aggregate reporting", "ANONYMIZE"),
    ("rule_08", "PRODUCT_TELEMETRY", "Usage events kept for aggregate reporting", "ANONYMIZE"),
    ("rule_09", "USER_EXPORT", "Data export prepared for the customer", "DELETE"),
    ("rule_10", "MARKETING_PROFILE", "Marketing audience membership", "DELETE"),
    ("rule_11", "EMAIL_DELIVERY_LOG", "Delivery record kept for sending reputation", "REDACT"),
    ("rule_12", "SECURITY_AUDIT_LOG", "Security event under a retention obligation", "RETAIN"),
    ("rule_13", "LEGAL_HOLD_RECORD", "Record under legal hold", "RETAIN"),
    ("rule_14", "BACKUP_ARCHIVE", "Backup kept intact; restores must be blocked", "RETAIN"),
    ("rule_15", "ACCOUNT_TOMBSTONE", "Marker that keeps only the stable user id", "REDACT"),
)

# Seconds within the minute for workflow rows. The timing matcher reads a sub-minute "run phase";
# keeping every run on the same phase makes the synthetic workflow look like one batch job.
SEC_PROFILE, SEC_POLICY, SEC_RESULT, SEC_COMPLETED = 12, 22, 3, 5


@dataclass(frozen=True)
class Person:
    user_id: str
    name: str
    email: str
    phone: str
    old_email: str | None = None  # set when the email changed before the request


@dataclass
class Case:
    case_id: str
    person: Person
    slot: int
    request_text: str
    reported_status: str
    sample: bool
    request_email: str = ""

    @property
    def t0(self) -> datetime:
        return START + timedelta(days=SLOT_DAYS * self.slot)

    @property
    def requested_at(self) -> datetime:
        return self.t0

    @property
    def reported_at(self) -> datetime:
        return self.t0 + timedelta(days=2, hours=1)

    @property
    def audit_at(self) -> datetime:
        return self.t0 + timedelta(days=6, hours=3)

    def at(self, days: float, hours: float = 0, minutes: float = 0, second: int = 0) -> datetime:
        base = self.t0 + timedelta(days=days, hours=hours, minutes=minutes)
        return base.replace(second=second)

    def work(self, minutes: float, second: int) -> datetime:
        """A workflow row `minutes` after reported_at, at a fixed second."""
        return (self.reported_at + timedelta(minutes=minutes)).replace(second=second)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


@dataclass
class World:
    identities: list[dict] = field(default_factory=list)
    audit_log: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    manifest: list[dict] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    fixtures: dict[str, dict] = field(default_factory=dict)
    cases: list[Case] = field(default_factory=list)
    gold: dict[str, tuple[str, list[dict]]] = field(default_factory=dict)

    # --- people and history ----------------------------------------------------------------
    def person(self, p: Person, updated: datetime) -> None:
        self.identities.append(
            {
                "user_id": p.user_id,
                "name": p.name,
                "email": p.email,
                "phone": p.phone,
                "email_hash": email_hash(p.email),
                "updated_at": iso(updated),
            }
        )

    def change(self, user_id: str, when: datetime, field_name: str, old: str, new: str) -> None:
        self.audit_log.append(
            {
                "audit_id": f"audit_{len(self.audit_log) + 1:02d}",
                "user_id": user_id,
                "event_time": iso(when),
                "field_name": field_name,
                "old_value": old,
                "new_value": new,
            }
        )

    def act(
        self, when: datetime, event: str, source: str, target: str, rid: str, result: str, details: str
    ) -> None:
        self.activity.append(
            {
                "event_time": iso(when),
                "event_type": event,
                "source_system": source,
                "target_system": target,
                "resource_id": rid,
                "reported_result": result,
                "details": details,
            }
        )

    # --- evidence files --------------------------------------------------------------------
    def evidence(self, rel: str, origin: str, captured: datetime, body: bytes) -> str:
        file_id = f"evidence_{len(self.manifest) + 1:02d}"
        self.manifest.append(
            {"file_id": file_id, "file_path": rel, "origin": origin, "captured_at": iso(captured)}
        )
        self.files[rel] = body
        return file_id

    def inventory(
        self,
        captured: datetime,
        scope: list[str],
        rows: list[tuple[str, str, dict]],
        replica_rows: list[tuple[str, str, dict]] = (),
        topology: str = "primary",
    ) -> str:
        """A SQLite snapshot: every record of the systems in `scope`, wrapped as JSON per row.

        A replica table, when given, holds copies that the primary no longer has.
        """
        stamp = iso(captured)
        out = [
            "-- Synthetic point-in-time database snapshot (fictional data)",
            f"-- Captured at: {stamp}",
            f"-- Topology: {topology}",
            "BEGIN TRANSACTION;",
            "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);",
        ]
        out += [f"INSERT INTO snapshot_scope VALUES('{s}','{stamp}');" for s in scope]
        tables = [("customer_data_primary", rows)]
        if replica_rows:
            tables.append(("customer_data_read_replica", replica_rows))
        for table, items in tables:
            out.append(
                f"CREATE TABLE {table} (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, "
                "collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);"
            )
            for system, collection, rec in items:
                body = json.dumps(rec, separators=(",", ":")).replace("'", "''")
                out.append(
                    f"INSERT INTO {table} VALUES('{rec['resource_id']}','{system}','{collection}',"
                    f"'{body}','{stamp}');"
                )
        out.append("COMMIT;")
        name = f"evidence/databases/{topology}_snapshot_{captured:%Y%m%d_%H%M}.sql"
        return self.evidence(name, f"{topology}_database_export", captured, _text(out))

    def json_export(self, captured: datetime, exports: dict) -> str:
        body = {
            "exported_at": iso(captured),
            "source": "service_resource_export",
            "service_exports": exports,
        }
        name = f"evidence/records/service_export_{captured:%Y%m%d_%H%M}.json"
        return self.evidence(name, "service_resource_export", captured, _json(body))

    def log(self, captured: datetime, name: str, lines: list[str]) -> str:
        return self.evidence(f"evidence/logs/{name}", "central_log_export", captured, _text(lines))

    def handover(self, captured: datetime, lines: list[str]) -> str:
        rel = f"evidence/records/account_handover_{captured:%Y%m%d}.txt"
        return self.evidence(rel, "operations_handover_note", captured, _text(lines))

    def media(self, captured: datetime, rel: str, origin: str, body: bytes, extraction: dict) -> str:
        file_id = self.evidence(rel, origin, captured, body)
        self.fixtures[Path(rel).name] = {
            "schema": "vision_extraction/v1",
            "model": "fixture",
            "status": "ok",
            "extraction": extraction,
            "errors": [],
            "usage": [],
            "note": "Hand-written transcription of a synthetic file; stands in for the vision call.",
        }
        return file_id

    # --- expected answers -------------------------------------------------------------------
    def expect(self, case: Case, status: str, steps: list[tuple[str, str, str, str]]) -> None:
        """Expected answer from the scenario's ground truth: (operation, system, resource, evidence)."""
        rendered = render_steps(make_step(op, sys_, rid, ev) for op, sys_, rid, ev in steps)
        self.gold[case.case_id] = (status, rendered)


def _text(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def _json(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# --- record builders ---------------------------------------------------------------------------


def profile(rid: str, p: Person, email: str | None = None) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "customer_reference": p.user_id,
        "full_name": p.name,
        "email": email or p.email,
        "phone": p.phone,
        "account_status": "ACTIVE",
    }
    return ("account_service", "customer_profiles", rec)


def analytics(rid: str, p: Person | None) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "customer_reference": p.user_id if p else None,
        "email": p.email if p else None,
        "audience_cohort": "home_and_garden",
    }
    return ("analytics_platform", "customer_analytics_profiles", rec)


def audit_record(rid: str, p: Person) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "subject_reference": p.user_id,
        "event_code": "PASSWORD_RESET_COMPLETED",
        "retention_until": "2033-12-31T23:59:59Z",
    }
    return ("security_service", "security_audit_records", rec)


def search_doc(rid: str, p: Person, email: str | None = None) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "customer_reference": p.user_id,
        "display_name": p.name,
        "email": email or p.email,
    }
    return ("search_service", "customer_search_documents", rec)


def connector(rid: str, p: Person, enabled: bool) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "customer_reference": p.user_id,
        "connector_name": "crm_contact_sync",
        "enabled": 1 if enabled else 0,
    }
    return ("integration_service", "connector_subscriptions", rec)


def backup(rid: str, p: Person) -> tuple[str, str, dict]:
    rec = {
        "resource_id": rid,
        "customer_reference": p.user_id,
        "archive_key": f"archive/customer-data/{rid}.tar.zst",
        "restore_allowed": 1,
    }
    return ("backup_service", "backup_restore_entries", rec)


# --- workflow rows -----------------------------------------------------------------------------


def wf_profile(w: World, c: Case, rid: str, result: str = "SUCCESS") -> None:
    msg = f"Deletion worker submitted primary profile {rid} to account_service."
    w.act(c.work(-60, SEC_PROFILE), "DELETE_RECORD", "deletion_worker", "account_service", rid, result, msg)


def wf_policy(w: World, c: Case, event: str, target: str, rid: str, minutes: float = -20) -> None:
    msg = f"Policy action completed for {rid}."
    w.act(c.work(minutes, SEC_POLICY), event, "deletion_worker", target, rid, "SUCCESS", msg)


def wf_result(w: World, c: Case, event: str, target: str, rid: str) -> None:
    msg = f"Deletion worker received the final result for {rid} from {target}."
    w.act(c.work(75, SEC_RESULT), event, "deletion_worker", target, rid, "SUCCESS", msg)


# --- scenarios ---------------------------------------------------------------------------------
# Each scenario writes the evidence for one request and records its expected answer.
# n is the resource-number base for the case, so ids never collide between cases.


def scenario_complete(w: World, c: Case, n: int) -> None:
    """Deletion really finished: profile gone, analytics anonymised, audit record retained."""
    p = c.person
    acc, ana, aud = f"account_{n + 1}", f"analytics_{n + 2}", f"audit_record_{n + 3}"
    w.inventory(c.at(-5), ["account_service", "analytics_platform", "security_service"],
                [profile(acc, p), analytics(ana, p), audit_record(aud, p)])  # fmt: skip
    wf_profile(w, c, acc)
    wf_policy(w, c, "ANONYMIZE_RECORD", "analytics_platform", ana)
    w.act(c.work(-10, 40), "RETAIN_RECORD", "deletion_worker", "security_service", aud, "SUCCESS",
          f"Retention policy kept {aud} under the security retention rule.")  # fmt: skip
    w.act(c.at(1, 2, 7, 1), "LINK_RESOURCE_IDENTITY", "data_catalog", "security_service", aud, "SUCCESS",
          f"Data catalog linked {aud} ({aud}) to {p.email} and {p.user_id}.")  # fmt: skip
    win = w.inventory(c.at(4, 3), ["account_service", "analytics_platform", "security_service"],
                      [analytics(ana, None), audit_record(aud, p)])  # fmt: skip
    w.expect(c, "COMPLETE", [
        ("VERIFY_RECORD_ABSENT", "account_service", acc, win),
        ("VERIFY_RECORD_PRESENT", "security_service", aud, win),
        ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", ana, win),
    ])  # fmt: skip


def scenario_ghost_copy(w: World, c: Case, n: int, decoy: Person) -> None:
    """The workflow said COMPLETE, but a read replica and the search index still hold copies.

    A look-alike customer's search document sits in the same export and must not be linked. The
    data export has no identifier anywhere; only the timing of its workflow row ties it to the case.
    """
    p = c.person
    acc, srch, exp, other = f"account_{n + 1}", f"search_{n + 2}", f"export_{n + 3}", f"search_{n + 9}"
    w.inventory(c.at(-5), ["account_service", "search_service"],
                [profile(acc, p), search_doc(srch, p), search_doc(other, decoy)])  # fmt: skip
    w.act(c.at(-5, 1, 0, 30), "INDEX_RECORD", "account_service", "search_service", srch, "SUCCESS",
          f"Customer search-index refresh processed {srch}.")  # fmt: skip
    wf_profile(w, c, acc)
    wf_policy(w, c, "DELETE_RECORD", "search_service", srch)
    w.act(c.work(2, SEC_COMPLETED), "DELETE_RECORD", "deletion_worker", "export_service", exp, "SUCCESS",
          f"Deletion worker reported the requested operation as completed for {exp}.")  # fmt: skip
    sql = w.inventory(c.at(4, 2), ["account_service"], [], replica_rows=[profile(acc, p)],
                      topology="read_replica")  # fmt: skip
    hit = {"resource_id": srch, "display_name": p.name, "email": p.email, "customer_reference": None}
    decoy_hit = {"resource_id": other, "display_name": decoy.name, "email": decoy.email}
    exports = {"search_service": {"index_queries": [
        {"observed_at": iso(c.at(4, 3, 10)), "query": p.email, "resource_id": srch,
         "hits": {"documents": [hit], "total": 1}},
        {"observed_at": iso(c.at(4, 3, 11)), "query": decoy.email, "resource_id": other,
         "hits": {"documents": [decoy_hit], "total": 1}},
    ]}}  # fmt: skip
    js = w.json_export(c.at(4, 4), exports)
    w.expect(c, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "account_service", acc, sql),
        ("REPORT_REMAINING_RECORD", "search_service", srch, js),
        ("DELETE_RECORD", "account_service", acc, ""),
        ("DELETE_RECORD", "search_service", srch, ""),
        ("VERIFY_RECORD_ABSENT", "account_service", acc, ""),
        ("VERIFY_RECORD_ABSENT", "export_service", exp, ""),
        ("VERIFY_RECORD_ABSENT", "search_service", srch, ""),
    ])  # fmt: skip


def scenario_active_flow(w: World, c: Case, n: int) -> None:
    """Request filed from an old email. A connector the workflow 'disabled' is still running and
    has re-created the deleted search document."""
    p = c.person
    acc, flow, srch = f"account_{n + 1}", f"data_flow_{n + 2}", f"search_{n + 3}"
    w.inventory(c.at(-5), ["account_service", "integration_service", "search_service"],
                [profile(acc, p), connector(flow, p, True), search_doc(srch, p)])  # fmt: skip
    wf_profile(w, c, acc)
    wf_policy(w, c, "DELETE_RECORD", "search_service", srch)
    wf_policy(w, c, "DISABLE_DATA_FLOW", "integration_service", flow, minutes=-19)
    w.act(c.work(45, SEC_RESULT), "CHECK_DATA_FLOW", "deletion_worker", "integration_service", flow,
          "SUCCESS", f"Connector status check confirmed that data flow {flow} remains enabled.")  # fmt: skip
    w.act(c.work(180, 0), "SYNC_RECORD", "integration_service", "search_service", flow, "SUCCESS",
          f"Active connector repopulated downstream resource {srch}.")  # fmt: skip
    w.inventory(c.at(4, 1), ["account_service"], [])
    exports = {
        "integration_service": {"connectors": [
            {"observed_at": iso(c.at(4, 2, 5)), "resource_id": flow, "customer_reference": p.user_id,
             "customer_email": p.email, "connector_name": "crm_contact_sync", "enabled": True},
        ]},
        "search_service": {"index_queries": [
            {"observed_at": iso(c.at(4, 2, 9)), "query": p.email, "resource_id": srch,
             "hits": {"documents": [{"resource_id": srch, "email": p.email}], "total": 1}},
        ]},
    }  # fmt: skip
    js = w.json_export(c.at(4, 3), exports)
    w.expect(c, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "search_service", srch, js),
        ("REPORT_ACTIVE_DATA_FLOW", "integration_service", flow, js),
        ("DISABLE_DATA_FLOW", "integration_service", flow, ""),
        ("DELETE_RECORD", "search_service", srch, ""),
        ("VERIFY_RECORD_ABSENT", "account_service", acc, ""),
        ("VERIFY_RECORD_ABSENT", "search_service", srch, ""),
        ("VERIFY_DATA_FLOW_DISABLED", "integration_service", flow, ""),
    ])  # fmt: skip


def scenario_restore_risk(w: World, c: Case, n: int) -> None:
    """Profile deleted, but a backup can still be restored (seen in a service log)."""
    p = c.person
    acc, bak = f"account_{n + 1}", f"backup_{n + 2}"
    w.inventory(c.at(-5), ["account_service", "backup_service"], [profile(acc, p), backup(bak, p)])
    wf_profile(w, c, acc)
    wf_policy(w, c, "BLOCK_RESTORE", "backup_service", bak)
    w.act(c.work(45, SEC_RESULT), "CHECK_RESTORE_ACCESS", "deletion_worker", "backup_service", bak,
          "SUCCESS", f"Restore-access check confirmed that archive {bak} can still be restored.")  # fmt: skip
    w.inventory(c.at(4, 1), ["account_service"], [])
    trace = f"tr-{n}"
    lines = [
        f"{iso(c.at(4, 2, 0, 1))} "
        + json.dumps({"service": "backup_service", "trace_id": trace, "level": "DEBUG",
                      "event": "restore_policy_read_started", "lookup_key": p.email}),
        f"{iso(c.at(4, 2, 0, 2))} "
        + json.dumps({"service": "backup_service", "trace_id": trace, "level": "INFO",
                      "event": "restore_policy_read_completed", "resource_id": bak,
                      "archive_present": True, "restore_allowed": True}),
    ]  # fmt: skip
    log = w.log(c.at(4, 6), f"backup_policy_events_{c.at(4):%Y%m%d}.log", lines)
    w.expect(c, "INCOMPLETE", [
        ("REPORT_RESTORE_RISK", "backup_service", bak, log),
        ("BLOCK_RESTORE", "backup_service", bak, ""),
        ("VERIFY_RECORD_ABSENT", "account_service", acc, ""),
        ("VERIFY_RESTORE_BLOCKED", "backup_service", bak, ""),
    ])  # fmt: skip


def scenario_not_deletion(w: World, c: Case, n: int) -> None:
    """The request is not a deletion request, even though data exists."""
    w.inventory(c.at(-5), ["account_service"], [profile(f"account_{n + 1}", c.person)])
    w.expect(c, "NOT_APPLICABLE", [])


def scenario_time_rule(w: World, c: Case, n: int) -> None:
    """Complete at audit time. A snapshot taken after audit_at shows the profile re-created; it
    must not change this answer."""
    p = c.person
    acc, ana = f"account_{n + 1}", f"analytics_{n + 2}"
    w.inventory(c.at(-5), ["account_service", "analytics_platform"], [profile(acc, p), analytics(ana, p)])
    wf_profile(w, c, acc)
    wf_policy(w, c, "ANONYMIZE_RECORD", "analytics_platform", ana)
    win = w.inventory(c.at(4, 3), ["account_service", "analytics_platform"], [analytics(ana, None)])
    w.inventory(c.at(9, 3), ["account_service"], [profile(acc, p)])  # after audit_at
    w.expect(c, "COMPLETE", [
        ("VERIFY_RECORD_ABSENT", "account_service", acc, win),
        ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", ana, win),
    ])  # fmt: skip


def scenario_media(w: World, c: Case, n: int) -> None:
    """Leftovers visible only in a PDF and a screenshot."""
    p = c.person
    acc, att, msg = f"account_{n + 1}", f"support_{n + 2}", f"support_{n + 3}"
    w.inventory(c.at(-5), ["account_service"], [profile(acc, p)])
    wf_profile(w, c, acc)
    wf_result(w, c, "REDACT_RECORD", "support_platform", msg)
    filename = "delivery_photo_upload.pdf"
    w.act(c.at(3, 1, 7, 1), "LINK_RESOURCE_IDENTITY", "data_catalog", "support_platform", att, "SUCCESS",
          f"Data catalog linked {att} ({filename}) to {p.email} and {p.user_id}.")  # fmt: skip
    w.inventory(c.at(4, 1), ["account_service"], [])
    pdf_lines = [
        "Support attachment record",
        f"Attachment: {att}",
        f"File: {filename}",
        f"Uploaded by: {p.name} <{p.email}>",
        f"Uploaded: {c.at(-20):%d %b %Y} 10:24 UTC",
        "Status: Stored",
    ]
    subject = {"names": [p.name], "emails": [p.email], "phones": [], "user_ids": [], "hashes": [],
               "resource_ids": [att], "labels": [filename]}  # fmt: skip
    pdf_extraction = _extraction(
        "document", "Support attachment record", subject,
        [_record("header", att, [filename], names=[p.name], emails=[p.email], part=True,
                 fields={"Status": "Stored"}, status=["Stored"])],
    )  # fmt: skip
    pdf = w.media(c.at(4, 2), "evidence/documents/support_attachment_record.pdf",
                  "document_store_export", make_pdf(pdf_lines), pdf_extraction)  # fmt: skip
    ticket = "TCK-0412"
    png_lines = [
        f"Support console   {ticket}   {msg}",
        f"Customer: {p.name}",
        f"Email: {p.email}",
        "Status: Open",
        "Message: Please send the replacement to my new address.",
    ]
    subject = {"names": [p.name], "emails": [p.email], "phones": [], "user_ids": [], "hashes": [],
               "resource_ids": [msg], "labels": [ticket]}  # fmt: skip
    png_extraction = _extraction(
        "screenshot", f"{ticket} support console", subject,
        [_record("header", msg, [ticket], names=[p.name], emails=[p.email], part=True,
                 fields={"Status": "Open"}, status=["Open"]),
         _record("table", None, [], names=[p.name], part=True,
                 fields={"Message": "Please send the replacement to my new address."})],
    )  # fmt: skip
    png = w.media(c.at(4, 3), "evidence/images/support_console_ticket.png", "screenshot_capture",
                  make_png(png_lines), png_extraction)  # fmt: skip
    w.expect(c, "INCOMPLETE", [
        ("REPORT_REMAINING_RECORD", "support_platform", att, pdf),
        ("REPORT_EXPOSED_PII", "support_platform", msg, png),
        ("DELETE_RECORD", "support_platform", att, ""),
        ("REDACT_RECORD", "support_platform", msg, ""),
        ("VERIFY_RECORD_ABSENT", "account_service", acc, ""),
        ("VERIFY_RECORD_ABSENT", "support_platform", att, ""),
        ("VERIFY_RECORD_REDACTED", "support_platform", msg, ""),
    ])  # fmt: skip


def scenario_exposed(w: World, c: Case, n: int) -> None:
    """Profile deleted, but the tombstone still shows the email and analytics was never anonymised."""
    p = c.person
    acc, tomb, ana = f"account_{n + 1}", f"tombstone_{n + 2}", f"analytics_{n + 3}"
    w.inventory(c.at(-5), ["account_service", "analytics_platform"], [profile(acc, p), analytics(ana, p)])
    wf_profile(w, c, acc)
    wf_policy(w, c, "ANONYMIZE_RECORD", "analytics_platform", ana)
    wf_result(w, c, "REDACT_RECORD", "identity_service", tomb)
    w.act(c.at(3, 1, 7, 1), "LINK_RESOURCE_IDENTITY", "data_catalog", "identity_service", tomb, "SUCCESS",
          f"Data catalog linked {tomb} ({p.user_id}) to {p.email} and {p.user_id}.")  # fmt: skip
    w.inventory(c.at(4, 1), ["account_service"], [])
    log = w.log(c.at(4, 5), f"analytics_reads_{c.at(4):%Y%m%d}.log", [
        f"{iso(c.at(4, 2, 30))} level=INFO service=analytics_platform trace_id=tr-{n} "
        f'event=analytics_profile_read resource_id={ana} customer_reference={p.user_id} email="{p.email}"',
    ])  # fmt: skip
    txt = w.handover(c.at(4, 6), [
        "Account handover note (synthetic)",
        "ts | system | resource | type | result | key",
        f"- {iso(c.at(4, 4))} | identity_service | {tomb} | ACCOUNT_TOMBSTONE | IDENTIFIABLE | {p.email}",
    ])  # fmt: skip
    w.expect(c, "INCOMPLETE", [
        ("REPORT_EXPOSED_PII", "analytics_platform", ana, log),
        ("REPORT_EXPOSED_PII", "identity_service", tomb, txt),
        ("REDACT_RECORD", "identity_service", tomb, ""),
        ("ANONYMIZE_RECORD", "analytics_platform", ana, ""),
        ("VERIFY_RECORD_ABSENT", "account_service", acc, ""),
        ("VERIFY_RECORD_REDACTED", "identity_service", tomb, ""),
        ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", ana, ""),
    ])  # fmt: skip


# --- vision fixtures, PDF and PNG ---------------------------------------------------------------


def _ids(**kw) -> dict:
    return {k: list(kw.get(k, [])) for k in ("names", "emails", "phones", "user_ids", "hashes")}


def _record(where, rid, labels, part=False, fields=None, status=(), **identifiers) -> dict:
    return {
        "where": where,
        "table": "Conversation" if where == "table" else None,
        "row": 1 if where == "table" else None,
        "resource_id": rid,
        "labels": list(labels),
        "identifiers": _ids(**identifiers),
        "part_of_page_subject": part,
        "fields": dict(fields or {}),
        "status_text": list(status),
        "state_hints": [],
        "timestamps": [],
    }


def _extraction(kind: str, title: str, subject: dict, records: list[dict]) -> dict:
    tables = [{"title": "Conversation", "declared_count": None, "rows_extracted": 1}]
    return {
        "schema": "vision_extraction/v1",
        "artifact": {
            "kind": kind,
            "product": None,
            "title": title,
            "subtitle": None,
            "pages": 1,
            "as_of": None,
            "status_text": [],
            "filters": [],
            "page_subject": subject,
            "tables": tables if kind == "screenshot" else [],
        },
        "records": records,
        "events": [],
        "unreadable": [],
    }


def make_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with plain text in Helvetica, built by hand (no dependencies)."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = ["BT", "/F1 11 Tf", "14 TL", "56 760 Td"]
    for line in lines:
        text += [f"({esc(line)}) Tj", "T*"]
    text.append("ET")
    stream = "\n".join(text).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_png(lines: list[str]) -> bytes:
    """A small screenshot-like PNG drawn with Pillow's built-in bitmap font."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default_imagefont()
    img = Image.new("RGB", (460, 40 + 18 * len(lines)), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, img.width, 26), fill=(32, 64, 96))
    draw.text((10, 8), "Helpdesk console (synthetic)", fill=(255, 255, 255), font=font)
    for i, line in enumerate(lines):
        draw.text((10, 36 + 18 * i), line, fill=(20, 20, 20), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


# --- the world ---------------------------------------------------------------------------------


def _person(i: int, first: str, last: str, domain: str, old: str | None = None) -> Person:
    email = f"{first.lower()}.{last.lower()}@{domain}"
    return Person(f"user_{i:02d}", f"{first} {last}", email, f"+1 202 555 01{i:02d}", old)


PEOPLE = {
    "avery": _person(1, "Avery", "Lane", "example.com"),
    "blake": _person(2, "Blake", "Sample", "example.net"),
    "casey": _person(3, "Casey", "Demo", "example.org", old="casey.d.old@example.org"),
    "devon": _person(4, "Devon", "Test", "example.com"),
    "emery": _person(5, "Emery", "Placeholder", "example.net"),
    "finley": _person(6, "Finley", "Mock", "example.org"),
    "gray": _person(7, "Gray", "Fixture", "example.com"),
    "harper": _person(8, "Harper", "Stub", "example.net"),
    "blake_lookalike": Person("user_09", "Blake Sample", "blake.sample.jr@example.net", "+1 202 555 0109"),
    "indigo": _person(11, "Indigo", "Draft", "example.com"),
    "jules": _person(12, "Jules", "Mockup", "example.net"),
    "kai": _person(13, "Kai", "Prototype", "example.org"),
    "lane": _person(14, "Lane", "Sketch", "example.com"),
    "morgan": _person(15, "Morgan", "Trial", "example.net"),
    "jules_lookalike": Person("user_19", "Jules Mockup", "jules.mockup2@example.net", "+1 202 555 0119"),
}

DELETE_TEXT = "Please close my account and delete the personal data you hold about me."

# (case_id, person, scenario, request text, reported status, sample?)
CASES: list[tuple[str, str, Callable, str, str, bool]] = [
    ("case_01", "avery", scenario_complete,
     "Hi, I no longer use this account. Please close it and remove my personal details.",
     "COMPLETE", True),
    ("case_02", "blake", scenario_ghost_copy,
     "Please delete my account, including any data export you prepared for me.", "COMPLETE", True),
    ("case_03", "casey", scenario_active_flow, DELETE_TEXT, "COMPLETE", True),
    ("case_04", "devon", scenario_restore_risk,
     "Erase my profile and everything linked to it, backups included.", "PARTIALLY_COMPLETE", True),
    ("case_05", "emery", scenario_not_deletion,
     "Can you tell me what personal information you hold about me? I am not asking for deletion.",
     "NOT_STARTED", True),
    ("case_06", "finley", scenario_time_rule, DELETE_TEXT, "COMPLETE", True),
    ("case_07", "gray", scenario_media,
     "Please delete my account and the photo I uploaded to support.", "COMPLETE", True),
    ("case_08", "harper", scenario_exposed, DELETE_TEXT, "COMPLETE", True),
    ("case_101", "indigo", scenario_complete, DELETE_TEXT, "COMPLETE", False),
    ("case_102", "jules", scenario_ghost_copy,
     "Delete my account and my data export, please.", "COMPLETE", False),
    ("case_103", "kai", scenario_restore_risk, DELETE_TEXT, "PARTIALLY_COMPLETE", False),
    ("case_104", "lane", scenario_exposed,
     "Por favor, eliminen mi cuenta y todos mis datos personales.", "COMPLETE", False),
    ("case_105", "morgan", scenario_not_deletion,
     "Please remove me from marketing lists only. I still want order receipts.", "NOT_STARTED", False),
]  # fmt: skip

DECOYS = {"blake": "blake_lookalike", "jules": "jules_lookalike"}


def build() -> World:
    w = World()
    for key, p in PEOPLE.items():
        w.person(p, START - timedelta(days=30))
        if p.old_email:
            when = START - timedelta(days=90)
            w.change(p.user_id, when, "email", p.old_email, p.email)
            w.change(p.user_id, when, "email_hash", email_hash(p.old_email), email_hash(p.email))
        if key == "devon":
            w.change(p.user_id, START - timedelta(days=200), "phone", "+1 202 555 0199", p.phone)
    for slot, (case_id, who, scenario, text, reported, sample) in enumerate(CASES):
        p = PEOPLE[who]
        c = Case(case_id, p, slot, text, reported, sample, request_email=p.old_email or p.email)
        w.cases.append(c)
        n = 1000 + 100 * slot
        if who in DECOYS:
            scenario(w, c, n, PEOPLE[DECOYS[who]])
        else:
            scenario(w, c, n)
    return w


# --- writing -------------------------------------------------------------------------------------


def _csv(rows: list[dict], header: list[str]) -> bytes:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
    wr.writeheader()
    wr.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _requests(cases: list[Case]) -> list[dict]:
    return [
        {
            "case_id": c.case_id,
            "email": c.request_email,
            "requested_at": iso(c.requested_at),
            "reported_status": c.reported_status,
            "reported_at": iso(c.reported_at),
            "audit_at": iso(c.audit_at),
            "request_text": c.request_text,
        }
        for c in cases
    ]


def render(w: World) -> dict[str, bytes]:
    """Every output file as bytes, keyed by path relative to the data directory."""
    activity = sorted(w.activity, key=lambda a: (a["event_time"], a["resource_id"]))
    for i, a in enumerate(activity, 1):
        a["activity_id"] = f"activity_{i:03d}"
    req_header = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at",
                  "request_text"]  # fmt: skip
    act_header = ["activity_id", "event_time", "event_type", "source_system", "target_system",
                  "resource_id", "reported_result", "details"]  # fmt: skip
    samples = [c for c in w.cases if c.sample]
    out = {
        "deletion_requests.csv": _csv(_requests([c for c in w.cases if not c.sample]), req_header),
        "sample_deletion_requests.csv": _csv(_requests(samples), req_header),
        "sample_output.csv": _csv(
            [
                {
                    "case_id": c.case_id,
                    "deletion_status": w.gold[c.case_id][0],
                    "steps_json": serialize_steps(w.gold[c.case_id][1]),
                }
                for c in samples
            ],
            ["case_id", "deletion_status", "steps_json"],
        ),
        "user_identities.csv": _csv(
            w.identities, ["user_id", "name", "email", "phone", "email_hash", "updated_at"]
        ),
        "user_audit_log.csv": _csv(
            w.audit_log, ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"]
        ),
        "system_activity.csv": _csv(activity, act_header),
        "deletion_rules.csv": _csv(
            [
                dict(zip(("rule_id", "data_category", "condition", "required_action"), r, strict=True))
                for r in RULES
            ],
            ["rule_id", "data_category", "condition", "required_action"],
        ),  # fmt: skip
        "evidence_files.csv": _csv(w.manifest, ["file_id", "file_path", "origin", "captured_at"]),
    }
    out.update(w.files)
    for name, entry in sorted(w.fixtures.items()):
        out[f"vision_fixtures/{name}.json"] = _json(entry)
    return out


def expected_answers() -> dict[str, tuple[str, list[dict]]]:
    """Expected (status, steps) for every request, samples and evaluation set alike."""
    return build().gold


def write(out_dir: Path) -> list[str]:
    files = render(build())
    out_dir = Path(out_dir)
    if out_dir.exists():
        if any(out_dir.iterdir()) and not (out_dir / "evidence_files.csv").exists():
            raise SystemExit(f"{out_dir} is not empty and does not look like a dataset; not replacing it")
        shutil.rmtree(out_dir)
    for rel, body in sorted(files.items()):
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return sorted(files)


def main(argv=None) -> int:
    from deletion_audit.paths import ROOT

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "synthetic"))
    args = ap.parse_args(argv)
    names = write(Path(args.out))
    print(f"wrote {len(names)} files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
