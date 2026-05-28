"""Regression test for the v0.1.3 first-use write bug.

Bug (sylvain1550 Discord report 2026-05-27 + QA note run-2026-05-28-mcp-run05;
related to KAPPA's coordination thread): with no credentials.json present,
`_build_client()` passed `tenant_id=creds.get("tenant_id")` == None EXPLICITLY,
overriding MemoryClient.local's DEFAULT_TENANT default. Every write then hit the
`entities.tenant_id NOT NULL` constraint and failed with an opaque
`SQLite error: IntegrityError`, while reads + tool discovery still worked -- so a
broken install looked healthy.

Fix: `tenant_id=creds.get("tenant_id") or DEFAULT_TENANT`. This test fails on the
pre-fix code (IntegrityError) and passes after.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make both packages importable from a source checkout.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent.parent.parent / "sibyl-memory-client" / "src"))


def test_build_client_writes_succeed_without_credentials(tmp_path, monkeypatch):
    import sibyl_memory_mcp.server as server
    from sibyl_memory_client import DEFAULT_TENANT

    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", tmp_path / "credentials.json")
    # credentials.json deliberately absent -> pre-activation free local mode.
    assert not (tmp_path / "credentials.json").exists()

    client = server._build_client()
    assert client is not None

    # THE regression: this write raised StorageError(IntegrityError) before the fix.
    client.set_entity("debug", "first-use", {"text": "pre-activation write probe"})

    # And it must be retrievable, proving the row actually landed under a tenant.
    hits = client.search_entities("probe")
    assert len(hits) >= 1
    assert hits[0]["tenant_id"] == DEFAULT_TENANT


def test_build_client_honors_real_tenant_when_present(tmp_path, monkeypatch):
    """When credentials DO carry a tenant_id, it is still used (no regression)."""
    import json
    import sibyl_memory_mcp.server as server

    cred = tmp_path / "credentials.json"
    real_tenant = "11111111-1111-1111-1111-111111111111"
    cred.write_text(json.dumps({"tenant_id": real_tenant, "account_id": "acct", "tier": "free"}))
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", cred)

    client = server._build_client()
    client.set_entity("debug", "scoped", {"text": "scoped write probe"})
    hits = client.search_entities("scoped")
    assert len(hits) >= 1
    assert hits[0]["tenant_id"] == real_tenant
