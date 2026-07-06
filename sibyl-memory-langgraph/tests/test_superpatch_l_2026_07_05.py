"""Unit L regression suite — sibyl-memory-langgraph 0.1.0 pre-publish gate.

One test per hardening/real finding recovered by the 2026-07-05 super-patch plan
(§4 Unit L). Each test pins the FIXED contract so a future regression is loud:

  R14 + Hardening #2  filtered/query search is a SINGLE FTS MATCH across all
                      categories (not O(categories) MATCHes), bounded by _POOL.
  R32                 negative limit clamps to an empty page (no negative-index
                      slice that broadened the result).
  R33                 limit=None + positive offset returns cleanly (no
                      `offset + None` TypeError).
  R16                 incomparable order-op -> excluded (not TypeError);
                      non-iterable $in/$nin operand -> clean ValueError.
  R34                 empty-dict filter {"f": {}} matches only rows where f == {}
                      (not vacuously every row).
  R35                 unknown match_type raises (does not fail open / match all).
  R25                 batch pre-validates every PutOp, so a malformed op applies
                      NONE of the batch.
  Contract T / H#5    default store resolves tenant from credentials.json via
                      creds.tenant_id -> creds.account_id -> DEFAULT_TENANT.

Hermetic: tmp SQLite DB + tmp credentials, no network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sibyl_memory_client import DEFAULT_TENANT, ValidationError
from sibyl_memory_langgraph import SibylStore
from sibyl_memory_langgraph.store import _POOL
from langgraph.store.base import (
    ListNamespacesOp,
    MatchCondition,
    PutOp,
    SearchOp,
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path):
    s = SibylStore(path=str(tmp_path / "l.db"), tier="free")
    try:
        yield s
    finally:
        s.close()


def keyset(items):
    return {i.key for i in items}


# --------------------------------------------------------------------------- #
# R14 + Hardening #2 — single FTS MATCH across all categories, bounded fan-out
# --------------------------------------------------------------------------- #
def test_r14_query_is_single_search_not_per_category(store):
    """Query search must issue ONE client.search_entities call spanning every
    category, not one MATCH per category. Pre-fix: _categories_under enumerated
    N categories and looped N MATCHes, each buffering up to _POOL rows."""
    N = 300
    ops = [
        PutOp(namespace=(f"cat{i:04d}",), key="k", value={"text": "needle", "keep": "yes" if i % 2 else "no"})
        for i in range(N)
    ]
    for start in range(0, len(ops), 100):
        store.batch(ops[start:start + 100])

    calls = {"n": 0, "rows": 0}
    orig = store._client.search_entities

    def counting(*a, **kw):
        calls["n"] += 1
        res = orig(*a, **kw)
        calls["rows"] += len(res)
        return res

    store._client.search_entities = counting  # type: ignore[attr-defined]

    hits = store.search((), query="needle", filter={"keep": "yes"}, limit=10)

    assert calls["n"] == 1, (
        f"expected ONE search_entities call across all {N} categories, "
        f"got {calls['n']} (per-category fan-out regressed)"
    )
    assert calls["rows"] <= _POOL, (
        f"materialized {calls['rows']} rows; must stay bounded by _POOL={_POOL}"
    )
    assert len(hits) == 10
    assert all(h.value.get("keep") == "yes" for h in hits)


def test_r14_query_prefix_scoping_preserved(store):
    """The single-search + prefix post-filter must still scope to the subtree."""
    store.put(("a", "b"), "k", {"text": "alpha"})
    store.put(("a", "c"), "k", {"text": "alpha"})
    store.put(("z",), "k", {"text": "alpha"})
    hits = store.search(("a",), query="alpha")
    assert {"/".join(h.namespace) for h in hits} == {"a/b", "a/c"}


def test_r14_query_plus_filter_not_truncated(store):
    """Filter-passing rows ranked deeper than `limit` are not dropped before the
    filter runs (full pool fetched when post-filtering)."""
    for i in range(6):
        store.put(("f",), f"drop{i}", {"text": "tok", "kind": "drop"})
    for i in range(6):
        store.put(("f",), f"keep{i}", {"text": "tok", "kind": "keep"})
    full = store.search(("f",), query="tok", filter={"kind": "keep"}, limit=100)
    assert keyset(full) == {f"keep{i}" for i in range(6)}


# --------------------------------------------------------------------------- #
# R32 — negative limit clamps to empty (no negative-index slice)
# --------------------------------------------------------------------------- #
def test_r32_search_negative_limit_is_empty(store):
    for i in range(5):
        store.put(("ns",), f"k{i}", {"i": i})
    assert store.search(("ns",), limit=-1) == []
    assert store.search(("ns",), query="k", limit=-1) == []


def test_r32_list_namespaces_negative_limit_is_empty(store):
    for i in range(5):
        store.put(("ns", f"s{i}"), "k", {"i": i})
    assert store.list_namespaces(limit=-2) == []


def test_r32_negative_offset_clamped_to_zero(store):
    for i in range(3):
        store.put(("ns",), f"k{i}", {"i": i})
    # A negative offset must not slice from the end; clamp to 0.
    got = store.search(("ns",), limit=3, offset=-5)
    assert len(got) == 3


# --------------------------------------------------------------------------- #
# R33 — limit=None + positive offset returns cleanly (no TypeError)
# --------------------------------------------------------------------------- #
def test_r33_search_limit_none_offset_no_typeerror(store):
    for i in range(3):
        store.put(("ns",), f"k{i}", {"i": i})
    # Direct op path: limit=None was `offset + None` -> TypeError pre-fix.
    res = store.batch([SearchOp(namespace_prefix=("ns",), limit=None, offset=5)])
    assert isinstance(res, list) and res[0] == []  # offset beyond end -> clean []


def test_r33_search_limit_none_uses_default(store):
    for i in range(15):
        store.put(("ns",), f"k{i:02d}", {"i": i})
    res = store.batch([SearchOp(namespace_prefix=("ns",), limit=None, offset=0)])
    assert len(res[0]) == 10  # None normalizes to the SearchOp default (10)


def test_r33_list_namespaces_limit_none_no_typeerror(store):
    for i in range(3):
        store.put(("ns", f"s{i}"), "k", {"i": i})
    res = store.batch([ListNamespacesOp(match_conditions=None, max_depth=None, limit=None, offset=2)])
    assert isinstance(res[0], list)  # no `offset + None` crash


# --------------------------------------------------------------------------- #
# R16 — incomparable / non-iterable operands never raise raw TypeError
# --------------------------------------------------------------------------- #
def test_r16_gt_on_dict_value_excludes_not_crash(store):
    store.put(("d",), "k", {"obj": {"a": 1}})   # dict in an order-filtered field
    store.put(("d",), "n", {"obj": 5})          # comparable
    # dict-vs-int is incomparable -> that row excluded, no TypeError.
    hits = store.search(("d",), filter={"obj": {"$gt": 1}})
    assert keyset(hits) == {"n"}


def test_r16_in_non_iterable_operand_is_valueerror_not_typeerror(store):
    store.put(("c",), "k", {"count": 3})
    with pytest.raises(ValueError) as exc:
        store.search(("c",), filter={"count": {"$in": 5}})
    assert "$in" in str(exc.value)
    # sanity: it is NOT a TypeError
    assert not isinstance(exc.value, TypeError)


def test_r16_nin_non_iterable_operand_is_valueerror(store):
    store.put(("c",), "k", {"count": 3})
    with pytest.raises(ValueError) as exc:
        store.search(("c",), filter={"count": {"$nin": 7}})
    assert "$nin" in str(exc.value)


def test_r16_in_with_iterable_still_works(store):
    store.put(("c",), "a", {"count": 3})
    store.put(("c",), "b", {"count": 9})
    assert keyset(store.search(("c",), filter={"count": {"$in": [3, 4]}})) == {"a"}


# --------------------------------------------------------------------------- #
# R34 — empty-dict filter matches only rows where the field equals {}
# --------------------------------------------------------------------------- #
def test_r34_empty_dict_filter_is_equality_not_vacuous(store):
    store.put(("r",), "a", {"f": {}})          # f == {}
    store.put(("r",), "b", {"f": {"x": 1}})    # f != {}
    store.put(("r",), "c", {"g": 9})           # no f at all
    hits = store.search(("r",), filter={"f": {}}, limit=100)
    assert keyset(hits) == {"a"}, (
        "empty-dict filter must fall to the equality branch (match only f=={}), "
        "not vacuously match every row"
    )


# --------------------------------------------------------------------------- #
# R35 — unknown match_type raises rather than matching all namespaces
# --------------------------------------------------------------------------- #
def test_r35_unknown_match_type_raises(store):
    store.put(("a", "b"), "k", {"x": 1})  # non-empty so the matcher actually runs
    bad = ListNamespacesOp(
        match_conditions=(MatchCondition(match_type="exact", path=("a",)),),
        max_depth=None,
        limit=100,
        offset=0,
    )
    with pytest.raises(ValueError) as exc:
        store.batch([bad])
    assert "match_type" in str(exc.value)


def test_r35_known_match_types_still_work(store):
    store.put(("a", "b"), "k", {"x": 1})
    store.put(("c", "d"), "k", {"x": 1})
    ok = ListNamespacesOp(
        match_conditions=(MatchCondition(match_type="prefix", path=("a",)),),
        max_depth=None,
        limit=100,
        offset=0,
    )
    res = store.batch([ok])
    assert ("a", "b") in res[0] and ("c", "d") not in res[0]


# --------------------------------------------------------------------------- #
# R25 — batch pre-validates all PutOps; a malformed op applies NONE of the batch
# --------------------------------------------------------------------------- #
def test_r25_batch_bad_key_applies_none(store):
    ops = [
        PutOp(namespace=("ns",), key="k1", value={"a": 1}),
        PutOp(namespace=("ns",), key="k2", value={"b": 2}),
        PutOp(namespace=("ns",), key=b"bytes-not-a-str", value={"c": 3}),  # bad 3rd op
    ]
    with pytest.raises((ValidationError, ValueError, TypeError)):
        store.batch(ops)
    # Pre-flight caught op3 BEFORE op1/op2 executed -> nothing persisted.
    assert store.get(("ns",), "k1") is None
    assert store.get(("ns",), "k2") is None


def test_r25_batch_nonserializable_value_applies_none(store):
    ops = [
        PutOp(namespace=("ns",), key="k1", value={"a": 1}),
        PutOp(namespace=("ns",), key="k2", value={"b": 2}),
        PutOp(namespace=("ns",), key="k3", value={"s": {1, 2, 3}}),  # set -> not JSON
    ]
    with pytest.raises(ValidationError):
        store.batch(ops)
    assert store.get(("ns",), "k1") is None
    assert store.get(("ns",), "k2") is None


def test_r25_batch_bad_namespace_applies_none(store):
    ops = [
        PutOp(namespace=("ns",), key="k1", value={"a": 1}),
        PutOp(namespace=("ns", ".."), key="k2", value={"b": 2}),  # path-traversal ns
    ]
    with pytest.raises(ValueError):
        store.batch(ops)
    assert store.get(("ns",), "k1") is None


def test_r25_valid_batch_still_applies_all(store):
    ops = [
        PutOp(namespace=("ns",), key="k1", value={"a": 1}),
        PutOp(namespace=("ns",), key="k2", value={"b": 2}),
        PutOp(namespace=("ns",), key="k3", value={"c": 3}),
    ]
    store.batch(ops)
    assert store.get(("ns",), "k1").value == {"a": 1}
    assert store.get(("ns",), "k3").value == {"c": 3}


# --------------------------------------------------------------------------- #
# Contract T / Hardening #5 — default store resolves tenant from credentials.json
# --------------------------------------------------------------------------- #
def _write_creds(dir_path: Path, **fields) -> Path:
    p = Path(dir_path) / "credentials.json"
    p.write_text(json.dumps(fields), encoding="utf-8")
    return p


def test_contract_t_resolves_tenant_id_first(tmp_path):
    _write_creds(tmp_path, tenant_id="tenant-XYZ", account_id="acct-ABC")
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == "tenant-XYZ"
    finally:
        s.close()


def test_contract_t_falls_back_to_account_id(tmp_path):
    # tenant_id absent -> ladder rung 2 = account_id.
    _write_creds(tmp_path, account_id="acct-ABC")
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == "acct-ABC"
    finally:
        s.close()


def test_contract_t_empty_tenant_falls_through_to_account(tmp_path):
    # present-but-empty tenant_id must NOT bind ""; fall through to account_id.
    _write_creds(tmp_path, tenant_id="", account_id="acct-ABC")
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == "acct-ABC"
    finally:
        s.close()


def test_contract_t_default_tenant_when_no_creds(tmp_path):
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == DEFAULT_TENANT
    finally:
        s.close()


def test_contract_t_all_empty_creds_resolve_default(tmp_path):
    _write_creds(tmp_path, tenant_id="", account_id="")
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == DEFAULT_TENANT
    finally:
        s.close()


def test_contract_t_explicit_tenant_overrides_creds(tmp_path):
    _write_creds(tmp_path, tenant_id="tenant-XYZ")
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free", tenant_id="explicit-T")
    try:
        assert s._client.get_tenant() == "explicit-T"
    finally:
        s.close()


def test_contract_t_symlinked_creds_are_ignored(tmp_path):
    # A symlinked credentials.json is treated as absent (SEC-11 parity), so a
    # hostile/stale link cannot redirect identity resolution.
    real = tmp_path / "real_creds.json"
    real.write_text(json.dumps({"tenant_id": "hijack"}), encoding="utf-8")
    link = tmp_path / "credentials.json"
    os.symlink(real, link)
    s = SibylStore(path=str(tmp_path / "memory.db"), tier="free")
    try:
        assert s._client.get_tenant() == DEFAULT_TENANT
    finally:
        s.close()


def test_contract_t_explicit_client_ignores_creds(tmp_path):
    from sibyl_memory_client import MemoryClient

    _write_creds(tmp_path, tenant_id="from-creds")
    client = MemoryClient.local(str(tmp_path / "memory.db"), tenant_id="from-client")
    s = SibylStore(client=client)
    try:
        assert s._client.get_tenant() == "from-client"
    finally:
        client.close() if hasattr(client, "close") else None
