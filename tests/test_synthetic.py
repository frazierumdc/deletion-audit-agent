"""The committed synthetic dataset is exactly what the generator writes, and it is fictional."""

import csv
import re
import tempfile
import unittest
from pathlib import Path

from deletion_audit.context import AuditContext
from deletion_audit.data import DATASET, load_requests
from deletion_audit.evaluation.leak_check import _check, _rows
from deletion_audit.synthetic import write


class Generator(unittest.TestCase):
    def test_committed_data_matches_the_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = write(Path(tmp) / "synthetic")
            committed = sorted(p.relative_to(DATASET).as_posix() for p in DATASET.rglob("*") if p.is_file())
            self.assertEqual(committed, names)
            for rel in names:
                fresh, saved = Path(tmp) / "synthetic" / rel, DATASET / rel
                if rel.endswith(".png"):  # compare pixels: PNG compression can differ by platform
                    from PIL import Image

                    with Image.open(fresh) as a, Image.open(saved) as b:
                        self.assertEqual(a.tobytes(), b.tobytes(), rel)
                else:
                    self.assertEqual(fresh.read_bytes(), saved.read_bytes(), rel)

    def test_only_reserved_domains_and_fictional_phones(self):
        with open(DATASET / "user_identities.csv", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.assertRegex(row["email"], r"@example\.(com|net|org)$")
                self.assertRegex(row["phone"], r"^\+1 202 555 01\d\d$")
        files = [p for p in DATASET.rglob("*") if p.is_file() and p.suffix not in (".png", ".pdf")]
        text = "\n".join(p.read_text(encoding="utf-8") for p in files)
        for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
            self.assertRegex(email.lower().rstrip("."), r"@example\.(com|net|org)$")


class TimeRule(unittest.TestCase):
    def test_no_answer_changes_when_later_data_is_removed(self):
        path = str(DATASET / "sample_deletion_requests.csv")
        targets = load_requests(path)
        pool = targets + load_requests()
        full = _rows(AuditContext.load(), targets, pool)
        for cutoff in sorted({r.audit_at for r in targets}):
            ids = {r.case_id for r in targets if r.audit_at == cutoff}
            _, rows = _check((cutoff, ids, path))
            for cid, row in rows.items():
                self.assertEqual(row, full[cid], f"{cid} at {cutoff}")
