"""Regression: the IDF corpus count must span every tier `search` recalls over.

Sibyl-Labs/Sibyl-Memory#27. `df[t]` in `multi_record_search` is
`len(client.search(t, limit=...))`, which spans entities, state documents,
reference documents and journal events. `_corpus_count` counted entities alone.
A store with one entity and forty journal events therefore produced
`df(meeting)=41 > corpus_n=1`, `log((1+1)/(41+1)) + 1.0` went negative, the sum
of the weights flipped sign, and the entity that matched the query in full fell
out of the top results entirely.

Two guards, matching the two parts of the fix:
  1. `_corpus_count` sums the four tiers.
  2. the IDF weight is clamped at zero, so even the entities-only fallback path
     (no storage, or no tenant) can never produce a negative weight.
"""
from __future__ import annotations

import math

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import _corpus_count, multi_record_search


def _seed(tmp_path, name: str) -> MemoryClient:
    c = MemoryClient.local(tmp_path / name, tenant_id="t1")
    c.set_entity("company", "acme", {"text": "acme project kickoff meeting scheduled"})
    for i in range(40):
        c.write_event(evaluated=f"meeting notes entry number {i} discussed roadmap")
    return c


def test_corpus_count_spans_every_recalled_tier(tmp_path):
    c = _seed(tmp_path, "corpus.db")
    # 1 entity + 40 journal events. Entities-only counting returned 1.
    assert _corpus_count(c) == 41


def test_full_match_entity_is_not_inverted_out_of_the_results(tmp_path):
    c = _seed(tmp_path, "rank.db")
    res = multi_record_search(c, "acme meeting", limit=10)
    assert res, "the full-match entity must be recalled at all"
    top = res[0]
    assert top.get("tier") == "entity"
    assert top.get("key") == "acme"
    # and nothing from the journal may outrank it
    for hit in res:
        if hit.get("tier") == "journal":
            break
        assert hit.get("key") == "acme"


def test_idf_weight_is_never_negative(tmp_path):
    """Part 2 in isolation: the clamp holds even when a caller supplies its own
    under-counted `corpus_n` (which is exactly what the fallback path does)."""
    c = _seed(tmp_path, "clamp.db")
    # corpus_n=1 is the pre-fix, entities-only count. Unclamped this scores
    # every candidate with a negative weight and inverts the ranking.
    assert math.log((1 + 1) / (41 + 1)) + 1.0 < 0  # the pre-fix weight
    res = multi_record_search(c, "acme meeting", limit=10, corpus_n=1)
    assert res, "an under-counted corpus must still return the full match"
    assert res[0].get("key") == "acme"
