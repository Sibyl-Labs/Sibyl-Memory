-- sibyl_plugin.* schema v3
--
-- Adds the email-magic-link table for the wallet-free activation path.
-- A user enters their email on the activation page; server generates a
-- 6-digit code, stores it here with a 10-min TTL, sends via Resend. User
-- types the code back into the page; server validates + binds.
--
-- Replaces the placeholder "Coming soon" button in activate.html.
-- The flow is intentionally same-device (code typed back into the same
-- browser tab) to dodge cross-device session-token weirdness.
--
-- Idempotent. Safe to re-run.

BEGIN;
SET search_path TO sibyl_plugin, public;

CREATE TABLE IF NOT EXISTS sibyl_plugin.email_codes (
  session_token   UUID PRIMARY KEY REFERENCES sibyl_plugin.sessions(session_token) ON DELETE CASCADE,
  email           TEXT NOT NULL,
  code_hash       TEXT NOT NULL,                                 -- sha256(code + session_token), never store plaintext
  expires_at      TIMESTAMPTZ NOT NULL,
  attempts        INT NOT NULL DEFAULT 0,                        -- bump on each confirm; reject when >= 5
  consumed_at     TIMESTAMPTZ,
  send_count      INT NOT NULL DEFAULT 1,                        -- bump on each resend; reject when >= 3
  last_sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_codes_email      ON sibyl_plugin.email_codes (email);
CREATE INDEX IF NOT EXISTS email_codes_expires_at ON sibyl_plugin.email_codes (expires_at);

INSERT INTO sibyl_plugin.schema_version (version, description)
VALUES (3, 'Add email_codes table for the wallet-free magic-link activation path. 6-digit codes hashed (sha256 with session_token salt) before storage, 10-min TTL, 5-attempt cap, 3-resend cap. Send via Resend; sender sibyl@sibyllabs.org. Replaces "Coming soon" placeholder on activate.html.')
ON CONFLICT (version) DO NOTHING;

COMMIT;
