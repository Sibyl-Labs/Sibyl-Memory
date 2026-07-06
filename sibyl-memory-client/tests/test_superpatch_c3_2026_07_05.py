"""Regression tests for super-patch BUILD UNIT C3 (client.py), 2026-07-05.

Covers the two client.py hardenings from
``memory/research/plugin-hardening-superpatch-plan-2026-07-05.md`` §4 Unit C3:

  * Hardening #9 (subsumes duplicate R15) — the FTS5 query string length was
    unbounded. ``_sanitize_fts5_query`` walks the input char-by-char up to three
    times and expands every token into an ANDed phrase MATCHed across four
    tiers, so a multi-MB / ~200k-token query became a ~200k-term MATCH. The fix
    truncates at ``MAX_QUERY_CHARS`` at the top of the sanitizer.

  * Hardening #16 — ``storage.logical_size_bytes`` returns 0 on any internal
    error, which fail-opened the in-transaction CAP-2 recheck
    (``check_total_local(0)`` trivially passes). The fix makes
    ``_verify_committed_size`` treat a 0 (or an exception) as "measurement
    unavailable" and fall back to the cap gate's own WAL-inclusive
    ``db_size_fn`` before gating, so the cap is still enforced.

Hermetic: reuses ``tests/conftest.py`` (home/env isolation + canonical src on
sys.path). No network, no real user store.
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    MemoryClient,
    Storage,
    TierCache,
    TierVerificationError,
    FREE_TIER_CAP_BYTES,
)
from sibyl_memory_client.client import MAX_QUERY_CHARS, _sanitize_fts5_query


# ======================================================================
# Hardening #9 — FTS5 query length ceiling
# ======================================================================

def test_h9_huge_query_is_truncated_not_expanded() -> None:
    """A multi-MB, ~200k-token query must NOT expand into a 200k-term MATCH."""
    n_tokens = 200_000
    huge = " ".join(f"tok{i}" for i in range(n_tokens))
    assert len(huge) > 1_000_000  # sanity: the raw input is multiple megabytes

    out = _sanitize_fts5_query(huge)

    # Each surviving token is wrapped as a phrase ("tok..."), i.e. two
    # double-quote characters per emitted term. The count must be a tiny
    # fraction of the 200k input tokens, not a 1:1 expansion.
    n_terms = out.count('"') // 2
    assert n_terms < n_tokens
    # Only the first MAX_QUERY_CHARS characters are ever considered, so the
    # emitted term count is bounded well below the ceiling.
    assert n_terms <= MAX_QUERY_CHARS
    # And the produced MATCH expression is bounded overall (a 200k-term
    # expansion would be ~1.6 MB); this stays within a small multiple of the
    # char ceiling instead.
    assert len(out) <= MAX_QUERY_CHARS * 6


def test_h9_single_giant_token_truncated_to_ceiling() -> None:
    """A single token longer than the ceiling collapses to one bounded term."""
    giant = "x" * (MAX_QUERY_CHARS * 3)
    out = _sanitize_fts5_query(giant)
    # Truncated to exactly MAX_QUERY_CHARS chars, then wrapped once as a phrase.
    assert out == '"' + "x" * MAX_QUERY_CHARS + '"'
    assert len(out) == MAX_QUERY_CHARS + 2


def test_h9_normal_queries_unaffected() -> None:
    """Real natural-language queries are far under the ceiling and pass through
    the sanitizer byte-for-byte unchanged."""
    assert _sanitize_fts5_query("auth database cache") == '"auth" "database" "cache"'

    q = "when did the operator approve the vesting grant"
    assert len(q) < MAX_QUERY_CHARS
    assert _sanitize_fts5_query(q) == " ".join(f'"{t}"' for t in q.split())

    # Prefix and phrase modes are equally unaffected for a normal-length query.
    assert _sanitize_fts5_query("proj atl", prefix=True) == "proj atl*"
    assert _sanitize_fts5_query("hello world", as_phrase=True) == '"hello world"'


# ======================================================================
# Hardening #16 — CAP-2 recheck must not fail open on a 0-byte measurement
# ======================================================================

def _offline_check(url, payload, timeout=4.0):
    """A check_fn that always fails: proves the local recheck needs no network."""
    raise TierVerificationError("offline (test)")


def _build_free_client(tmp_path, *, db_size: int):
    """A free-tier MemoryClient whose cap gate reports ``db_size`` bytes."""
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: db_size,
        local_tier_hint="free",
        cache=cache,
        check_fn=_offline_check,
    )
    storage = Storage(str(tmp_path / "memory.db"))
    client = MemoryClient(
        storage=storage,
        tenant_id="alice",
        tier="free",
        account_id="acc-1",
        session_token="sess-1",
        cap_gate=gate,
    )
    return client, storage


# A footprint well past the 4x fail-open ceiling (8 MB) so the local recheck
# must hard-block regardless of any grace/offline concession.
_WAY_OVER_CAP = 50 * 1024 * 1024


def test_h16_zero_would_fail_open_without_the_fallback(tmp_path) -> None:
    """Documents the bug the fix closes: the raw local recheck on a 0-byte
    total passes even for a free account that is massively over cap. This is
    exactly the fail-open ``_verify_committed_size`` must not inherit."""
    client, _ = _build_free_client(tmp_path, db_size=_WAY_OVER_CAP)
    # No raise: 0 <= cap, so the naive recheck silently allows the write.
    client._cap_gate.check_total_local(0)


def test_h16_zero_measurement_still_enforces_cap_via_fallback(tmp_path, monkeypatch) -> None:
    """The headline fix: when logical_size_bytes fails open to 0, the recheck
    still enforces the cap via the gate's db_size_fn fallback, so an over-cap
    write is REJECTED (not silently passed)."""
    client, storage = _build_free_client(tmp_path, db_size=_WAY_OVER_CAP)
    # storage.logical_size_bytes returns 0 on any internal error; simulate it.
    monkeypatch.setattr(storage, "logical_size_bytes", lambda conn: 0)

    with pytest.raises(CapExceededError):
        with storage.transaction() as conn:
            client._verify_committed_size(conn)


def test_h16_fallback_value_governs_under_cap_passes(tmp_path, monkeypatch) -> None:
    """Proves the fix uses the FALLBACK MEASUREMENT (not a blanket
    reject-on-zero): with logical_size_bytes at 0 but db_size_fn comfortably
    under the free cap, the recheck passes cleanly."""
    under_cap = FREE_TIER_CAP_BYTES - 500_000
    client, storage = _build_free_client(tmp_path, db_size=under_cap)
    monkeypatch.setattr(storage, "logical_size_bytes", lambda conn: 0)

    with storage.transaction() as conn:
        client._verify_committed_size(conn)  # must not raise


def test_h16_logical_size_exception_also_falls_back(tmp_path, monkeypatch) -> None:
    """An exception from logical_size_bytes (not just a 0 return) is likewise
    treated as measurement-unavailable and routed through the fallback."""
    client, storage = _build_free_client(tmp_path, db_size=_WAY_OVER_CAP)

    def _boom(conn):
        raise RuntimeError("pragma exploded")

    monkeypatch.setattr(storage, "logical_size_bytes", _boom)

    with pytest.raises(CapExceededError):
        with storage.transaction() as conn:
            client._verify_committed_size(conn)


def test_h16_fallback_helper_edge_cases(tmp_path) -> None:
    """_fallback_committed_size returns a positive size from the real gate and
    None (never raising) for missing / broken / non-positive db_size_fns."""
    client, _ = _build_free_client(tmp_path, db_size=123)
    # Real gate wires db_size_fn -> the account-level aggregate; here 123.
    assert client._fallback_committed_size(client._cap_gate) == 123

    class Fake:
        pass

    # No _db_size_fn attribute at all.
    assert client._fallback_committed_size(Fake()) is None

    # A raising size fn must be swallowed (return None, never propagate).
    def _raises():
        raise RuntimeError("boom")

    broken = Fake()
    broken._db_size_fn = _raises
    assert client._fallback_committed_size(broken) is None

    # A non-positive size is unusable -> None (do not fail open on it either).
    zero = Fake()
    zero._db_size_fn = lambda: 0
    assert client._fallback_committed_size(zero) is None
