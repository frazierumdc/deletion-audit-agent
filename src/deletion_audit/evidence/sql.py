"""SQLite snapshot exports: primary, read, regional, reporting and failover replicas, and inventories."""

import json
import re
import sqlite3
from collections import defaultdict

from deletion_audit.evidence.model import Coverage, record_observation
from deletion_audit.systems import category_from_hints, system_for
from deletion_audit.timeutil import ts_norm

INVENTORY_SYSTEMS = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def _table_system(table, rec):
    if table.startswith("customer_profiles"):
        return "account_service"
    if table.startswith("search_documents"):
        return "search_service"
    return INVENTORY_SYSTEMS.get(table) or system_for(rec.get("resource_id") or "")


def parse_sql(store, eid, captured, path) -> None:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"Captured at:\s*(\S+)", text)
    snap_ts = (ts_norm(m.group(1)) if m else None) or captured
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(text)
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        covered, scope = defaultdict(set), set()
        if "snapshot_scope" in tables:
            scope = {r[0] for r in db.execute("SELECT origin_system FROM snapshot_scope")}
        for table in tables:
            if table == "snapshot_scope":
                continue
            cur = db.execute(f'SELECT * FROM "{table}"')
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row, strict=False))
                if "record_json" in rec:  # inventory row wrapping one record from any system
                    body = json.loads(rec["record_json"])
                    system = rec.get("origin_system")
                    collection = rec.get("collection_name", "")
                    ts = ts_norm(rec.get("updated_at")) or snap_ts
                    store.add(
                        record_observation(
                            eid,
                            captured,
                            ts,
                            system,
                            body,
                            category=category_from_hints(system, collection),
                            source=f"{table}.{collection}",
                        )
                    )
                    covered[system].add(body.get("resource_id"))
                    continue
                system = _table_system(table, rec)
                ts = ts_norm(rec.get("updated_at") or rec.get("indexed_at")) or snap_ts
                present = str(rec.get("account_status", "")).upper() != "DELETED"
                store.add(
                    record_observation(
                        eid,
                        captured,
                        ts,
                        system,
                        rec,
                        present=present,
                        category=category_from_hints(system, table),
                        source=table,
                    )
                )
                covered[system].add(rec.get("resource_id"))
        for system in sorted(set(covered) | scope, key=str):
            store.add_coverage(
                Coverage(eid, captured, snap_ts, system, frozenset(r for r in covered.get(system, ()) if r))
            )
    finally:
        db.close()
