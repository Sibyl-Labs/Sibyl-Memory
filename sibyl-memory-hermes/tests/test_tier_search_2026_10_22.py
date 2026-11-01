"""2.C (R3, 2026-10-22): the Hermes rail gets tier-aware search parity.

provider.search_multi_record passes scope / with_links through to
multi_record_search (links on by default, as in the MCP tool), and the
sibyl_search tool takes `scope`.
"""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_client import entity_ref, reference_ref
from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes._hermes_plugin.adapter import SEARCH_SCHEMA, SibylAdapter


def _adapter(tmp_path: Path) -> SibylAdapter:
    a = SibylAdapter()
    a._sibyl = SibylMemoryProvider(db_path=str(tmp_path / "a.db"), autoload_credentials=False)
    a._session_id = "s"
    a._hermes_home = tmp_path
    c = a._sibyl.client
    c.set_entity("person", "Maya Patel", {"role": "lead"})
    c.set_entity("company", "Redwood", {"note": "onboarding"})
    c.set_reference("doc/k", "Kickoff. Redwood onboarding.", metadata={"title": "Kickoff"})
    c.link(entity_ref("company", "Redwood"), reference_ref("doc/k"), "source_of")
    return a


def test_provider_name_first_with_links(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    res = a._sibyl.search_multi_record("Maya Patel", limit=5)
    assert res[0]["key"] == "Maya Patel" and res[0]["match"] == "name"
    assert res[0]["links"] == {"degree": {"out": 0, "in": 0}, "by_relation": {}, "sample": []}
    res = a._sibyl.search_multi_record("redwood onboarding", limit=5, with_links=False)
    assert res and all("links" not in h for h in res)


def test_sibyl_search_scope(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    allr = json.loads(a.handle_tool_call("sibyl_search", {"query": "redwood onboarding"}))
    assert "reference" in {h["tier"] for h in allr["results"]}
    red = next(h for h in allr["results"] if h["key"] == "Redwood")
    assert red["links"]["by_relation"] == {"source_of": 1}
    graph = json.loads(a.handle_tool_call(
        "sibyl_search", {"query": "redwood onboarding", "scope": "graph"}))
    assert graph["results"] and {h["tier"] for h in graph["results"]} <= {"entity", "state"}
    assert graph["verdict"]["scope"] == "graph"
    bad = json.loads(a.handle_tool_call("sibyl_search", {"query": "redwood", "scope": "x"}))
    assert "error" in bad


def test_schema_documents_scope() -> None:
    props = SEARCH_SCHEMA["parameters"]["properties"]
    assert props["scope"]["enum"] == ["all", "graph"]
    assert "match='name'" in SEARCH_SCHEMA["description"]


def test_sibyl_search_output_budget_parity(tmp_path: Path, monkeypatch) -> None:
    """2026-10-22 output budget: same degrade order as the MCP server
    (bodies of snippet hits, then link samples, then trailing hits), verdict
    reports truncation, env override."""
    a = _adapter(tmp_path)
    c = a._sibyl.client
    for i in range(40):
        c.set_entity("project", f"P{i}", {"summary": "redwood onboarding " + "word " * 1500})
        c.link(entity_ref("project", f"P{i}"), entity_ref("company", "Redwood"), "member_of")
    from sibyl_memory_client.output_budget import estimate_tokens
    monkeypatch.delenv("SIBYL_MEMORY_HERMES_OUTPUT_BUDGET", raising=False)
    monkeypatch.delenv("SIBYL_MEMORY_HERMES_OUTPUT_TOKENS", raising=False)
    small = a.handle_tool_call("sibyl_search", {"query": "Maya Patel", "limit": 3})
    assert "degraded" not in json.loads(small)["verdict"]
    default = a.handle_tool_call("sibyl_search", {"query": "redwood onboarding", "limit": 50})
    assert estimate_tokens(default) <= 22_000
    monkeypatch.setenv("SIBYL_MEMORY_HERMES_OUTPUT_TOKENS", "3000")
    text = a.handle_tool_call("sibyl_search", {"query": "redwood onboarding", "limit": 50})
    out = json.loads(text)
    v = out["verdict"]
    assert estimate_tokens(text) <= 3_000 and v["returned"] == len(out["results"])
    assert v["truncated"] > 0 and "sibyl_recall" in v["hint"]
    assert v["degraded"][:3] == ["body_500", "body_150", "body_omitted"]
    assert all("body" not in h for h in out["results"] if h.get("snippet"))
    monkeypatch.setenv("SIBYL_MEMORY_HERMES_OUTPUT_BUDGET", "500000")
    monkeypatch.setenv("SIBYL_MEMORY_HERMES_OUTPUT_TOKENS", "125000")
    wide = json.loads(a.handle_tool_call("sibyl_search", {"query": "redwood onboarding", "limit": 50}))
    assert "truncated" not in wide["verdict"]
    keys = [h["key"] for h in wide["results"]]
    assert [h["key"] for h in out["results"]] == keys[:len(out["results"])]
