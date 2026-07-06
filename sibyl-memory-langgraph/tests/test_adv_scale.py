"""Adversarial scale / pagination / concurrency tests for SibylStore.

Lane: scale
Target: data completeness at the _POOL enumeration bound, pagination math,
multi-instance cross-visibility, WAL concurrency, resource leaks, max_depth
edge cases, and Cap/Validation error propagation.

History: the original adversarial pass found SILENT DATA LOSS at _POOL=1000
(browse / list_namespaces / _categories_under all truncated at 1000 with no
signal). store.py was then fixed:
  * _POOL raised 1000 -> 10_000.
  * All three paths route through _list_capped(), which LOGS A WARNING when the
    result hits the cap (no longer fully silent).
  * Negative max_depth now raises ValueError.

These tests now assert the CORRECTED behavior:
  * POSITIVE regression guards: at 1500 entities / 1100 namespaces (< cap),
    everything is returned, nothing dropped.
  * ONE xfail (strict=False) documents the RESIDUAL truncation above 10_000
    (client MAX_LIMIT + no cursor — an architectural limit, not silent).
  * A caplog test proves the warning fires at the cap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from typing import Any

import pytest

from sibyl_memory_client import CapExceededError, ValidationError as SibylValidationError
from sibyl_memory_langgraph import SibylStore
from sibyl_memory_langgraph.store import _POOL  # bound under test (10_000 post-fix)
from langgraph.store.base import (
    GetOp,
    ListNamespacesOp,
    MatchCondition,
    PutOp,
    SearchOp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "t.db")


def _new_store(path: str | None = None) -> SibylStore:
    if path is None:
        path = _fresh_path()
    return SibylStore(path=path, tier="free")


def _put_op(ns: tuple, key: str, value: dict) -> PutOp:
    return PutOp(namespace=ns, key=key, value=value, index=None, ttl=None)


def _seed_many(store: SibylStore, ns: tuple, n: int, *, batch_size: int = 500) -> None:
    """Insert n entities under one namespace via batched put ops (fast)."""
    ops: list[PutOp] = []
    for i in range(n):
        ops.append(_put_op(ns, f"k{i:06d}", {"n": i}))
        if len(ops) >= batch_size:
            store.batch(ops)
            ops = []
    if ops:
        store.batch(ops)


def _search_op(prefix, *, query=None, filter=None, limit=10, offset=0) -> SearchOp:
    return SearchOp(
        namespace_prefix=prefix,
        filter=filter,
        limit=limit,
        offset=offset,
        query=query,
        refresh_ttl=True,
    )


def _ls_op(
    *,
    max_depth=None,
    limit=100,
    offset=0,
    prefix=None,
    suffix=None,
) -> ListNamespacesOp:
    conditions: list[MatchCondition] = []
    if prefix is not None:
        conditions.append(MatchCondition(match_type="prefix", path=prefix))
    if suffix is not None:
        conditions.append(MatchCondition(match_type="suffix", path=suffix))
    return ListNamespacesOp(
        match_conditions=tuple(conditions) if conditions else None,
        max_depth=max_depth,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# 1.  Browse completeness below the _POOL cap (regression guard for fix #1)
# ---------------------------------------------------------------------------

class TestBrowseFullResultsBelowCap:
    """REGRESSION GUARD (was: HIGH silent data loss at _POOL=1000).

    After the fix (_POOL=10_000), browsing 1500 entities in one namespace
    returns ALL 1500 — no truncation below the cap, no dropped rows.
    """

    N = 1500   # < _POOL (10_000)
    NS = ("pool-browse",)

    @pytest.fixture(autouse=True)
    def _setup(self):
        assert _POOL > self.N, f"test assumes N({self.N}) < _POOL({_POOL})"
        path = _fresh_path()
        self.store = _new_store(path)
        _seed_many(self.store, self.NS, self.N)
        yield
        self.store.close()

    def test_browse_returns_all_entities_below_cap(self):
        """Browse (query=None) returns every entity when count < _POOL."""
        results = self.store.search(self.NS, limit=self.N + 500)
        actual = len(results)
        assert actual == self.N, (
            f"REGRESSION: browse at {self.N} (< _POOL={_POOL}) must return all; "
            f"got {actual} (dropped {self.N - actual})."
        )

    def test_pagination_below_cap_is_correct(self):
        """offset=1000 on 1500 items now returns the remaining 500 (no dead zone)."""
        page_after_1000 = self.store.search(self.NS, limit=self.N, offset=1000)
        assert len(page_after_1000) == self.N - 1000, (
            f"offset=1000 on {self.N} items should return {self.N - 1000}; "
            f"got {len(page_after_1000)} (this used to be a permanent dead zone)."
        )
        full = self.store.search(self.NS, limit=self.N + 500)
        assert len(full) == self.N

    def test_full_pagination_union_covers_everything(self):
        """Stepping through pages of 250 must reach exactly N distinct keys."""
        page_size = 250
        seen: set[str] = set()
        off = 0
        while True:
            page = self.store.search(self.NS, limit=page_size, offset=off)
            if not page:
                break
            seen.update(item.key for item in page)
            off += page_size
            if off > self.N + page_size:
                break
        assert len(seen) == self.N, (
            f"Paginated union covers {len(seen)} keys, expected {self.N}."
        )

    def test_subtree_browse_returns_all_below_cap(self):
        """Subtree browse via a parent prefix also returns all 1500."""
        path = _fresh_path()
        s = _new_store(path)
        try:
            _seed_many(s, ("tree", "u1"), self.N)
            results = s.search(("tree",), limit=self.N + 500)
            assert len(results) == self.N, (
                f"REGRESSION: subtree browse must return all {self.N}; "
                f"got {len(results)}."
            )
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 2.  list_namespaces completeness below the cap (regression guard for fix #2)
# ---------------------------------------------------------------------------

class TestListNamespacesFullResultsBelowCap:
    """REGRESSION GUARD (was: HIGH silent data loss at _POOL=1000).

    1100 distinct namespaces (< _POOL) are ALL present in list_namespaces().
    """

    N_NS = 1100   # < _POOL

    @pytest.fixture(autouse=True)
    def _setup(self):
        assert _POOL > self.N_NS, f"test assumes N_NS({self.N_NS}) < _POOL({_POOL})"
        path = _fresh_path()
        self.store = _new_store(path)
        ops = [_put_op(("ns", f"s{i:04d}"), "k", {"n": i}) for i in range(self.N_NS)]
        # batch in chunks for speed
        for start in range(0, len(ops), 500):
            self.store.batch(ops[start:start + 500])
        yield
        self.store.close()

    def test_list_namespaces_returns_all_below_cap(self):
        result = self.store.list_namespaces(limit=self.N_NS + 200)
        actual = len(result)
        assert actual == self.N_NS, (
            f"REGRESSION: {self.N_NS} distinct namespaces (< _POOL={_POOL}) must "
            f"all appear; got {actual} (dropped {self.N_NS - actual})."
        )

    def test_list_namespaces_paginated_union_complete(self):
        seen: set[tuple] = set()
        for off in range(0, self.N_NS + 250, 250):
            page = self.store.list_namespaces(limit=250, offset=off)
            if not page:
                break
            seen.update(page)
        assert len(seen) == self.N_NS, (
            f"Paginated list_namespaces union has {len(seen)}, expected {self.N_NS}."
        )


# ---------------------------------------------------------------------------
# 3.  FTS _categories_under completeness below the cap (regression guard #3)
# ---------------------------------------------------------------------------

class TestFTSCategoriesFullBelowCap:
    """REGRESSION GUARD (was: MEDIUM silent data loss — oldest categories evicted
    from the 1000-row pool, FTS returned 0 instead of 50).

    With _POOL=10_000 and 1050 total categories, the keyword entities inserted
    FIRST (the oldest) are still inside the pool, so FTS finds all 50.
    """

    def test_fts_finds_all_matching_categories_below_cap(self):
        path = _fresh_path()
        s = _new_store(path)
        try:
            # 50 keyword entities FIRST (the oldest) ...
            for i in range(50):
                s.put((f"cat{i:04d}",), "k", {"text": "zzzunique"})
            # ... then 1000 ordinary ones. Total 1050 << _POOL, so nothing evicted.
            for i in range(50, 1050):
                s.put((f"cat{i:04d}",), "k", {"text": "ordinary"})

            results = s.search((), query="zzzunique", limit=100)
            assert len(results) == 50, (
                f"REGRESSION: FTS should find all 50 'zzzunique' categories "
                f"(1050 total < _POOL={_POOL}); got {len(results)}. The oldest "
                f"categories used to be evicted from the 1000-row pool."
            )
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 4.  Residual truncation ABOVE the cap + warning signal
#
# NOTE on methodology: the 2 MB free-tier cap makes >10_000 real rows
# IMPOSSIBLE to insert (the cap is on the true DB footprint, page_count *
# page_size, which is exhausted at ~3,900 entities even with empty bodies; a
# paid/uncapped tier needs offline-unavailable server verification). So the
# only feasible way to exercise the adapter's >_POOL behavior on the free tier
# is to stub the client's data source (list_entities) while keeping ALL the
# real adapter code: _list_capped()'s cap detection + warning, the prefix
# filter, and the offset/limit slicing. This isolates exactly the adapter logic
# the fix changed.
# ---------------------------------------------------------------------------

def _synthetic_rows(n: int, category: str = "big") -> list[dict[str, Any]]:
    """n entity rows shaped like the real client's list_entities output."""
    return [
        {
            "id": f"id{i:07d}",
            "tenant_id": "t",
            "category": category,
            "name": f"k{i:07d}",
            "status": None,
            "body": {"n": i},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        for i in range(n)
    ]


def _stub_list_entities(store: SibylStore, rows: list[dict[str, Any]]) -> None:
    """Replace store._client.list_entities with one that clamps to `limit`,
    exactly like the real client's MAX_LIMIT clamp (_clamp_limit -> 10_000).
    """
    def fake(category: str | None = None, *, status: str | None = None, limit: int = 100):
        scoped = rows if category is None else [r for r in rows if r["category"] == category]
        return scoped[:limit]  # mimic the client clamp; >limit rows are dropped here
    store._client.list_entities = fake  # type: ignore[attr-defined]


@pytest.fixture()
def store_over_pool():
    """Real SibylStore whose data source reports _POOL+50 rows in one namespace."""
    n = _POOL + 50
    path = _fresh_path()
    s = _new_store(path)
    _stub_list_entities(s, _synthetic_rows(n, category="big"))
    try:
        yield s, n
    finally:
        s.close()


@pytest.mark.xfail(
    reason="architectural: client MAX_LIMIT=10_000 + no cursor; full fix needs a "
    "client-side enumeration API — pending operator decision",
    strict=False,
)
def test_browse_over_pool_still_truncates(store_over_pool):
    """RESIDUAL LIMIT — above _POOL the browse pool is still bounded.

    With _POOL+50 rows available, browsing returns only _POOL. This is no longer
    SILENT (a warning is logged — see test_enumeration_warns_at_cap), but the
    LangGraph return type carries no has_more flag, so rows past the cap are
    unreachable in one pass. xfail(strict=False): documents the residual hole.
    """
    s, n = store_over_pool
    results = s.search(("big",), limit=n + 1000)
    assert len(results) == n, (
        f"residual truncation: {n} rows available (> _POOL={_POOL}), "
        f"browse returned {len(results)}; {n - len(results)} unreachable in one pass."
    )


def test_enumeration_warns_at_cap(store_over_pool, caplog):
    """The fix's key improvement: hitting the enumeration cap LOGS A WARNING
    (no longer fully silent), even though the return type has no has_more flag.
    """
    s, _n = store_over_pool
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sibyl_memory_langgraph.store"):
        s.search(("big",), limit=10)  # browse path -> _list_capped() hits the cap
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned, (
        "Expected a WARNING when enumeration hits the _POOL cap; none logged. "
        "The fix is supposed to make truncation non-silent."
    )
    joined = " ".join(r.getMessage().lower() for r in warned)
    assert "cap" in joined or str(_POOL) in joined, (
        f"Warning fired but did not mention the cap: {[r.getMessage() for r in warned]}"
    )


def test_list_namespaces_warns_at_cap(store_over_pool, caplog):
    """list_namespaces shares the _list_capped() path, so it warns at the cap too."""
    s, _n = store_over_pool
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sibyl_memory_langgraph.store"):
        s.list_namespaces(limit=10)
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected a WARNING from list_namespaces when enumeration hits the cap."
    )


def test_below_pool_does_not_warn(caplog):
    """Negative control: at well under _POOL rows, NO cap warning is logged."""
    path = _fresh_path()
    s = _new_store(path)
    try:
        _stub_list_entities(s, _synthetic_rows(100, category="big"))
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="sibyl_memory_langgraph.store"):
            s.search(("big",), limit=10)
            s.list_namespaces(limit=10)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "A cap warning fired below _POOL — the cap detection is too eager."
        )
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 5.  Pagination math edge cases (unchanged — all passing)
# ---------------------------------------------------------------------------

class TestPaginationEdgeCases:

    N = 12  # small, deterministic

    @pytest.fixture()
    def store_with_data(self):
        path = _fresh_path()
        s = _new_store(path)
        ns = ("pag",)
        ops = [_put_op(ns, f"k{i:02d}", {"n": i}) for i in range(self.N)]
        s.batch(ops)
        try:
            yield s
        finally:
            s.close()

    def test_limit_zero_returns_empty_not_crash(self, store_with_data):
        """limit=0 must not crash; must return empty list."""
        try:
            results = store_with_data.search(("pag",), limit=0, offset=0)
            assert len(results) == 0, f"limit=0 returned {len(results)} items"
        except (ValueError, TypeError):
            pass  # also acceptable

    def test_limit_larger_than_total_returns_all(self, store_with_data):
        """limit >> N should return exactly N items without error."""
        results = store_with_data.search(("pag",), limit=100_000, offset=0)
        assert len(results) == self.N, (
            f"limit=100000 returned {len(results)}, expected {self.N}"
        )

    def test_offset_beyond_end_returns_empty(self, store_with_data):
        """offset > N should return empty without wrapping or crashing."""
        results = store_with_data.search(("pag",), limit=10, offset=self.N + 100)
        assert len(results) == 0, (
            f"offset past end returned {len(results)} items (expected 0)"
        )

    def test_offset_exactly_at_end_returns_empty(self, store_with_data):
        """offset == N (one past last item) should return empty."""
        results = store_with_data.search(("pag",), limit=10, offset=self.N)
        assert len(results) == 0

    def test_negative_limit_does_not_return_unbounded_set(self, store_with_data):
        """Negative limit must not silently return all rows (unbounded scan).

        Python slice [::-1] with a negative limit could theoretically reverse
        or do surprising things; the client clamps negative limits.  We verify
        the result is not larger than N and no unchecked exception escapes.
        """
        try:
            results = store_with_data.search(("pag",), limit=-1, offset=0)
            assert len(results) <= self.N, (
                f"Negative limit=-1 returned {len(results)} items > N={self.N} — "
                f"possible unbounded scan."
            )
        except (ValueError, TypeError):
            pass  # ideal: reject negative limits explicitly

    def test_negative_offset_does_not_wrap_or_crash(self, store_with_data):
        """Negative offset — Python slicing wraps around; should raise or return <=N items.

        `rows[-2:-2+limit]` for large lists returns wrong results (not a
        tail-anchor window).  We assert no crash and result count <= N.
        """
        try:
            results = store_with_data.search(("pag",), limit=5, offset=-1)
            # Document actual count — likely 0 or 1 (wrong), never an error
            assert len(results) <= self.N, (
                f"Negative offset=-1 returned {len(results)} items > N"
            )
        except (ValueError, TypeError):
            pass  # ideal: reject negative offsets

    def test_full_pagination_covers_all_items(self, store_with_data):
        """Step through all pages; union must equal the full item set."""
        page_size = 5
        seen_keys: set[str] = set()
        off = 0
        while True:
            page = store_with_data.search(("pag",), limit=page_size, offset=off)
            if not page:
                break
            for item in page:
                seen_keys.add(item.key)
            off += page_size
            if off > self.N + page_size:
                break

        assert len(seen_keys) == self.N, (
            f"Paginated union covers {len(seen_keys)} items, expected {self.N}. "
            f"Possible off-by-one or pagination gap."
        )

    def test_list_namespaces_offset_beyond_end_returns_empty(self):
        """list_namespaces: offset past end returns empty without wrapping."""
        path = _fresh_path()
        s = _new_store(path)
        try:
            for i in range(5):
                s.put((f"ns{i}",), "k", {"n": i})
            result = s.list_namespaces(limit=10, offset=1000)
            assert len(result) == 0, (
                f"offset=1000 (well past 5 namespaces) returned {len(result)} items"
            )
        finally:
            s.close()

    def test_list_namespaces_paginated_union_is_complete(self):
        """Paginating through list_namespaces must cover every namespace."""
        path = _fresh_path()
        s = _new_store(path)
        try:
            for i in range(20):
                s.put((f"ns{i:02d}",), "k", {"n": i})
            seen: set[tuple] = set()
            for off in range(0, 25, 5):
                page = s.list_namespaces(limit=5, offset=off)
                seen.update(page)
            assert len(seen) == 20, (
                f"Paginated list_namespaces union has {len(seen)} namespaces, expected 20."
            )
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 6.  max_depth edge cases
# ---------------------------------------------------------------------------

class TestMaxDepthEdgeCases:

    def test_max_depth_zero_truncates_to_empty_tuple(self):
        """max_depth=0 truncates every namespace to () — should produce [()]
        after dedup, or nothing.  Must not crash with IndexError.
        """
        path = _fresh_path()
        s = _new_store(path)
        try:
            s.put(("a", "b"), "k", {"x": 1})
            s.put(("c",), "k", {"x": 2})
            try:
                result = s.list_namespaces(max_depth=0)
                # All namespaces truncate to (); deduplication leaves [()]
                for ns in result:
                    assert len(ns) == 0, f"max_depth=0 gave non-empty tuple: {ns}"
            except (ValueError, TypeError, IndexError) as e:
                pytest.fail(f"max_depth=0 raised unexpected exception: {type(e).__name__}: {e}")
        finally:
            s.close()

    def test_max_depth_exceeds_deepest_namespace_returns_full(self):
        """max_depth >> deepest depth: ns[:very_large] = ns — no padding, no crash."""
        path = _fresh_path()
        s = _new_store(path)
        try:
            s.put(("a",), "k", {"x": 1})
            s.put(("a", "b", "c"), "k", {"x": 2})
            result = s.list_namespaces(max_depth=999)
            ns_set = set(result)
            assert ("a",) in ns_set
            assert ("a", "b", "c") in ns_set
        finally:
            s.close()

    def test_negative_max_depth_raises_value_error(self):
        """FIXED (#4): negative max_depth now raises ValueError instead of
        silently truncating the last namespace element via Python's ns[:-1].
        """
        path = _fresh_path()
        s = _new_store(path)
        try:
            s.put(("a", "b", "c"), "k", {"x": 1})
            with pytest.raises(ValueError):
                s.list_namespaces(max_depth=-1)
            # a more-negative value must also raise
            with pytest.raises(ValueError):
                s.list_namespaces(max_depth=-5)
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 7.  Multi-instance cross-visibility (same DB, WAL)
# ---------------------------------------------------------------------------

class TestMultiInstance:
    """Two SibylStore objects on the same file path.  WAL mode means readers
    never block writers and commits are visible immediately.
    """

    @pytest.fixture()
    def shared_path(self) -> str:
        return _fresh_path()

    def test_write_on_instance1_visible_to_instance2(self, shared_path):
        s1 = _new_store(shared_path)
        s2 = _new_store(shared_path)
        try:
            s1.put(("shared",), "k1", {"msg": "from s1"})
            item = s2.get(("shared",), "k1")
            assert item is not None, "Instance 2 could not see write from instance 1"
            assert item.value == {"msg": "from s1"}
        finally:
            s1.close(); s2.close()

    def test_delete_on_instance1_visible_to_instance2(self, shared_path):
        s1 = _new_store(shared_path)
        s2 = _new_store(shared_path)
        try:
            s1.put(("shared",), "k1", {"x": 1})
            assert s2.get(("shared",), "k1") is not None
            s1.delete(("shared",), "k1")
            assert s2.get(("shared",), "k1") is None, (
                "Instance 2 still sees item deleted by instance 1"
            )
        finally:
            s1.close(); s2.close()

    def test_overwrite_on_instance1_not_stale_on_instance2(self, shared_path):
        s1 = _new_store(shared_path)
        s2 = _new_store(shared_path)
        try:
            s1.put(("shared",), "k1", {"v": 1})
            s1.put(("shared",), "k1", {"v": 2})
            item = s2.get(("shared",), "k1")
            assert item is not None
            assert item.value == {"v": 2}, (
                f"Instance 2 got stale value {item.value!r}, expected {{'v': 2}}"
            )
        finally:
            s1.close(); s2.close()

    def test_concurrent_writes_both_instances_no_corruption(self, shared_path):
        """50 writes from each instance concurrently — WAL + busy_timeout=5000ms
        should prevent data corruption or deadlock.
        """
        s1 = _new_store(shared_path)
        s2 = _new_store(shared_path)
        errors: list[str] = []

        def write_s1():
            for i in range(50):
                try:
                    s1.put(("conc",), f"s1_{i:02d}", {"v": i})
                except Exception as e:
                    errors.append(f"s1 write {i}: {type(e).__name__}: {e}")

        def write_s2():
            for i in range(50):
                try:
                    s2.put(("conc",), f"s2_{i:02d}", {"v": i})
                except Exception as e:
                    errors.append(f"s2 write {i}: {type(e).__name__}: {e}")

        t1 = threading.Thread(target=write_s1, daemon=True)
        t2 = threading.Thread(target=write_s2, daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)

        try:
            assert not errors, f"Concurrent writes from two instances produced errors:\n" + "\n".join(errors)
            total = s1.search(("conc",), limit=200)
            assert len(total) == 100, (
                f"Expected 100 items after concurrent writes from 2 instances, got {len(total)}"
            )
        finally:
            s1.close(); s2.close()

    def test_concurrent_abatch_same_store_no_deadlock(self, shared_path):
        """Multiple asyncio tasks calling abatch() on the same store instance."""
        s = _new_store(shared_path)
        try:
            for i in range(10):
                s.put(("ab",), f"k{i}", {"n": i})

            async def run():
                op = SearchOp(
                    namespace_prefix=("ab",),
                    filter=None,
                    limit=10,
                    offset=0,
                    query=None,
                    refresh_ttl=True,
                )
                tasks = [s.abatch([op]) for _ in range(8)]
                return await asyncio.gather(*tasks)

            results = asyncio.run(run())
            assert len(results) == 8
            for r in results:
                assert len(r[0]) == 10
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 8.  Resource / connection leaks
# ---------------------------------------------------------------------------

def test_open_close_many_stores_no_exception():
    """Open and close 60 separate SibylStore instances — each should open
    cleanly and release without OS errors.  Uses a shared DB to exercise
    WAL contention in the open/close cycle.
    """
    path = _fresh_path()
    # Seed some data
    s0 = _new_store(path)
    s0.put(("leak",), "k", {"x": 1})
    s0.close()

    for i in range(60):
        s = _new_store(path)
        try:
            item = s.get(("leak",), "k")
            assert item is not None, f"Iteration {i}: data lost after open"
        finally:
            s.close()

    # Final sanity: one more open should still work
    s_final = _new_store(path)
    try:
        assert s_final.get(("leak",), "k") is not None
    finally:
        s_final.close()


def test_abatch_worker_threads_close_cleanly():
    """abatch() dispatches to run_in_executor (thread pool).  Many sequential
    abatch calls must not exhaust file descriptors or leave zombie threads.
    """
    path = _fresh_path()
    s = _new_store(path)
    try:
        for i in range(5):
            s.put(("ab",), f"k{i}", {"n": i})

        async def run_many():
            op = SearchOp(
                namespace_prefix=("ab",),
                filter=None, limit=5, offset=0,
                query=None, refresh_ttl=True,
            )
            for _ in range(20):
                await s.abatch([op])

        asyncio.run(run_many())
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 9.  Cap / Validation error surface
# ---------------------------------------------------------------------------

def test_cap_exceeded_error_propagates_not_swallowed():
    """Writes past the 2MB free-tier cap must raise CapExceededError — not
    silently succeed, not crash with an internal SQLite or StorageError, and
    not corrupt the DB (existing data must still be readable after the error).
    """
    path = _fresh_path()
    s = _new_store(path)
    try:
        # ~250 KB per write; 2MB / 250KB ≈ 8 writes before cap.
        # We allow up to 12 writes and assert cap is hit before 12.
        payload = {"data": "x" * 250_000}
        cap_hit_at: int | None = None

        for i in range(12):
            try:
                s.put(("cap",), f"large{i}", payload)
            except CapExceededError:
                cap_hit_at = i
                break
            except Exception as e:
                pytest.fail(
                    f"Unexpected exception type at write {i}: "
                    f"{type(e).__name__}: {e}"
                )

        assert cap_hit_at is not None, (
            "Expected CapExceededError before 12 × 250KB writes (3MB > 2MB cap). "
            "Cap may not be enforced, or the error is swallowed inside SibylStore."
        )

        # Existing data must survive the cap hit
        item = s.get(("cap",), "large0")
        assert item is not None, (
            "Entity written before cap hit is gone after CapExceededError — "
            "possible DB corruption."
        )
    finally:
        s.close()


def test_single_value_over_per_value_limit_raises():
    """A single value exceeding the per-value 1024 KB limit raises SibylValidationError.

    The adapter has two independent size gates:
      - Per-value: 1024 KB max body → ValidationError
      - Total DB: 2 MB free-tier → CapExceededError

    A 1.5MB value hits the per-value limit first and must raise ValidationError
    (not crash silently, not corrupt the DB).
    """
    path = _fresh_path()
    s = _new_store(path)
    try:
        # 1.5 MB — above the 1024 KB per-value limit, below the 2MB DB cap
        large_payload = {"data": "z" * 1_500_000}
        with pytest.raises(SibylValidationError):
            s.put(("huge",), "single", large_payload)
        # DB must still be usable after the rejection
        s.put(("huge",), "small_ok", {"ok": True})
        assert s.get(("huge",), "small_ok") is not None
    finally:
        s.close()


def test_cap_exceeded_then_delete_allows_new_write():
    """After hitting the cap, deleting items should allow new writes to succeed
    (cap gate re-evaluates committed size, not a latching error).
    """
    path = _fresh_path()
    s = _new_store(path)
    try:
        payload = {"data": "x" * 250_000}
        keys_written = []

        # Fill to cap
        for i in range(12):
            try:
                s.put(("cap2",), f"k{i}", payload)
                keys_written.append(f"k{i}")
            except CapExceededError:
                break

        assert keys_written, "Should have written at least one entity before cap"

        # Delete all written keys to free up space
        for k in keys_written:
            s.delete(("cap2",), k)

        # Now a new write should succeed (cap freed)
        try:
            s.put(("cap2",), "new_after_delete", {"small": "value"})
        except CapExceededError:
            pytest.fail(
                "CapExceededError after deleting all prior entities — "
                "cap gate does not re-evaluate freed space."
            )
    finally:
        s.close()
