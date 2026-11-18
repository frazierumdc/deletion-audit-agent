-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-02-20T11:00:00Z
-- Topology: read_replica
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-02-20T11:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_read_replica VALUES('account_1101','account_service','customer_profiles','{"resource_id":"account_1101","customer_reference":"user_02","full_name":"Blake Sample","email":"blake.sample@example.net","phone":"+1 202 555 0102","account_status":"ACTIVE"}','2026-02-20T11:00:00Z');
COMMIT;
