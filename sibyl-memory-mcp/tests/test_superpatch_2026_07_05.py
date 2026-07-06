"""Regression tests for the 2026-07-05 super-patch (Unit M).

Covers the recovered/confirmed audit findings landed in server.py:

  R26  MemoryClient cache rebuild must close the OLD client's storage first,
       or its registered per-thread SQLite connections leak on every
       credentials.json mtime change (post-init / post-logout).
  R30  _build_client must create ~/.sibyl-memory at 0o700 (and tighten a
       pre-existing looser dir) so a first-touch by the MCP server never
       leaves the memory dir world-readable.
  R31  memory_search unknown-tiers must raise the SDK's ValidationError (not a
       builtin ValueError) so the error envelope carries code=VALIDATION_ERROR;
       and _err() gives ANY unmapped exception a `code` (belt-and-suspenders).
  Contract T  tenant resolution ladder: tenant_id -> account_id -> DEFAULT_TENANT.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient


# ----------------------------------------------------------------------
# R31 — error envelope always carries a `code`
# ----------------------------------------------------------------------

def _invoke_expect_toolerror(mcp, tool, args):
    """Run a tool via the real FastMCP path; return the JSON payload embedded in
    the raised ToolError message (the audited error-envelope contract)."""
    from mcp.server.fastmcp.exceptions import ToolError

    async def go():
        return await mcp.call_tool(tool, args)

    with pytest.raises(ToolError) as ei:
        asyncio.run(go())
    text = str(ei.value)
    # Message is "Error executing tool <name>: {json}"; parse from the first brace.
    return json.loads(text[text.index("{"):])


def test_r31_unknown_tiers_envelope_has_code(monkeypatch):
    d = tempfile.mkdtemp()
    shared = MemoryClient.local(os.path.join(d, "m.db"), tenant_id="qa")
    monkeypatch.setattr(server, "_open_client", lambda: shared)
    mcp = server.build_server()

    payload = _invoke_expect_toolerror(
        mcp, "memory_search", {"query": "xyzzy", "tiers": "bogus"}
    )
    # The regression: pre-fix this raised a builtin ValueError and the envelope
    # had NO `code`. Now it is a ValidationError -> VALIDATION_ERROR.
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["error"] == "ValidationError"
    assert "bogus" in payload["message"]


def test_r31_err_else_branch_always_sets_code(monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    # An exception that is NOT in the typed isinstance chain must still get a
    # `code` from the belt-and-suspenders else-branch.
    with pytest.raises(ToolError) as ei:
        server._err(RuntimeError("something unexpected"))
    payload = json.loads(str(ei.value))
    assert payload["code"] == "ERROR"
    assert payload["error"] == "RuntimeError"


# ----------------------------------------------------------------------
# R30 — memory dir created / tightened to 0o700
# ----------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_r30_build_client_creates_dir_0o700(tmp_path, monkeypatch):
    db = tmp_path / "nested" / "memory.db"
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", tmp_path / "credentials.json")
    assert not db.parent.exists()

    server._build_client()

    mode = stat.S_IMODE(os.stat(db.parent).st_mode)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")
def test_r30_build_client_tightens_existing_dir(tmp_path, monkeypatch):
    parent = tmp_path / "pre"
    parent.mkdir()
    os.chmod(parent, 0o755)  # pre-existing world-readable dir
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", parent / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", tmp_path / "credentials.json")

    server._build_client()

    mode = stat.S_IMODE(os.stat(parent).st_mode)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


# ----------------------------------------------------------------------
# R26 — old client's storage closed on cache rebuild
# ----------------------------------------------------------------------

def test_r26_old_storage_closed_on_rebuild(monkeypatch, tmp_path):
    class FakeStorage:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeClient:
        def __init__(self) -> None:
            self.storage = FakeStorage()

    built: list[FakeClient] = []

    def fake_build() -> FakeClient:
        c = FakeClient()
        built.append(c)
        return c

    monkeypatch.setattr(server, "_build_client", fake_build)
    # Drive rebuilds by changing the credentials mtime between the two opens.
    mtimes = iter([100.0, 200.0])
    monkeypatch.setattr(server, "_credentials_mtime", lambda: next(mtimes))
    # Keep the exists() input stable (points at an absent file).
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", tmp_path / "absent.json")
    # Start from a clean cache (auto-restored by monkeypatch.setitem).
    monkeypatch.setitem(server._client_cache, "client", None)
    monkeypatch.setitem(server._client_cache, "creds_mtime", None)
    monkeypatch.setitem(server._client_cache, "creds_path_exists", False)

    first = server._open_client()   # builds built[0]
    second = server._open_client()  # mtime changed -> rebuild built[1], close built[0]

    assert first is built[0]
    assert second is built[1]
    assert built[0].storage.closed is True, "old storage was NOT closed on rebuild (R26 leak)"
    assert built[1].storage.closed is False, "the live client must stay open"


# ----------------------------------------------------------------------
# Contract T (mcp half) — tenant resolution ladder
# ----------------------------------------------------------------------

def test_contract_t_falls_back_to_account_id(tmp_path, monkeypatch):
    """No tenant_id in creds but an account_id present -> tenant = account_id
    (not DEFAULT_TENANT)."""
    acct = "22222222-2222-2222-2222-222222222222"
    cred = tmp_path / "credentials.json"
    cred.write_text(json.dumps({"account_id": acct, "tier": "free"}))
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", cred)

    client = server._build_client()
    client.set_entity("debug", "scoped", {"text": "account-id fallback probe"})
    hits = client.search_entities("probe")
    assert len(hits) >= 1
    assert hits[0]["tenant_id"] == acct
    assert hits[0]["tenant_id"] != DEFAULT_TENANT
