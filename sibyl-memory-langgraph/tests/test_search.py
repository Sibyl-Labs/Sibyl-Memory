"""Rigorous SEARCH + FILTER + PAGINATION + adversarial coverage for SibylStore.

Dimension: search / filter / pagination. The adapter maps a LangGraph
BaseStore onto Sibyl Memory (SQLite + FTS5). search() is LEXICAL (FTS5), not
semantic; cross-category ranking is best-effort and score may be None — those
are documented scope, not bugs, and are NOT asserted as failures here.

Tests that assert the *correct* contract behaviour and FAIL are left failing on
purpose: they document a real adapter bug (see test_query_plus_filter_*).
"""
from __future__ import annotations

import pytest

from sibyl_memory_langgraph import SibylStore
from langgraph.store.base import SearchItem


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    """Fresh, isolated SibylStore (own SQLite file) per test."""
    s = SibylStore(path=str(tmp_path / "t.db"), tier="free")
    try:
        yield s
    finally:
        s.close()


def keyset(items):
    return {i.key for i in items}


def nsset(items):
    return {"/".join(i.namespace) for i in items}


def seed_basic(store):
    """Two users under ('memories', *) plus a sibling subtree."""
    store.put(("memories", "u1"), "fact1", {"text": "operator prefers dark mode", "kind": "pref"})
    store.put(("memories", "u1"), "fact2", {"text": "billing handled by stripe", "kind": "ops"})
    store.put(("memories", "u1"), "fact3", {"text": "deploys ship on fridays", "kind": "ops"})
    store.put(("memories", "u2"), "fact1", {"text": "different user likes light mode", "kind": "pref"})


# --------------------------------------------------------------------------- #
# query match: single / multi-word / case / no-match
# --------------------------------------------------------------------------- #
def test_query_single_word_match(store):
    seed_basic(store)
    hits = store.search(("memories", "u1"), query="stripe")
    assert keyset(hits) == {"fact2"}


def test_query_no_match_returns_empty(store):
    seed_basic(store)
    assert store.search(("memories", "u1"), query="zzz_no_such_token") == []


def test_query_case_insensitive(store):
    store.put(("c",), "k", {"text": "Stripe Billing System"})
    lowered = keyset(store.search(("c",), query="stripe"))
    upped = keyset(store.search(("c",), query="STRIPE"))
    mixed = keyset(store.search(("c",), query="StRiPe"))
    assert lowered == upped == mixed == {"k"}


def test_query_multiword_is_and(store):
    # "dark mode" must require BOTH tokens (FTS implicit AND), in any order.
    store.put(("m",), "k1", {"text": "dark mode preference"})   # has both
    store.put(("m",), "k2", {"text": "dark theme only"})        # missing 'mode'
    store.put(("m",), "k3", {"text": "light mode preference"})  # missing 'dark'
    hits = store.search(("m",), query="dark mode")
    assert keyset(hits) == {"k1"}
    # order-independent
    assert keyset(store.search(("m",), query="mode dark")) == {"k1"}


def test_query_matches_across_body_fields(store):
    store.put(("p",), "note", {"title": "quarterly review", "owner": "alice"})
    assert keyset(store.search(("p",), query="quarterly")) == {"note"}
    assert keyset(store.search(("p",), query="alice")) == {"note"}


# --------------------------------------------------------------------------- #
# subtree search
# --------------------------------------------------------------------------- #
def test_subtree_prefix_finds_descendants(store):
    store.put(("a", "b"), "k", {"text": "alpha token"})
    store.put(("a", "c"), "k", {"text": "alpha token"})
    store.put(("a", "b", "d"), "k", {"text": "alpha token"})
    # prefix ('a',) spans b, c, and the deeper b/d
    assert nsset(store.search(("a",), query="alpha")) == {"a/b", "a/c", "a/b/d"}


def test_subtree_exact_excludes_siblings(store):
    store.put(("a", "b"), "k", {"text": "alpha token"})
    store.put(("a", "c"), "k", {"text": "alpha token"})
    hits = store.search(("a", "b"), query="alpha")
    assert nsset(hits) == {"a/b"}  # ('a','c') must not leak


def test_subtree_query_isolation_does_not_leak_other_user(store):
    seed_basic(store)
    hits = store.search(("memories", "u1"), query="mode")
    assert all(h.namespace == ("memories", "u1") for h in hits)
    # u2 also has "mode" but exact-namespace search must not surface it
    assert keyset(hits) == {"fact1"}


def test_subtree_spans_multiple_children(store):
    seed_basic(store)
    hits = store.search(("memories",), query="mode")
    assert nsset(hits) == {"memories/u1", "memories/u2"}


def test_prefix_longer_than_any_stored_namespace_returns_empty(store):
    store.put(("a",), "k", {"text": "alpha"})
    assert store.search(("a", "deeper"), query="alpha") == []
    assert store.search(("a", "deeper")) == []


# --------------------------------------------------------------------------- #
# browse (query=None)
# --------------------------------------------------------------------------- #
def test_browse_returns_all_in_exact_namespace(store):
    seed_basic(store)
    hits = store.search(("memories", "u1"), limit=100)
    assert keyset(hits) == {"fact1", "fact2", "fact3"}


def test_browse_returns_all_in_subtree(store):
    seed_basic(store)
    hits = store.search(("memories",), limit=100)
    assert nsset(hits) == {"memories/u1", "memories/u2"}
    assert len(hits) == 4


def test_browse_empty_namespace_returns_empty(store):
    seed_basic(store)
    assert store.search(("nonexistent",), limit=100) == []


def test_browse_respects_default_limit(store):
    for i in range(12):
        store.put(("b",), f"k{i:02d}", {"i": i})
    assert len(store.search(("b",))) == 10          # default limit = 10
    assert len(store.search(("b",), limit=100)) == 12


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #
def test_browse_pagination_covers_all_disjoint(store):
    for i in range(7):
        store.put(("p",), f"k{i:02d}", {"i": i})
    p1 = store.search(("p",), limit=3, offset=0)
    p2 = store.search(("p",), limit=3, offset=3)
    p3 = store.search(("p",), limit=3, offset=6)
    assert len(p1) == 3 and len(p2) == 3 and len(p3) == 1
    # non-overlapping
    assert keyset(p1).isdisjoint(keyset(p2))
    assert keyset(p1).isdisjoint(keyset(p3))
    assert keyset(p2).isdisjoint(keyset(p3))
    # full coverage
    assert keyset(p1) | keyset(p2) | keyset(p3) == {f"k{i:02d}" for i in range(7)}


def test_query_pagination_single_category_covers_all_disjoint(store):
    for i in range(7):
        store.put(("q",), f"k{i}", {"text": "alpha token", "i": i})
    collected = []
    for off in (0, 2, 4, 6):
        page = store.search(("q",), query="alpha", limit=2, offset=off)
        collected.extend(h.key for h in page)
    assert len(collected) == 7                       # no dupes across pages
    assert set(collected) == {f"k{i}" for i in range(7)}


def test_query_pagination_multi_category_covers_all_disjoint(store):
    for c in ("x", "y"):
        for i in range(4):
            store.put(("multi", c), f"{c}{i}", {"text": "common token", "i": i})
    collected = []
    for off in (0, 2, 4, 6):
        page = store.search(("multi",), query="token", limit=2, offset=off)
        collected.extend("/".join(h.namespace) + ":" + h.key for h in page)
    assert len(collected) == 8
    assert len(set(collected)) == 8                  # disjoint pages


def test_limit_zero_returns_empty(store):
    seed_basic(store)
    assert store.search(("memories", "u1"), limit=0) == []
    assert store.search(("memories", "u1"), query="mode", limit=0) == []


def test_offset_beyond_end_returns_empty(store):
    seed_basic(store)
    assert store.search(("memories", "u1"), limit=10, offset=100) == []
    assert store.search(("memories", "u1"), query="ops", limit=10, offset=100) == []


def test_offset_partial_last_page(store):
    for i in range(5):
        store.put(("p",), f"k{i}", {"i": i})
    page = store.search(("p",), limit=3, offset=3)   # only 2 remain
    assert len(page) == 2


# --------------------------------------------------------------------------- #
# filter: implicit eq + every operator + combined + missing field
# --------------------------------------------------------------------------- #
@pytest.fixture
def filter_store(store):
    store.put(("f",), "a", {"kind": "pref", "score": 10, "tag": "x"})
    store.put(("f",), "b", {"kind": "ops", "score": 20, "tag": "y"})
    store.put(("f",), "c", {"kind": "pref", "score": 30, "tag": "z"})
    return store


def fkeys(store, flt, q=None):
    return {h.key for h in store.search(("f",), query=q, filter=flt, limit=100)}


def test_filter_implicit_eq(filter_store):
    assert fkeys(filter_store, {"kind": "pref"}) == {"a", "c"}


def test_filter_eq(filter_store):
    assert fkeys(filter_store, {"kind": {"$eq": "ops"}}) == {"b"}


def test_filter_ne(filter_store):
    assert fkeys(filter_store, {"kind": {"$ne": "pref"}}) == {"b"}


def test_filter_gt(filter_store):
    assert fkeys(filter_store, {"score": {"$gt": 10}}) == {"b", "c"}


def test_filter_gte(filter_store):
    assert fkeys(filter_store, {"score": {"$gte": 20}}) == {"b", "c"}


def test_filter_lt(filter_store):
    assert fkeys(filter_store, {"score": {"$lt": 30}}) == {"a", "b"}


def test_filter_lte(filter_store):
    assert fkeys(filter_store, {"score": {"$lte": 20}}) == {"a", "b"}


def test_filter_in(filter_store):
    assert fkeys(filter_store, {"kind": {"$in": ["ops", "other"]}}) == {"b"}


def test_filter_nin(filter_store):
    assert fkeys(filter_store, {"kind": {"$nin": ["ops"]}}) == {"a", "c"}


def test_filter_multiple_conditions_are_anded(filter_store):
    # kind=pref AND score>15  -> only c
    assert fkeys(filter_store, {"kind": "pref", "score": {"$gt": 15}}) == {"c"}


def test_filter_missing_field_eq_excludes_all(filter_store):
    assert fkeys(filter_store, {"absent": "x"}) == set()


def test_filter_missing_field_ne_includes_all(filter_store):
    # missing field reads as None; None != "x" -> all pass
    assert fkeys(filter_store, {"absent": {"$ne": "x"}}) == {"a", "b", "c"}


def test_filter_missing_field_gt_excludes_all_without_crash(filter_store):
    # None vs > must not raise TypeError; guarded to exclude
    assert fkeys(filter_store, {"absent": {"$gt": 5}}) == set()


def test_unsupported_operator_raises_valueerror(filter_store):
    with pytest.raises(ValueError):
        filter_store.search(("f",), filter={"score": {"$bad": 1}})


def test_filter_combined_with_query(filter_store):
    # both 'a' and 'c' contain token (via tag etc.); query narrows, filter narrows
    filter_store.put(("f",), "d", {"kind": "pref", "score": 5, "tag": "match"})
    filter_store.put(("f",), "e", {"kind": "ops", "score": 5, "tag": "match"})
    hits = filter_store.search(("f",), query="match", filter={"kind": "pref"})
    assert keyset(hits) == {"d"}


# --------------------------------------------------------------------------- #
# REAL BUG (left failing): query + filter truncates before filtering.
# The query path fetches only offset+limit FTS hits, THEN filters, so
# filter-passing rows ranked deeper than `limit` are silently dropped. The
# browse path (no query) fetches the full pool first and is unaffected.
# --------------------------------------------------------------------------- #
def test_query_plus_filter_not_truncated_by_limit(store):
    # 5 non-matching-filter rows ranked first, then 5 that pass the filter.
    for i in range(5):
        store.put(("f",), f"drop{i}", {"text": "token here", "kind": "drop"})
    for i in range(5):
        store.put(("f",), f"keep{i}", {"text": "token here", "kind": "keep"})

    full = store.search(("f",), query="token", filter={"kind": "keep"}, limit=100)
    assert keyset(full) == {f"keep{i}" for i in range(5)}        # all 5 exist

    # Contract: filter applies to ALL query matches, then paginate -> 3 keeps.
    small = store.search(("f",), query="token", filter={"kind": "keep"}, limit=3)
    assert len(small) == 3, (
        "query+filter truncates: filter is applied to only the first "
        f"offset+limit FTS hits. got {[h.key for h in small]} (expected 3 keep rows)"
    )
    assert all(h.value.get("kind") == "keep" for h in small)


def test_query_plus_filter_browse_control_is_correct(store):
    # Same data; the BROWSE path (no query) filters the full pool, so this works.
    for i in range(5):
        store.put(("f",), f"drop{i}", {"text": "token here", "kind": "drop"})
    for i in range(5):
        store.put(("f",), f"keep{i}", {"text": "token here", "kind": "keep"})
    got = store.search(("f",), filter={"kind": "keep"}, limit=3)
    assert len(got) == 3
    assert all(h.value.get("kind") == "keep" for h in got)


# --------------------------------------------------------------------------- #
# adversarial: FTS5-special query strings must not crash; sane results
# --------------------------------------------------------------------------- #
ADVERSARIAL_QUERIES = [
    '"', '""', '"unterminated', 'AND', 'OR', 'NOT', 'NEAR',
    'a AND b', 'a OR b', 'a NOT b', 'NEAR(a b)', '(hello)', 'hello*', '*',
    '-hello', '^hello', 'cache:eviction', 'name:foo', 'rowid:1', 'foo AND',
    'AND OR NOT', '((()))', 'a*b*c', '"hello world"', 'café', '日本語',
    'hello\x00world', '   ', 'x' * 5000, 'a' * 50000,
]


@pytest.mark.parametrize("q", ADVERSARIAL_QUERIES)
def test_adversarial_query_does_not_crash(store, q):
    store.put(("a",), "k1", {"text": "hello world cache eviction policy"})
    store.put(("a",), "k2", {"text": "plain normal text about dogs"})
    result = store.search(("a",), query=q)
    assert isinstance(result, list)
    # sane: every returned item is a SearchItem inside the searched namespace
    for h in result:
        assert isinstance(h, SearchItem)
        assert h.namespace == ("a",)


def test_fts5_special_chars_dont_match_unrelated_rows(store):
    store.put(("a",), "k1", {"text": "hello world"})
    # column-filter injection shapes must not behave as FTS operators
    assert store.search(("a",), query="name:nonsense") == []
    assert store.search(("a",), query="rowid:1") == []


def test_whitespace_only_query_returns_empty(store):
    seed_basic(store)
    # whitespace is a (truthy) query that sanitizes to empty -> no FTS match
    assert store.search(("memories", "u1"), query="   ") == []


def test_empty_string_query_behaves_as_browse(store):
    seed_basic(store)
    # falsy query routes to the browse path -> returns the namespace contents
    hits = store.search(("memories", "u1"), query="", limit=100)
    assert keyset(hits) == {"fact1", "fact2", "fact3"}


# --------------------------------------------------------------------------- #
# adversarial: unicode + special-char VALUES round-trip and are searchable
# --------------------------------------------------------------------------- #
def test_unicode_value_roundtrips_and_is_searchable(store):
    store.put(("u",), "k", {"text": "café déjà vu 日本語 emoji 🎉", "n": 5})
    got = store.get(("u",), "k")
    assert got.value == {"text": "café déjà vu 日本語 emoji 🎉", "n": 5}
    assert keyset(store.search(("u",), query="café")) == {"k"}
    assert keyset(store.search(("u",), query="日本語")) == {"k"}


def test_value_with_fts_special_chars_roundtrips(store):
    payload = {"text": 'value with "quotes" AND star * and : colon (parens)'}
    store.put(("v",), "k", payload)
    assert store.get(("v",), "k").value == payload
    # a clean token inside the messy value is still findable
    assert keyset(store.search(("v",), query="quotes")) == {"k"}


def test_very_long_query_is_safe(store):
    store.put(("a",), "k", {"text": "needle in haystack"})
    long_q = "needle " + ("filler " * 5000)
    result = store.search(("a",), query=long_q)
    assert isinstance(result, list)


# --------------------------------------------------------------------------- #
# result object shape
# --------------------------------------------------------------------------- #
def test_search_item_shape(store):
    store.put(("s",), "k", {"text": "shape token"})
    hits = store.search(("s",), query="shape")
    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, SearchItem)
    assert h.namespace == ("s",)
    assert h.key == "k"
    assert h.value == {"text": "shape token"}
    # timestamps present and ordered; score may be None (documented) or numeric
    import datetime as _dt
    assert isinstance(h.created_at, _dt.datetime)
    assert isinstance(h.updated_at, _dt.datetime)
    assert h.updated_at >= h.created_at
    assert h.score is None or isinstance(h.score, (int, float))


def test_browse_item_shape(store):
    store.put(("s",), "k", {"text": "browse token"})
    h = store.search(("s",), limit=10)[0]
    assert isinstance(h, SearchItem)
    assert h.namespace == ("s",) and h.key == "k"
    assert h.value == {"text": "browse token"}
