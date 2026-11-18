"""Manifest -> parser by file extension -> one EvidenceStore."""

from deletion_audit.data import load_manifest
from deletion_audit.evidence.handover import parse_handover_txt
from deletion_audit.evidence.images import parse_media
from deletion_audit.evidence.json_records import parse_json
from deletion_audit.evidence.logs import parse_log
from deletion_audit.evidence.model import EvidenceStore, FileReport
from deletion_audit.evidence.sql import parse_sql
from deletion_audit.usage import redact

PARSERS = {
    "sql": parse_sql,
    "json": parse_json,
    "txt": parse_handover_txt,
    "log": parse_log,
    "png": parse_media,
    "pdf": parse_media,
}


def load_evidence(manifest=None) -> EvidenceStore:
    """Parse every manifest file. A parser error is recorded on the file report, never raised."""
    store = EvidenceStore()
    for f in manifest if manifest is not None else load_manifest():
        report = FileReport(f.file_id, f.file_path, f.extension)
        store.reports[f.file_id] = report
        parser = PARSERS.get(f.extension)
        if parser is None:
            report.note = f"no parser for .{f.extension}"
            continue
        if not f.abs_path.is_file():
            report.note = "file missing"
            continue
        n_obs, n_cov = len(store.observations), len(store.coverage)
        try:
            note = parser(store, f.file_id, f.captured_at, f.abs_path)
        except Exception as exc:  # one bad file must not stop the run
            note = f"parser error: {type(exc).__name__}: {redact(exc)}"
        report.observations = len(store.observations) - n_obs
        report.coverage = len(store.coverage) - n_cov
        if not note and not report.observations:
            note = "no records (empty snapshot: coverage only)" if report.coverage else "no records"
        report.note = note or ""
    return store


def report_lines(store: EvidenceStore) -> list[str]:
    """Per-file parse counts, for --evidence-report."""
    lines = []
    for r in sorted(store.reports.values(), key=lambda r: (r.fmt, r.evidence_id)):
        lines.append(
            f"{r.evidence_id:12} {r.fmt:4} {r.observations:5} obs {r.coverage:3} cov  "
            f"{r.file_path.rsplit('/', 1)[-1]:52} {r.note}"
        )
    lines.append(
        f"total: {len(store.observations)} observations, {len(store.coverage)} coverage snapshots, "
        f"{len(store.by_resource)} resources, {len(store.reports)} files"
    )
    return lines
