"""Rule-based request-intent classifier: does the request text authorize deletion?

Deletion vocabulary covers the request languages in the data (English, Portuguese, Spanish,
French, Korean, Japanese, German). Explicit non-deletion statements override it, so "remove me
from marketing lists only" or "change my email, not asking for deletion" are not deletions.
"""

import re
from dataclasses import dataclass

DELETION_PATTERNS = (
    r"\bdelet",
    r"\berase",
    r"\bremov",
    r"\bclos(e|ed|ing)\b",
    r"\bgone\b",
    r"\bsuppression",
    r"\bsupprim",
    r"\belimin",
    r"\bexclus",
    r"\bexclu[ií]",
    r"삭제",
    r"削除",
    r"\bl[öo]sch",
    r"\bcan no longer (identify|send)",
    r"cannot identify me",
)
# Explicit statements that the person wants something other than deletion.
NON_DELETION_PATTERNS = (
    r"not asking for deletion",
    r"don'?t delete",
    r"do not (delete|close)",
    r"without removing",
    r"marketing lists only",
    r"leave (the|my) (account|profile)",
    r"keep the account",
    r"continue using the service",
    r"i still (want|need|use)",
    r"stop the promotional",
    r"out of date",
    r"misspelled",
    r"copy of (the information|last year)",
    r"portable format",
    r"what personal information you hold",
    r"change the email address",
    r"resetting the login",
)


@dataclass(frozen=True)
class Intent:
    authorized: bool
    reason: str  # the pattern that decided it, for the trace


def rule_classify(text: str) -> Intent:
    t = (text or "").lower()
    for p in NON_DELETION_PATTERNS:
        if re.search(p, t):
            return Intent(False, f"non-deletion: {p}")
    for p in DELETION_PATTERNS:
        if re.search(p, t):
            return Intent(True, f"deletion: {p}")
    return Intent(False, "no deletion wording")
