"""Regression: a negative ``limit`` must never broaden search results.

SQLite treats ``LIMIT -1`` as unbounded, so passing ``limit=-1`` previously
returned MORE rows, not fewer. Both ``search`` and ``search_entities`` now clamp
with ``max(0, limit)``. Source: adversarial QA finding
SEARCH-NEGATIVE-LIMIT-CANNOT-BROADEN-RESULTS (2026-06-01).
"""
from sibyl_memory_client import MemoryClient


def _seed(tmp_path):
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    for i in range(6):
        c.set_entity("notes", f"item-{i}", {"text": "alpha beta gamma token"})
    return c


def test_search_negative_limit_does_not_broaden(tmp_path):
    c = _seed(tmp_path)
    bounded = c.search("token", limit=2)
    negative = c.search("token", limit=-1)
    # negative must never return MORE than a small positive limit, and must not
    # fall through to SQLite's unbounded LIMIT -1.
    assert len(negative) <= len(bounded)
    assert len(negative) == 0


def test_search_entities_negative_limit_does_not_broaden(tmp_path):
    c = _seed(tmp_path)
    assert c.search_entities("token", limit=-1) == []
