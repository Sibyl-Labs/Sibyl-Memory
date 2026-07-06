"""Rigorous CRUD + batch + durability coverage for SibylStore.

Dimension: core CRUD round-trips, value variety, overwrite semantics,
mixed batch() index/type alignment, on-disk durability across reopen,
and scale. Hunts for real adapter bugs; documented-scope behaviours
(lexical search, ignored index/ttl, supports_ttl=False, 2MB free cap,
ValueError on bad namespace) are asserted as the contract, not flagged.

Adapter source is READ-ONLY. A failure here that reflects a genuine
adapter bug is left failing on purpose.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from sibyl_memory_langgraph import SibylStore
from langgraph.store.base import (
    GetOp,
    PutOp,
    SearchOp,
    ListNamespacesOp,
    Item,
    SearchItem,
)


# --------------------------------------------------------------------------
# helpers / fixtures
# --------------------------------------------------------------------------
def _fresh_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "t.db")


@pytest.fixture()
def store():
    s = SibylStore(path=_fresh_path(), tier="free")
    try:
        yield s
    finally:
        s.close()


def _put_op(ns, key, value, index=None, ttl=None):
    return PutOp(namespace=ns, key=key, value=value, index=index, ttl=ttl)


def _get_op(ns, key):
    return GetOp(namespace=ns, key=key, refresh_ttl=None)


def _search_op(prefix, *, query=None, filter=None, limit=10, offset=0):
    return SearchOp(
        namespace_prefix=prefix,
        filter=filter,
        limit=limit,
        offset=offset,
        query=query,
        refresh_ttl=None,
    )


def _ls_op(*, max_depth=None, limit=100, offset=0):
    return ListNamespacesOp(
        match_conditions=(), max_depth=max_depth, limit=limit, offset=offset
    )


# --------------------------------------------------------------------------
# put -> get round trip
# --------------------------------------------------------------------------
def test_put_then_get_full_roundtrip(store):
    ns = ("memories", "u1")
    val = {"text": "operator prefers dark mode", "kind": "pref"}
    store.put(ns, "fact1", val)
    it = store.get(ns, "fact1")

    assert isinstance(it, Item)
    assert it.value == val
    assert it.namespace == ns
    assert it.key == "fact1"
    assert it.created_at is not None and it.updated_at is not None


def test_timestamps_are_tz_aware_and_ordered(store):
    store.put(("ns", "u"), "k", {"v": 1})
    it = store.get(("ns", "u"), "k")
    # tz-aware UTC datetimes
    assert it.created_at.tzinfo is not None
    assert it.updated_at.tzinfo is not None
    # fresh insert: updated_at must be >= created_at
    assert it.updated_at >= it.created_at


def test_get_returns_independent_value_no_shared_reference(store):
    ns = ("m", "u")
    store.put(ns, "k", {"n": 1, "nested": {"a": 1}})
    a = store.get(ns, "k")
    a.value["n"] = 999
    a.value["nested"]["a"] = 999
    b = store.get(ns, "k")
    # mutating a returned Item must not bleed into the store
    assert b.value == {"n": 1, "nested": {"a": 1}}


def test_namespace_isolation_same_key(store):
    store.put(("memories", "u1"), "fact1", {"who": "u1"})
    store.put(("memories", "u2"), "fact1", {"who": "u2"})
    assert store.get(("memories", "u1"), "fact1").value == {"who": "u1"}
    assert store.get(("memories", "u2"), "fact1").value == {"who": "u2"}


# --------------------------------------------------------------------------
# missing / delete
# --------------------------------------------------------------------------
def test_get_missing_returns_none(store):
    assert store.get(("memories", "u1"), "nope") is None


def test_get_missing_on_never_used_namespace(store):
    assert store.get(("never", "seen"), "k") is None


def test_delete_then_get_none(store):
    ns = ("m", "u")
    store.put(ns, "k", {"x": 1})
    assert store.get(ns, "k") is not None
    store.delete(ns, "k")
    assert store.get(ns, "k") is None


def test_delete_missing_key_is_idempotent_no_crash(store):
    ns = ("m", "u")
    # delete on totally empty store
    store.delete(ns, "ghost")
    assert store.get(ns, "ghost") is None
    # put, delete twice
    store.put(ns, "k", {"x": 1})
    store.delete(ns, "k")
    store.delete(ns, "k")  # second delete must not raise
    assert store.get(ns, "k") is None


def test_delete_via_batch_put_none_sentinel(store):
    ns = ("m", "u")
    store.put(ns, "k", {"x": 1})
    res = store.batch([_put_op(ns, "k", None)])
    assert res == [None]
    assert store.get(ns, "k") is None


# --------------------------------------------------------------------------
# overwrite semantics
# --------------------------------------------------------------------------
def test_overwrite_replaces_value_exactly_one_item(store):
    ns = ("memories", "u1")
    store.put(ns, "fact1", {"text": "dark mode", "kind": "pref"})
    store.put(ns, "fact1", {"text": "light mode", "kind": "pref"})
    it = store.get(ns, "fact1")
    assert it.value["text"] == "light mode"
    # exactly one item under this namespace after overwrite
    browse = store.search(ns, limit=100)
    keys = [h.key for h in browse]
    assert keys.count("fact1") == 1
    assert len(browse) == 1


def test_overwrite_is_full_replace_not_merge(store):
    ns = ("m", "u")
    store.put(ns, "k", {"a": 1, "b": 2})
    store.put(ns, "k", {"a": 9})
    it = store.get(ns, "k")
    assert it.value == {"a": 9}  # 'b' must be gone, not merged


def test_overwrite_preserves_created_at_advances_updated_at(store):
    ns = ("m", "u")
    store.put(ns, "k", {"v": 1})
    first = store.get(ns, "k")
    time.sleep(0.05)
    store.put(ns, "k", {"v": 2})
    second = store.get(ns, "k")
    assert second.created_at == first.created_at  # created_at immutable
    assert second.updated_at >= second.created_at
    assert second.updated_at > first.updated_at  # advanced on rewrite


# --------------------------------------------------------------------------
# value variety
# --------------------------------------------------------------------------
def test_value_nested_dict_and_list(store):
    ns = ("v", "u")
    val = {
        "name": "alpha",
        "tags": ["x", "y", "z"],
        "meta": {"created_by": "op", "scores": [1, 2, 3]},
        "matrix": [[1, 2], [3, 4]],
    }
    store.put(ns, "k", val)
    assert store.get(ns, "k").value == val


def test_value_primitive_types_preserved(store):
    ns = ("v", "u")
    val = {"s": "str", "i": 42, "f": 3.14159, "t": True, "fa": False, "n": None}
    store.put(ns, "k", val)
    got = store.get(ns, "k").value
    assert got == val
    # type fidelity: bool must not collapse to int and vice-versa
    assert got["t"] is True and got["fa"] is False
    assert isinstance(got["i"], int) and not isinstance(got["i"], bool)
    assert isinstance(got["f"], float)
    assert got["n"] is None


def test_value_empty_dict_roundtrips(store):
    ns = ("v", "u")
    store.put(ns, "empty", {})
    it = store.get(ns, "empty")
    assert it is not None
    assert it.value == {}


def test_value_deeply_nested(store):
    ns = ("v", "u")
    deep = cur = {}
    for i in range(40):
        cur["level"] = i
        cur["child"] = {}
        cur = cur["child"]
    cur["leaf"] = "bottom"
    store.put(ns, "deep", deep)
    got = store.get(ns, "deep").value
    assert got == deep
    # walk to confirm depth survived
    node = got
    for i in range(40):
        assert node["level"] == i
        node = node["child"]
    assert node["leaf"] == "bottom"


def test_value_unicode_and_special_chars(store):
    ns = ("v", "u")
    val = {
        "emoji": "rocket \U0001F680 and snow ❄",
        "accents": "naive cafe Zurich Munchen",
        "quotes": 'he said "hi" and it\'s fine',
        "newline": "line1\nline2\ttabbed",
        "json_like": '{"not":"parsed"}',
    }
    store.put(ns, "k", val)
    assert store.get(ns, "k").value == val


def test_value_numeric_edges(store):
    ns = ("v", "u")
    val = {
        "big_int": 2**53 + 1,
        "neg": -123456789,
        "zero": 0,
        "small_float": 1e-9,
        "neg_float": -2.5,
    }
    store.put(ns, "k", val)
    assert store.get(ns, "k").value == val


# --------------------------------------------------------------------------
# batch() directly: mix of op types, index alignment, types
# --------------------------------------------------------------------------
def test_batch_empty_returns_empty_list(store):
    assert store.batch([]) == []


def test_batch_mixed_ops_aligned_by_index_and_typed(store):
    # seed something so search/list have content from a prior write
    store.put(("seed",), "s0", {"text": "seeded apple"})

    ops = [
        _put_op(("b", "x"), "k1", {"text": "hello world", "kind": "a"}),
        _get_op(("b", "x"), "k1"),               # read-your-write in same batch
        _get_op(("b", "x"), "missing"),          # -> None
        _search_op(("b",), query="hello"),       # -> list[SearchItem]
        _ls_op(),                                # -> list[tuple]
        _put_op(("b", "x"), "k1", None),         # delete sentinel -> None
    ]
    res = store.batch(ops)

    assert len(res) == len(ops)
    # index 0: PutOp -> None
    assert res[0] is None
    # index 1: GetOp sees the just-written value (sequential semantics)
    assert isinstance(res[1], Item)
    assert res[1].value["text"] == "hello world"
    assert res[1].namespace == ("b", "x") and res[1].key == "k1"
    # index 2: missing GetOp -> None
    assert res[2] is None
    # index 3: SearchOp -> list of SearchItem
    assert isinstance(res[3], list)
    assert all(isinstance(h, SearchItem) for h in res[3])
    assert any(h.key == "k1" for h in res[3])
    # index 4: ListNamespacesOp -> list of tuples
    assert isinstance(res[4], list)
    assert all(isinstance(t, tuple) for t in res[4])
    assert ("b", "x") in res[4]
    # index 5: delete via Put None -> None
    assert res[5] is None

    # post-condition: the in-batch delete took effect
    assert store.get(("b", "x"), "k1") is None


def test_batch_put_returns_none_per_put(store):
    ops = [
        _put_op(("p",), "a", {"i": 1}),
        _put_op(("p",), "b", {"i": 2}),
        _put_op(("p",), "c", {"i": 3}),
    ]
    res = store.batch(ops)
    assert res == [None, None, None]
    assert store.get(("p",), "b").value == {"i": 2}


def test_batch_multiple_gets_aligned(store):
    for i in range(5):
        store.put(("g",), f"k{i}", {"i": i})
    ops = [_get_op(("g",), f"k{i}") for i in range(5)]
    res = store.batch(ops)
    assert [r.value["i"] for r in res] == [0, 1, 2, 3, 4]


def test_batch_index_and_ttl_accepted_but_ignored(store):
    # documented contract: PutOp.index and PutOp.ttl are accepted and IGNORED
    res = store.batch([_put_op(("c",), "kk", {"a": 1}, index=["a"], ttl=999.0)])
    assert res == [None]
    assert store.get(("c",), "kk").value == {"a": 1}


def test_supports_ttl_is_false(store):
    assert store.supports_ttl is False


# --------------------------------------------------------------------------
# namespace validation (adapter contract -> ValueError)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ns",
    [
        (),            # empty tuple
        ("a/b",),      # contains separator
        ("..",),       # path traversal
        ("a..b",),     # contains ..
        ("",),         # empty element
        ("ok", ""),    # empty element in 2nd position
    ],
)
def test_bad_namespace_raises_valueerror_via_batch(store, ns):
    with pytest.raises(ValueError):
        store.batch([_put_op(ns, "k", {"x": 1})])


def test_bad_namespace_on_get_raises_valueerror(store):
    with pytest.raises(ValueError):
        store.batch([_get_op(("a/b",), "k")])


# --------------------------------------------------------------------------
# durability across close() + reopen on same path
# --------------------------------------------------------------------------
def test_durability_reopen_same_path():
    path = _fresh_path()
    s1 = SibylStore(path=path, tier="free")
    s1.put(("d", "u"), "k1", {"text": "persist me", "n": 1})
    s1.put(("d", "u"), "k2", {"text": "me too", "n": 2})
    before = s1.get(("d", "u"), "k1")
    s1.close()

    s2 = SibylStore(path=path, tier="free")
    try:
        after = s2.get(("d", "u"), "k1")
        assert after is not None
        assert after.value == {"text": "persist me", "n": 1}
        # timestamps survive the reopen unchanged
        assert after.created_at == before.created_at
        assert after.updated_at == before.updated_at
        assert s2.get(("d", "u"), "k2").value == {"text": "me too", "n": 2}
    finally:
        s2.close()


def test_durability_overwrite_and_delete_persist():
    path = _fresh_path()
    s1 = SibylStore(path=path, tier="free")
    s1.put(("d",), "a", {"v": 1})
    s1.put(("d",), "b", {"v": 1})
    s1.put(("d",), "a", {"v": 2})  # overwrite
    s1.delete(("d",), "b")         # delete
    s1.close()

    s2 = SibylStore(path=path, tier="free")
    try:
        assert s2.get(("d",), "a").value == {"v": 2}
        assert s2.get(("d",), "b") is None
    finally:
        s2.close()


def test_two_stores_same_path_live_visibility():
    # WAL: a second store opened on the same path sees committed writes from
    # the first without an explicit reopen.
    path = _fresh_path()
    a = SibylStore(path=path, tier="free")
    b = SibylStore(path=path, tier="free")
    try:
        a.put(("X",), "k", {"v": 1})
        assert b.get(("X",), "k").value == {"v": 1}
        a.put(("X",), "k", {"v": 2})
        assert b.get(("X",), "k").value == {"v": 2}
        a.delete(("X",), "k")
        assert b.get(("X",), "k") is None
    finally:
        a.close()
        b.close()


def test_shared_client_close_does_not_destroy_store():
    # SibylStore(client=...) does not own the client; close() must be a no-op
    # for the underlying storage so a second store on the same client survives.
    from sibyl_memory_client import MemoryClient

    client = MemoryClient.local(_fresh_path(), tier="free")
    s1 = SibylStore(client=client)
    s2 = SibylStore(client=client)
    s1.put(("sh",), "k", {"v": 1})
    s1.close()  # should NOT close the shared client's storage
    # s2 (same client) must still read/write
    assert s2.get(("sh",), "k").value == {"v": 1}
    s2.put(("sh",), "k2", {"v": 2})
    assert s2.get(("sh",), "k2").value == {"v": 2}


# --------------------------------------------------------------------------
# scale
# --------------------------------------------------------------------------
def test_scale_200_put_get_browse_list(store):
    ns = ("scale", "batch")
    n = 200
    for i in range(n):
        store.put(ns, f"e{i:04d}", {"i": i, "text": f"entity number {i}"})

    # get each one back
    for i in range(n):
        it = store.get(ns, f"e{i:04d}")
        assert it is not None and it.value["i"] == i

    # browse (no query) must surface all 200 when limit is raised
    browse = store.search(ns, limit=n + 50)
    got_keys = {h.key for h in browse}
    assert len(got_keys) == n
    assert all(f"e{i:04d}" in got_keys for i in range(n))

    # list_namespaces collapses the 200 entities to the single namespace
    spaces = store.list_namespaces()
    assert ns in spaces
    assert store.list_namespaces(max_depth=1) and ("scale",) in store.list_namespaces(max_depth=1)


def test_scale_default_search_limit_is_ten(store):
    # documented: default search limit is 10 (not a bug); confirm it caps.
    ns = ("cap",)
    for i in range(25):
        store.put(ns, f"k{i:02d}", {"i": i, "text": "common token"})
    default = store.search(ns)  # no limit -> 10
    assert len(default) == 10
    wide = store.search(ns, limit=100)
    assert len(wide) == 25


# --------------------------------------------------------------------------
# large value
# --------------------------------------------------------------------------
def test_large_value_50kb_roundtrip(store):
    ns = ("big",)
    payload = {
        "blob": "A" * 50_000,
        "rows": [{"id": i, "name": f"row-{i}"} for i in range(300)],
    }
    store.put(ns, "k", payload)
    got = store.get(ns, "k").value
    assert got == payload
    assert len(got["blob"]) == 50_000
    assert len(got["rows"]) == 300


# --------------------------------------------------------------------------
# async batch
# --------------------------------------------------------------------------
def test_abatch_roundtrip_in_executor(store):
    async def run():
        await store.abatch([_put_op(("async",), "k", {"v": 1})])
        res = await store.abatch([_get_op(("async",), "k")])
        return res

    res = asyncio.run(run())
    assert isinstance(res[0], Item)
    assert res[0].value == {"v": 1}
    # and a sync read agrees (cross-thread persistence)
    assert store.get(("async",), "k").value == {"v": 1}
