"""Unit checks: contract, timestamps, identifiers, systems and the CSV loaders."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from deletion_audit import data
from deletion_audit.contract import (
    OP_PHASE,
    OP_STATE,
    case_sort_key,
    make_step,
    render_steps,
    serialize_steps,
    validate_row,
    write_output,
)
from deletion_audit.identifiers import email_hash, identifier_tokens, is_null, norm_identifier, parse_bool
from deletion_audit.systems import category_from_hints, system_for
from deletion_audit.timeutil import minutes_between, ts_norm


class Identifiers(unittest.TestCase):
    def test_emails(self):
        self.assertEqual(norm_identifier("Avery.Lane@Example.com"), "e:avery.lane@example.com")
        self.assertEqual(norm_identifier(' "x.y@example.org" '), "e:x.y@example.org")
        self.assertIsNone(norm_identifier("not an email@"))
        # look-alikes stay distinct: matching is exact
        self.assertNotEqual(
            norm_identifier("blake.sample.jr@example.net"), norm_identifier("blake.sample@example.net")
        )

    def test_phones(self):
        self.assertEqual(norm_identifier("+1 202 555 0101"), "p:12025550101")
        self.assertEqual(norm_identifier("+1 (202) 555-0101"), "p:12025550101")
        self.assertEqual(norm_identifier("+12025550101"), "p:12025550101")
        self.assertIsNone(norm_identifier("2026-09-30"))  # a date is not a phone
        self.assertIsNone(norm_identifier("+12 345"))  # too few digits

    def test_hashes_and_user_ids(self):
        h = email_hash("avery.lane@example.com").upper()
        self.assertEqual(norm_identifier(h), "h:" + h.lower())
        self.assertIsNone(norm_identifier(h[:63]))
        self.assertEqual(norm_identifier("user_17"), "u:user_17")
        self.assertIsNone(norm_identifier("user_17x"))
        self.assertIsNone(norm_identifier("tombstone_1702"))

    def test_nulls_and_bools(self):
        for v in (None, "", "null", "NULL", "None", "[REDACTED]", '"null"', 5):
            self.assertIsNone(norm_identifier(v))
        self.assertTrue(is_null("[REDACTED]"))
        self.assertFalse(is_null("user_1"))
        self.assertEqual(
            [parse_bool(v) for v in ("true", "0", "Denied", 1, "maybe")], [True, False, False, True, None]
        )

    def test_email_tokens_include_hash(self):
        toks = identifier_tokens("Avery.Lane@example.com")
        self.assertEqual(toks, {"e:avery.lane@example.com", "h:" + email_hash("avery.lane@example.com")})


class Time(unittest.TestCase):
    def test_ts_norm(self):
        self.assertEqual(ts_norm("2026-09-15T11:59:39Z"), "2026-09-15T11:59:39Z")
        self.assertEqual(ts_norm("2026-09-15 11:59"), "2026-09-15T11:59:00Z")
        self.assertEqual(ts_norm("2026-09-15T11:59:39.123Z"), "2026-09-15T11:59:39Z")
        self.assertEqual(ts_norm("2026-09-15T01:30:00+02:00"), "2026-09-14T23:30:00Z")
        self.assertEqual(ts_norm("2026-09-15"), "2026-09-15T00:00:00Z")
        self.assertIsNone(ts_norm("Sep 15 11:59:39"))
        self.assertEqual(minutes_between("2026-09-15T12:00:00Z", "2026-09-15T11:30:00Z"), 30.0)


class Systems(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(system_for("audit_record_701"), "security_service")
        self.assertEqual(system_for("data_flow_1202"), "integration_service")
        self.assertEqual(system_for("email_delivery_12"), "email_delivery_service")
        self.assertEqual(system_for("object_42"), "object_storage")
        self.assertIsNone(system_for("order_19"))

    def test_categories(self):
        self.assertEqual(category_from_hints("support_platform", "attachment_pii_scan"), "SUPPORT_ATTACHMENT")
        self.assertEqual(category_from_hints("support_platform", "support_conversations"), "SUPPORT_MESSAGE")
        self.assertIsNone(category_from_hints("support_platform"))
        self.assertEqual(category_from_hints("identity_service"), "ACCOUNT_TOMBSTONE")
        self.assertEqual(category_from_hints("analytics_platform", "contact_points"), "SECONDARY_IDENTIFIER")
        self.assertEqual(category_from_hints("integration_service"), "DATA_FLOW")


class Contract(unittest.TestCase):
    # Hand-written, deliberately shuffled, with one duplicate.
    RAW = [
        {"operation": "VERIFY_RESTORE_BLOCKED", "system_id": "backup_service", "resource_id": "backup_7301"},
        {"operation": "DELETE_RECORD", "system_id": "search_service", "resource_id": "search_402"},
        {
            "operation": "REPORT_RESTORE_RISK",
            "system_id": "backup_service",
            "resource_id": "backup_7301",
            "evidence_id": "evidence_03",
        },
        {"operation": "VERIFY_RECORD_ABSENT", "system_id": "account_service", "resource_id": "account_7001"},
        {
            "operation": "REPORT_REMAINING_RECORD",
            "system_id": "search_service",
            "resource_id": "search_402",
            "evidence_id": "evidence_02",
        },
        {"operation": "BLOCK_RESTORE", "system_id": "backup_service", "resource_id": "backup_7301"},
        {"operation": "VERIFY_RECORD_ABSENT", "system_id": "search_service", "resource_id": "search_402"},
        {
            "operation": "REPORT_REMAINING_RECORD",
            "system_id": "account_service",
            "resource_id": "account_7001",
            "evidence_id": "evidence_04",
        },
        {"operation": "DELETE_RECORD", "system_id": "account_service", "resource_id": "account_7001"},
        {
            "operation": "VERIFY_RECORD_PRESENT",
            "system_id": "security_service",
            "resource_id": "audit_record_701",
        },
        {
            "operation": "DELETE_RECORD",
            "system_id": "search_service",
            "resource_id": "search_402",
        },  # duplicate
        {"operation": "DELETE_RECORD", "system_id": "search_service", "resource_id": "search_1201"},
        {
            "operation": "REPORT_REMAINING_RECORD",
            "system_id": "search_service",
            "resource_id": "search_1201",
            "evidence_id": "evidence_02",
        },
        {"operation": "VERIFY_RECORD_ABSENT", "system_id": "search_service", "resource_id": "search_1201"},
    ]
    EXPECTED = [
        ("FINDING", "REPORT_REMAINING_RECORD", "account_service", "account_7001", "evidence_04"),
        ("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1201", "evidence_02"),
        ("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_402", "evidence_02"),
        ("FINDING", "REPORT_RESTORE_RISK", "backup_service", "backup_7301", "evidence_03"),
        ("REMEDIATION", "BLOCK_RESTORE", "backup_service", "backup_7301", ""),
        ("REMEDIATION", "DELETE_RECORD", "account_service", "account_7001", ""),
        ("REMEDIATION", "DELETE_RECORD", "search_service", "search_1201", ""),
        ("REMEDIATION", "DELETE_RECORD", "search_service", "search_402", ""),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_7001", ""),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1201", ""),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_402", ""),
        ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_701", ""),
        ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_service", "backup_7301", ""),
    ]

    def test_render_sorts_numbers_and_dedupes(self):
        steps = render_steps(self.RAW)
        got = [
            (s["phase"], s["operation"], s["system_id"], s["resource_id"], s["evidence_id"]) for s in steps
        ]
        self.assertEqual(got, self.EXPECTED)
        self.assertEqual([s["step_no"] for s in steps], list(range(1, 14)))
        for s in steps:
            self.assertEqual(
                list(s), ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
            )
            self.assertEqual(s["state"], OP_STATE[s["operation"]])
        self.assertEqual(validate_row("INCOMPLETE", steps), [])
        # plain string sort on resource_id: "search_1201" < "search_402"
        self.assertLess(got.index(self.EXPECTED[1]), got.index(self.EXPECTED[2]))

    def test_serialization(self):
        steps = render_steps(
            [make_step("VERIFY_RECORD_ABSENT", "account_service", "account_5", "activity_9")]
        )
        self.assertEqual(
            serialize_steps(steps),
            '[{"step_no":1,"phase":"VERIFICATION","system_id":"account_service",'
            '"resource_id":"account_5","operation":"VERIFY_RECORD_ABSENT","state":"ABSENT",'
            '"evidence_id":"activity_9"}]',
        )
        self.assertEqual(serialize_steps([]), "[]")

    def test_validate_catches_errors(self):
        good = render_steps(self.RAW)
        self.assertEqual(validate_row("NOT_APPLICABLE", []), [])
        self.assertTrue(validate_row("NOT_APPLICABLE", good))
        self.assertTrue(validate_row("DONE", []))
        self.assertTrue(validate_row("COMPLETE", good))  # findings in a COMPLETE case
        self.assertTrue(validate_row("INCOMPLETE", list(reversed(good))))  # wrong order and step_no
        bad_ev = [dict(s) for s in good]
        bad_ev[0]["evidence_id"] = ""
        self.assertIn("step 1: a finding must cite evidence", validate_row("INCOMPLETE", bad_ev))
        verify_only = render_steps([make_step("VERIFY_RECORD_ABSENT", "account_service", "account_5")])
        self.assertTrue(validate_row("COMPLETE", verify_only))  # COMPLETE needs evidence
        self.assertTrue(validate_row("INCOMPLETE", verify_only))  # INCOMPLETE needs a finding
        extra = [dict(good[0], note="x")] + good[1:]
        self.assertTrue(validate_row("INCOMPLETE", extra))
        self.assertEqual(sorted(OP_PHASE), sorted(OP_STATE))

    def test_write_output(self):
        rows = [
            ("case_100", "NOT_APPLICABLE", []),
            ("case_33", "INCOMPLETE", render_steps(self.RAW)),
            (
                "case_5",
                "COMPLETE",
                render_steps(
                    [make_step("VERIFY_RECORD_ABSENT", "account_service", "account_5", "evidence_01")]
                ),
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "output.csv"
            write_output(rows, out)
            raw = out.read_bytes()
            self.assertNotIn(b"\r\n", raw)
            back = list(csv.reader(raw.decode("utf-8").splitlines()))
        self.assertEqual(back[0], ["case_id", "deletion_status", "steps_json"])
        self.assertEqual([r[0] for r in back[1:]], ["case_5", "case_33", "case_100"])
        self.assertEqual(json.loads(back[2][2]), render_steps(self.RAW))
        self.assertEqual(
            sorted(["case_100", "case_33", "case_550", "case_01"], key=case_sort_key),
            ["case_01", "case_33", "case_100", "case_550"],
        )


class Loaders(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(data.load_requests()), 5)
        self.assertEqual(len(data.load_requests(data.DATASET / "sample_deletion_requests.csv")), 8)
        self.assertEqual(len(data.load_identities()), 15)
        self.assertEqual(len(data.load_audit_log()), 3)
        self.assertEqual(len(data.load_activity()), 40)
        self.assertEqual(len(data.load_rules()), 15)
        manifest = data.load_manifest()
        self.assertEqual(len(manifest), 36)
        self.assertEqual({f.extension for f in manifest}, {"sql", "json", "log", "txt", "pdf", "png"})
        self.assertTrue(all(f.abs_path.is_file() for f in manifest))

    def test_values(self):
        reqs = data.load_requests()
        self.assertEqual([r.case_id for r in reqs], sorted((r.case_id for r in reqs), key=case_sort_key))
        for r in reqs:
            self.assertTrue(r.requested_at and r.reported_at and r.audit_at)
            self.assertIn(r.reported_status, ("COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"))
        self.assertTrue(all(a.ts for a in data.load_activity()))
        rules = data.load_rules()
        self.assertEqual(rules["ACCOUNT_TOMBSTONE"].required_action, "REDACT")
        self.assertEqual(rules["BACKUP_ARCHIVE"].required_action, "RETAIN")
        # email_hash is sha256 of the lower-cased email
        for ident in data.load_identities():
            self.assertEqual(ident.email_hash, email_hash(ident.email), ident.user_id)
            self.assertTrue(norm_identifier(ident.phone), ident.user_id)
