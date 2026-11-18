"""Evidence checks: log layouts, record mapping, the loader over every file, and CaseView cutoffs."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from deletion_audit.context import AuditContext, CaseView
from deletion_audit.data import DATASET, EvidenceFile, load_requests
from deletion_audit.evidence.loader import load_evidence
from deletion_audit.evidence.logs import parse_log_line
from deletion_audit.evidence.model import lookup_observation, record_observation

CTX = AuditContext.load()


class LogLayouts(unittest.TestCase):
    """One line per supported layout (fictional values)."""

    def check(self, line, service, trace, **expect):
        ts, svc, tr, f = parse_log_line(line)
        self.assertEqual((ts, svc, tr), ("2026-07-28T05:51:18Z", service, trace))
        for k, v in expect.items():
            self.assertEqual(f.get(k), v, k)

    def test_key_value(self):
        self.check(
            "2026-07-28T05:51:18Z level=DEBUG service=search_service trace_id=tr-1 event=index_query_started "
            'match_key="blake.sample.jr@example.net" attempt=1',
            "search_service",
            "tr-1",
            event="index_query_started",
            match_key="blake.sample.jr@example.net",
        )

    def test_json(self):
        self.check(
            '2026-07-28T05:51:18Z {"service":"search_service","trace_id":"tr-1","event":"index_query_started",'
            '"match_key":"a@example.com","attempt":1}',
            "search_service",
            "tr-1",
            match_key="a@example.com",
            attempt="1",
        )

    def test_syslog(self):
        self.check(
            '2026-07-28T05:51:18Z <15>1 node-1 account_service tr-1 - - level=DEBUG event="profile_lookup_started"'
            ' lookup_key="devon.test@example.com"',
            "account_service",
            "tr-1",
            event="profile_lookup_started",
            lookup_key="devon.test@example.com",
        )

    def test_bracketed(self):
        self.check(
            '2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-1] profile_lookup_started: instance="a-5" '
            'lookup_key="finley.mock@example.org"',
            "account_service",
            "tr-1",
            event="profile_lookup_started",
            lookup_key="finley.mock@example.org",
        )

    def test_pipe(self):
        self.check(
            "2026-07-28T05:51:18Z | DEBUG | security_service | tr-1 | audit_record_read_completed | "
            'resource_id="audit_record_1003" rows="1" correlation_key="user_01"',
            "security_service",
            "tr-1",
            event="audit_record_read_completed",
            resource_id="audit_record_1003",
            correlation_key="user_01",
        )

    def test_cloud_runtime(self):
        self.check(
            '2026-07-28T05:51:18Z requestId=tr-1 level=DEBUG function=profile_service message="instance=\\"p-3\\" '
            'resource_id=\\"account_1001\\" event=\\"profile_lookup_completed\\" rows=\\"1\\" '
            'correlation_key=\\"avery.lane@example.com\\""',
            "profile_service",
            "tr-1",
            resource_id="account_1001",
            event="profile_lookup_completed",
            correlation_key="avery.lane@example.com",
        )

    def test_not_a_log_line(self):
        self.assertIsNone(parse_log_line("Sep 30 12:00:01 host something"))


class RecordMapping(unittest.TestCase):
    def test_upper_case_email_in_message_is_exposed(self):
        o = record_observation(
            "e1",
            "2026-01-01T00:00:00Z",
            None,
            "support_platform",
            {
                "resource_id": "support_1",
                "subject_reference": "user_5",
                "message_text": "send it to BOB@EXAMPLE.COM please",
            },
        )
        self.assertTrue(o.exposed)
        self.assertEqual(o.keys, {"e:bob@example.com", "u:user_5"})
        self.assertEqual(o.ts, "2026-01-01T00:00:00Z")

    def test_analytics_reference_counts_as_exposed(self):
        o = record_observation(
            "e1", "t", "t", None, {"resource_id": "analytics_9", "customer_reference": "user_9"}
        )
        self.assertEqual((o.system, o.exposed), ("analytics_platform", True))
        o = record_observation(
            "e1", "t", "t", None, {"resource_id": "analytics_9", "customer_reference": None}
        )
        self.assertFalse(o.exposed)

    def test_flags(self):
        o = record_observation(
            "e1", "t", "t", None, {"resource_id": "export_1", "download_available": "false"}
        )
        self.assertIs(o.present, False)
        o = record_observation("e1", "t", "t", None, {"resource_id": "data_flow_1", "enabled": 1})
        self.assertIs(o.flow_enabled, True)
        self.assertIsNone(record_observation("e1", "t", "t", None, {"name": "no id"}))

    def test_lookup_results(self):
        o = lookup_observation(
            "e1", "t", "t", None, "backup_7", "BACKUP_ARCHIVE", "active", "x@example.com", "handover"
        )
        self.assertEqual(
            (o.system, o.present, o.restore_allowed, o.flow_enabled), ("backup_service", True, True, None)
        )
        o = lookup_observation(
            "e1", "t", "t", None, "account_7", "USER_PROFILE", "NOT_FOUND", "x@example.com", "handover"
        )
        self.assertEqual((o.present, o.keys), (False, {"e:x@example.com"}))


class Loader(unittest.TestCase):
    def test_every_file_yields_observations_or_a_note(self):
        reports = CTX.store.reports
        self.assertEqual(len(reports), 36)
        for r in reports.values():
            self.assertNotIn("error", r.note, r.file_path)
            if r.fmt in ("png", "pdf"):  # mapped from the vision fixtures
                self.assertGreater(r.observations, 0, r.file_path)
            else:
                self.assertTrue(r.observations > 0 or r.note.startswith("no records"), r.file_path)

    def test_parser_error_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{not json", encoding="utf-8")
            store = load_evidence([EvidenceFile("evidence_x", str(bad), "test", "2026-01-01T00:00:00Z")])
        self.assertTrue(store.reports["evidence_x"].note.startswith("parser error: JSONDecodeError"))

    def test_deterministic(self):
        again = load_evidence(list(CTX.manifest.values()))
        self.assertEqual(again.observations, CTX.store.observations)
        self.assertEqual(again.coverage, CTX.store.coverage)

    def test_all_observations_have_system_and_capture_time(self):
        for o in CTX.store.observations:
            self.assertTrue(o.system and o.captured_at and o.ts, o)


class CaseViewCutoff(unittest.TestCase):
    def setUp(self):
        self.request = load_requests()[0]

    def test_nothing_captured_after_audit_at(self):
        for req in load_requests()[:50]:
            view = CaseView(CTX, req)
            for o in view.observations(window=False):
                self.assertLessEqual(o.captured_at, req.audit_at)
            for o in view.observations():
                self.assertTrue(req.requested_at <= o.ts <= req.audit_at)
            for c in view.coverage():
                self.assertLessEqual(c.captured_at, req.audit_at)
            for a in view.activity():
                self.assertTrue(req.requested_at <= a.ts <= req.audit_at)

    def test_moving_audit_at_hides_later_evidence(self):
        o = CTX.store.by_evidence["evidence_15"][0]  # snapshot captured after case_06's audit_at
        late = replace(self.request, requested_at="2026-04-13T00:00:00Z", audit_at="2026-04-30T00:00:00Z")
        early = replace(late, audit_at=o.captured_at[:10] + "T00:00:00Z")
        self.assertIn(o, CaseView(CTX, late).observations(resource_id=o.resource_id))
        self.assertNotIn(o, CaseView(CTX, early).observations(resource_id=o.resource_id, window=False))
        self.assertIsNone(CaseView(CTX, early).evidence_file("evidence_15"))

    def test_key_lookup(self):
        whole_year = replace(
            self.request, requested_at="2026-01-01T00:00:00Z", audit_at="2026-12-31T00:00:00Z"
        )
        found = {
            o.resource_id for o in CaseView(CTX, whole_year).observations(key="e:blake.sample@example.net")
        }
        self.assertIn("search_1102", found)
        self.assertNotIn("search_1109", found)  # blake.sample.jr is a look-alike

    def test_every_data_file_is_in_the_manifest(self):
        listed = {f.file_path for f in CTX.manifest.values()}
        on_disk = {
            p.relative_to(DATASET).as_posix() for p in (DATASET / "evidence").rglob("*") if p.is_file()
        }
        self.assertEqual(listed, on_disk)
