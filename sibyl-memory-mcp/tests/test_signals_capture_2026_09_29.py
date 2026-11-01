"""R2 skill signals on the MCP server (spec plugin-paid-tier-r2 1.2, 1.6):
every memory tool the server handles is one tool event (its name and a 12-hex
argument-name fingerprint), on the success and the error path; the free tier
records nothing; no argument value, no query, no entity name reaches
signals.json; no network.

On b591aa1 these fail: the server had no ``_recorded`` wrapper and
``sibyl_memory_client.lifecycle.signals`` did not exist (ImportError).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.lifecycle import notices as N
from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.testing import write_entitlement

CANARY_VALUE = "CANARY-VALUE-7a1f"
CANARY_ENTITY = "Canary Person 3e5d"
TOOLS = ("memory_remember", "memory_recall", "memory_search", "memory_list", "memory_set_state",
         "memory_get_state", "memory_record_event", "memory_forget")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    client_mod._NOTICE_SHOWN.clear()
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    n = N.ProcessNotices("mcp")
    n.run_fn = lambda *a, **k: None
    n.learn_fn = lambda *a, **k: None
    monkeypatch.setattr(server, "_LIFECYCLE", n)
    monkeypatch.setattr(server, "_HOST_CACHE", {})
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    yield
    with SG._registry_lock:
        for b in SG._BUFFERS.values():
            b.discard()
        SG._BUFFERS.clear()


@pytest.fixture
def pro(entitled_test_home, test_signer):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    return entitled_test_home


def _handler():
    mcp = server.build_server()
    return mcp._mcp_server.request_handlers[CallToolRequest]


def _call(handler, name, args):
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": name, "arguments": args})))
    return res.root.isError


def _calls(handler):
    errs = [
        _call(handler, "memory_remember", {"category": "people", "name": CANARY_ENTITY,
                                           "body": {"note": CANARY_VALUE}}),
        _call(handler, "memory_recall", {"category": "people", "name": CANARY_ENTITY}),
        _call(handler, "memory_search", {"query": f"about {CANARY_VALUE}"}),
        _call(handler, "memory_list", {"category": "people"}),
        _call(handler, "memory_set_state", {"key": "focus", "body": {"v": CANARY_VALUE}}),
        _call(handler, "memory_get_state", {"key": "focus"}),
        _call(handler, "memory_record_event", {"kind": "decision", "body": {"why": CANARY_VALUE}}),
        _call(handler, "memory_forget", {"category": "people", "name": CANARY_ENTITY}),
    ]
    return errs


def _home_text() -> bytes:
    out = b""
    for p in (Path.home() / ".sibyl-memory").rglob("*"):
        if p.is_file() and p.suffix != ".db" and "-wal" not in p.name and "-shm" not in p.name:
            out += p.read_bytes()
    return out


def test_every_tool_records_its_name_and_a_12_hex_shape_on_pro(pro):
    h = _handler()
    assert _calls(h) == [False] * 8
    buf = SG.recorder(Path.home())
    names = [t[1] for t in buf._tools]
    assert names == list(TOOLS)
    assert all(len(t[2]) == 12 for t in buf._tools)
    assert {t[0] for t in buf._tools} == {server._LIFECYCLE.session_key[:8]}
    SG.flush_all()
    sig = json.loads(SG.path_of(Path.home()).read_bytes())
    seen = {t for e in sig["ngrams"].values() for t in e["tools"]}
    assert seen == set(TOOLS)
    # the entity writes (remember, forget) are there as hashes
    assert len(sig["entity_writes"]) == 1
    blob = _home_text()
    for canary in (CANARY_VALUE, CANARY_ENTITY, "about ", '"people"', '"focus"'):
        assert canary.encode() not in blob, canary


def test_the_error_path_records_too(pro):
    h = _handler()
    assert _call(h, "memory_recall", {"category": "people", "name": "nobody"}) is True
    assert _call(h, "memory_get_state", {"key": "absent"}) is False     # NOT_FOUND payload, not an error
    names = [t[1] for t in SG.recorder(Path.home())._tools]
    assert names == ["memory_recall", "memory_get_state"]


def test_free_tier_records_nothing(entitled_test_home):
    h = _handler()
    _calls(h)
    assert SG.recorder(Path.home()).pending() == 0
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()


def test_signed_out_records_nothing(entitled_test_home):
    (entitled_test_home / "credentials.json").unlink()
    (entitled_test_home / "entitlement.json").unlink()
    h = _handler()
    assert _call(h, "memory_list", {}) is True
    assert SG.recorder(Path.home()).pending() == 0


def test_recording_off_records_nothing(pro):
    SG.set_enabled(Path.home(), False)
    h = _handler()
    _calls(h)
    assert SG.recorder(Path.home()).pending() == 0
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()


def test_tool_signatures_seen_by_the_host_are_unchanged():
    mcp = server.build_server()
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    # 0.10.0: the graph line adds seven tools and two search parameters; the
    # wrapper changes none of them (full schemas: test_recall_hubs_2026_10_28).
    assert set(TOOLS) <= set(tools) and len(tools) == 15
    props = tools["memory_search"].inputSchema["properties"]
    assert set(props) == {"query", "limit", "tiers", "scope", "with_links"}
    assert tools["memory_remember"].inputSchema["required"] == ["category", "name", "body"]


def test_claude_code_with_the_stop_hook_records_nothing_here(pro, monkeypatch):
    """R2F-2: the Stop hook's transcript slice is the record of truth in Claude
    Code; the MCP path must not record the same eight calls a second time."""
    monkeypatch.setattr(server, "_HOST_CACHE", {"host": "claude-code", "stop_hook": True})
    h = _handler()
    assert _calls(h) == [False] * 8
    buf = SG.recorder(Path.home())
    assert buf._tools == [], "no tool event from the MCP path"
    # pending() also counts the client library's entity writes (remember and
    # forget: 2). Those are not tool events and the transcript slice never
    # records them, so they stay: they are not a duplicate.
    assert len(buf._writes) == 2
    assert buf.pending() == 2


@pytest.mark.parametrize("cache", [
    {"host": "claude-code", "stop_hook": False},     # Claude Code without the hook
    {"host": "cursor", "stop_hook": True},           # another host (the hook is Claude Code only)
])
def test_hosts_without_the_hook_still_record(pro, monkeypatch, cache):
    monkeypatch.setattr(server, "_HOST_CACHE", dict(cache))
    h = _handler()
    assert _calls(h) == [False] * 8
    assert [t[1] for t in SG.recorder(Path.home())._tools] == list(TOOLS)
