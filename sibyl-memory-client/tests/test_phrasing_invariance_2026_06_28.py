"""Phrasing-invariance + short-token + noise-blocklist regression (2026-06-28).

Guards three properties of the zero-hit search fallback:
  1. framing-tolerant recall — natural-language questions retrieve when they
     share content tokens with the stored note (paraphrase fallback);
  2. short-identifier recall — a 2-char identifier like ``q3`` is a valid
     recovery token (CORE-11, client 0.4.15);
  3. short-noise exclusion — function words / contraction tails
     (``us``/``me``/``re``/``ll``/``ve``) never reach the single-token recovery
     step (operator-directed, client 0.4.16), so they cannot trigger a junk
     last-resort match now that the len>=2 floor admits short tokens.

Strict search is intentionally NOT asserted here — it keeps every token and is
unchanged; these guard only the relaxation step.
"""
from sibyl_memory_client import MemoryClient
from sibyl_memory_client.client import _relaxed_query_strings, _SEARCH_STOPWORDS


def _client(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="phr")
    facts = {
        ("people", "alice"): "Alice manages the billing system",
        ("people", "bob"): "Bob runs the deployment pipeline for the backend",
        ("projects", "orion"): "Project Orion ships the mobile wallet in Q3",
        ("ops", "vpn"): "VPN access requires hardware key enrollment",
    }
    for (cat, k), n in facts.items():
        c.set_entity(cat, k, {"note": n})
    return c


def test_question_framing_recovers(tmp_path):
    c = _client(tmp_path)
    for q, key in [
        ("who manages billing", "alice"),
        ("what is the pipeline for the backend", "bob"),
        ("how do i get vpn access", "vpn"),
    ]:
        assert any(h.get("key") == key for h in c.search(q, limit=10)), q


def test_short_identifier_recovers(tmp_path):
    c = _client(tmp_path)
    # strict misses (no "whats"/"launching"); the q3 token recovers it.
    assert c._search_strict("whats launching in Q3", limit=10) == []
    assert any(h.get("key") == "orion" for h in c.search("whats launching in Q3", limit=10))


def test_short_noise_words_excluded_from_fallback(tmp_path):
    for w in ("us", "me", "am", "re", "ll", "ve"):
        assert w in _SEARCH_STOPWORDS, w
    # "us" must not survive into the relaxed variants; recovery keys on owes/money.
    variants = list(_relaxed_query_strings("who owes us money"))
    assert "us" not in variants, variants
    assert variants == ["owes money", "money", "owes"], variants


def test_out_of_contract_returns_nothing(tmp_path):
    c = _client(tmp_path)
    # no shared content token with any note -> correctly empty (no distractor).
    for q in ["who does our pentest", "chargeback timeline", "remote network login"]:
        assert c.search(q, limit=10) == [], (q, c.search(q, limit=10))
