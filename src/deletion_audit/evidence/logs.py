"""Service logs in six layouts: key=value, JSON lines, RFC5424 syslog, bracketed [LEVEL] [service] [trace],
pipe '| LEVEL | ...', and cloud-runtime lines with an escaped message="...".

Lines are grouped by trace id, because the lookup key is often on the "started" line and the
resource on the "completed" line.
"""

import json
import re
from collections import defaultdict

from deletion_audit.evidence.model import Observation, clip, lookup_observation
from deletion_audit.identifiers import LOOKUP_FIELDS, PII_FIELDS, is_null, norm_identifier, parse_bool
from deletion_audit.systems import system_for

KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S+)')
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
_SYSLOG_RE = re.compile(r"<\d+>1 \S+ (\S+) (\S+) - - (.*)$")
_BRACKET_RE = re.compile(r"\[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+): (.*)$")
_PIPE_RE = re.compile(r"\|?\s*(\w+) \| (\S+) \| (\S+) \| (\w+) \| (.*)$")
_MESSAGE_RE = re.compile(r'message="((?:[^"\\]|\\.)*)"')

LOG_SYSTEM_ALIASES = {"profile_service": "account_service", "support_replica": "support_platform"}
TRACE_KEY_FIELDS = LOOKUP_FIELDS | {
    "subject_ref",
    "subject_reference",
    "customer_reference",
    "customer_email",
    "owner_email",
    "matched_value",
}
# Events that say nothing about a resource's state.
SKIP_EVENTS = {
    "connection_acquired",
    "cache_lookup",
    "thumbnail_failed",
    "dependency_retry",
    "dependency_recovered",
}


def parse_log_line(line):
    """Return (ts, service, trace, fields) for one log line in any supported layout, or None."""
    m = _TS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    fields, service, trace = {}, None, None
    if rest.startswith("{"):
        try:
            fields = json.loads(rest)
        except json.JSONDecodeError:
            return None
        return (
            ts,
            fields.get("service"),
            fields.get("trace_id"),
            {k: str(v) if v is not None else None for k, v in fields.items()},
        )
    m_sys, m_app, m_ops = _SYSLOG_RE.match(rest), _BRACKET_RE.match(rest), _PIPE_RE.match(rest)
    if m_sys:
        service, trace, rest = m_sys.group(1), m_sys.group(2), m_sys.group(3)
    elif m_app:
        service, trace, fields["event"], rest = m_app.group(2), m_app.group(3), m_app.group(4), m_app.group(5)
    elif m_ops:
        service, trace, fields["event"], rest = m_ops.group(2), m_ops.group(3), m_ops.group(4), m_ops.group(5)
    m_msg = _MESSAGE_RE.search(rest)
    if m_msg:
        inner = m_msg.group(1).replace('\\"', '"')
        rest = rest[: m_msg.start()] + " " + inner
    for k, v in KV_RE.findall(rest):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        fields.setdefault(k, v)
    service = service or fields.get("service") or fields.get("function")
    trace = trace or fields.get("trace_id") or fields.get("requestId")
    return ts, service, trace, fields


def _event_state(event, f) -> dict | None:
    """State flags a log event establishes for its resource, or None if it establishes nothing."""
    rows, status = f.get("rows"), f.get("http_status")
    if event.endswith("_started") or event in SKIP_EVENTS:
        return None
    if event == "profile_lookup_completed":
        st = {
            "present": rows not in (None, "0") and status != "404",
            "category": "USER_PROFILE",
            "exposed": any(not is_null(f.get(k)) for k in ("email", "phone", "full_name")),
        }
    elif event == "index_query_completed":
        hits = f.get("total_hits")
        st = {
            "present": f.get("result") == "hit" or (hits is not None and hits != "0"),
            "category": "SEARCH_INDEX_ENTRY",
        }
    elif event in ("analytics_profile_read", "analytics_profile_read_completed", "analytics_identity_scan"):
        di = f.get("direct_identifiers")
        st = {
            "present": True,
            "category": "ANALYTICS_PROFILE",
            "exposed": di not in (None, "0")
            or any(
                not is_null(f.get(k))
                for k in ("email", "phone", "email_hash", "customer_reference", "matched_value")
            ),
        }
    elif event == "contact_lookup_completed":
        st = {"present": rows not in (None, "0"), "category": "SECONDARY_IDENTIFIER"}
    elif event == "restore_candidate_evaluated":
        st = {
            "present": True,
            "category": "BACKUP_ARCHIVE",
            "restore_allowed": parse_bool(f.get("eligible")) is True
            and parse_bool(f.get("deletion_block")) is not True,
        }
    elif event == "restore_authorization_completed":
        st = {
            "present": True,
            "category": "BACKUP_ARCHIVE",
            "restore_allowed": (f.get("decision") or "").lower() == "allow",
        }
    elif event == "restore_policy_read_completed":
        st = {
            "present": parse_bool(f.get("archive_present")) is not False,
            "category": "BACKUP_ARCHIVE",
            "restore_allowed": parse_bool(f.get("restore_allowed")),
        }
    elif event in ("export_download_check_completed", "export_metadata_read_completed"):
        st = {"present": parse_bool(f.get("available")) is not False, "category": "USER_EXPORT"}
    elif event in ("connector_config_loaded", "connector_heartbeat", "connector_config_read_completed"):
        st = {"present": True, "category": "DATA_FLOW", "flow_enabled": parse_bool(f.get("enabled"))}
    elif event == "audit_record_read_completed":
        st = {"present": rows != "0", "category": "SECURITY_AUDIT_LOG"}
    elif event == "attachment_pii_scan":
        st = {"present": True, "exposed": True, "category": "SUPPORT_ATTACHMENT"}
    elif event == "message_pii_scan":
        st = {"present": True, "exposed": True, "category": "SUPPORT_MESSAGE"}
    elif event == "message_read":
        st = {"present": True, "category": "SUPPORT_MESSAGE", "exposed": not is_null(f.get("message_text"))}
    elif event == "support_ticket_read_completed":
        st = {
            "present": True,
            "category": "SUPPORT_MESSAGE",
            "exposed": any(
                not is_null(f.get(k)) for k in ("requester_email", "requester_name", "requester_phone")
            ),
        }
    elif event == "contact_query_completed":
        st = {"present": rows not in (None, "0")}
    elif event == "deletion_marker_check":
        if parse_bool(f.get("marker_found")) is not False:
            return None
        st = {"present": True}
    else:
        return None
    if st.get("exposed") is None and st["present"]:
        st["exposed"] = any(not is_null(f.get(k)) for k in PII_FIELDS)
    return st


def parse_log(store, eid, captured, path) -> None:
    groups = defaultdict(list)
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parsed = parse_log_line(line)
        if not parsed:
            continue
        ts, service, trace, f = parsed
        if f.get("event") == "record_lookup" and f.get("resource_id"):
            store.add(
                lookup_observation(
                    eid,
                    captured,
                    ts,
                    f.get("system"),
                    f["resource_id"],
                    f.get("record_type"),
                    f.get("result"),
                    f.get("subject_reference"),
                    "record_lookup",
                    raw=line,
                )
            )
            continue
        groups[trace or f"{ts}-{n}"].append((ts, service, f, line))
    for lines in groups.values():
        keys = {tok for _, _, f, _ in lines for k in TRACE_KEY_FIELDS if (tok := norm_identifier(f.get(k)))}
        raw = clip("\n".join(line for *_, line in lines))
        for ts, service, f, _ in lines:
            rid = f.get("resource_id")
            if not rid or not system_for(rid):
                continue
            st = _event_state(f.get("event") or "", f)
            if st is None:
                continue
            system = LOG_SYSTEM_ALIASES.get(service, service)
            if system_for(rid) != system:
                system = system_for(rid)
            store.add(
                Observation(
                    eid,
                    captured,
                    ts,
                    system,
                    rid,
                    keys=frozenset(keys),
                    source=f.get("event") or "",
                    raw=raw,
                    **st,
                )
            )
