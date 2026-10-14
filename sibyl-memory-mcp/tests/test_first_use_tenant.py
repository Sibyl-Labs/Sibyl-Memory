"""Regression test for the v0.1.3 first-use write bug.

Bug (Discord beta report 2026-05-27 + a test-run note;
related to an external test report): with no credentials.json present,
`_build_client()` passed `tenant_id=creds.get("tenant_id")` == None EXPLICITLY,
overriding MemoryClient.local's DEFAULT_TENANT default. Every write then hit the
`entities.tenant_id NOT NULL` constraint and failed with an opaque
`SQLite error: IntegrityError`, while reads + tool discovery still worked -- so a
broken install looked healthy.

Fix: `tenant_id=creds.get("tenant_id") or DEFAULT_TENANT`. 0.3.0 (account gate):
the tenant fallback stays, but with no credentials every memory tool now refuses
with no_account, so the first test asserts the refusal instead of the write.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make both packages importable from a source checkout.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE.parent.parent.parent / "sibyl-memory-client" / "src"))


def test_build_client_without_credentials_builds_but_refuses(tmp_path, monkeypatch):
    """0.3.0 (account gate): the v0.1.3 IntegrityError stays fixed (the tenant
    still resolves to DEFAULT_TENANT and the client builds, so the server
    starts), but with no credentials the memory tools now refuse with
    no_account instead of writing into an anonymous store."""
    import pytest
    import sibyl_memory_mcp.server as server
    from sibyl_memory_client import DEFAULT_TENANT, EntitlementError

    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", tmp_path / "credentials.json")
    assert not (tmp_path / "credentials.json").exists()

    client = server._build_client()
    assert client is not None
    assert client.get_tenant() == DEFAULT_TENANT
    with pytest.raises(EntitlementError) as exc:
        client.set_entity("debug", "first-use", {"text": "pre-activation write probe"})
    assert exc.value.code == "no_account"


def test_build_client_honors_real_tenant_when_present(tmp_path, monkeypatch, test_signer):
    """When credentials DO carry a tenant_id, it is still used (no regression)."""
    import sibyl_memory_mcp.server as server
    from sibyl_memory_client.testing import write_credentials, write_entitlement

    real_tenant = "11111111-1111-1111-1111-111111111111"
    cred = write_credentials(tmp_path, tenant_id=real_tenant)
    write_entitlement(tmp_path, test_signer.mint(tenant_id=real_tenant))
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", cred)

    client = server._build_client()
    client.set_entity("debug", "scoped", {"text": "scoped write probe"})
    hits = client.search_entities("scoped")
    assert len(hits) >= 1
    assert hits[0]["tenant_id"] == real_tenant
