"""memory_recall hubs / siblings (0.10.0, SIBYL_MEMORY_RECALL_HUBS default on).

An entity that hub entities link to with `source_of` comes back with `hubs`
and `siblings` (and `siblings_omitted` when a hub was left out); any other
entity returns the pre-0.10 shape. "0" turns the keys off. A store error in
the neighborhood read never fails the recall: the entity comes back with a
`neighborhood_note`. The host-visible tool schemas equal the graph-line merge
(fixture), and the experimental tool-text constants are not shipped.
"""
import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import MemoryClient, entity_ref

SCHEMAS_S2 = Path(__file__).resolve().parent / "fixtures" / "tool_schemas_s2_2026_10_28.json"


@pytest.fixture
def wired(monkeypatch):
    for v in ("SIBYL_MEMORY_RECALL_HUBS", "SIBYL_MEMORY_RECALL_SMALL_HUB_MAX",
              "SIBYL_MEMORY_RECALL_SIBLING_CAP"):
        monkeypatch.delenv(v, raising=False)
    d = tempfile.mkdtemp()
    c = MemoryClient.local(os.path.join(d, "m.db"), tenant_id="qa")
    for doc in ("d1", "d2", "d3"):
        c.set_entity("email", doc, {"text": doc})
    c.set_entity("person", "Plain", {"role": "x"})
    c.set_entity("customer", "Acme", {"card": 1})
    c.set_entity("ticket", "T-1", {"card": 1})
    for doc in ("d1", "d2", "d3"):
        c.link(entity_ref("customer", "Acme"), entity_ref("email", doc), "source_of",
               metadata={"title": f"{doc} subject", "date": "2026-02-01"})
    c.link(entity_ref("ticket", "T-1"), entity_ref("email", "d1"), "source_of")
    monkeypatch.setattr(server, "_open_client", lambda: c)
    return server.build_server(), c


def _recall(mcp, category, name) -> dict:
    res = asyncio.run(mcp.call_tool("memory_recall", {"category": category, "name": name}))
    return res[1] if isinstance(res, tuple) else res


def test_plain_entity_has_no_hub_keys(wired) -> None:
    mcp, _ = wired
    out = _recall(mcp, "person", "Plain")
    assert out["ok"] is True and out["entity"]["name"] == "Plain"
    for k in ("hubs", "siblings", "siblings_omitted", "neighborhood_note"):
        assert k not in out


def test_source_of_in_edges_give_hubs_and_siblings(wired) -> None:
    mcp, _ = wired
    out = _recall(mcp, "email", "d1")
    assert [(h["kind"], h["key"], h["doc_count"]) for h in out["hubs"]] == [
        ("ticket", "T-1", 1), ("customer", "Acme", 3)]
    sib = {s["hub"]["key"]: s for s in out["siblings"]}
    assert [r["dsid"] for r in sib["Acme"]["rows"]] == ["d2", "d3"]
    assert sib["Acme"]["rows"][0]["title"] == "d2 subject"
    assert "siblings_omitted" not in out                       # nothing left out


def test_sibling_cap_reports_omitted(wired, monkeypatch) -> None:
    mcp, _ = wired
    monkeypatch.setenv("SIBYL_MEMORY_RECALL_SIBLING_CAP", "1")
    out = _recall(mcp, "email", "d1")
    assert out["siblings_omitted"] == 1


def test_recall_hubs_zero_turns_it_off(wired, monkeypatch) -> None:
    mcp, _ = wired
    monkeypatch.setenv("SIBYL_MEMORY_RECALL_HUBS", "0")
    out = _recall(mcp, "email", "d1")
    assert out["ok"] is True and "hubs" not in out and "siblings" not in out


def test_store_error_gives_a_note_not_a_failure(wired, monkeypatch) -> None:
    mcp, c = wired

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(c, "doc_neighborhood", boom)
    out = _recall(mcp, "email", "d1")
    assert out["ok"] is True and out["entity"]["name"] == "d1"
    assert out["neighborhood_note"] == "hubs unavailable (OperationalError)"
    assert "hubs" not in out


def test_recall_is_still_recorded(wired, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_note_tool", lambda name, kwargs: calls.append(name))
    mcp, _ = wired
    _recall(mcp, "email", "d1")
    assert calls == ["memory_recall"]


def test_tool_schemas_equal_the_graph_merge() -> None:
    want = json.loads(SCHEMAS_S2.read_text())
    tools = asyncio.run(server.build_server().list_tools())
    got = {t.name: {"inputSchema": t.inputSchema, "meta": t.meta} for t in tools}
    assert json.loads(json.dumps(got, sort_keys=True)) == want


def test_experimental_tool_text_is_not_shipped() -> None:
    for name in ("_desc", "_SEARCH_FIX_BASE", "_SEARCH_FIX_OWNERS", "_SEARCH_EXTRA_RANKED",
                 "_NEIGHBORS_FIX", "_RECALL_FIX", "_RECALL_EXTRA_HUBS"):
        assert not hasattr(server, name), name
    src = Path(server.__file__).read_text()
    assert "TOOLTEXT_V2" not in src
    tools = {t.name: t for t in asyncio.run(server.build_server().list_tools())}
    assert "`hubs`" in tools["memory_recall"].description
