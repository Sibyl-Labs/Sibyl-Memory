"""Adversarial SECURITY / INJECTION / ABUSE suite for SibylStore.

Lane: injection, path-traversal, encoding-collision, isolation, abuse/DoS.

Structure:
  * Tests prefixed ``test_defense_*`` PASS — they confirm a defense holds
    (SQL/FTS injection closed, no encoding collision, tenant + subtree
    isolation solid, traversal rejected).
  * ``test_fix_*`` PASS — regression guards for the two security fixes the
    coordinator applied to store.py (noisy-neighbor _POOL raised 1000 ->
    10_000; $gt/$lt now documented as native Python comparison by design).
  * ``test_residual_*`` are ``xfail`` — they assert the fully-correct behavior
    and document the RESIDUAL architectural limitation that the applied fix
    bounds but does not eliminate.

Run:
  cd <repo>/sibyl-memory-langgraph && . .venv/bin/activate \
    && python -m pytest tests/test_adv_security.py -v
"""

from __future__ import annotations

import os
import tempfile

import pytest

import sibyl_memory_langgraph.store as store_mod
from sibyl_memory_langgraph import SibylStore
from langgraph.store.memory import InMemoryStore


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _new_store(**kw) -> SibylStore:
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    return SibylStore(path=db, tier="free", **kw)


@pytest.fixture()
def store() -> SibylStore:
    return _new_store()


# ========================================================================== #
# DEFENSES THAT HOLD  (these should PASS)
# ========================================================================== #
class TestParameterizationHolds:
    """Confirm the no-SQLi audit finding still holds at the adapter layer."""

    def test_defense_sql_payload_in_key_is_inert(self, store):
        # An apostrophe is a legal name char (parameterized); ';' and '"' are
        # rejected by the client identifier validator. Either way: no SQLi.
        store.put(("legit",), "anchor", {"v": 1})
        store.put(("ns",), "x' OR '1'='1", {"secret": "leak"})
        # The classic injection string is stored verbatim as a literal name,
        # not interpreted, and does not widen the query.
        assert store.get(("ns",), "x' OR '1'='1").value == {"secret": "leak"}
        # The OR-injection did NOT make the anchor visible under ("ns",).
        assert store.get(("ns",), "anchor") is None
        # Table still intact.
        assert store.get(("legit",), "anchor").value == {"v": 1}

    def test_defense_sql_drop_in_key_rejected_not_executed(self, store):
        from sibyl_memory_client import MemoryClient  # noqa: F401
        # ';' is a forbidden identifier char -> ValidationError, not execution.
        with pytest.raises(Exception):
            store.put(("ns",), "a'; DROP TABLE entities;--", {"v": 1})
        # Prove the table was never dropped.
        store.put(("ns",), "ok", {"v": 2})
        assert store.get(("ns",), "ok").value == {"v": 2}

    def test_defense_sql_payload_in_filter_field_and_value_inert(self, store):
        store.put(("f",), "row", {"role": "admin", "n": 1})
        # Filter eval is pure Python; SQL-shaped field/value cannot reach SQL.
        assert store.search(("f",), filter={"role'; DROP TABLE entities;--": "x"}) == []
        assert store.search(("f",), filter={"role": "x' OR '1'='1"}) == []
        # Table intact + legitimate filter still works.
        assert len(store.search(("f",), filter={"role": "admin"})) == 1


class TestFTS5InjectionContained:
    """FTS5 query-injection must not crash and must not cross namespaces."""

    FTS_PAYLOADS = [
        "body:secret", "category:nsB", "name:k", "rowid:1",          # column filters
        "apple OR cherry", "apple AND banana", "NOT apple",           # boolean ops
        "apple NEAR cherry", "^apple", "app*",                       # near / col / prefix
        'secret"', '"unbalanced', '""', "(apple", "apple)", "\\",   # quotes / parens
        "*", "' OR 1=1 --",                                          # wildcard / sqli-shaped
    ]

    def _seed(self):
        s = _new_store()
        s.put(("nsA",), "k", {"text": "secret apple banana"})
        s.put(("nsB",), "k", {"text": "public cherry"})
        return s

    @pytest.mark.parametrize("q", FTS_PAYLOADS)
    def test_defense_fts_payload_no_crash(self, q):
        s = self._seed()
        # Must not raise for any namespace scope.
        s.search(("nsA",), query=q)
        s.search(("nsB",), query=q)
        s.search((), query=q)

    @pytest.mark.parametrize("q", FTS_PAYLOADS)
    def test_defense_fts_payload_no_cross_namespace_leak(self, q):
        s = self._seed()
        # nsB's scope must NEVER surface nsA's "secret" body, regardless of the
        # FTS operator / column-filter / quote trick injected.
        for it in s.search(("nsB",), query=q):
            assert "secret" not in str(it.value), f"FTS payload {q!r} leaked nsA into nsB"

    def test_defense_search_all_keeps_each_item_in_its_own_namespace(self):
        s = self._seed()
        for it in s.search((), query="secret"):
            assert it.namespace == ("nsA",)


class TestNoEncodingCollision:
    """The highest-priority class: prove NO cross-namespace read/write/delete
    collision via key/element separator tricks or unicode slash lookalikes."""

    def test_defense_key_with_slash_vs_deeper_namespace_are_distinct(self, store):
        # ("users",)/"alice/profile"  must NOT collide with
        # ("users","alice")/"profile" — separate category & name columns.
        store.put(("users",), "alice/profile", {"who": "A"})
        store.put(("users", "alice"), "profile", {"who": "B"})
        assert store.get(("users",), "alice/profile").value == {"who": "A"}
        assert store.get(("users", "alice"), "profile").value == {"who": "B"}

    def test_defense_overwrite_does_not_cross_collide(self, store):
        store.put(("users",), "alice/profile", {"who": "A"})
        store.put(("users", "alice"), "profile", {"who": "B"})
        store.put(("users",), "alice/profile", {"who": "A2"})   # overwrite A
        assert store.get(("users", "alice"), "profile").value == {"who": "B"}  # B untouched

    def test_defense_delete_does_not_cross_collide(self, store):
        store.put(("users",), "alice/profile", {"who": "A"})
        store.put(("users", "alice"), "profile", {"who": "B"})
        store.delete(("users",), "alice/profile")              # delete A
        assert store.get(("users", "alice"), "profile").value == {"who": "B"}  # B survives

    def test_defense_unicode_slash_lookalikes_do_not_collide(self, store):
        # U+002F "/" real separator, U+2044 fraction slash, U+FF0F fullwidth.
        store.put(("a", "b"), "k", {"id": "real-sep"})         # category "a/b"
        store.put(("a⁄b",), "k", {"id": "fraction"})      # single element
        store.put(("a／b",), "k", {"id": "fullwidth"})     # single element
        assert store.get(("a", "b"), "k").value == {"id": "real-sep"}
        assert store.get(("a⁄b",), "k").value == {"id": "fraction"}
        assert store.get(("a／b",), "k").value == {"id": "fullwidth"}
        ns = set(store.list_namespaces(limit=100))
        assert {("a", "b"), ("a⁄b",), ("a／b",)} <= ns


class TestPathTraversalRejected:
    def test_defense_dotdot_and_slash_rejected(self, store):
        for bad in [("..",), ("a", ".."), ("....//",), ("..\\..",)]:
            with pytest.raises(Exception):
                store.put(bad, "k", {"v": 1})

    def test_defense_control_chars_in_namespace_rejected_on_write(self, store):
        for bad in [("a\nb",), ("a\tb",), ("a\x00b",)]:
            with pytest.raises(Exception):
                store.put(bad, "k", {"v": 1})

    def test_defense_encoded_traversal_is_inert_literal(self, store):
        # "%2e%2e" is not a real traversal (no filesystem path is built); it is
        # stored as an opaque literal and round-trips, no escape.
        store.put(("%2e%2e",), "k", {"v": 1})
        item = store.get(("%2e%2e",), "k")
        assert item.namespace == ("%2e%2e",)


class TestIsolation:
    def test_defense_tenant_isolation_on_shared_db(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "t.db")
        a = SibylStore(path=db, tier="free", tenant_id="tenantA")
        b = SibylStore(path=db, tier="free", tenant_id="tenantB")
        a.put(("ns",), "k", {"secret": "A-only"})
        assert b.get(("ns",), "k") is None
        assert b.search(("ns",), query="A-only") == []
        assert b.list_namespaces() == []
        assert a.get(("ns",), "k").value == {"secret": "A-only"}

    def test_defense_sibling_subtree_no_leak(self, store):
        store.put(("team", "alpha"), "k", {"v": "alpha"})
        store.put(("team", "beta"), "k", {"v": "beta"})
        hits = store.search(("team", "alpha"), query="alpha")
        assert all(it.namespace == ("team", "alpha") for it in hits)
        # beta's body never appears in alpha's subtree.
        assert not any("beta" in str(it.value) for it in store.search(("team", "alpha")))

    def test_defense_prefix_cannot_string_escape_subtree(self, store):
        # ("a",) prefix must not match sibling ("ab",) via string-prefix bleed.
        store.put(("a",), "k", {"v": "in-a"})
        store.put(("ab",), "k", {"v": "in-ab"})
        subtree = store.search(("a",))
        assert all(it.namespace == ("a",) for it in subtree)
        assert not any("in-ab" in str(it.value) for it in subtree)


class TestFilterCrashParityNonIssue:
    """A 'poison record' (string in a numerically-filtered field) used to crash
    the whole filtered search with a raw TypeError. R16 (2026-07-05) hardened
    this: an incomparable ``$gt``/``$lt`` pair is now read as 'no match' instead
    of raising, so ONE malformed record can no longer abort an otherwise valid
    search (or a batch that contains it). This is a DOCUMENTED divergence from
    the reference InMemoryStore, which still raises on the same input."""

    def test_poison_record_excluded_not_crash_in_sibyl(self):
        sib = _new_store()
        im = InMemoryStore()
        for s in (sib, im):
            s.put(("p",), "good", {"age": 30})
            s.put(("p",), "poison", {"age": "old"})
        # R16: SibylStore does NOT crash — the good record passes, the poison
        # record (str vs int is incomparable) is silently excluded.
        hits = sib.search(("p",), filter={"age": {"$gt": 18}})
        assert {it.key for it in hits} == {"good"}
        # Divergence preserved: InMemoryStore float-coerces "old" and raises.
        with pytest.raises(ValueError):
            im.search(("p",), filter={"age": {"$gt": 18}})


# ========================================================================== #
# FIX REGRESSION GUARDS  (these should PASS after the applied store.py fixes)
# ========================================================================== #
class TestNoisyNeighborFixedAtPoolBound:
    """Finding #1 fix: _POOL raised 1000 -> 10_000. At the original 1100-entity
    repro scale the victim namespace is NO LONGER evicted — it stays searchable
    AND listable even though a sibling namespace wrote far more than the OLD cap.
    1100 rows fit comfortably under the free-tier 2 MB cap (~3,584 rows)."""

    def _flooded_1100(self):
        s = _new_store()
        s.put(("victim",), "vkey", {"text": "victim secret apple"})  # oldest row
        # >old _POOL (1000) but well under the new _POOL (10_000). Written via
        # the client directly only for speed; same tenant / same DB / same path.
        for i in range(1100):
            s._client.set_entity("noisy", f"k{i}", {"text": f"noise item {i}"})
        return s

    def test_fix_victim_get_still_works(self):
        s = self._flooded_1100()
        assert s.get(("victim",), "vkey").value == {"text": "victim secret apple"}

    def test_fix_victim_query_search_not_evicted(self):
        s = self._flooded_1100()
        hits = s.search(("victim",), query="apple")
        assert len(hits) == 1, "regression: victim evicted from query search below the new _POOL cap"

    def test_fix_victim_subtree_listing_not_evicted(self):
        s = self._flooded_1100()
        hits = s.search(("victim",))  # no-query subtree listing
        assert len(hits) == 1, "regression: victim evicted from subtree listing below the new _POOL cap"

    def test_fix_victim_namespace_still_listed(self):
        s = self._flooded_1100()
        assert ("victim",) in s.list_namespaces(limit=5000), (
            "regression: victim namespace dropped from list_namespaces below the new _POOL cap"
        )


class TestGtLtNativeComparisonByDesign:
    """Finding #2 resolution: the divergence from InMemoryStore is INTENTIONAL.
    store.py _OPS uses native Python ordering (NOT float() coercion); the
    docstring now documents this. These tests pin the documented native-
    comparison contract (and assert it deliberately differs from InMemoryStore's
    float coercion for numeric strings, so a future silent regression to float
    coercion would be caught)."""

    def test_fix_gt_uses_native_lexical_comparison(self):
        s = _new_store()
        s.put(("p",), "x", {"v": "10"})
        # native: "10" > "3" is False ('1' < '3'); "10" > "09" is True ('1' > '0').
        assert s.search(("p",), filter={"v": {"$gt": "3"}}) == []
        assert len(s.search(("p",), filter={"v": {"$gt": "09"}})) == 1

    def test_fix_lt_uses_native_lexical_comparison(self):
        s = _new_store()
        s.put(("p",), "x", {"v": "10"})
        # native: "10" < "3" is True (lexical), the opposite of numeric 10 < 3.
        assert len(s.search(("p",), filter={"v": {"$lt": "3"}})) == 1

    def test_fix_native_comparison_intentionally_differs_from_inmemorystore(self):
        sib = _new_store()
        im = InMemoryStore()
        for s in (sib, im):
            s.put(("p",), "x", {"v": "10"})
        sib_hits = len(sib.search(("p",), filter={"v": {"$gt": "3"}}))
        im_hits = len(im.search(("p",), filter={"v": {"$gt": "3"}}))
        # Documented, intentional divergence: native lexical (0) vs float (1).
        assert sib_hits == 0
        assert im_hits == 1
        assert sib_hits != im_hits


# ========================================================================== #
# RESIDUAL (xfail)  — bounded by the fix but not eliminated
# ========================================================================== #
class TestResidualEnumerationEviction:
    """Finding #1 RESIDUAL. The fix raises the enumeration cap (1000 -> 10_000)
    and logs a warning when it is hit, but the candidate pool is still bounded:
    `_list_capped` / `_categories_under` read `list_entities(limit=_POOL)`, the
    client clamps every read to MAX_LIMIT=10_000, and there is NO cursor. Once a
    tenant holds more rows than the cap, the oldest namespaces are still evicted
    from search() and list_namespaces() while remaining retrievable via get().

    A literal >10_000-row repro is not reachable in this sandbox: the free-tier
    cap is 2 MB (~3,584 rows) and paid tiers fail closed offline (server tier
    verification is unreachable, so writes are gated at the free cap). The
    architectural property is cap-magnitude-independent, so it is demonstrated
    faithfully by lowering the enumeration cap and exceeding it — the identical
    `list_entities(limit=_POOL)` code path with the identical eviction outcome.
    """

    @pytest.mark.xfail(
        reason="architectural: bounded by client MAX_LIMIT=10_000 with no cursor; "
        "full fix needs a client-side enumeration API — pending operator decision",
        strict=False,
    )
    def test_residual_oldest_namespace_evicted_beyond_cap(self, monkeypatch):
        # Lower the enumeration cap to exceed it cheaply (stands in for >10_000
        # rows, which the 2 MB free cap blocks). Same code path, same outcome.
        monkeypatch.setattr(store_mod, "_POOL", 50)
        s = _new_store()
        s.put(("victim",), "vkey", {"text": "victim apple"})  # oldest row
        for i in range(60):                                    # > lowered cap (50)
            s._client.set_entity("noisy", f"k{i}", {"t": i})
        # The data still exists ...
        assert s.get(("victim",), "vkey") is not None
        # ... but the CORRECT behavior (still searchable + listable) does NOT
        # hold once the row count exceeds the bounded, cursorless enumeration.
        assert len(s.search(("victim",))) == 1            # residual: returns 0
        assert ("victim",) in s.list_namespaces(limit=5000)  # residual: absent
