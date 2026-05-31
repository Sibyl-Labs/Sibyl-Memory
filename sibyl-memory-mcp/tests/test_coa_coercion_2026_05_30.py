"""Coerce-on-Adapter (CoA) regression tests for the MCP server — 2026-05-30.

The client (>= 0.4.5) enforces dict/list entity+state bodies. The MCP server
widens body to Any and coerces primitives to {"value": body} so an MCP client
(Claude Code / Codex / Cursor) sending a bare value never hits VALIDATION_ERROR.
Mirrors the hermes adapter's _coerce_body. Drives the real FastMCP call_tool path.
"""
import asyncio, tempfile, os
import pytest
import sibyl_memory_mcp.server as server
from sibyl_memory_client import MemoryClient


@pytest.fixture
def wired(monkeypatch):
    d = tempfile.mkdtemp()
    db = os.path.join(d, "m.db")
    shared = MemoryClient.local(db, tenant_id="qa")
    monkeypatch.setattr(server, "_open_client", lambda: shared)
    return server.build_server(), shared


def _invoke(mcp, tool, args):
    return asyncio.run(mcp.call_tool(tool, args))


@pytest.mark.parametrize("val", ["a fact", 42, 3.14, True, False, None])
def test_mcp_remember_coerces_primitive(wired, val):
    mcp, shared = wired
    _invoke(mcp, "memory_remember", {"category": "n", "name": "k", "body": val})
    assert shared.get_entity("n", "k")["body"] == {"value": val}


@pytest.mark.parametrize("val", ["s", 7, None, True])
def test_mcp_set_state_coerces_primitive(wired, val):
    mcp, shared = wired
    _invoke(mcp, "memory_set_state", {"key": "key", "body": val})
    assert shared.get_state("key")["body"] == {"value": val}


def test_mcp_dict_list_passthrough(wired):
    mcp, shared = wired
    _invoke(mcp, "memory_remember", {"category": "n", "name": "d", "body": {"k": "v"}})
    _invoke(mcp, "memory_set_state", {"key": "s", "body": [1, 2]})
    assert shared.get_entity("n", "d")["body"] == {"k": "v"}
    assert shared.get_state("s")["body"] == [1, 2]


def test_mcp_coerced_value_is_searchable(wired):
    mcp, shared = wired
    _invoke(mcp, "memory_remember", {"category": "n", "name": "f", "body": "the quick brown fox"})
    assert any(h.get("key") == "f" for h in shared.search("fox"))
