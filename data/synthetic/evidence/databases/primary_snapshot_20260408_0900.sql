-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-04-08T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-04-08T09:00:00Z');
INSERT INTO snapshot_scope VALUES('analytics_platform','2026-04-08T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_1501','account_service','customer_profiles','{"resource_id":"account_1501","customer_reference":"user_06","full_name":"Finley Mock","email":"finley.mock@example.org","phone":"+1 202 555 0106","account_status":"ACTIVE"}','2026-04-08T09:00:00Z');
INSERT INTO customer_data_primary VALUES('analytics_1502','analytics_platform','customer_analytics_profiles','{"resource_id":"analytics_1502","customer_reference":"user_06","email":"finley.mock@example.org","audience_cohort":"home_and_garden"}','2026-04-08T09:00:00Z');
COMMIT;
