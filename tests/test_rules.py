"""Rules-engine checks: intent, identity cutoffs, planner rendering, the engine against the
synthetic ground truth, and the time rule."""

import unittest
from dataclasses import replace

from deletion_audit.context import AuditContext, CaseView
from deletion_audit.contract import serialize_steps, validate_row
from deletion_audit.data import DATASET, load_audit_log, load_identities, load_requests
from deletion_audit.decisions import Decision
from deletion_audit.engine_rules import run_rules
from deletion_audit.identity import IdentityIndex
from deletion_audit.intent import rule_classify
from deletion_audit.planner import render
from deletion_audit.synthetic import expected_answers

CTX = AuditContext.load()


class Intent(unittest.TestCase):
    def test_deletion_in_several_languages(self):
        for text in (
            "Please delete my account and all data.",
            "Por favor, eliminen mi cuenta.",
            "Merci de supprimer mon compte.",
            "계정을 삭제해 주세요.",
            "アカウントを削除してください。",
            "Bitte löschen Sie mein Konto.",
            "Please erase everything you hold about me.",
        ):
            self.assertTrue(rule_classify(text).authorized, text)

    def test_negations_override_deletion_words(self):
        for text in (
            "Please remove me from marketing lists only.",
            "I need a copy of my data, please don't delete anything.",
            "Fix my surname without removing any of my records.",
            "Help me with the login. Do not close the account.",
        ):
            self.assertFalse(rule_classify(text).authorized, text)
        self.assertFalse(rule_classify("").authorized)

    def test_evaluation_requests(self):
        na = [r.case_id for r in load_requests() if not rule_classify(r.request_text).authorized]
        self.assertEqual(na, ["case_105"])


class Identity(unittest.TestCase):
    IDX = IdentityIndex(load_identities(), load_audit_log())
    # audit_01: user_03 changed casey.d.old@example.org -> casey.demo@example.org at 2025-11-04T09:00:00Z

    def test_current_and_historical_email(self):
        for email in ("casey.demo@example.org", "Casey.D.Old@Example.org"):
            s = self.IDX.resolve(email, "2026-06-01T00:00:00Z")
            self.assertEqual(s.user_id, "user_03", email)
        s = self.IDX.resolve("casey.demo@example.org", "2026-06-01T00:00:00Z")
        self.assertIn("e:casey.d.old@example.org", s.identifiers)
        self.assertIn("audit_01", s.audit_ids)

    def test_identifier_acquired_after_cutoff_is_not_held(self):
        before = "2025-10-01T00:00:00Z"
        self.assertIsNone(self.IDX.resolve("casey.demo@example.org", before))
        s = self.IDX.resolve("casey.d.old@example.org", before)
        self.assertNotIn("e:casey.demo@example.org", s.identifiers)
        self.assertEqual(s.audit_ids, ())

    def test_look_alikes_do_not_resolve(self):
        self.assertIsNone(self.IDX.resolve("casey.dem0@example.org", "2026-06-01T00:00:00Z"))
        self.assertEqual(
            self.IDX.resolve("blake.sample.jr@example.net", "2026-06-01T00:00:00Z").user_id, "user_09"
        )


def d(rid, system, category, present=True, exposed=None, flow=None, restore=None, ev="evidence_01"):
    return Decision(system, rid, category, present, exposed, flow, restore, ev, ["test"])


class Planner(unittest.TestCase):
    RULES = CTX.rules

    def ops(self, decisions):
        status, steps = render(decisions, self.RULES)
        self.assertEqual(validate_row(status, steps), [])
        return status, [(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps]

    def test_complete_case_cites_evidence(self):
        status, ops = self.ops(
            [
                d("account_1", "account_service", "USER_PROFILE", present=False, ev="evidence_05"),
                d("audit_record_1", "security_service", "SECURITY_AUDIT_LOG"),
                d("analytics_1", "analytics_platform", "ANALYTICS_PROFILE", exposed=False),
                d("data_flow_1", "integration_service", "DATA_FLOW", flow=False),
                d("backup_1", "backup_service", "BACKUP_ARCHIVE", restore=False),
            ]
        )
        self.assertEqual(status, "COMPLETE")
        self.assertEqual(
            ops,
            [
                ("VERIFY_RECORD_ABSENT", "account_1", "evidence_05"),
                ("VERIFY_RECORD_PRESENT", "audit_record_1", "evidence_01"),
                ("VERIFY_RECORD_ANONYMIZED", "analytics_1", "evidence_01"),
                ("VERIFY_DATA_FLOW_DISABLED", "data_flow_1", "evidence_01"),
                ("VERIFY_RESTORE_BLOCKED", "backup_1", "evidence_01"),
            ],
        )

    def test_incomplete_case_pairs_every_finding(self):
        status, ops = self.ops(
            [
                d("search_1", "search_service", "SEARCH_INDEX_ENTRY"),
                d("tombstone_1", "identity_service", "ACCOUNT_TOMBSTONE", exposed=True),
                d("support_1", "support_platform", "SUPPORT_MESSAGE", exposed=False),
                d("data_flow_1", "integration_service", "DATA_FLOW", flow=True),
                d("backup_1", "backup_service", "BACKUP_ARCHIVE", restore=True),
            ]
        )
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(
            ops,
            [
                ("REPORT_REMAINING_RECORD", "search_1", "evidence_01"),
                ("REPORT_EXPOSED_PII", "tombstone_1", "evidence_01"),
                ("REPORT_ACTIVE_DATA_FLOW", "data_flow_1", "evidence_01"),
                ("REPORT_RESTORE_RISK", "backup_1", "evidence_01"),
                ("DISABLE_DATA_FLOW", "data_flow_1", ""),
                ("BLOCK_RESTORE", "backup_1", ""),
                ("DELETE_RECORD", "search_1", ""),
                ("REDACT_RECORD", "tombstone_1", ""),
                ("VERIFY_RECORD_ABSENT", "search_1", ""),
                ("VERIFY_RECORD_REDACTED", "tombstone_1", ""),
                ("VERIFY_RECORD_REDACTED", "support_1", ""),
                ("VERIFY_DATA_FLOW_DISABLED", "data_flow_1", ""),
                ("VERIFY_RESTORE_BLOCKED", "backup_1", ""),
            ],
        )


class Engine(unittest.TestCase):
    SAMPLES = load_requests(DATASET / "sample_deletion_requests.csv")
    POOL = {r.case_id: r for r in load_requests() + SAMPLES}

    def run_all(self, ctx=CTX, workers=1):
        targets = list(self.POOL.values())
        return {r.case_id: (r.status, serialize_steps(r.steps), r.notes)
                for r in run_rules(ctx, targets, targets, workers=workers)}  # fmt: skip

    def test_valid_and_deterministic(self):
        first, second = self.run_all(), self.run_all(workers=2)
        self.assertEqual(first, second)
        for cid, (_, _, notes) in first.items():
            self.assertFalse([n for n in notes if n.startswith("contract:")], cid)

    def test_matches_the_synthetic_ground_truth(self):
        # Expected answers come from the generator's scenarios, not from running this engine.
        got = self.run_all()
        for cid, (status, steps) in expected_answers().items():
            self.assertEqual(got[cid][:2], (status, serialize_steps(steps)), cid)

    def test_timing_link_for_a_row_with_no_identifier(self):
        # export_1103's only tie to case_02 is the timing of its workflow row.
        res = {r.case_id: r for r in run_rules(CTX, [self.POOL["case_02"]], list(self.POOL.values()))}
        prov = {d.resource_id: d.provenance for d in res["case_02"].decisions}
        self.assertTrue(all(p.startswith("timing:") for p in prov["export_1103"]))
        self.assertNotIn("search_1109", prov)  # the look-alike customer's document

    def test_view_hides_ownership_from_later_evidence(self):
        # tombstone_1702 is tied to user_08 by the catalog (2026-05-14T10:07:01Z) and the handover
        # note (evidence_23, captured 2026-05-15T15:00:00Z). Before either exists, nobody owns it.
        req = self.POOL["case_08"]
        before = CaseView(CTX, replace(req, audit_at="2026-05-14T00:00:00Z"))
        after = CaseView(CTX, req)
        self.assertEqual(before.owners("tombstone_1702", "2026-05-13T11:15:03Z"), set())
        self.assertEqual(after.owners("tombstone_1702", "2026-05-13T11:15:03Z"), {"user_08"})
