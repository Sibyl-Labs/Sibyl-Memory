"""Pinned Ed25519 public keys that may sign a Sibyl Memory entitlement (SE1).

The client trusts an entitlement ONLY if its signature verifies under one of the
raw 32-byte public keys below. There is no runtime key fetch, ever: a key that
arrives over the network is not a pin. Up to two kids live here during a
rotation (spec account-gated 2.4).

kid = first 16 hex chars of sha256(raw 32-byte public key).

Production key pinned 2026-10-02 (kid cb6fda3561b7ba39). The private half is
held only by the Sibyl Labs api and never ships in this package. Rotation: ship
both kids here first, then sign with the new key, then retire the old one
(spec account-gated 2.4).

Release gate: python scripts/check_release_keys.py --expect-kids cb6fda3561b7ba39
(exit 0 required) and tests/smoke/venv-compat.sh with the same kid. The test
suites monkeypatch TRUSTED_KEYS with their own test key.

KEYS_ISSUED (date, release, kid):
  2026-09-27  0.9.0-dev  5dd0b6c80d97bae2  PLACEHOLDER, never valid in production (removed)
  2026-10-02  0.9.0      cb6fda3561b7ba39  PRODUCTION (active)
"""
from __future__ import annotations

import base64

TRUSTED_KEYS: dict[str, bytes] = {
    # Production entitlement signing key, generated 2026-10-02.
    "cb6fda3561b7ba39": base64.b64decode("2qgUJ1mPdSeuqd9hICQNAp+0uCKxGyFrq8C+rmSCp+A="),
}

# Kids that must never ship. The release check fails while any of these is
# still in TRUSTED_KEYS. Set to frozenset() when the production key lands.
PLACEHOLDER_KIDS: frozenset[str] = frozenset()
