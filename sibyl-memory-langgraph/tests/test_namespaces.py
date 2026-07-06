"""Rigorous coverage of SibylStore NAMESPACES + list_namespaces + VALIDATION.

Dimension: namespace listing, max_depth, prefix/suffix/wildcard matching,
limit/offset pagination, namespace validation, deep-namespace round-trip,
namespace isolation, and a SET-based differential against the reference
``InMemoryStore``.

Contract under test (per task brief, treated as intentional / NOT bugs):
  * namespace tuple -> category "/".join(namespace).
  * namespace elements MUST be non-empty strings with no "/" and no "..";
    invalid -> ValueError. Empty namespace tuple -> ValueError.
  * list_namespaces(*, prefix, suffix, max_depth, limit=100, offset=0)
    returns a list of namespace tuples (dispatches to ListNamespacesOp).

Note on the reference impl: langgraph's ``InMemoryStore`` validates via
``_validate_namespace`` which bans "." (and thereby ".."), empty strings,
non-strings, and a "langgraph" root, but ALLOWS "/". SibylStore instead bans
"/" and "..". These validation *rules* differ by design, so the differential
below only ever uses namespaces that are valid under BOTH stores, and the
validation tests assert SibylStore's own documented rules directly.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from sibyl_memory_langgraph import SibylStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import ListNamespacesOp, MatchCondition


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
def _new_sibyl() -> SibylStore:
    d = tempfile.mkdtemp()
    return SibylStore(path=os.path.join(d, "t.db"), tier="free")


@pytest.fixture
def store():
    s = _new_sibyl()
    try:
        yield s
    finally:
        s.close()


# A namespace set valid under BOTH SibylStore and InMemoryStore (no ".", no "/",
# no "..", no "langgraph" root). Used for listing + differential tests.
DIFF_NS = [
    ("a", "b", "c"),
    ("a", "b", "d", "e"),
    ("a", "b", "f"),
    ("a", "c", "f"),
    ("docs", "reports", "2024"),
    ("docs", "reports", "2025"),
]


def _populate(s, namespaces, value=None):
    for ns in namespaces:
        s.put(ns, "k", value or {"v": 1})


@pytest.fixture
def pair():
    """A SibylStore and an InMemoryStore populated identically with DIFF_NS."""
    s = _new_sibyl()
    m = InMemoryStore()
    _populate(s, DIFF_NS)
    _populate(m, DIFF_NS)
    try:
        yield s, m
    finally:
        s.close()


# --------------------------------------------------------------------------
# basic listing + round-trip
# --------------------------------------------------------------------------
def test_list_namespaces_returns_every_distinct_namespace(store):
    _populate(store, DIFF_NS)
    got = store.list_namespaces()
    assert set(got) == set(DIFF_NS)


def test_list_namespaces_returns_tuples(store):
    _populate(store, DIFF_NS)
    got = store.list_namespaces()
    assert all(isinstance(ns, tuple) for ns in got)
    assert all(all(isinstance(el, str) for el in ns) for ns in got)


def test_list_namespaces_is_sorted_ascending(store):
    _populate(store, DIFF_NS)
    got = store.list_namespaces()
    assert got == sorted(got)


def test_list_namespaces_dedups_overwrites(store):
    store.put(("a", "b"), "k1", {"v": 1})
    store.put(("a", "b"), "k1", {"v": 2})  # overwrite same key
    store.put(("a", "b"), "k2", {"v": 3})  # second key, same namespace
    got = store.list_namespaces()
    assert got.count(("a", "b")) == 1


def test_list_namespaces_reflects_delete(store):
    store.put(("solo",), "k", {"v": 1})
    assert ("solo",) in store.list_namespaces()
    store.delete(("solo",), "k")
    assert ("solo",) not in store.list_namespaces()


def test_parent_and_child_namespaces_are_distinct(store):
    store.put(("a",), "k", {"v": 1})
    store.put(("a", "b"), "k", {"v": 2})
    got = set(store.list_namespaces())
    assert ("a",) in got
    assert ("a", "b") in got


# --------------------------------------------------------------------------
# deep namespaces round-trip
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ns",
    [
        ("l1", "l2", "l3", "l4", "l5"),
        ("a", "b", "c", "d", "e", "f"),
    ],
)
def test_deep_namespace_roundtrip(store, ns):
    store.put(ns, "k", {"depth": len(ns)})
    item = store.get(ns, "k")
    assert item is not None
    assert item.namespace == ns
    assert item.value["depth"] == len(ns)
    assert ns in store.list_namespaces()


# --------------------------------------------------------------------------
# namespace isolation
# --------------------------------------------------------------------------
def test_namespace_isolation_same_key_independent(store):
    store.put(("a", "b"), "key", {"who": "b"})
    store.put(("a", "c"), "key", {"who": "c"})
    assert store.get(("a", "b"), "key").value == {"who": "b"}
    assert store.get(("a", "c"), "key").value == {"who": "c"}
    # deleting one leaves the other intact
    store.delete(("a", "b"), "key")
    assert store.get(("a", "b"), "key") is None
    assert store.get(("a", "c"), "key").value == {"who": "c"}


# --------------------------------------------------------------------------
# max_depth (alone) — truncate + dedup
# --------------------------------------------------------------------------
def test_max_depth_truncates_and_dedups(store):
    store.put(("a", "b", "c"), "k", {"v": 1})
    got = store.list_namespaces(max_depth=1)
    assert ("a",) in got
    # nothing longer than depth 1 should survive
    assert all(len(ns) <= 1 for ns in got)


def test_max_depth_dedup_collapses_siblings(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(max_depth=2))
    assert got == {("a", "b"), ("a", "c"), ("docs", "reports")}


def test_parent_and_child_collapse_under_max_depth(store):
    store.put(("a",), "k", {"v": 1})
    store.put(("a", "b"), "k", {"v": 2})
    got = store.list_namespaces(max_depth=1)
    assert got.count(("a",)) == 1


# --------------------------------------------------------------------------
# prefix / suffix / wildcard matching
# --------------------------------------------------------------------------
def test_prefix_match(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(prefix=("a", "b")))
    assert got == {("a", "b", "c"), ("a", "b", "d", "e"), ("a", "b", "f")}


def test_prefix_no_match_returns_empty(store):
    _populate(store, DIFF_NS)
    assert store.list_namespaces(prefix=("nope",)) == []


def test_suffix_match(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(suffix=("f",)))
    assert got == {("a", "b", "f"), ("a", "c", "f")}


def test_prefix_wildcard(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(prefix=("a", "*")))
    assert got == {
        ("a", "b", "c"),
        ("a", "b", "d", "e"),
        ("a", "b", "f"),
        ("a", "c", "f"),
    }


def test_suffix_wildcard(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(suffix=("*", "f")))
    assert got == {("a", "b", "f"), ("a", "c", "f")}


def test_prefix_and_suffix_combined(store):
    _populate(store, DIFF_NS)
    got = set(store.list_namespaces(prefix=("a", "b"), suffix=("c",)))
    assert got == {("a", "b", "c")}


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------
def test_limit(store):
    _populate(store, DIFF_NS)
    full = store.list_namespaces()
    got = store.list_namespaces(limit=2)
    assert got == full[:2]


def test_offset(store):
    _populate(store, DIFF_NS)
    full = store.list_namespaces()
    got = store.list_namespaces(offset=2)
    assert got == full[2:]


def test_offset_limit_combo(store):
    _populate(store, DIFF_NS)
    full = store.list_namespaces()
    got = store.list_namespaces(offset=2, limit=2)
    assert got == full[2:4]


def test_offset_past_end_returns_empty(store):
    _populate(store, DIFF_NS)
    assert store.list_namespaces(offset=999) == []


# --------------------------------------------------------------------------
# DIFFERENTIAL vs InMemoryStore (compare as SETS)
# --------------------------------------------------------------------------
# Each entry is kwargs for list_namespaces. SibylStore and InMemoryStore must
# return the same SET of namespaces. Cases tagged below with comments that
# include "BUG" are expected to fail and expose adapter defects.
DIFF_QUERIES = [
    {},
    {"max_depth": 1},
    {"max_depth": 2},
    {"max_depth": 3},
    {"prefix": ("a",)},
    {"prefix": ("a", "b")},
    {"suffix": ("f",)},
    {"suffix": ("2024",)},
    {"prefix": ("a", "*")},
    {"suffix": ("*", "f")},
    {"prefix": ("a", "b"), "suffix": ("c",)},
    {"prefix": ("a", "b"), "max_depth": 3},   # control: max_depth >= prefix len
    {"suffix": ("c",), "max_depth": 3},       # control: max_depth keeps suffix elem
    {"limit": 2},
    {"offset": 2, "limit": 2},
    {"prefix": ("a", "b"), "max_depth": 1},   # BUG A: max_depth < prefix len
    {"suffix": ("f",), "max_depth": 2},       # BUG A: max_depth truncates suffix elem
]


@pytest.mark.parametrize("kwargs", DIFF_QUERIES, ids=[str(q) for q in DIFF_QUERIES])
def test_differential_list_namespaces(pair, kwargs):
    s, m = pair
    sib = set(s.list_namespaces(**kwargs))
    ref = set(m.list_namespaces(**kwargs))
    assert sib == ref, f"divergence for {kwargs}: sibyl={sib} inmemory={ref}"


def test_differential_via_low_level_op(pair):
    """Same divergence reproduced through the raw ListNamespacesOp API."""
    s, m = pair
    op = ListNamespacesOp(
        match_conditions=(MatchCondition(match_type="prefix", path=("a", "b")),),
        max_depth=1,
    )
    sib = set(s.batch([op])[0])
    ref = set(m.batch([op])[0])
    assert sib == ref, f"sibyl={sib} inmemory={ref}"


# --------------------------------------------------------------------------
# VALIDATION
# --------------------------------------------------------------------------
# Non-empty namespaces that are invalid under SibylStore's documented rules.
INVALID_NS = [
    pytest.param(("bad/elem",), id="slash"),
    pytest.param(("ok", "bad/elem"), id="slash-nested"),
    pytest.param(("..",), id="dotdot"),
    pytest.param(("a", "x..y"), id="dotdot-nested"),
    pytest.param(("",), id="empty-string"),
    pytest.param(("a", ""), id="empty-string-nested"),
    pytest.param((123,), id="non-string"),
    pytest.param(("a", 123), id="non-string-nested"),
]


@pytest.mark.parametrize("ns", INVALID_NS)
def test_get_rejects_invalid_namespace(store, ns):
    with pytest.raises(ValueError):
        store.get(ns, "k")


@pytest.mark.parametrize("ns", INVALID_NS)
def test_put_rejects_invalid_namespace(store, ns):
    with pytest.raises(ValueError):
        store.put(ns, "k", {"v": 1})


@pytest.mark.parametrize("ns", INVALID_NS)
def test_delete_rejects_invalid_namespace(store, ns):
    with pytest.raises(ValueError):
        store.delete(ns, "k")


# Per the contract, search must ALSO enforce namespace validation. These are
# expected to FAIL (Bug B): search silently returns [] for invalid prefixes.
@pytest.mark.parametrize(
    "ns",
    [
        pytest.param(("bad/elem",), id="slash"),
        pytest.param(("",), id="empty-string"),
        pytest.param((123,), id="non-string"),
    ],
)
def test_search_rejects_invalid_namespace(store, ns):
    with pytest.raises(ValueError):
        store.search(ns, query="x")


# Empty tuple is invalid for get/put/delete (single-entity addressing) ...
@pytest.mark.parametrize(
    "fn",
    [
        lambda s: s.get((), "k"),
        lambda s: s.put((), "k", {"v": 1}),
        lambda s: s.delete((), "k"),
    ],
    ids=["get", "put", "delete"],
)
def test_empty_tuple_rejected_for_addressed_ops(store, fn):
    with pytest.raises(ValueError):
        fn(store)


# ... but empty tuple IS a valid search prefix (search-all) and must not raise.
def test_search_empty_prefix_is_valid(store):
    store.put(("x", "y"), "k", {"text": "hello world"})
    hits = store.search((), query="hello")
    assert any(h.namespace == ("x", "y") for h in hits)


def test_valid_namespace_roundtrips_through_all_ops(store):
    ns = ("users", "u1", "profile")
    store.put(ns, "k", {"text": "fine"})
    assert store.get(ns, "k") is not None
    assert store.search(ns, query="fine")  # exact-namespace search ok
    assert ns in store.list_namespaces()
    store.delete(ns, "k")
    assert store.get(ns, "k") is None
