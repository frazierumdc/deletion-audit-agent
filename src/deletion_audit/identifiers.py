"""Identifier normalisation and the field-name vocabularies used by the evidence parsers.

norm_identifier() maps a raw value to a typed token so that values from different
sources compare exactly:
    e:<lower-case email>   p:<digits only>   h:<lower-case sha256 hex>   u:user_NN
Matching is exact on purpose: look-alike addresses in the evidence are decoys.
"""

import hashlib
import re

NULLS = {"", "null", "none", "nil", "n/a", "[redacted]", "redacted"}

# Fields whose non-null value means a direct identifier is still exposed.
PII_FIELDS = frozenset(
    {
        "email",
        "phone",
        "email_hash",
        "full_name",
        "name",
        "display_name",
        "requester_email",
        "requester_name",
        "requester_phone",
        "owner_email",
        "owner_phone",
        "customer_email",
        "matched_value",
        "uploaded_by",
        "subject_selector",
        "lookup_value_exposed",
    }
)
# Fields that only reference the stable internal user id.
REF_FIELDS = frozenset({"user_id", "customer_reference", "subject_reference", "subject_ref"})
# Fields that say which identifier a lookup was keyed on (linking, not exposure).
LOOKUP_FIELDS = frozenset(
    {
        "lookup_key",
        "match_key",
        "correlation_key",
        "scan_key",
        "subject_key",
        "owner_key",
        "query",
        "lookup_value",
        "subject_selector",
    }
)

_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_USER_RE = re.compile(r"user_\d+")
_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")
_PHONE_RE = re.compile(r"\+?[\d\s().\-]{8,}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().strip('"').lower() in NULLS)


def parse_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().strip('"').lower()
        if s in ("1", "true", "yes", "allow", "allowed"):
            return True
        if s in ("0", "false", "no", "deny", "denied"):
            return False
    return None


def email_hash(email: str) -> str:
    """The dataset's email_hash: sha256 of the lower-cased address."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def norm_identifier(value) -> str | None:
    """Map a raw value to a typed identifier token, or None if it is not one."""
    if not isinstance(value, str) or is_null(value):
        return None
    v = value.strip().strip('"').strip()
    if _EMAIL_RE.fullmatch(v):
        return "e:" + v.lower()
    if _USER_RE.fullmatch(v):
        return "u:" + v
    if _HASH_RE.fullmatch(v):
        return "h:" + v.lower()
    if _PHONE_RE.fullmatch(v) and not _DATE_RE.fullmatch(v) and sum(c.isdigit() for c in v) >= 8:
        return "p:" + re.sub(r"\D", "", v)
    return None


def identifier_tokens(value) -> set[str]:
    """Tokens for one value; an email also yields its hash token, since evidence may hold either."""
    tok = norm_identifier(value)
    if tok is None:
        return set()
    if tok.startswith("e:"):
        return {tok, "h:" + email_hash(tok[2:])}
    return {tok}
