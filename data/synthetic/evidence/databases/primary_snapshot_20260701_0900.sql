-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-07-01T09:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-07-01T09:00:00Z');
INSERT INTO snapshot_scope VALUES('analytics_platform','2026-07-01T09:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('account_2101','account_service','customer_profiles','{"resource_id":"account_2101","customer_reference":"user_14","full_name":"Lane Sketch","email":"lane.sketch@example.com","phone":"+1 202 555 0114","account_status":"ACTIVE"}','2026-07-01T09:00:00Z');
INSERT INTO customer_data_primary VALUES('analytics_2103','analytics_platform','customer_analytics_profiles','{"resource_id":"analytics_2103","customer_reference":"user_14","email":"lane.sketch@example.com","audience_cohort":"home_and_garden"}','2026-07-01T09:00:00Z');
COMMIT;
