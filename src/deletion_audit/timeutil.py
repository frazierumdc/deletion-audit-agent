"""Timestamps. Everything is compared as normalised UTC strings 'YYYY-MM-DDTHH:MM:SSZ'."""

import re
from datetime import UTC, datetime, timedelta

_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE
)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})$")


def ts_norm(value) -> str | None:
    """Normalise an ISO-8601 timestamp (or a bare date) to UTC 'YYYY-MM-DDTHH:MM:SSZ'.

    A missing offset is read as UTC. Returns None for anything else.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    m = _TS_RE.match(v)
    if not m:
        d = _DATE_RE.match(v)
        return f"{d.group(1)}T00:00:00Z" if d else None
    day, hh, mm, ss, tz = m.groups()
    out = f"{day}T{hh}:{mm}:{ss or '00'}Z"
    if tz and tz.upper() != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        out = (ts_dt(out) - sign * offset).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def ts_dt(value: str) -> datetime:
    """Parse a normalised timestamp into an aware UTC datetime."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def minutes_between(a: str, b: str) -> float:
    """a - b in minutes, for normalised timestamps."""
    return (ts_dt(a) - ts_dt(b)).total_seconds() / 60.0
