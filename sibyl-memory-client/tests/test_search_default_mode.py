"""Regression: default sanitizer mode is AND-of-tokens (v0.4.2+).

Before v0.4.2, multi-word natural-language queries were wrapped as FTS5
phrases: required exact word sequence: so ``search("H&M tops bought")``
returned 0 hits even when the haystack contained all three words. The
LongMemEval 50-Q benchmark on 2026-05-22 surfaced this as the dominant
default-UX gap for Hermes-plugin users.

This test pins the new default behaviour so it can't silently regress.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sibyl_memory_client.client import (  # noqa: E402
    MemoryClient,
    _sanitize_fts5_query,
)


def test_default_mode_tokenizes_and_ANDs():
    """Multi-word query should become AND-of-quoted-tokens, not a phrase."""
    out = _sanitize_fts5_query("H&M tops bought")
    # Each token wrapped as phrase, joined with spaces (implicit AND)
    assert out == '"H" "M" "tops" "bought"', f"got: {out!r}"


def test_single_word_query_is_one_quoted_token():
    out = _sanitize_fts5_query("smoker")
    assert out == '"smoker"', f"got: {out!r}"


def test_explicit_phrase_mode_still_works():
    out = _sanitize_fts5_query("H&M tops bought", as_phrase=True)
    assert out == '"H&M tops bought"', f"got: {out!r}"


def test_prefix_mode_unchanged():
    out = _sanitize_fts5_query("H&M tops bought", prefix=True)
    assert out == "H M tops bought*", f"got: {out!r}"


def test_empty_input_returns_empty():
    assert _sanitize_fts5_query("") == ""
    assert _sanitize_fts5_query("   ") == ""
    assert _sanitize_fts5_query(None) == ""  # type: ignore[arg-type]


def test_all_symbol_input_falls_back_to_phrase():
    # Defensive: if tokenization yields nothing, still emit a safe phrase
    out = _sanitize_fts5_query("!@#$%")
    assert out.startswith('"') and out.endswith('"')


def test_end_to_end_multi_word_recall_against_live_storage():
    """Real SQLite + FTS5: multi-word natural query finds the row."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        with MemoryClient.local(path=db, tier="staker") as client:
            client.set_entity(
                "purchase",
                "h_and_m_tops",
                {"item": "tops", "store": "H&M", "count": 5, "action": "bought"},
            )
            hits = client.search("tops bought H M", limit=10)
            assert len(hits) >= 1, "multi-word natural query should match the entity"
            keys = [(h.get("tier"), h.get("key")) for h in hits]
            assert ("entity", "h_and_m_tops") in keys, f"got: {keys}"


def test_explicit_phrase_mode_requires_exact_sequence():
    """as_phrase=True still requires consecutive-token match."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "memory.db"
        with MemoryClient.local(path=db, tier="staker") as client:
            client.set_entity(
                "movie",
                "inception",
                {"title": "Inception", "director": "Christopher Nolan"},
            )
            # Phrase that exists consecutively
            hits = client.search('"Christopher Nolan"', limit=10)
            assert any(h.get("key") == "inception" for h in hits)


if __name__ == "__main__":
    test_default_mode_tokenizes_and_ANDs()
    test_single_word_query_is_one_quoted_token()
    test_explicit_phrase_mode_still_works()
    test_prefix_mode_unchanged()
    test_empty_input_returns_empty()
    test_all_symbol_input_falls_back_to_phrase()
    test_end_to_end_multi_word_recall_against_live_storage()
    test_explicit_phrase_mode_requires_exact_sequence()
    print("all 8 default-mode regression tests passed")
