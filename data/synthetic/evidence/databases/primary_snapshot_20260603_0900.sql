-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-06-03T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-06-03T09:00:00Z');
INSERT INTO snapshot_scope VALUES('search_service','2026-06-03T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_1901','account_service','customer_profiles','{"resource_id":"account_1901","customer_reference":"user_12","full_name":"Jules Mockup","email":"jules.mockup@example.net","phone":"+1 202 555 0112","account_status":"ACTIVE"}','2026-06-03T09:00:00Z');
INSERT INTO customer_data_primary VALUES('search_1902','search_service','customer_search_documents','{"resource_id":"search_1902","customer_reference":"user_12","display_name":"Jules Mockup","email":"jules.mockup@example.net"}','2026-06-03T09:00:00Z');
INSERT INTO customer_data_primary VALUES('search_1909','search_service','customer_search_documents','{"resource_id":"search_1909","customer_reference":"user_19","display_name":"Jules Mockup","email":"jules.mockup2@example.net"}','2026-06-03T09:00:00Z');
COMMIT;
