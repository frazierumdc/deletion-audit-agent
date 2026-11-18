"""Hand-built evaluation cases on the synthetic data.

Each case rewrites fields of one sample request to probe one behaviour. When it runs, the rewritten
request replaces its base request in the request pool, so the two never compete for the same
workflow rows. The expected answer is either the base sample's answer (when the change must not
alter the outcome) or a hand-worked answer derived from the evidence.

Steps in an explicit expected answer are (phase, system_id, resource_id, operation, state, evidence_id).
"""

CASES = [
    {
        "id": "custom_01_current_email",
        "purpose": "Identity: case_03 is filed from a historical address. The same person writing from "
        "the current address must get the same answer.",
        "base_case": "case_03",
        "overrides": {"email": "casey.demo@example.org"},
        "expected": {"same_as_sample": "case_03"},
    },
    {
        "id": "custom_02_uppercase_email",
        "purpose": "Identity: email matching ignores case, so an upper-cased request email resolves to "
        "the same subject as case_02 (and still not to the look-alike customer).",
        "base_case": "case_02",
        "overrides": {"email": "BLAKE.SAMPLE@EXAMPLE.NET"},
        "expected": {"same_as_sample": "case_02"},
    },
    {
        "id": "custom_03_later_audit_cutoff",
        "purpose": "Time rule: audit_at decides which evidence counts. Moving case_06's cutoff past the "
        "later snapshot (evidence_15) exposes the re-created profile account_1501, so the case flips "
        "from COMPLETE to INCOMPLETE.",
        "base_case": "case_06",
        "overrides": {"audit_at": "2026-04-23T12:00:00Z"},
        "expected": {
            "deletion_status": "INCOMPLETE",
            "steps": [
                (
                    "FINDING",
                    "account_service",
                    "account_1501",
                    "REPORT_REMAINING_RECORD",
                    "PRESENT",
                    "evidence_15",
                ),
                ("REMEDIATION", "account_service", "account_1501", "DELETE_RECORD", "ABSENT", ""),
                ("VERIFICATION", "account_service", "account_1501", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
                (
                    "VERIFICATION",
                    "analytics_platform",
                    "analytics_1502",
                    "VERIFY_RECORD_ANONYMIZED",
                    "ANONYMIZED",
                    "",
                ),
            ],
        },  # fmt: skip
    },
    {
        "id": "custom_04_marketing_only",
        "purpose": "Intent: 'remove me from marketing lists only' uses deletion vocabulary but is an "
        "opt-out, so the case is NOT_APPLICABLE even though a deletion workflow ran.",
        "base_case": "case_01",
        "overrides": {
            "request_text": "Please remove me from marketing lists only. I still want receipts for "
            "this account."
        },
        "expected": {"deletion_status": "NOT_APPLICABLE", "steps": []},
    },
    {
        "id": "custom_05_german_request",
        "purpose": "Intent: a language no sample uses (German) must still be recognised as a deletion "
        "request; the answer is unchanged from case_01.",
        "base_case": "case_01",
        "overrides": {
            "request_text": "Bitte löschen Sie mein Konto und alle damit verbundenen personenbezogenen Daten."
        },
        "expected": {"same_as_sample": "case_01"},
    },
    {
        "id": "custom_06_access_request",
        "purpose": "Intent: an access request on an account with a live deletion workflow is not a "
        "deletion request, so case_04's findings must not be reported.",
        "base_case": "case_04",
        "overrides": {
            "request_text": "Can you tell me what personal information you hold about me? I am not "
            "asking for deletion."
        },
        "expected": {"deletion_status": "NOT_APPLICABLE", "steps": []},
    },
    {
        "id": "custom_07_media_only_evidence",
        "purpose": "Multimodal: in case_07 the leftover attachment is visible only in a PDF and the "
        "exposed message only in a screenshot. Reworded request, same answer. It passes only when "
        "the PDF and PNG have been transcribed (by Claude, or by the fixtures).",
        "base_case": "case_07",
        "overrides": {"request_text": "Remove my account and the document I sent to the help desk."},
        "expected": {"same_as_sample": "case_07"},
    },
]
