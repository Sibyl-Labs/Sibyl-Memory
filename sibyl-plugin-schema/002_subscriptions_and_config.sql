-- sibyl_plugin.* schema v2
--
-- Adds three tables and one column required for paid subscriptions + SIWE
-- staker auth on top of the v1 activation flow:
--
--   config         — runtime-adjustable values (price, threshold, addresses,
--                    per-tier caps, feature flags). Operator can edit via
--                    /api/plugin/admin-config.
--   subscriptions  — one row per active or expired paid subscription. The
--                    authoritative record of who paid for what, when.
--   staker_cache   — caches the on-chain SIBYL balance check per account.
--                    Avoids hitting Base RPC on every plugin call. TTL is
--                    config-driven (staker_recache_seconds).
--
-- Plus: `sessions.revoked` column (so bound sessions can be killed without
-- waiting for expires_at) and `sessions.expires_at` extension on first API
-- use (handled in code, not schema).
--
-- All idempotent. Safe to re-run.

BEGIN;
SET search_path TO sibyl_plugin, public;

-- ============================================================================
-- 1. config: runtime-adjustable values, operator-tunable at any time
-- ============================================================================
CREATE TABLE IF NOT EXISTS sibyl_plugin.config (
  key            TEXT PRIMARY KEY,
  value          TEXT NOT NULL,
  value_type     TEXT NOT NULL CHECK (value_type IN ('number','string','boolean','address','json')),
  description    TEXT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by     TEXT
);

CREATE INDEX IF NOT EXISTS config_updated_at ON sibyl_plugin.config (updated_at DESC);

-- Seed defaults. ON CONFLICT DO NOTHING so re-running this migration never
-- clobbers operator-tuned values. To reset a value, DELETE the row + re-run.
INSERT INTO sibyl_plugin.config (key, value, value_type, description) VALUES
  -- ── Staker auth ─────────────────────────────────────────────────
  ('staker_threshold_sibyl',   '100000',                                       'number',  'Minimum SIBYL token holdings (whole tokens, 18 decimals applied in code) for free plugin access via SIWE staker auth. Operator-adjustable. Operator directive 2026-05-16: starting point 100k.'),
  ('staker_check_method',      'combined',                                     'string',  'Which balance counts toward threshold: "liquid" (wallet only), "staked" (stake contract only), or "combined" (liquid+staked, default — matches the existing /demo gate policy).'),
  ('staker_recache_seconds',   '600',                                          'number',  'How often to re-verify on-chain SIBYL balance. Default 10 min. Lower = fresher, higher = fewer RPC calls.'),
  ('staker_access_enabled',    'true',                                         'boolean', 'Toggle staker-based plugin access on/off without redeploying.'),
  ('staker_effective_tier',    'stake',                                        'string',  'sibyl_plugin.accounts.tier value assigned to wallets that pass the staker check.'),
  ('sibyl_token_address',      '0x797f214a2CD64a4963A91Fa21c8C55Ec3EBa4714',   'address', 'SIBYL ERC-20 token on Base. Used for staker balance checks. Adjustable if a future migration changes the contract.'),
  ('sibyl_stake_contract',     '0x6151aa0689576e8f8d218f4dc7f6a4ec1533d44d',   'address', 'SIBYL staking contract on Base. Returns userInfo(address) → (totalWeightedBalance, totalRawBalance, ...).'),
  ('chain_id',                 '8453',                                         'number',  'Base mainnet chain ID. Used for SIWE message validation and RPC routing.'),

  -- ── Paid subscriptions (x402) ──────────────────────────────────
  ('subscription_access_enabled', 'true',                                      'boolean', 'Toggle x402 paid plugin subscription path on/off.'),
  ('payment_recipient',           '0xe3e14118238b5693c854674f7c276136a2dd311f','address', 'USDC payment recipient address for x402 subscriptions. Matches BANKR_WALLET in _x402.js. Adjustable if the payment wallet changes.'),

  ('tier_monthly_enabled',    'true',  'boolean', 'Whether the monthly subscription tier is offered on /api/plugin/pricing.'),
  ('tier_monthly_name',       'sync',  'string',  'sibyl_plugin.accounts.tier value assigned when a monthly subscription is active.'),
  ('tier_monthly_price_usdc', '29',    'number',  'Monthly subscription price in USDC (whole units, x402 converts to 6-decimal wire format).'),
  ('tier_monthly_days',       '30',    'number',  'Monthly subscription duration in days.'),

  ('tier_quarterly_enabled',    'true',  'boolean', 'Whether the quarterly subscription tier is offered.'),
  ('tier_quarterly_name',       'sync',  'string',  'sibyl_plugin.accounts.tier value when a quarterly subscription is active.'),
  ('tier_quarterly_price_usdc', '79',    'number',  'Quarterly subscription price in USDC (~10% off monthly).'),
  ('tier_quarterly_days',       '90',    'number',  'Quarterly subscription duration in days.'),

  ('tier_annual_enabled',    'true',  'boolean', 'Whether the annual subscription tier is offered.'),
  ('tier_annual_name',       'sync',  'string',  'sibyl_plugin.accounts.tier value when an annual subscription is active.'),
  ('tier_annual_price_usdc', '290',   'number',  'Annual subscription price in USDC (~17% off monthly).'),
  ('tier_annual_days',       '365',   'number',  'Annual subscription duration in days.'),

  -- ── Tier → cap_bytes mapping (the Python client''s _capcheck.py uses this) ──
  ('cap_free_bytes',     '2097152', 'number', 'Local DB size cap for the "free" tier (bytes). Default 2 MB. Once a free user reaches this, writes get gated through /api/plugin/check-write.'),
  ('cap_sync_bytes',     'null',    'json',   'Cap for the "sync" tier. null = unlimited. JSON encoding so we can express null cleanly.'),
  ('cap_stake_bytes',    'null',    'json',   'Cap for staker-authenticated accounts. null = unlimited.'),
  ('cap_team_bytes',     'null',    'json',   'Cap for the "team" tier. null = unlimited.'),
  ('cap_lifetime_bytes', 'null',    'json',   'Cap for "lifetime" tier. null = unlimited.'),
  ('cap_enterprise_bytes','null',   'json',   'Cap for "enterprise" tier. null = unlimited.'),

  -- ── Misc ───────────────────────────────────────────────────────
  ('upgrade_url',           'https://sibyllabs.org/plugin/upgrade', 'string', 'URL returned to the plugin when a free user hits the cap. The page should explain subscription + staker paths.'),
  ('session_sliding_days',  '30',                                   'number', 'How many days to extend a bound session''s expires_at on each successful API call (sliding-window renewal). Plugins in active use stay authenticated indefinitely; dormant ones expire.')

ON CONFLICT (key) DO NOTHING;

-- ============================================================================
-- 2. subscriptions: paid subscription records (one row per purchase)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sibyl_plugin.subscriptions (
  subscription_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id        UUID NOT NULL REFERENCES sibyl_plugin.accounts(account_id) ON DELETE CASCADE,
  tier              TEXT NOT NULL,                            -- copy of tier name at purchase
  period_days       INT  NOT NULL,
  amount_usdc       NUMERIC(18,6) NOT NULL,
  starts_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','expired','refunded','revoked')),
  payment_source    TEXT NOT NULL                              -- 'x402-facilitator' | 'x402-direct-tx' | 'admin'
                       CHECK (payment_source IN ('x402-facilitator','x402-direct-tx','admin')),
  tx_hash           TEXT,
  facilitator_ref   TEXT,
  payer_wallet      TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata          JSONB
);

CREATE INDEX IF NOT EXISTS subs_account_active
  ON sibyl_plugin.subscriptions (account_id, expires_at DESC)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS subs_expires
  ON sibyl_plugin.subscriptions (expires_at)
  WHERE status = 'active';
CREATE INDEX IF NOT EXISTS subs_payer_wallet
  ON sibyl_plugin.subscriptions (payer_wallet);
CREATE UNIQUE INDEX IF NOT EXISTS subs_tx_hash_unique
  ON sibyl_plugin.subscriptions (tx_hash) WHERE tx_hash IS NOT NULL;

-- ============================================================================
-- 3. staker_cache: on-chain SIBYL balance cache per account
-- ============================================================================
CREATE TABLE IF NOT EXISTS sibyl_plugin.staker_cache (
  account_id          UUID PRIMARY KEY REFERENCES sibyl_plugin.accounts(account_id) ON DELETE CASCADE,
  wallet              TEXT NOT NULL,
  liquid_balance_wei  NUMERIC(78,0) NOT NULL DEFAULT 0,  -- uint256 fits in NUMERIC(78,0)
  staked_balance_wei  NUMERIC(78,0) NOT NULL DEFAULT 0,
  total_balance_wei   NUMERIC(78,0) NOT NULL DEFAULT 0,
  threshold_wei       NUMERIC(78,0) NOT NULL,
  check_method        TEXT NOT NULL,                     -- snapshot of staker_check_method at time of check
  qualified           BOOLEAN NOT NULL DEFAULT FALSE,
  checked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  next_recheck_at     TIMESTAMPTZ NOT NULL,
  last_error          TEXT                                -- non-null if last check failed (so we know to retry sooner)
);

CREATE INDEX IF NOT EXISTS staker_cache_recheck    ON sibyl_plugin.staker_cache (next_recheck_at);
CREATE INDEX IF NOT EXISTS staker_cache_qualified  ON sibyl_plugin.staker_cache (qualified, checked_at DESC);
CREATE INDEX IF NOT EXISTS staker_cache_wallet     ON sibyl_plugin.staker_cache (wallet);

-- ============================================================================
-- 4. sessions: add revoked + updated_at for sliding-renewal semantics
-- ============================================================================
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS revoked    BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS last_api_call_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS sessions_revoked ON sibyl_plugin.sessions (revoked);

-- ============================================================================
-- 5. accounts: add subscription_expires_at + staker_qualified denorm columns.
--    These mirror the source-of-truth tables (subscriptions, staker_cache)
--    so the hot-path access check can be a single account row read.
--    Kept in sync by access.js whenever it computes effective access.
-- ============================================================================
ALTER TABLE sibyl_plugin.accounts ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ;
ALTER TABLE sibyl_plugin.accounts ADD COLUMN IF NOT EXISTS staker_qualified        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sibyl_plugin.accounts ADD COLUMN IF NOT EXISTS staker_last_checked_at  TIMESTAMPTZ;

-- ============================================================================
-- Schema version bump
-- ============================================================================
INSERT INTO sibyl_plugin.schema_version (version, description)
VALUES (2, 'Add config (runtime-adjustable values), subscriptions (paid subscription records via x402), and staker_cache (on-chain SIBYL balance cache). Extend sessions with revoked + updated_at for sliding-renewal semantics. Extend accounts with subscription_expires_at + staker_qualified denorm columns. All values adjustable via /api/plugin/admin-config. 100k SIBYL is the default staker threshold per 2026-05-16 operator directive.')
ON CONFLICT (version) DO NOTHING;

COMMIT;
