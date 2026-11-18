-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-06-17T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-06-17T09:00:00Z');
INSERT INTO snapshot_scope VALUES('backup_service','2026-06-17T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_2001','account_service','customer_profiles','{"resource_id":"account_2001","customer_reference":"user_13","full_name":"Kai Prototype","email":"kai.prototype@example.org","phone":"+1 202 555 0113","account_status":"ACTIVE"}','2026-06-17T09:00:00Z');
INSERT INTO customer_data_primary VALUES('backup_2002','backup_service','backup_restore_entries','{"resource_id":"backup_2002","customer_reference":"user_13","archive_key":"archive/customer-data/backup_2002.tar.zst","restore_allowed":1}','2026-06-17T09:00:00Z');
COMMIT;
