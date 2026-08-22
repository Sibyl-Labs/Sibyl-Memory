"""N3 (Kravento PL eval 2026-08-16): selectivity-ordered D2L probe ladder.

The stem rescue ladder used to probe uncovered stems longest-first and STOP at
the first that appended. Length is unrelated to selectivity, so a saturated
high-frequency stem ('aktualiza', 20 rows) won over a discriminating one ('cenni',
1 row incl. the target) purely because it was longer — the target never got
probed. The fix pre-fetches the (bounded) probe set and orders by MEASURED
selectivity (fewest hits = most discriminating) with length as the tie-break, so
the discriminating probe fires first while the pinned stop-at-first-append
discipline is preserved.
"""
from __future__ import annotations

from sibyl_memory_client import MemoryClient


def test_selectivity_beats_length(tmp_path):
    c = MemoryClient.local(tmp_path / "n3.db", tenant_id="t1")
    # 20 rows saturate stem 'aktualiza'; 2 rows carry stem 'hurtow'; 1 target row
    # is reachable only via the short-but-discriminating stem 'cenni'.
    for i in range(20):
        c.set_entity("akt", f"akt-{i}", {"text": f"aktualizacji systemu numer {i}"})
    c.set_entity("hurt", "hurt-1", {"text": "oferta hurtownia produkty"})
    c.set_entity("hurt", "hurt-2", {"text": "zamowienie hurtowni realizacja"})
    c.set_entity("cen", "cennik-target", {"text": "cennik hurtowy aktualny"})

    # empty head: strict AND misses and no single token strict-matches anything
    assert c._search_strict("aktualizacja cennika hurtowego", limit=20) == []
    for t in ("aktualizacja", "cennika", "hurtowego"):
        assert c._search_strict(t, limit=20) == []
    # selectivity ordering: cenni(1) < hurtow(3) < aktualiza(20)
    assert len(c._shadow_fallback("aktualiza", limit=20)) == 20
    assert len(c._shadow_fallback("cenni", limit=20)) == 1

    hits = c.search("aktualizacja cennika hurtowego", limit=20)
    assert "cennik-target" in [h["key"] for h in hits], \
        "N3: the discriminating probe lost to the saturated one"


def test_no_truncation_beyond_probe_cap(tmp_path):
    """Panel Finding A: the selectivity ladder must NOT truncate the candidate
    set. An interim build sliced the probe SELECTION to the 8 LONGEST uncovered
    stems to bound fan-out; that lost a target reachable only via a SHORTER stem
    past position 8 (a recall regression vs 0.6.0, which probed every uncovered
    stem). Here the query has 9 uncovered stems: 8 long ones each match 2 junk
    rows, and the single most-discriminating stem ('cenni', 1 row incl. the
    target) is the SHORTEST — so a longest-first [:8] slice drops it and the
    target never surfaces. The fix probes every uncovered stem, so 'cenni' (the
    globally most selective) still wins."""
    c = MemoryClient.local(tmp_path / "n3c.db", tenant_id="t1")
    # 8 long tokens; each stored as two DIFFERENTLY-inflected rows sharing the
    # token's stem (so strict AND misses, the stem still matches 2 rows).
    longs = [
        ("aktualizacja", ("aktualizacji systemu", "aktualizacje danych")),
        ("harmonogramy", ("harmonogramie prac", "harmonogramow zmian")),
        ("reklamacyjne", ("reklamacyjnej sprawy", "reklamacyjni klienci")),
        ("magazynowej",  ("magazynowym stanie", "magazynowa hala")),
        ("logistyczna",  ("logistycznej trasy", "logistyczny wezel")),
        ("produktowej",  ("produktowym opisie", "produktowa karta")),
        ("inwentarzem",  ("inwentarza spis", "inwentarzu pozycje")),
        ("serwisowego",  ("serwisowym zgloszeniu", "serwisowa naprawa")),
    ]
    for i, (_tok, (b1, b2)) in enumerate(longs):
        c.set_entity("junk", f"j{i}a", {"text": b1})
        c.set_entity("junk", f"j{i}b", {"text": b2})
    # the discriminating target: reachable only via the SHORT stem 'cenni' (1 row)
    c.set_entity("price", "cennik-target", {"text": "cennika hurtowego dokument"})

    query = " ".join(tok for tok, _ in longs) + " cennik"
    # head is empty: strict AND misses and no single query token strict-matches
    assert c._search_strict(query, limit=20) == []
    # 'cenni' is the most selective probe (1 row) but also the shortest stem
    assert len(c._shadow_fallback("cenni", limit=20)) == 1

    hits = c.search(query, limit=20)
    assert "cennik-target" in [h["key"] for h in hits], \
        "N3: candidate-set truncation dropped a reachable target past the probe cap"


def test_tie_break_reproduces_length_order_and_continues(tmp_path):
    """No-regression twin of test_covgate_stem::test_ladder_longest_first_and_continues.
    N3' (Kravento PL eval, 2026-08-18) overturned this test's original name
    (test_tie_break_reproduces_length_order_and_stop): when two disjoint probes
    TIE on hit count (1 each), the length tie-break still keeps today's winner
    (the longer stem leads), but the ladder no longer stops at the first
    append — both rows answer the query, so the shorter stem's row now
    surfaces too."""
    c = MemoryClient.local(tmp_path / "n3b.db", tenant_id="t1")
    c.set_entity("support", "reklamacja-obsluga", {"text": "reklamacja rozpatrzona"})
    c.set_entity("wh", "magazyn-glowny", {"text": "magazyn glowny lokalizacja"})

    # each stem matches exactly its own row (disjoint, 1 hit each)
    assert [h["key"] for h in c._shadow_fallback("reklama", limit=10)] == ["reklamacja-obsluga"]
    assert [h["key"] for h in c._shadow_fallback("magazy", limit=10)] == ["magazyn-glowny"]

    keys = [h["key"] for h in c.search("reklamacji magazynie", limit=10)]
    # longer stem 'reklama' (7) wins the tie and leads; the ladder continues
    # past it, so 'magazy' still runs and R2 surfaces below it.
    assert keys[0] == "reklamacja-obsluga"
    assert "magazyn-glowny" in keys
