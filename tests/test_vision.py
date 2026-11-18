"""Vision-step checks, offline: JSON validation, the retry and cache with a fake SDK client, the
fixtures, and the code mapping from extractions to Observations."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from deletion_audit.agent.vision import (
    cache_path,
    extract_file,
    fixture_path,
    load_extraction,
    media_files,
    parse_json_object,
    validate_extraction,
)
from deletion_audit.data import load_manifest
from deletion_audit.evidence.images import MISSING_NOTE, parse_media, record_to_observation
from deletion_audit.evidence.model import EvidenceStore
from deletion_audit.usage import UsageLog

EMPTY_IDS = {"names": [], "emails": [], "phones": [], "user_ids": [], "hashes": []}


def ids(**kw):
    return {**EMPTY_IDS, **kw}


def rec(resource_id=None, labels=(), hints=(), part=False, **identifiers):
    return {
        "where": "table",
        "table": "t",
        "row": 1,
        "resource_id": resource_id,
        "labels": list(labels),
        "identifiers": ids(**identifiers),
        "part_of_page_subject": part,
        "fields": {},
        "status_text": [],
        "state_hints": list(hints),
        "timestamps": [],
    }


def extraction(records, events=(), subject=None, tables=()):
    return {
        "schema": "vision_extraction/v1",
        "artifact": {
            "kind": "screenshot",
            "product": None,
            "title": "t",
            "subtitle": None,
            "pages": 1,
            "as_of": None,
            "status_text": [],
            "filters": [],
            "page_subject": subject,
            "tables": list(tables),
        },
        "records": list(records),
        "events": list(events),
        "unreadable": [],
    }


class Validation(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(validate_extraction(extraction([rec("support_0420", emails=["a@example.com"])])), [])
        errs = validate_extraction(extraction([rec("TCK-0412"), rec("object_1", hints=["maybe"])]))
        self.assertTrue(any("not a resource id" in e for e in errs))
        self.assertTrue(any("'maybe'" in e for e in errs))
        self.assertTrue(validate_extraction({"schema": "x"}))
        self.assertEqual(validate_extraction(None), ["the reply is not a JSON object"])

    def test_fixtures_are_valid_extractions(self):
        media = media_files(load_manifest())
        self.assertEqual(len(media), 2)
        for f in media:
            self.assertTrue(fixture_path(f.abs_path).exists(), f.file_path)
            self.assertEqual(validate_extraction(load_extraction(f.abs_path)["extraction"]), [], f.file_path)

    def test_parse(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_object('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertIsNone(parse_json_object("sorry"))


class Mapping(unittest.TestCase):
    def test_printed_id_with_page_subject(self):
        subject = ids(names=["Gray Fixture"], emails=["gray.fixture@example.com"])
        o = record_to_observation(rec("support_1602", labels=["TCK-0412"], part=True), subject, "evidence_18",
                                  "2026-05-01T11:00:00Z")  # fmt: skip
        self.assertEqual((o.system, o.present, o.exposed), ("support_platform", True, True))
        self.assertIn("e:gray.fixture@example.com", o.keys)

    def test_catalog_label_lookup_waits_for_the_catalog_row(self):
        # The catalog links support_1602 to "delivery_photo_upload.pdf" at 2026-04-30T10:07:01Z.
        o = record_to_observation(rec(labels=["delivery_photo_upload.pdf"]), None, "evidence_x",
                                  "2026-04-30T08:00:00Z")  # fmt: skip
        self.assertEqual(o.resource_id, "support_1602")
        self.assertEqual(o.captured_at, "2026-04-30T10:07:01Z")  # visible once the catalog row exists
        self.assertIsNone(
            record_to_observation(rec(labels=["TCK-0000"]), None, "evidence_x", "2026-04-30T08:00:00Z")
        )

    def test_user_id_names_a_record_only_on_its_own_page(self):
        row = rec(labels=["ORD-1001"], user_ids=["user_08"])  # a register row that mentions user_08
        self.assertIsNone(record_to_observation(row, None, "evidence_x", "2026-05-29T08:00:00Z"))
        header = dict(rec(user_ids=["user_08"]), where="header")  # the reference entry itself
        self.assertEqual(record_to_observation(header, None, "evidence_x", "2026-05-29T08:00:00Z").resource_id,
                         "tombstone_1702")  # fmt: skip

    def test_states_from_hints(self):
        b = record_to_observation(rec("backup_1302"), None, "evidence_11", "2026-03-20T15:00:00Z")
        self.assertTrue(b.restore_allowed)  # no restore_blocked hint -> restorable
        b = record_to_observation(
            rec("backup_1302", hints=["restore_blocked"]), None, "e", "2026-03-20T15:00:00Z"
        )
        self.assertFalse(b.restore_allowed)
        f = record_to_observation(rec("data_flow_1202", hints=["active", "continuous_sync"]), None, "e", "t")
        self.assertTrue(f.flow_enabled)
        a = record_to_observation(rec("analytics_1703", user_ids=["user_08"]), None, "e", "t")
        self.assertTrue(a.exposed)  # analytics must carry no user reference
        t = record_to_observation(rec("tombstone_1702", user_ids=["user_08"]), None, "e", "t")
        self.assertFalse(t.exposed)  # a tombstone keeps only the stable user_id


class FakeSdk:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return NS(
            content=[NS(type="text", text=self.replies.pop(0))],
            stop_reason="end_turn",
            usage=NS(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=2000,
            ),
        )


class ExtractFile(unittest.TestCase):
    def test_retry_cache_and_usage(self):
        f = next(m for m in load_manifest() if m.extension == "pdf")
        good = json.dumps(extraction([rec("support_1602", emails=["x@example.com"])]))
        with tempfile.TemporaryDirectory() as tmp:
            cache, log = Path(tmp) / "vision", UsageLog(Path(tmp) / "u.jsonl")
            sdk = FakeSdk(["not json", good])
            entry = extract_file(f, log, sdk_client=sdk, cache_dir=cache)
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(len(sdk.calls), 2)
            first = sdk.calls[0]["messages"]
            self.assertEqual(len(first), 1)  # only the file block
            self.assertEqual([b["type"] for b in first[0]["content"]], ["document"])
            self.assertNotIn("support_attachment", json.dumps(first))  # no file name or path
            self.assertIn("problems", sdk.calls[1]["messages"][-1]["content"])
            rows = [json.loads(line) for line in log.path.read_text().splitlines()]
            self.assertEqual(
                [(r["purpose"], r["turn"], r["cache_write_tokens"]) for r in rows],
                [("vision", 1, 2000), ("vision", 2, 2000)],
            )
            self.assertTrue(cache_path(f.abs_path, cache_dir=cache).exists())
            again = extract_file(f, log, sdk_client=FakeSdk([]), cache_dir=cache)  # cache hit, no call
            self.assertEqual(again["extraction"], entry["extraction"])

    def test_missing_cache_is_reported(self):
        f = next(m for m in load_manifest() if m.extension == "png")
        from unittest import mock

        with mock.patch("deletion_audit.agent.vision.load_extraction", return_value=None):
            self.assertEqual(parse_media(EvidenceStore(), f.file_id, f.captured_at, f.abs_path), MISSING_NOTE)
