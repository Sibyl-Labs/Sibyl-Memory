"""Rigorous coverage of SibylStore's ASYNC API + CONCURRENCY.

Dimension: async (abatch + a* methods) and concurrency.

pytest-asyncio is NOT installed. Every test is a plain function that drives an
inner coroutine via ``asyncio.run(...)``. Each test gets an isolated on-disk DB.

What the contract says (intentional, NOT bugs):
  * a* methods (aget/aput/adelete/asearch/alist_namespaces) dispatch to abatch().
  * abatch() offloads the synchronous SQLite batch() to a thread-pool executor.
  * search() is lexical (FTS5); index/ttl ignored; namespace rules as elsewhere.

Watch: SQLite cross-thread connection errors. abatch offloads to a thread pool,
so repeated + gathered async ops are exercised to surface any
"SQLite objects created in a thread can only be used in another thread".
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from langgraph.store.base import (
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)
from sibyl_memory_langgraph import SibylStore


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mkstore() -> SibylStore:
    """Fresh isolated on-disk store (own temp dir => own SQLite file)."""
    d = tempfile.mkdtemp()
    return SibylStore(path=os.path.join(d, "t.db"), tier="free")


def _run(coro):
    """Drive a coroutine on a fresh event loop (no pytest-asyncio)."""
    return asyncio.run(coro)


def _kv(item):
    """Comparable projection of an Item/SearchItem ignoring timestamps."""
    if item is None:
        return None
    return (tuple(item.namespace), item.key, item.value)


# --------------------------------------------------------------------------- #
# 1. a* methods each work and match their sync equivalents (parity)
# --------------------------------------------------------------------------- #
def test_aput_aget_basic():
    store = _mkstore()
    try:
        async def main():
            await store.aput(("memories", "u1"), "fact1",
                             {"text": "operator prefers dark mode", "kind": "pref"})
            it = await store.aget(("memories", "u1"), "fact1")
            assert it is not None
            assert isinstance(it, Item)
            assert it.value == {"text": "operator prefers dark mode", "kind": "pref"}
            assert tuple(it.namespace) == ("memories", "u1")
            assert it.key == "fact1"
            assert it.created_at is not None and it.updated_at is not None
        _run(main())
    finally:
        store.close()


def test_aget_missing_returns_none():
    store = _mkstore()
    try:
        async def main():
            assert await store.aget(("memories", "u1"), "nope") is None
        _run(main())
    finally:
        store.close()


def test_aput_get_parity_sync_writes_async_reads():
    """An item written synchronously is observably identical when read async."""
    store = _mkstore()
    try:
        store.put(("ns", "a"), "k", {"v": 1, "text": "hello world"})

        async def main():
            return await store.aget(("ns", "a"), "k")
        async_item = _run(main())
        sync_item = store.get(("ns", "a"), "k")
        assert _kv(async_item) == _kv(sync_item)
        assert _kv(async_item) == (("ns", "a"), "k", {"v": 1, "text": "hello world"})
    finally:
        store.close()


def test_aput_get_parity_async_writes_sync_reads():
    """An item written asynchronously is observably identical when read sync."""
    store = _mkstore()
    try:
        async def main():
            await store.aput(("ns", "b"), "k", {"v": 2, "text": "second item"})
        _run(main())
        sync_item = store.get(("ns", "b"), "k")
        assert _kv(sync_item) == (("ns", "b"), "k", {"v": 2, "text": "second item"})
    finally:
        store.close()


def test_aput_overwrite():
    store = _mkstore()
    try:
        async def main():
            await store.aput(("ns", "o"), "k", {"text": "dark mode"})
            await store.aput(("ns", "o"), "k", {"text": "light mode"})
            it = await store.aget(("ns", "o"), "k")
            assert it.value["text"] == "light mode"
        _run(main())
    finally:
        store.close()


def test_adelete_and_parity_with_sync_delete():
    store = _mkstore()
    try:
        async def main():
            await store.aput(("ns", "d"), "k1", {"text": "alpha"})
            await store.aput(("ns", "d"), "k2", {"text": "beta"})
            # adelete (dispatches to abatch -> PutOp(value=None))
            await store.adelete(("ns", "d"), "k1")
            assert await store.aget(("ns", "d"), "k1") is None
            # sync delete still present-parity: k2 removed via sync, observed async
            store.delete(("ns", "d"), "k2")
            assert await store.aget(("ns", "d"), "k2") is None
        _run(main())
    finally:
        store.close()


def test_aput_none_value_deletes():
    """aput(value=None) deletes (mirrors sync semantics)."""
    store = _mkstore()
    try:
        async def main():
            await store.aput(("ns", "n"), "k", {"text": "to be removed"})
            assert await store.aget(("ns", "n"), "k") is not None
            await store.aput(("ns", "n"), "k", None)
            assert await store.aget(("ns", "n"), "k") is None
        _run(main())
    finally:
        store.close()


def test_asearch_parity_with_sync():
    store = _mkstore()
    try:
        store.put(("memories", "u1"), "f1", {"text": "operator prefers dark mode", "kind": "pref"})
        store.put(("memories", "u1"), "f2", {"text": "billing handled by stripe", "kind": "ops"})
        store.put(("memories", "u2"), "f1", {"text": "another dark theme note", "kind": "pref"})

        async def main():
            a_exact = await store.asearch(("memories", "u1"), query="stripe")
            a_subtree = await store.asearch(("memories",), query="dark")
            a_filter = await store.asearch(("memories", "u1"), filter={"kind": "ops"})
            a_browse = await store.asearch(("memories", "u1"))
            return a_exact, a_subtree, a_filter, a_browse
        a_exact, a_subtree, a_filter, a_browse = _run(main())

        s_exact = store.search(("memories", "u1"), query="stripe")
        s_subtree = store.search(("memories",), query="dark")
        s_filter = store.search(("memories", "u1"), filter={"kind": "ops"})
        s_browse = store.search(("memories", "u1"))

        def proj(hits):
            return sorted((tuple(h.namespace), h.key, tuple(sorted(h.value.items())))
                          for h in hits)

        # each async hit is a SearchItem
        for h in a_exact + a_subtree + a_filter + a_browse:
            assert isinstance(h, SearchItem)

        assert proj(a_exact) == proj(s_exact)
        assert proj(a_subtree) == proj(s_subtree)
        assert proj(a_filter) == proj(s_filter)
        assert proj(a_browse) == proj(s_browse)

        # content sanity
        assert any(h.key == "f2" for h in a_exact)
        assert all(tuple(h.namespace) == ("memories", "u1") for h in a_exact)
        assert {tuple(h.namespace) for h in a_subtree} >= {("memories", "u1"), ("memories", "u2")}
        assert all(h.value.get("kind") == "ops" for h in a_filter) and len(a_filter) >= 1
        assert len(a_browse) == 2
    finally:
        store.close()


def test_alist_namespaces_parity_with_sync():
    store = _mkstore()
    try:
        store.put(("memories", "u1"), "f1", {"x": 1})
        store.put(("memories", "u2"), "f1", {"x": 2})
        store.put(("notes", "u1"), "f1", {"x": 3})

        async def main():
            full = await store.alist_namespaces()
            depth1 = await store.alist_namespaces(max_depth=1)
            return full, depth1
        a_full, a_depth1 = _run(main())

        assert sorted(map(tuple, a_full)) == sorted(map(tuple, store.list_namespaces()))
        assert sorted(map(tuple, a_depth1)) == sorted(map(tuple, store.list_namespaces(max_depth=1)))
        assert ("memories", "u1") in [tuple(n) for n in a_full]
        assert ("memories", "u2") in [tuple(n) for n in a_full]
        assert ("memories",) in [tuple(n) for n in a_depth1]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 2. abatch() — mixed ops aligned by index; empty -> []
# --------------------------------------------------------------------------- #
def test_abatch_mixed_ops_aligned_by_index():
    store = _mkstore()
    try:
        # seed
        store.put(("memories", "u1"), "seed", {"text": "seeded dark note", "kind": "pref"})

        ops = [
            PutOp(("memories", "u1"), "new1", {"text": "fresh item", "kind": "ops"}),  # 0 -> None
            GetOp(("memories", "u1"), "seed"),                                         # 1 -> Item
            GetOp(("memories", "u1"), "absent"),                                       # 2 -> None
            SearchOp(("memories",), query="dark"),                                     # 3 -> list[SearchItem]
            ListNamespacesOp(),                                                        # 4 -> list[tuple]
            PutOp(("memories", "u1"), "seed", None),                                   # 5 -> None (delete)
        ]

        async def main():
            return await store.abatch(ops)
        res = _run(main())

        assert len(res) == len(ops)
        assert res[0] is None                                  # Put returns None
        assert isinstance(res[1], Item) and res[1].key == "seed"
        assert res[2] is None                                  # missing Get -> None
        assert isinstance(res[3], list) and all(isinstance(h, SearchItem) for h in res[3])
        assert isinstance(res[4], list) and ("memories", "u1") in [tuple(n) for n in res[4]]
        assert res[5] is None                                  # delete Put -> None

        # side effects landed: new1 created, seed deleted
        assert store.get(("memories", "u1"), "new1") is not None
        assert store.get(("memories", "u1"), "seed") is None
    finally:
        store.close()


def test_abatch_empty_returns_empty_list():
    store = _mkstore()
    try:
        async def main():
            return await store.abatch([])
        res = _run(main())
        assert res == []
        assert isinstance(res, list)
    finally:
        store.close()


def test_abatch_get_order_preserved_for_many_gets():
    """Index alignment under a larger homogeneous batch."""
    store = _mkstore()
    try:
        for i in range(20):
            store.put(("ns", "ord"), f"k{i}", {"i": i})
        # interleave present/absent keys to verify positional alignment
        keys = []
        for i in range(20):
            keys.append(f"k{i}")
            keys.append(f"missing{i}")
        ops = [GetOp(("ns", "ord"), k) for k in keys]

        async def main():
            return await store.abatch(ops)
        res = _run(main())

        assert len(res) == len(ops)
        for idx, k in enumerate(keys):
            if k.startswith("missing"):
                assert res[idx] is None, f"index {idx} ({k}) should be None"
            else:
                assert res[idx] is not None and res[idx].key == k
                assert res[idx].value["i"] == int(k[1:])
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 3. concurrency — gather of many aput to DISTINCT keys; read all back
# --------------------------------------------------------------------------- #
def test_concurrent_aput_distinct_keys_none_lost():
    store = _mkstore()
    try:
        N = 150

        async def main():
            await asyncio.gather(*[
                store.aput(("ns", "distinct"), f"k{i}", {"i": i, "text": f"item {i}"})
                for i in range(N)
            ])
            got = await asyncio.gather(*[
                store.aget(("ns", "distinct"), f"k{i}") for i in range(N)
            ])
            return got
        got = _run(main())

        assert len(got) == N
        for i, it in enumerate(got):
            assert it is not None, f"key k{i} was lost"
            assert it.value == {"i": i, "text": f"item {i}"}, f"key k{i} corrupted: {it.value}"

        # cross-check via list/search count
        browse = store.search(("ns", "distinct"), limit=1000)
        assert len({h.key for h in browse}) == N
    finally:
        store.close()


def test_concurrent_interleaved_aput_aget_same_namespace():
    store = _mkstore()
    try:
        N = 80

        async def writer(i):
            await store.aput(("ns", "shared"), f"k{i}", {"i": i})

        async def reader(i):
            # may or may not see it yet; must never raise / corrupt
            it = await store.aget(("ns", "shared"), f"k{i}")
            if it is not None:
                assert it.value["i"] == i

        async def main():
            tasks = []
            for i in range(N):
                tasks.append(writer(i))
                tasks.append(reader(i))   # interleaved with the write
            await asyncio.gather(*tasks)
            # final settle: everything must be present + correct
            final = await asyncio.gather(*[store.aget(("ns", "shared"), f"k{i}") for i in range(N)])
            return final
        final = _run(main())
        assert all(it is not None and it.value["i"] == i for i, it in enumerate(final))
    finally:
        store.close()


def test_concurrent_overwrites_same_key_no_corruption():
    """Many concurrent writers to ONE key: final value is one valid write, never corrupt."""
    store = _mkstore()
    try:
        N = 60

        async def main():
            await asyncio.gather(*[
                store.aput(("ns", "hot"), "k", {"writer": i, "payload": f"v{i}"})
                for i in range(N)
            ])
            return await store.aget(("ns", "hot"), "k")
        it = _run(main())
        assert it is not None
        # value must be a clean, complete dict from exactly one writer
        assert set(it.value.keys()) == {"writer", "payload"}
        assert it.value["payload"] == f"v{it.value['writer']}"
        assert 0 <= it.value["writer"] < N
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 4. cross-thread / repeated-op watch (the SQLite thread-affinity hunt)
# --------------------------------------------------------------------------- #
def test_many_sequential_awaits_no_cross_thread_error():
    """50+ sequential awaits, each offloaded to the thread pool. Surfaces any
    'SQLite objects created in a thread can only be used in another thread'."""
    store = _mkstore()
    try:
        async def main():
            for i in range(80):
                await store.aput(("ns", "seq"), f"k{i}", {"i": i})
                it = await store.aget(("ns", "seq"), f"k{i}")
                assert it is not None and it.value["i"] == i
            # mix in searches + namespace listings which also hit the pool
            for _ in range(20):
                await store.asearch(("ns",), query="k")
                await store.alist_namespaces()
        _run(main())
    finally:
        store.close()


def test_gathered_then_sequential_then_gathered_stress():
    """Alternate burst-concurrency and sequential phases to thrash the pool's
    thread-local connections (each pool thread opens its own SQLite conn)."""
    store = _mkstore()
    try:
        async def main():
            # burst 1
            await asyncio.gather(*[store.aput(("ns", "s"), f"a{i}", {"i": i}) for i in range(50)])
            # sequential
            for i in range(50):
                assert (await store.aget(("ns", "s"), f"a{i}")).value["i"] == i
            # burst 2 (overwrites + new)
            await asyncio.gather(*[store.aput(("ns", "s"), f"a{i}", {"i": i * 10}) for i in range(50)])
            got = await asyncio.gather(*[store.aget(("ns", "s"), f"a{i}") for i in range(50)])
            return got
        got = _run(main())
        assert all(it is not None and it.value["i"] == i * 10 for i, it in enumerate(got))
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 5. event loop is not blocked (offload sanity)
# --------------------------------------------------------------------------- #
def test_gather_of_many_ops_completes_within_timeout():
    """Sanity: a gather of N ops completes (loop not deadlocked/blocked)."""
    store = _mkstore()
    try:
        async def main():
            await asyncio.wait_for(
                asyncio.gather(*[
                    store.aput(("ns", "t"), f"k{i}", {"i": i}) for i in range(120)
                ]),
                timeout=30,
            )
            results = await asyncio.wait_for(
                asyncio.gather(*[store.aget(("ns", "t"), f"k{i}") for i in range(120)]),
                timeout=30,
            )
            return results
        results = _run(main())
        assert sum(1 for r in results if r is not None) == 120
    finally:
        store.close()


def test_event_loop_progresses_during_store_ops():
    """A concurrent ticker coroutine must make progress while store ops run,
    proving abatch offloads instead of blocking the loop thread."""
    store = _mkstore()
    try:
        async def ticker(state):
            # runs alongside the store-op gather; counts loop turns it gets
            while not state["done"]:
                state["ticks"] += 1
                await asyncio.sleep(0)
            return state["ticks"]

        async def workload(state):
            await asyncio.gather(*[
                store.aput(("ns", "lp"), f"k{i}", {"i": i, "blob": "x" * 64})
                for i in range(200)
            ])
            state["done"] = True

        async def main():
            state = {"ticks": 0, "done": False}
            t = asyncio.create_task(ticker(state))
            await workload(state)
            await t
            return state["ticks"]
        ticks = _run(main())
        # If the loop were blocked by synchronous SQLite work, the ticker would
        # get few/zero turns. Offloading lets it spin many times.
        assert ticks > 1, f"loop appears blocked during store ops (ticks={ticks})"
    finally:
        store.close()


if __name__ == "__main__":  # allow direct execution too
    raise SystemExit(pytest.main([__file__, "-v"]))
