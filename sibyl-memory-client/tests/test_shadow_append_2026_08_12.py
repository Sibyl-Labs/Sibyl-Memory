"""F2 (Kravento PL eval 2026-08-12): the folded-trigram shadow runs UNCONDITIONALLY
and its hits are APPENDED after the primary (strict/relaxed) hits, deduped on the
(tier, category, key) identity triple and capped at limit.

Append-only invariant: the primary head is never reordered or dropped — the shadow
can only extend the tail — so English recall + existing ranking cannot regress.
Previously the shadow fired only on a total zero-hit, so any weak/English primary
hit hid a same-fact row in another language (the packshot case).
"""
from __future__ import annotations

import pytest

import sqlite3

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import shadow


def _ident_seq(hits):
    return [(h.get("tier"), h.get("category"), h.get("key")) for h in hits]


# --------------------------------------------------------------------------
# (a) the packshot scenario: EN strict hit FIRST, PL row appended
# --------------------------------------------------------------------------

def _packshot_store(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    # category 'media' does NOT porter-stem to 'packshot' (so the scenario is real).
    c.set_entity("media", "product-packshots",
                 {"text": "Every product packshot on white background; packshots to drive"})
    c.set_entity("media", "packshoty-produktowe",
                 {"text": "Packshoty produktow, gotowe packshoty na dysku"})
    return c


@pytest.mark.skip(reason="F2 unconditional shadow append: removed 2026-08-30 lang-core-strip (operator directive)")
def test_packshot_english_strict_first_polish_appended(tmp_path):
    c = _packshot_store(tmp_path)
    strict = c._search_strict("packshot", limit=10)
    # pre-condition: strict finds ONLY the English row (the F2 trigger)
    assert [h["key"] for h in strict] == ["product-packshots"]

    hits = c.search("packshot", limit=10)
    keys = [h["key"] for h in hits]
    # both twins present now; the English strict hit is FIRST, the Polish appended
    assert "product-packshots" in keys and "packshoty-produktowe" in keys
    assert keys[0] == "product-packshots"
    # append-only: the strict head is byte-for-byte preserved at the front
    assert _ident_seq(hits)[:len(strict)] == _ident_seq(strict)


# --------------------------------------------------------------------------
# (b) strict-prefix battery: primary head preserved for every query
# --------------------------------------------------------------------------

def _battery_store(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("work", "invoice", {"note": "billing handled by alice for q3"})
    c.set_entity("work", "report", {"note": "quarterly report drafted by bob"})
    c.set_state("session", {"focus": "billing reconciliation project"})
    c.set_reference("skill/deploy", "deploy runbook: staging then prod")
    c.set_entity("media", "product-packshots", {"text": "product packshot packshots"})
    c.set_entity("media", "packshoty-produktowe", {"text": "packshoty produktow"})
    c.set_entity("places", "beijing", {"text": "北京烤鸭"})
    return c


def test_strict_head_preserved_for_every_query(tmp_path):
    c = _battery_store(tmp_path)
    battery = ["billing", "report bob", "deploy runbook", "project", "quarterly",
               "packshot", "北京", "alice q3", "nonexistent-xyz"]
    for q in battery:
        strict = c._search_strict(q, limit=10)
        hits = c.search(q, limit=10)
        assert _ident_seq(hits)[:len(strict)] == _ident_seq(strict), q


# --------------------------------------------------------------------------
# (c) limit cap respected: limit=1 -> strict hit only, shadow cannot exceed cap
# --------------------------------------------------------------------------

def test_limit_cap_leaves_no_room_for_shadow(tmp_path):
    c = _packshot_store(tmp_path)
    hits = c.search("packshot", limit=1)
    assert [h["key"] for h in hits] == ["product-packshots"]


# --------------------------------------------------------------------------
# (d) no duplicate identity triples in any result
# --------------------------------------------------------------------------

def test_no_duplicate_identity_triples(tmp_path):
    c = _battery_store(tmp_path)
    for q in ["packshot", "billing", "北京", "report bob", "packshoty"]:
        seq = _ident_seq(c.search(q, limit=20))
        assert len(seq) == len(set(seq)), (q, seq)


# --------------------------------------------------------------------------
# (e) tiers= filter honored by the appended shadow hits
# --------------------------------------------------------------------------

def test_tiers_filter_honored_by_appended_hits(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("places", "beijing", {"text": "北京烤鸭"})   # shadow-only (glued CJK)
    c.set_state("cfg", {"note": "北京烤鸭 setting"})            # shadow-only, state tier
    ent = c.search("北京", limit=10, tiers=("entity",))
    assert [h["tier"] for h in ent] == ["entity"]
    st = c.search("北京", limit=10, tiers=("state",))
    assert [h["tier"] for h in st] == ["state"]
    both = {h["tier"] for h in c.search("北京", limit=10)}
    assert both == {"entity", "state"}


# --------------------------------------------------------------------------
# (f) shadow error containment: a raising shadow yields the primary result, never
#     an exception
# --------------------------------------------------------------------------

def test_shadow_error_contained_primary_returned(tmp_path, monkeypatch):
    c = _battery_store(tmp_path)

    def raiser(*a, **k):
        raise sqlite3.OperationalError("simulated shadow failure")

    monkeypatch.setattr(shadow, "shadow_search", raiser)
    # strict-hit query: primary returned unchanged, no exception
    assert any(h["key"] == "invoice" for h in c.search("billing", limit=10))
    # shadow-only query: primary empty -> [] (contained), no exception
    assert c.search("北京", limit=10) == []


# --------------------------------------------------------------------------
# stem pass recovers a Polish inflection append-only (D2L: single-token
# empty-head rescue — the ladder runs the fully-stemmed query, unconditional
# under the coverage gate because the empty head covers nothing)
# --------------------------------------------------------------------------

@pytest.mark.skip(reason="D2L stem rescue: removed 2026-08-30 lang-core-strip (operator directive)")
def test_stem_pass_recovers_inflection_append_only(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("support", "reklamacja-obsluga",
                 {"text": "Kazda reklamacja rozpatrzona w 7 dni"})
    # strict + raw shadow both miss the inflected query 'reklamacje' (ending swap)
    assert c._search_strict("reklamacje", limit=10) == []
    assert c._shadow_fallback("reklamacje", limit=10) == []
    hits = c.search("reklamacje", limit=10)
    assert any(h["key"] == "reklamacja-obsluga" for h in hits)
