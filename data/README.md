# Data

`synthetic/` is a small, fully fictional dataset that `python -m deletion_audit.synthetic` writes. A
test regenerates it and checks that the committed copy matches byte for byte (the PNG pixel for pixel).

Nothing here comes from the challenge. The challenge dataset belongs to HackerRank and is not in this
repository. The synthetic files use the same column names and file formats, so the code runs unchanged.

- People, emails and phone numbers are invented: emails use the reserved `example.com`, `example.net`
  and `example.org` domains, and phones use the fictional `555-01xx` range.
- Each request follows one scenario. The scenario states what really happened to each record before any
  evidence is written, and the expected answers in `sample_output.csv` come from those scenarios, not
  from running the audit.

| File | What it holds |
|---|---|
| `sample_deletion_requests.csv` | 8 requests with expected answers (`sample_output.csv`) |
| `deletion_requests.csv` | 5 more requests, no answers: the default run |
| `user_identities.csv`, `user_audit_log.csv` | current identities and their change history (one email change) |
| `system_activity.csv` | 40 rows: deletion-workflow claims, connector syncs, data-catalog links |
| `deletion_rules.csv` | required action per data category (written for this repository) |
| `evidence_files.csv` + `evidence/` | 36 evidence files: SQLite snapshots, JSON exports, two log layouts, handover notes, one PDF, one PNG |
| `vision_fixtures/` | hand-written transcriptions of the PDF and PNG, used when no Claude transcription is cached |

| Request | Scenario | What it tests |
|---|---|---|
| case_01, case_101 | deletion really finished | absence proved by a full snapshot; anonymised analytics; retained audit record |
| case_02, case_102 | "complete", but copies remain | a read-replica copy; a search document; a look-alike customer who must not be linked; an export tied to the case only by the timing of its workflow row |
| case_03 | old email, live connector | request filed from a historical address; a connector the workflow "disabled" is still running and re-created a deleted record |
| case_04, case_103 | restore risk | profile gone, backup still restorable (seen in a JSON-lines log) |
| case_05, case_105 | not a deletion request | access request; marketing opt-out that uses the word "remove" |
| case_06 | time rule | a snapshot taken after `audit_at` shows the profile re-created; it must not change the answer |
| case_07 | PDF and screenshot only | an attachment visible only in a PDF; an exposed email visible only in a screenshot |
| case_08, case_104 | exposed identifiers | tombstone still shows the email (handover note); analytics never anonymised (key=value log); case_104 is in Spanish |
