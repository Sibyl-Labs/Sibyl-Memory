-- 005_usage_log.sql
--
-- Per-request usage log for the plugin server-side endpoints. Privacy-first:
--   * No raw IPs stored. IP is hashed with sha256(ip + account_id) so the same
--     wallet/user across requests is linkable for debugging, but cross-account
--     correlation is impossible and the raw IP cannot be recovered.
--   * No user-agent string stored. Only categorized fields: ua_class (cli/sdk/
--     browser/mcp/unknown) and ua_os (macos/linux/windows/unknown). No version
--     fingerprint.
--   * No request bodies. The bytes_proposed / bytes_current fields capture
--     cap-gate context only (sizes, not content).
--   * Coarse outcome categories: ok / cap_exceeded / rate_limited / auth_failed
--     / token_expired / not_found / dependency_failed. Surfaces what went wrong
--     without leaking specifics.
--
-- Retention: 30 days. Pruned daily by an admin cron or manual DELETE.
--
-- Burden: 1 INSERT per /access /check-write /heartbeat /bind /email-bind call.
-- Single small row (~150 bytes); indexed on the queries the dashboard needs.

BEGIN;

CREATE TABLE IF NOT EXISTS sibyl_plugin.usage_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id      UUID REFERENCES sibyl_plugin.accounts(account_id) ON DELETE CASCADE,
  ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  endpoint        TEXT NOT NULL,        -- '/access', '/check-write', '/heartbeat', '/bind', '/email-bind'
  method          TEXT NOT NULL,        -- 'GET' or 'POST'
  status          INT NOT NULL,         -- HTTP status code returned
  duration_ms     INT,                  -- server-side processing time
  ip_hash         TEXT,                 -- sha256(raw_ip + account_id), 64 hex chars; null if no account context
  ua_class        TEXT,                 -- 'cli' / 'sdk' / 'mcp' / 'browser' / 'unknown'
  ua_os           TEXT,                 -- 'macos' / 'linux' / 'windows' / 'unknown'
  bytes_proposed  INT,                  -- check-write only: proposed_delta_bytes
  bytes_current   INT,                  -- check-write only: current_size_bytes
  outcome         TEXT,                 -- 'ok' / 'cap_exceeded' / 'auth_failed' / etc.

  CONSTRAINT usage_log_endpoint_len CHECK (length(endpoint) <= 100),
  CONSTRAINT usage_log_method_len   CHECK (length(method) <= 16),
  CONSTRAINT usage_log_ua_class_len CHECK (ua_class IS NULL OR length(ua_class) <= 32),
  CONSTRAINT usage_log_ua_os_len    CHECK (ua_os IS NULL OR length(ua_os) <= 32),
  CONSTRAINT usage_log_outcome_len  CHECK (outcome IS NULL OR length(outcome) <= 64),
  CONSTRAINT usage_log_iphash_len   CHECK (ip_hash IS NULL OR length(ip_hash) = 64)
);

-- Hot paths the dashboard queries:
--   1. Per-account rollup (drill-down)
CREATE INDEX IF NOT EXISTS usage_log_account_ts_idx
  ON sibyl_plugin.usage_log (account_id, ts DESC);

--   2. Time-window aggregates (overview)
CREATE INDEX IF NOT EXISTS usage_log_ts_idx
  ON sibyl_plugin.usage_log (ts DESC);

--   3. Endpoint distribution (overview)
CREATE INDEX IF NOT EXISTS usage_log_endpoint_ts_idx
  ON sibyl_plugin.usage_log (endpoint, ts DESC);

-- Schema version bump
INSERT INTO sibyl_plugin.schema_version (version, applied_at, notes)
VALUES (
  6,
  NOW(),
  'usage_log: per-request privacy-first server-side log. Hashed IP, categorized UA, coarse outcome. 30d retention.'
)
ON CONFLICT (version) DO NOTHING;

COMMIT;
