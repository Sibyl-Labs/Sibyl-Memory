"""Regression: proximity re-ranking for multi-word search (v0.4.10).

Bug (chainriffs + KAPPA Discord reports, v0.4.2 / v0.4.4): the AND-of-tokens
default gives recall 100% but precision ~73%. Short "near-negative decoy" rows
that contain the query tokens in an unrelated context out-rank the real answer
under BM25, which rewards term density over proximity.

Fix: bucket each hit by match tightness (contiguous phrase > tight window >
scattered), sort by (bucket, bm25_rank). No hit is dropped (recall unchanged),
single-token + prefix queries keep plain BM25 order (anchor resolver unaffected).
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sibyl_memory_client.client import (  # noqa: E402
    MemoryClient,
    _match_tokens,
    _proximity_bucket,
)

LONG = ("reviewed during the quarterly infrastructure audit with many other "
        "operational notes the team captured for onboarding. ")


# ---- unit: _proximity_bucket ------------------------------------------------

def test_bucket_contiguous_phrase_is_0():
    toks = _match_tokens("redis cache ttl")
    assert _proximity_bucket(toks, "the redis cache ttl is twenty minutes") == 0


def test_bucket_tight_window_any_order_is_1():
    toks = _match_tokens("redis cache ttl")
    # all three within a small window but not in query order
    assert _proximity_bucket(toks, "ttl and cache and redis values") == 1


def test_bucket_scattered_is_2():
    toks = _match_tokens("redis cache ttl")
    filler = " ".join(["x"] * 30)
    assert _proximity_bucket(toks, f"redis {filler} cache {filler} ttl") == 2


def test_bucket_missing_token_is_2():
    toks = _match_tokens("redis cache ttl")
    assert _proximity_bucket(toks, "redis cache only here") == 2


def test_bucket_single_token_is_0_noop():
    # single-token queries must be a no-op (every hit bucket 0) so multi_record
    # (single-token searches) keeps plain BM25 order.
    assert _proximity_bucket(_match_tokens("redis"), "anything at all") == 0
    assert _proximity_bucket(_match_tokens("redis"), "no match here") == 0


# ---- end-to-end: precision + recall -----------------------------------------

def _seed(client):
    # true answer: contiguous phrase buried in a long (BM25-diluted) body
    client.set_entity("c", "true_answer",
                      {"note": LONG + "the redis cache ttl is twenty minutes. " + LONG})
    # scattered decoys: all tokens present, never the contiguous phrase, short
    client.set_entity("c", "decoy_a", {"note": "redis is the broker, the cache uses lru, ttl differs"})
    client.set_entity("c", "decoy_b", {"note": "password reset ttl 15m; cache warm; redis ping ok"})
    client.set_entity("c", "decoy_c", {"note": "ttl semantics, cache eviction, and redis memory reviewed"})


def test_true_answer_outranks_scattered_decoys():
    with tempfile.TemporaryDirectory() as tmp:
        with MemoryClient.local(path=Path(tmp) / "m.db", tier="staker") as client:
            _seed(client)
            hits = client.search("redis cache ttl", limit=20)
            keys = [h["key"] for h in hits]
            assert keys[0] == "true_answer", f"true answer should rank #1, got {keys}"


def test_recall_unchanged_all_rows_returned():
    with tempfile.TemporaryDirectory() as tmp:
        with MemoryClient.local(path=Path(tmp) / "m.db", tier="staker") as client:
            _seed(client)
            hits = client.search("redis cache ttl", limit=20)
            keys = set(h["key"] for h in hits)
            # every seeded row contains all three tokens -> all must still be present
            assert {"true_answer", "decoy_a", "decoy_b", "decoy_c"} <= keys


def test_single_token_order_matches_plain_bm25():
    """Single-token query: proximity is a no-op, so order is pure BM25 (the
    behavior multi_record_search relies on)."""
    with tempfile.TemporaryDirectory() as tmp:
        with MemoryClient.local(path=Path(tmp) / "m.db", tier="staker") as client:
            _seed(client)
            hits = client.search("redis", limit=20)
            # ranks must be non-decreasing (pure FTS5 rank order, untouched)
            ranks = [h["rank"] for h in hits]
            assert ranks == sorted(ranks), f"single-token order should be plain BM25, got {ranks}"


def test_search_entities_also_reranked():
    with tempfile.TemporaryDirectory() as tmp:
        with MemoryClient.local(path=Path(tmp) / "m.db", tier="staker") as client:
            _seed(client)
            ents = client.search_entities("redis cache ttl", limit=20)
            assert ents and ents[0]["name"] == "true_answer", \
                f"true answer should rank #1 in search_entities, got {[e['name'] for e in ents]}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all proximity-rerank tests passed")
