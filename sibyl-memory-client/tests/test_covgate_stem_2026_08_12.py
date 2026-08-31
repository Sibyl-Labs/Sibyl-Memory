"""D2L — coverage-gated stem rescue with rescue ladder (Kravento PL eval
2026-08-12). Targeted tests for the four behaviors that distinguish D2L
(covgate_l) from the earlier unconditional-stem build (config C):

  1. COVERAGE GATE skips the stem pass when the query's stem is already
     substring-covered by the head — so same-stem sibling rows are NOT appended
     to a query the head already answered (the C-vs-D2L precision win).
  2. RESCUE LADDER surfaces a realistic multi-token PL query ('status
     reklamacji') whose full-stemmed AND matches nothing, via an uncovered-stem
     single probe — the class config C hard-misses.
  3. LADDER DISCIPLINE: uncovered stems are probed longest-first and the ladder
     STOPS at the first probe that appends anything.
  4. APPEND-ONLY holds with gate + ladder active: the strict/relaxed head is
     preserved byte-for-byte at the front, D2L only extends the tail.

Every assertion is on the public `search()` result; the gate/ladder are
internal but their effect is observable at the boundary.
"""
from __future__ import annotations

import pytest

# D2L coverage-gated stem rescue + probe ladder: removed 2026-08-30 lang-core-strip (operator directive).
# The module is kept, not deleted, so the behaviour it pinned stays on
# the record and can be re-read when stage 2 replaces it at write time.
pytestmark = pytest.mark.skip(reason="removed 2026-08-30 lang-core-strip (operator directive)")

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import client as cmod


def _ident_seq(hits):
    return [(h.get("tier"), h.get("category"), h.get("key")) for h in hits]


def _head(client, query, limit=10):
    """Reconstruct the head (strict -> relaxed -> raw folded-trigram shadow) the
    D2L stage sees, mirroring search()'s pre-stem assembly, so a test can assert
    the append-only prefix against it."""
    hits = client._search_strict(query, limit=limit)
    if not hits:
        for relaxed in cmod._relaxed_query_strings(query):
            hits = client._search_strict(relaxed, limit=limit)
            if hits:
                break
    out = list(hits)
    seen = set(_ident_seq(out))
    for h in client._shadow_fallback(query, limit=limit):
        ident = (h.get("tier"), h.get("category"), h.get("key"))
        if ident in seen or len(out) >= limit:
            continue
        seen.add(ident)
        out.append(h)
    return out


# --------------------------------------------------------------------------
# 1. coverage gate: covered stem -> zero stem rows appended
# --------------------------------------------------------------------------

def test_covered_stem_appends_nothing(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("support", "reklamacja-obsluga", {"text": "reklamacja rozpatrzona w 7 dni"})
    c.set_entity("support", "reklamacje-sla", {"text": "reklamacje SLA i normy jakosci"})

    # The stem 'reklama' WOULD match the sibling row on its own...
    assert any(h["key"] == "reklamacje-sla"
               for h in c._shadow_fallback("reklama", limit=10))

    # ...but for query 'reklamacja' the head already covers the stem, so the gate
    # runs no stem pass and the sibling is NOT appended.
    hits = c.search("reklamacja", limit=10)
    keys = [h["key"] for h in hits]
    assert keys == ["reklamacja-obsluga"]
    assert cmod._uncovered_stem_tokens("reklamacja", hits) == []


# --------------------------------------------------------------------------
# 2. rescue ladder: multi-token PL query surfaced via an uncovered-stem single
# --------------------------------------------------------------------------

def test_multitoken_ladder_rescue(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("support", "reklamacja-obsluga", {"text": "reklamacja rozpatrzona w 7 dni"})

    # Head is empty (no row has both tokens, no single-token strict hit) and the
    # full-stemmed 'statu reklama' AND matches nothing; the ladder rescues it via
    # the uncovered-stem single 'reklama'.
    assert c._search_strict("status reklamacji", limit=10) == []
    assert c._shadow_fallback(cmod._stem_truncated_query("status reklamacji"), limit=10) == []
    hits = c.search("status reklamacji", limit=10)
    assert any(h["key"] == "reklamacja-obsluga" for h in hits)


# --------------------------------------------------------------------------
# 3. ladder discipline: longest-first, stop at first append
# --------------------------------------------------------------------------

def test_ladder_longest_first_and_continues(tmp_path):
    """N3' (Kravento PL eval, 2026-08-18) overturned this test's original name
    (test_ladder_longest_first_and_stops): the query names TWO concepts and
    both rows answer it, so requiring magazyn-glowny to be ABSENT encoded the
    N3' bug (stopping at the first appending probe) rather than an invariant.
    What this test legitimately pins — the tie-break ORDER, longer/more-
    selective stem leads — is retained below."""
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    # 'reklama' (len 7) matches R1; 'magazy' (len 6) matches R2 — disjoint rows.
    c.set_entity("support", "reklamacja-obsluga", {"text": "reklamacja rozpatrzona"})
    c.set_entity("wh", "magazyn-glowny", {"text": "magazyn glowny lokalizacja"})

    # Each single stem matches its own row in isolation:
    assert [h["key"] for h in c._shadow_fallback("reklama", limit=10)] == ["reklamacja-obsluga"]
    assert [h["key"] for h in c._shadow_fallback("magazy", limit=10)] == ["magazyn-glowny"]

    # Query has both tokens uncovered; full-stemmed AND appends nothing, so the
    # ladder runs. Both probes tie on hit count (1 row each); the longer stem
    # ('reklama') leads per the tie-break, but the ladder now CONTINUES past
    # its append instead of stopping, so 'magazy' still runs and R2 surfaces.
    keys = [h["key"] for h in c.search("reklamacji magazynie", limit=10)]
    assert keys[0] == "reklamacja-obsluga"
    assert "magazyn-glowny" in keys


# --------------------------------------------------------------------------
# 4. append-only: strict/relaxed head preserved with gate + ladder active
# --------------------------------------------------------------------------

def test_append_only_under_d2l(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("fin", "faktury-vat", {"text": "faktury vat rozliczenie"})
    c.set_entity("support", "reklamacja-obsluga", {"text": "reklamacja rozpatrzona"})

    head = _head(c, "faktury reklamacji", limit=10)
    hits = c.search("faktury reklamacji", limit=10)

    # the head is the strict/relaxed 'faktury' hit; D2L only extends the tail
    assert head and head[0]["key"] == "faktury-vat"
    assert _ident_seq(hits)[:len(head)] == _ident_seq(head)
    # and the stem rescue genuinely fired (reklamacja-obsluga appended below head)
    keys = [h["key"] for h in hits]
    assert "reklamacja-obsluga" in keys
    assert keys.index("faktury-vat") < keys.index("reklamacja-obsluga")
