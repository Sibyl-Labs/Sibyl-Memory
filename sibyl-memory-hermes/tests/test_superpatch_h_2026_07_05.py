"""Unit H regression (super-patch 2026-07-05) — Contract T, hermes half.

Contract T: the tenant-resolution site in provider.py must use the ONE
canonical ladder shared by every surface (client / mcp / hermes / langgraph):

    tenant = creds.tenant_id  or  creds.account_id  or  DEFAULT_TENANT

with DEFAULT_TENANT reached ONLY when credentials are genuinely absent.

Prior behavior (hermes 0.3.12): `resolved_tenant = creds.tenant_id` — the
ladder SKIPPED account_id, so an activated user whose credentials.json carries
an account but a present-but-empty tenant_id resolved to the empty string
(and, once normalized, drifted toward the shared DEFAULT_TENANT constant)
instead of their OWN account. This test pins the full ladder.

Hermetic: each provider is built against a per-case tmp db + tmp credentials
file; no network, no shared state, no reliance on ~/.sibyl-memory.
"""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_client import DEFAULT_TENANT

from sibyl_memory_hermes.provider import SibylMemoryProvider


def _write_creds(dir_path: Path, payload: dict) -> Path:
    cred = dir_path / "credentials.json"
    cred.write_text(json.dumps(payload), encoding="utf-8")
    return cred


def _provider(tmp_path: Path, cred_path: Path) -> SibylMemoryProvider:
    return SibylMemoryProvider(
        db_path=str(tmp_path / "memory.db"),
        credentials_path=str(cred_path),
    )


# ----------------------------------------------------------------------
# Rung 1: tenant_id present -> tenant resolves to tenant_id
# ----------------------------------------------------------------------
def test_ladder_uses_tenant_id_when_present(tmp_path):
    cred = _write_creds(
        tmp_path, {"account_id": "acct-1", "tenant_id": "tenant-xyz"}
    )
    prov = _provider(tmp_path, cred)
    assert prov.tenant_id == "tenant-xyz"


# ----------------------------------------------------------------------
# Rung 2: tenant_id absent but account present -> falls to account_id
# ----------------------------------------------------------------------
def test_ladder_falls_to_account_when_tenant_key_absent(tmp_path):
    # tenant_id key genuinely MISSING (legacy schema-v1 single-key file).
    cred = _write_creds(tmp_path, {"account_id": "acct-2"})
    prov = _provider(tmp_path, cred)
    assert prov.tenant_id == "acct-2"
    assert prov.tenant_id != DEFAULT_TENANT


def test_ladder_falls_to_account_when_tenant_present_but_empty(tmp_path):
    # THE case Unit H repairs: tenant_id present-but-empty (the loader does not
    # mirror account over a present-empty tenant). Pre-fix this resolved to ""
    # (and drifted off the account); the ladder must land on the account.
    cred = _write_creds(
        tmp_path, {"account_id": "acct-3", "tenant_id": ""}
    )
    prov = _provider(tmp_path, cred)
    assert prov.tenant_id == "acct-3", (
        "empty tenant_id must fall through to account_id, not resolve to "
        "empty / DEFAULT_TENANT"
    )
    assert prov.tenant_id != DEFAULT_TENANT


# ----------------------------------------------------------------------
# Rung 3: credentials genuinely absent -> DEFAULT_TENANT
# ----------------------------------------------------------------------
def test_ladder_uses_default_only_when_creds_absent(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    assert not missing.exists()
    prov = _provider(tmp_path, missing)
    assert prov.tenant_id == DEFAULT_TENANT


# ----------------------------------------------------------------------
# Explicit override still wins over the credentials ladder.
# ----------------------------------------------------------------------
def test_explicit_tenant_id_overrides_ladder(tmp_path):
    cred = _write_creds(
        tmp_path, {"account_id": "acct-9", "tenant_id": "tenant-from-file"}
    )
    prov = SibylMemoryProvider(
        db_path=str(tmp_path / "memory.db"),
        tenant_id="explicit-tenant",
        credentials_path=str(cred),
    )
    assert prov.tenant_id == "explicit-tenant"
