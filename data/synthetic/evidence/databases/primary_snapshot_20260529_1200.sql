-- Synthetic point-in-time database snapshot (fictional data)
-- Captured at: 2026-05-29T12:00:00Z
-- Topology: primary
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES('account_service','2026-05-29T12:00:00Z');
INSERT INTO snapshot_scope VALUES('analytics_platform','2026-05-29T12:00:00Z');
INSERT INTO snapshot_scope VALUES('security_service','2026-05-29T12:00:00Z');
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_primary VALUES('analytics_1802','analytics_platform','customer_analytics_profiles','{"resource_id":"analytics_1802","customer_reference":null,"email":null,"audience_cohort":"home_and_garden"}','2026-05-29T12:00:00Z');
INSERT INTO customer_data_primary VALUES('audit_record_1803','security_service','security_audit_records','{"resource_id":"audit_record_1803","subject_reference":"user_11","event_code":"PASSWORD_RESET_COMPLETED","retention_until":"2033-12-31T23:59:59Z"}','2026-05-29T12:00:00Z');
COMMIT;
