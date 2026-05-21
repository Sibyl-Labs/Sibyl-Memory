-- sibyl_plugin.* schema v6
--
-- Auth-redesign wave 1 schema migration. Three structural changes:
--
--   1. bearer_tokens table — long-lived API auth, separate from short-lived
--      activation sessions. One row per device. Revocable. Rotatable.
--      Multiple per account. Closes the session-token-doubles-as-bearer
--      latent issue surfaced in memory/research/auth-redesign-2026-05-19.md.
--
--   2. account_links table — N identity primitives per account. Today: email
--      and wallet. Tomorrow: Farcaster, ENS, GitHub OAuth, etc. without
--      schema churn. Replaces flat accounts.email + accounts.wallet as
--      canonical source; those columns remain as denormalized convenience
--      for hot-path reads. UNIQUE (link_type, value) enforces no double-
--      registration of the same identity across accounts.
--
--   3. sessions: bind_amount_micros + bind_bus_wallet + bind_from_wallet +
--      bind_tx_hash columns for the USDC-send mobile activation path.
--      Each active session gets a unique sub-cent suffix on $0.01 USDC
--      that the user sends from their wallet to BIND_BUS_WALLET as proof
--      of control. Backend watcher matches incoming Transfer by exact
--      amount → session → binds the from-wallet.
--
-- Plus backfills for continuity:
--   - account_links rows from existing accounts.email / accounts.wallet
--   - bearer_tokens rows from existing bound sessions (so already-activated
--     plugins keep working with their current credentials.json files)
--
-- Idempotent. Safe to re-run. ON CONFLICT DO NOTHING on all backfills.
--
-- Companion: memory/research/auth-redesign-2026-05-19.md (full design).

BEGIN;
SET search_path TO sibyl_plugin, public;

-- ─── bearer_tokens ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sibyl_plugin.bearer_tokens (
  bearer_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id         UUID NOT NULL REFERENCES sibyl_plugin.accounts(account_id) ON DELETE CASCADE,
  hashed_bearer      TEXT NOT NULL UNIQUE,   -- sha256(plaintext bearer), hex
  device_label       TEXT,                    -- e.g. "sibyl-cli/0.1.4 macos"
  install_method     TEXT,                    -- 'cli' | 'browser-activation' | 'legacy'
  os_family          TEXT,
  user_agent         TEXT,
  issued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS bearer_tokens_account
  ON sibyl_plugin.bearer_tokens (account_id) WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS bearer_tokens_lookup
  ON sibyl_plugin.bearer_tokens (hashed_bearer) WHERE revoked_at IS NULL;

-- ─── account_links ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sibyl_plugin.account_links (
  link_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id     UUID NOT NULL REFERENCES sibyl_plugin.accounts(account_id) ON DELETE CASCADE,
  link_type      TEXT NOT NULL,                -- 'email' | 'wallet' | future
  value          TEXT NOT NULL,                -- lowercased
  linked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  linked_via     TEXT NOT NULL,                -- 'siwe' | 'usdc-send' | 'email-pairing' | 'legacy-email' | 'legacy-wallet' | 'manual'
  is_primary     BOOLEAN NOT NULL DEFAULT FALSE,
  metadata       JSONB,
  UNIQUE (link_type, value)
);

CREATE INDEX IF NOT EXISTS account_links_account
  ON sibyl_plugin.account_links (account_id);

-- ─── sessions: USDC-send bind fields ────────────────────────────────

ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS bind_amount_micros BIGINT;
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS bind_bus_wallet    TEXT;
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS bind_from_wallet   TEXT;
ALTER TABLE sibyl_plugin.sessions ADD COLUMN IF NOT EXISTS bind_tx_hash       TEXT;

-- Unique partial index: no two active unbound sessions can claim the same amount.
CREATE UNIQUE INDEX IF NOT EXISTS sessions_active_bind_amount
  ON sibyl_plugin.sessions (bind_amount_micros)
  WHERE bind_amount_micros IS NOT NULL
    AND bound_to IS NULL;

-- ─── Backfill account_links from existing accounts ──────────────────

INSERT INTO sibyl_plugin.account_links (account_id, link_type, value, linked_via, is_primary, linked_at)
SELECT
  account_id,
  'email',
  LOWER(email),
  'legacy-email',
  TRUE,
  created_at
FROM sibyl_plugin.accounts
WHERE email IS NOT NULL
  AND deleted_at IS NULL
ON CONFLICT (link_type, value) DO NOTHING;

INSERT INTO sibyl_plugin.account_links (account_id, link_type, value, linked_via, is_primary, linked_at)
SELECT
  account_id,
  'wallet',
  LOWER(wallet),
  'legacy-wallet',
  TRUE,
  created_at
FROM sibyl_plugin.accounts
WHERE wallet IS NOT NULL
  AND deleted_at IS NULL
ON CONFLICT (link_type, value) DO NOTHING;

-- ─── Backfill bearer_tokens from existing bound sessions ────────────
-- Continuity guarantee: every already-activated credentials.json file
-- continues to authenticate. The session_token UUID is treated AS IF
-- it were the plaintext bearer for that device. Future activations
-- get a freshly-generated 256-bit bearer separate from the session UUID.

INSERT INTO sibyl_plugin.bearer_tokens (account_id, hashed_bearer, device_label, install_method, issued_at, last_seen_at)
SELECT
  s.bound_to,
  encode(sha256(s.session_token::text::bytea), 'hex'),
  'legacy-pre-v6',
  COALESCE(NULLIF(s.user_agent, ''), 'pre-v6-bind'),
  s.bound_at,
  s.bound_at
FROM sibyl_plugin.sessions s
WHERE s.bound_to IS NOT NULL
  AND s.bound_at IS NOT NULL
ON CONFLICT (hashed_bearer) DO NOTHING;

-- Uses Postgres built-in sha256() (since pg11, no extension required).
-- Earlier draft tried pgcrypto's digest() which isn't enabled on this Neon instance.

-- ─── Bump schema_version ────────────────────────────────────────────

INSERT INTO sibyl_plugin.schema_version (version, description)
VALUES (6, 'Auth-redesign wave 1: bearer_tokens (long-lived rotatable per-device API auth, separate from short-lived activation sessions), account_links (N identity primitives per account: email, wallet, future Farcaster/ENS/GitHub), sessions.bind_amount_micros + bind_bus_wallet + bind_from_wallet + bind_tx_hash for USDC-send mobile activation path. Backfill account_links from accounts.email/wallet (legacy-email / legacy-wallet linked_via). Backfill bearer_tokens from already-bound sessions for continuity (existing credentials.json files keep working). Companion: memory/research/auth-redesign-2026-05-19.md.')
ON CONFLICT (version) DO NOTHING;

COMMIT;
