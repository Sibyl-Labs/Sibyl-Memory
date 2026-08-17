"""Faithful default-MCP-path repro (N1 acceptance test, 2026-08-16).

Drives the EXACT agent-default path: memory_search with `tiers` OMITTED, which
routes through multi_record_search. Question-shaped queries in PL + EN (whose
interrogative / copula function words carry zero corpus support) previously
abstained to count==0 because the Stage-1 df=0 gate could not tell a function
word from a content word — the 6/15 -> 15/15 miss. This test seeds a fresh
parallel PL/EN corpus and asserts every question-shaped query returns the target,
while the content-shaped zero-df abstention contract still holds at the tool
boundary.

Reuses the `wired` fixture pattern from test_injection_fence_2026_06_25.py
(build_server() + monkeypatched server._open_client -> shared MemoryClient.local).
"""
import asyncio
import os
import tempfile

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
    res = asyncio.run(mcp.call_tool(tool, args))
    if isinstance(res, tuple):
        return res[1]
    return res


# Parallel PL/EN facts, each stored ONCE per language with naturally inflected
# body text. Content nouns are unique to their target so coverage is unambiguous.
_PAIRS = [
    ("ops", "inwentaryzacja", "inwentaryzacja magazynu zaplanowana na piatek"),
    ("ops", "stocktake", "the stocktake is scheduled for friday"),
    ("price", "cennik-hurtowy", "cennik hurtowy dostepny do pobrania"),
    ("price", "wholesale-price-list", "the wholesale price list published monthly"),
    ("support", "reklamacja", "zespol obsluguje reklamacje klientow"),
    ("support", "complaint", "the support team handles the complaint quickly"),
    ("logi", "wysylka", "status wysylki potwierdzony przez kuriera"),
    ("logi", "shipment", "the shipment tracked by an external carrier"),
    # decoys: extra corpus mass with distinct vocabulary
    ("wh", "magazyn", "magazyn glowny w centrali firmy"),
    ("wh", "warehouse", "the central warehouse holds bulk stock"),
    ("fin", "faktura", "faktura vat wystawiona dla odbiorcy"),
    ("fin", "invoice", "the invoice was settled last week"),
    ("del", "dostawa", "dostawa realizowana w dwa dni robocze"),
    ("del", "delivery", "the delivery route optimized overnight"),
    # co-anchor rows for the abstention contract
    ("order", "co0001-order", "co0001 order shipped confirmation note"),
    ("order", "co0002-order", "co0002 order packed awaiting pickup"),
]

# question-shaped query -> expected target entity key (tiers OMITTED = agent default)
_PL_QUERIES = {
    "kiedy jest inwentaryzacja": "inwentaryzacja",
    "gdzie jest cennik hurtowy": "cennik-hurtowy",
    "kto obsluguje reklamacje": "reklamacja",
    "jaki jest status wysylki": "wysylka",
}
_EN_QUERIES = {
    "when is the stocktake": "stocktake",
    "who handles the complaint": "complaint",
    "what is in the wholesale price list": "wholesale-price-list",
    "how is the shipment tracked": "shipment",
}


def _seed(shared):
    for cat, name, body in _PAIRS:
        shared.set_entity(cat, name, {"text": body})


@pytest.mark.parametrize("query,target", list(_PL_QUERIES.items()) + list(_EN_QUERIES.items()))
def test_default_path_question_recall(wired, query, target):
    mcp, shared = wired
    _seed(shared)
    out = _invoke(mcp, "memory_search", {"query": query, "limit": 10})
    assert out["count"] >= 1, f"{query!r} abstained (count 0) on the default path"
    keys = {r.get("key") for r in out["results"]}
    assert target in keys, f"{query!r} did not surface {target!r}; got {keys}"


def test_default_path_full_gate(wired):
    """The 6/15 -> 15/15 gate as a single aggregate: every question-shaped query
    (PL + EN) returns its target through the tiers-omitted default path."""
    mcp, shared = wired
    _seed(shared)
    misses = []
    for query, target in {**_PL_QUERIES, **_EN_QUERIES}.items():
        out = _invoke(mcp, "memory_search", {"query": query, "limit": 10})
        keys = {r.get("key") for r in out["results"]}
        if out["count"] < 1 or target not in keys:
            misses.append(query)
    assert not misses, f"default-path recall misses: {misses}"


# --------------------------------------------------------------------------
# MCP-level abstention contract preserved (content-shaped zero-df still []).
# --------------------------------------------------------------------------

def test_injection_token_abstains_at_tool_boundary(wired):
    mcp, shared = wired
    _seed(shared)
    out = _invoke(mcp, "memory_search", {"query": "co0001 nonexistenttokenzzzq report"})
    assert out["count"] == 0
    assert out["results"] == []


def test_content_shaped_absence_abstains(wired):
    mcp, shared = wired
    _seed(shared)
    # no row bears 'rejected' (nor a stem match) -> content-shaped zero-df term
    # collapses the query to [] even though it is a natural question.
    out = _invoke(mcp, "memory_search", {"query": "was the co0001 order rejected"})
    assert out["count"] == 0
    assert out["results"] == []
