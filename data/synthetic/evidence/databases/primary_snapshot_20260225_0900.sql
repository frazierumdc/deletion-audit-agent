-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-02-25T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-02-25T09:00:00Z');
INSERT INTO snapshot_scope VALUES('integration_service','2026-02-25T09:00:00Z');
INSERT INTO snapshot_scope VALUES('search_service','2026-02-25T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_1201','account_service','customer_profiles','{"resource_id":"account_1201","customer_reference":"user_03","full_name":"Casey Demo","email":"casey.demo@example.org","phone":"+1 202 555 0103","account_status":"ACTIVE"}','2026-02-25T09:00:00Z');
INSERT INTO customer_data_primary VALUES('data_flow_1202','integration_service','connector_subscriptions','{"resource_id":"data_flow_1202","customer_reference":"user_03","connector_name":"crm_contact_sync","enabled":1}','2026-02-25T09:00:00Z');
INSERT INTO customer_data_primary VALUES('search_1203','search_service','customer_search_documents','{"resource_id":"search_1203","customer_reference":"user_03","display_name":"Casey Demo","email":"casey.demo@example.org"}','2026-02-25T09:00:00Z');
COMMIT;
