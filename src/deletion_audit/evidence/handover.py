"""Plain-text handover: pipe-separated 'ts | system | resource | type | result | key' lines."""

from deletion_audit.evidence.model import lookup_observation
from deletion_audit.systems import system_for
from deletion_audit.timeutil import ts_norm


def parse_handover_txt(store, eid, captured, path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.strip().lstrip("-").split("|")]
        if len(parts) == 6 and system_for(parts[2]):
            ts, system, rid, rtype, result, key = parts
            store.add(
                lookup_observation(
                    eid, captured, ts_norm(ts), system, rid, rtype, result, key, "handover", raw=line.strip()
                )
            )
