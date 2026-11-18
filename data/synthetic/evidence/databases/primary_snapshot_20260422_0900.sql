-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-04-22T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-04-22T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_1601','account_service','customer_profiles','{"resource_id":"account_1601","customer_reference":"user_07","full_name":"Gray Fixture","email":"gray.fixture@example.com","phone":"+1 202 555 0107","account_status":"ACTIVE"}','2026-04-22T09:00:00Z');
COMMIT;
