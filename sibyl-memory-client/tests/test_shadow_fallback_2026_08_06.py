"""Zero-hit shadow fallback — end-to-end through search() + the linker.

v0.5.0 multi-language search (spec §4.3 / §7). Covers the M4 substring class
(matches inside an unbroken indexed token) and the M5 non-decomposable fold, the
``tiers=`` filter in the fallback, the journal cap, the additivity property
(gate 5), and shadow-error containment.
"""
from __future__ import annotations

import sqlite3

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import shadow
from sibyl_memory_client.multi_record import multi_record_search


# --------------------------------------------------------------------------
# The M4 substring class + M5 fold, through the public funnels
# --------------------------------------------------------------------------

def _corpus(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("places", "beijing", {"text": "北京烤鸭"})          # 北京 glued (M4)
    c.set_entity("places", "khonkaen", {"text": "เมืองขอนแก่น"})     # Thai glue (M4)
    c.set_entity("places", "chiangmai", {"text": "เชียงใหม่"})        # Std-Thai regress
    c.set_entity("places", "durban", {"text": "laseThekwini"})       # Zulu compound (M4)
    c.set_entity("places", "belzyce", {"address": "Bełżyce, Lublin"}) # ł fold (M5)
    return c


@pytest.mark.parametrize("query, key", [
    ("北京", "beijing"),
    ("ขอนแก่น", "khonkaen"),
    ("เชียงใหม่", "chiangmai"),
    ("Thekwini", "durban"),
    ("Belzyce", "belzyce"),
])
def test_substring_and_fold_via_client_search(tmp_path, query, key):
    c = _corpus(tmp_path)
    assert any(h["key"] == key for h in c.search(query, limit=10)), query


@pytest.mark.parametrize("query, key", [
    ("北京", "beijing"),
    ("Thekwini", "durban"),
    ("Belzyce", "belzyce"),
])
def test_substring_and_fold_via_multi_record(tmp_path, query, key):
    """The real MCP untiered path (server.py memory_search -> multi_record_search)."""
    c = _corpus(tmp_path)
    hits = multi_record_search(c, query, limit=10)
    assert any(h["key"] == key for h in hits), query


# --------------------------------------------------------------------------
# tiers= filter is respected inside the fallback
# --------------------------------------------------------------------------

def test_tiers_filter_respected_in_fallback(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("places", "beijing", {"text": "北京烤鸭"})
    c.set_state("cfg", {"note": "北京烤鸭 setting"})
    # entity-only: state hit must be filtered out even though it also matches
    ent = c.search("北京", limit=10, tiers=("entity",))
    assert [h["tier"] for h in ent] == ["entity"]
    assert ent[0]["key"] == "beijing"
    # state-only
    st = c.search("北京", limit=10, tiers=("state",))
    assert [h["tier"] for h in st] == ["state"]
    assert st[0]["key"] == "cfg"
    # both tiers present when unrestricted
    both = {h["tier"] for h in c.search("北京", limit=10)}
    assert both == {"entity", "state"}


# --------------------------------------------------------------------------
# Journal cap (symmetry with _search_strict: max(1, limit//4))
# --------------------------------------------------------------------------

def test_journal_cap_in_fallback(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    for i in range(10):
        c.write_event(evaluated={"note": f"北京烤鸭 event {i}"})  # 北京 glued -> shadow path
    hits = c.search("北京", limit=8, tiers=("journal",))
    assert hits, "expected shadow journal hits"
    assert all(h["tier"] == "journal" for h in hits)
    assert len(hits) == max(1, 8 // 4) == 2


# --------------------------------------------------------------------------
# Additivity (gate 5): non-empty strict results are byte-identical
# with the shadow present vs absent.
# --------------------------------------------------------------------------

def test_additivity_nonempty_strict_byte_identical(tmp_path, monkeypatch):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("work", "invoice", {"note": "billing handled by alice for q3"})
    c.set_entity("work", "report", {"note": "quarterly report drafted by bob"})
    c.set_state("session", {"focus": "billing reconciliation project"})
    c.set_reference("skill/deploy", "deploy runbook: staging then prod")
    c.set_entity("places", "beijing", {"text": "北京烤鸭"})   # only shadow can satisfy

    queries = ["billing", "report bob", "deploy runbook", "project",
               "quarterly", "北京", "nonexistent-xyz", "alice q3"]

    present = {q: c.search(q, limit=10) for q in queries}
    # force the shadow ABSENT
    monkeypatch.setattr(MemoryClient, "_shadow_fallback",
                        lambda self, *a, **k: [])
    absent = {q: c.search(q, limit=10) for q in queries}

    for q in queries:
        strict = c._search_strict(q, limit=10)
        if strict:  # only claim identity where the strict result is non-empty
            assert present[q] == absent[q], q
    # sanity: the shadow-only query DID differ (present found it, absent did not)
    assert present["北京"] and not absent["北京"]


# --------------------------------------------------------------------------
# Shadow-error containment: a broken shadow yields primary behaviour, never raises
# --------------------------------------------------------------------------

def test_shadow_error_contained_returns_primary(tmp_path, monkeypatch):
    c = _corpus(tmp_path)

    def raiser(*a, **k):
        raise sqlite3.OperationalError("simulated shadow failure")

    monkeypatch.setattr(shadow, "shadow_search", raiser)
    # a shadow-only query now returns [] (primary path was empty) — no exception
    assert c.search("北京", limit=10) == []
    # a normal strict query is unaffected (never reaches the fallback)
    c.set_entity("work", "note", {"text": "billing handled by alice"})
    assert any(h["key"] == "note" for h in c.search("billing", limit=10))


class _FailMatchConn:
    """Wraps a real connection but raises on the shadow MATCH query, so we can
    exercise shadow_search's internal error containment (sqlite3.Connection is an
    immutable type and cannot be monkeypatched directly)."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **k):
        if "search_shadow" in sql and "MATCH" in sql:
            # non-healable message: containment must return [] without a heal
            raise sqlite3.OperationalError("simulated shadow query failure")
        return self._real.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_shadow_internal_containment_returns_empty(tmp_path):
    """shadow_search itself contains a DB error on the query and returns []."""
    c = _corpus(tmp_path)
    with c.storage.connection() as conn:
        proxy = _FailMatchConn(conn)
        # 'Belzyce' -> match_toks path -> the MATCH raises -> contained -> []
        assert shadow.shadow_search(proxy, "t1", "Belzyce", limit=10) == []
