"""V1 panel fixes at the MCP boundary (graph read side): memory_neighbors
surfaces the queried node's existence, write tools report edges
{created, updated, unchanged}, and memory_search never fails on a broken links index."""
import asyncio
import os
import tempfile

import pytest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import MemoryClient

MAYA = {"tier": "entity", "category": "person", "key": "Maya Patel"}
REDWOOD = {"tier": "entity", "category": "company", "key": "Redwood"}


@pytest.fixture
def wired(monkeypatch):
    d = tempfile.mkdtemp()
    shared = MemoryClient.local(os.path.join(d, "m.db"), tenant_id="qa")
    shared.set_entity("person", "Maya Patel", {"role": "lead", "summary": "runs onboarding"})
    shared.set_entity("company", "Redwood", {"kind": "customer", "summary": "onboarding customer"})
    monkeypatch.setattr(server, "_open_client", lambda: shared)
    return server.build_server(), shared


def _invoke(mcp, tool, args):
    res = asyncio.run(mcp.call_tool(tool, args))
    return res[1] if isinstance(res, tuple) else res


def test_neighbors_distinguishes_typo_from_isolated_node(wired):
    mcp, c = wired
    real = _invoke(mcp, "memory_neighbors", {"node": MAYA})
    assert real["node"]["exists"] is True and "note" not in real
    typo = _invoke(mcp, "memory_neighbors", {"node": {**MAYA, "key": "maya patel"}})
    assert typo["edges"] == [] and typo["node"]["exists"] is False
    assert typo["node"]["archived"] is False and "case-sensitive" in typo["note"]
    c.link(MAYA, REDWOOD, "works_at")
    c.archive_entity("person", "Maya Patel")
    arch = _invoke(mcp, "memory_neighbors", {"node": MAYA})
    assert arch["node"]["archived"] is True and "archived" in arch["note"]
    assert arch["count"] == 1


def test_write_tools_report_created_and_updated(wired):
    mcp, _ = wired
    links = [{"to": REDWOOD, "relation": "works_at"}]
    first = _invoke(mcp, "memory_remember", {"category": "person", "name": "Sam",
                                             "body": {}, "links": links})
    assert first["links"] == {"created": 1, "updated": 0, "unchanged": 0}
    again = _invoke(mcp, "memory_remember", {"category": "person", "name": "Sam",
                                             "body": {}, "links": links})
    assert again["links"] == {"created": 0, "updated": 0, "unchanged": 1}
    ref = _invoke(mcp, "memory_set_reference", {"key": "doc/a", "body": "a",
                                                "links": [{"to": MAYA, "relation": "about"},
                                                          {"to": REDWOOD, "relation": "about"}]})
    assert ref["links"] == {"created": 2, "updated": 0, "unchanged": 0}
    ref = _invoke(mcp, "memory_set_reference", {"key": "doc/a", "body": "a",
                                                "links": [{"to": MAYA, "relation": "about"}]})
    assert ref["links"] == {"created": 0, "updated": 0, "unchanged": 1}
    plain = _invoke(mcp, "memory_remember", {"category": "person", "name": "Sam", "body": {}})
    assert "links" not in plain


@pytest.mark.parametrize("tiers", [None, "entity"])
def test_search_survives_dropped_links_index(wired, tiers):
    mcp, c = wired
    c.link(MAYA, REDWOOD, "works_at")
    with c.storage.connection() as conn:
        conn.execute("DROP INDEX memory_links_to")
    args = {"query": "redwood onboarding"}
    if tiers:
        args["tiers"] = tiers
    out = _invoke(mcp, "memory_search", args)
    assert out["ok"] is True and out["results"]
    assert all(h["links"] is None for h in out["results"])
    assert "links unavailable" in out["verdict"]["decoration_note"]
