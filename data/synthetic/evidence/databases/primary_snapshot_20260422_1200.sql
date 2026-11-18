-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-04-22T12:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-04-22T12:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_1501','account_service','customer_profiles','{"resource_id":"account_1501","customer_reference":"user_06","full_name":"Finley Mock","email":"finley.mock@example.org","phone":"+1 202 555 0106","account_status":"ACTIVE"}','2026-04-22T12:00:00Z');
COMMIT;
