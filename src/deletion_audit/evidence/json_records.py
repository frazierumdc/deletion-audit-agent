"""JSON evidence: snapshot bundles, service exports, record-lookup lists and plain record collections."""

import json
from dataclasses import replace

from deletion_audit.evidence.model import Coverage, clip, lookup_observation, record_observation
from deletion_audit.systems import category_from_hints, system_for
from deletion_audit.timeutil import ts_norm


def _snapshots(store, eid, captured, data):
    for snap in data["snapshots"]:
        sts = ts_norm(snap.get("captured_at")) or captured
        system = snap.get("data_store")
        collection = snap.get("collection", "")
        if collection == "operational_record_history":
            for r in snap.get("records", []):
                store.add(
                    lookup_observation(
                        eid,
                        captured,
                        ts_norm(r.get("recorded_at")) or sts,
                        r.get("system"),
                        r.get("resource_id"),
                        r.get("record_type"),
                        r.get("result"),
                        r.get("lookup_value") or r.get("subject_reference"),
                        "record_history",
                        raw=r,
                    )
                )
            continue
        ids = set()
        for r in snap.get("records", []):
            rid = r.get("resource_id") or ""
            sys_ = system if system_for(rid) in (system, None) else system_for(rid)
            store.add(
                record_observation(
                    eid,
                    captured,
                    sts,
                    sys_,
                    r,
                    category=category_from_hints(sys_, collection),
                    source=collection,
                )
            )
            ids.add(rid)
        store.add_coverage(Coverage(eid, captured, sts, system, frozenset(i for i in ids if i)))


def _service_exports(store, eid, captured, data):
    exported = ts_norm(data.get("exported_at")) or captured
    for system, colls in data["service_exports"].items():
        for coll, items in colls.items():
            for r in items:
                ts = ts_norm(r.get("observed_at")) or exported
                if coll == "profile_lookups":
                    prof = r.get("profile") or {}
                    rec = {"resource_id": r["resource_id"], **prof}
                    found = r.get("status_code") == 200 and r.get("profile") is not None
                    o = record_observation(
                        eid, captured, ts, system, rec, present=found, category="USER_PROFILE", source=coll
                    )
                elif coll == "index_queries":
                    docs = (r.get("hits") or {}).get("documents") or []
                    doc = next((d for d in docs if d.get("resource_id") == r["resource_id"]), None)
                    rec = {"resource_id": r["resource_id"], **(doc or {})}
                    o = record_observation(
                        eid,
                        captured,
                        ts,
                        system,
                        rec,
                        present=doc is not None,
                        category="SEARCH_INDEX_ENTRY",
                        extra_keys=[r.get("query")],
                        source=coll,
                    )
                else:
                    o = record_observation(
                        eid, captured, ts, system, r, category=category_from_hints(system, coll), source=coll
                    )
                store.add(o and replace(o, raw=clip(r)))  # keep the whole export item, not just the profile


def _record_lookups(store, eid, captured, data):
    for r in data["records"]:
        store.add(
            lookup_observation(
                eid,
                captured,
                ts_norm(r.get("recorded_at")) or captured,
                r.get("system"),
                r.get("resource_id"),
                r.get("record_type"),
                r.get("result"),
                r.get("subject_reference") or r.get("lookup_value"),
                "record_lookup",
                raw=r,
            )
        )


def _plain_records(store, eid, captured, data):
    cts = ts_norm(data.get("captured_at")) or captured
    coll = data.get("collection", "")
    ids, systems = set(), set()
    for r in data["records"]:
        system = system_for(r.get("resource_id") or "")
        systems.add(system)
        store.add(
            record_observation(
                eid, captured, cts, system, r, category=category_from_hints(system, coll), source=coll
            )
        )
        ids.add(r.get("resource_id"))
    systems.discard(None)
    if len(systems) == 1:  # a collection is a full snapshot only of the one system it holds
        store.add_coverage(Coverage(eid, captured, cts, systems.pop(), frozenset(i for i in ids if i)))


def parse_json(store, eid, captured, path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "snapshots" in data:
        _snapshots(store, eid, captured, data)
    elif "service_exports" in data:
        _service_exports(store, eid, captured, data)
    elif isinstance(data.get("records"), list) and data["records"] and "record_type" in data["records"][0]:
        _record_lookups(store, eid, captured, data)
    elif isinstance(data.get("records"), list):
        _plain_records(store, eid, captured, data)
