"""Paraphrase zero-hit search fallback (beta deadguy 2026-06-14).

Natural-language queries miss under strict token-AND (+ Porter stem). The public
MemoryClient.search adds a fallback that ONLY fires when the strict search returns
nothing, so it is purely additive: a non-empty strict result is returned untouched
and single-token / prefix queries never trigger it.
"""
from sibyl_memory_client import MemoryClient


def _client(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    c.set_entity("people", "alice", {"note": "billing is handled by alice"})
    return c


def test_strict_paraphrase_miss_is_recovered_by_fallback(tmp_path):
    c = _client(tmp_path)
    # Strict token-AND misses: the doc has none of who/responsible (no stem match).
    assert c._search_strict("who is responsible for the billing", limit=10) == []
    # Public search recovers via the fallback (rarest in-doc token: 'billing').
    hits = c.search("who is responsible for the billing", limit=10)
    assert any(h.get("key") == "alice" for h in hits), hits


def test_nonempty_strict_result_returned_untouched(tmp_path):
    c = _client(tmp_path)
    strict = c._search_strict("billing handled", limit=10)
    assert strict, "expected a strict hit for the no-regression case"
    wrapped = c.search("billing handled", limit=10)
    # Additive: identical to strict whenever strict is non-empty.
    assert [h.get("key") for h in wrapped] == [h.get("key") for h in strict]


def test_single_token_query_does_not_trigger_fallback(tmp_path):
    c = _client(tmp_path)
    # len(tokens) < 2 -> no relaxation; wrapper == strict.
    assert c.search("billing", limit=10) == c._search_strict("billing", limit=10)


def test_total_miss_returns_empty_not_error(tmp_path):
    c = _client(tmp_path)
    # No query token is in the corpus -> fallback exhausts, returns [] cleanly.
    assert c.search("quantum zeppelin chronosynclastic", limit=10) == []
