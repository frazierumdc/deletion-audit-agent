-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-04-17T12:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-04-17T12:00:00Z');
INSERT INTO snapshot_scope VALUES('analytics_platform','2026-04-17T12:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('analytics_1502','analytics_platform','customer_analytics_profiles','{"resource_id":"analytics_1502","customer_reference":null,"email":null,"audience_cohort":"home_and_garden"}','2026-04-17T12:00:00Z');
COMMIT;
