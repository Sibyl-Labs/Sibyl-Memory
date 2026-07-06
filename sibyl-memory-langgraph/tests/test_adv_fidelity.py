"""Adversarial DATA-FIDELITY suite for SibylStore.

Lane: value/key/timestamp round-trip fidelity. Hunts silent corruption,
silent data loss, and round-trip type infidelity that a functional suite
(which tends to use clean str-keyed JSON dicts) sails right past.

Reference oracle: langgraph's InMemoryStore (deepcopy semantics, no JSON
boundary). Where SibylStore must cross a JSON+SQLite boundary, anything that
silently diverges from the InMemoryStore round-trip is the finding.

Status: the four original fidelity holes (silent non-string-key coercion and
collision data-loss) were FIXED in store.py via ``_ensure_string_keys`` — a
recursive pre-write guard that raises ``ValueError`` on any non-string dict
key (int / float / bool / NaN), nested inside dicts/lists too, on both the
``put`` and ``batch`` paths. The former ``test_HOLE_*`` tests below now assert
that LOUD rejection (corrected behavior: no silent merge, no silent type
coercion, no data loss). The tuple->list coercion is documented, intentional
JSON behavior (inherent to any JSON-backed store) and is asserted as such.
All tests in this file should PASS.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from sibyl_memory_langgraph import SibylStore
from sibyl_memory_client.exceptions import ValidationError, StorageError
from langgraph.store.base import PutOp
from langgraph.store.memory import InMemoryStore


NS = ("mem", "u1")


def fresh() -> SibylStore:
    return SibylStore(path=os.path.join(tempfile.mkdtemp(), "t.db"), tier="free")


# ---------------------------------------------------------------------------
# FORMER HOLES — now FIXED. These assert the corrected loud-rejection behavior
# (ValueError before write) instead of the old silent merge/coercion.
# ---------------------------------------------------------------------------

def test_FIXED_nonstring_key_collision_now_raises():
    """CRITICAL (was silent data loss): two DISTINCT dict keys that would
    stringify to the same JSON key used to be silently merged, dropping one
    value. _ensure_string_keys now raises ValueError BEFORE any write — no
    silent loss, and nothing is persisted.
    """
    s = fresh()
    try:
        with pytest.raises(ValueError):
            s.put(NS, "k", {"m": {1: "a", "1": "b"}})
        # write was rejected atomically — nothing landed
        assert s.get(NS, "k") is None
    finally:
        s.close()


def test_FIXED_nan_key_now_raises():
    """HIGH (was silent data loss bypassing the json_valid CHECK): NaN dict
    keys (nan != nan, so genuinely two keys) used to collapse to {"NaN": ...}
    and slip past the CHECK constraint because the *key*, once stringified,
    made the body valid JSON. Now rejected loudly before write.
    """
    s = fresh()
    try:
        n1, n2 = float("nan"), float("nan")
        assert len({n1: "a", n2: "b"}) == 2  # genuinely two keys in Python
        with pytest.raises(ValueError):
            s.put(NS, "k", {n1: "a", n2: "b"})
        assert s.get(NS, "k") is None
    finally:
        s.close()


def test_FIXED_nonstring_key_coercion_now_raises():
    """HIGH (was silent type coercion): int / float / bool dict keys used to
    silently become str on round-trip. Now any non-string key — including
    nested in dicts and lists, and on the batch path — raises ValueError.
    """
    s = fresh()
    try:
        # nested-in-dict int and float keys
        with pytest.raises(ValueError):
            s.put(NS, "k", {"counts": {1: "a", 2: "b"}, "ratio": {1.5: "x"}})
        assert s.get(NS, "k") is None

        # bool key (bool is not str)
        with pytest.raises(ValueError):
            s.put(NS, "b", {True: "a"})

        # non-string key nested inside a list element
        with pytest.raises(ValueError):
            s.put(NS, "l", {"items": [{"ok": 1}, {2: "bad"}]})

        # the batch/PutOp path is guarded too
        with pytest.raises(ValueError):
            s.batch([PutOp(NS, "p", {9: "x"})])
    finally:
        s.close()


def test_tuple_value_roundtrips_as_list():
    """DOCUMENTED, INTENTIONAL (not a hole): JSON has no tuple type, so a tuple
    value round-trips as a list. This is inherent to any JSON-backed store and
    is left unchanged by design (unlike InMemoryStore's deepcopy, which keeps
    the tuple). The values are preserved; only the container type changes, and
    it does so deterministically — no data loss.
    """
    s = fresh()
    try:
        s.put(NS, "k", {"t": (1, 2, 3)})
        out = s.get(NS, "k").value["t"]
        assert out == [1, 2, 3]
        assert isinstance(out, list)  # documented JSON coercion

        # InMemoryStore diverges here (keeps the tuple); recorded for context.
        ref = InMemoryStore()
        ref.put(NS, "k", {"t": (1, 2, 3)})
        assert isinstance(ref.get(NS, "k").value["t"], tuple)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# NON-ISSUES — probed surfaces that round-trip correctly (these PASS).
# ---------------------------------------------------------------------------

def test_bool_int_float_stay_distinct():
    """True is not 1, 1 is not 1.0 — JSON keeps the three apart on values."""
    s = fresh()
    try:
        s.put(NS, "k", {"b": True, "i": 1, "f": 1.0, "b0": False, "z": 0})
        v = s.get(NS, "k").value
        assert v["b"] is True and isinstance(v["b"], bool)
        assert isinstance(v["i"], int) and not isinstance(v["i"], bool)
        assert isinstance(v["f"], float) and v["f"] == 1.0
        assert v["b0"] is False
        assert v["i"] == 1 and v["z"] == 0
    finally:
        s.close()


def test_empty_dict_and_list_roundtrip():
    """Empty {} stays {}, empty [] stays [] (regression D: body `or {}` would
    have coerced [] -> {}; that is fixed)."""
    s = fresh()
    try:
        s.put(NS, "d", {})
        s.put(NS, "l", [])
        assert s.get(NS, "d").value == {}
        out = s.get(NS, "l").value
        assert out == [] and isinstance(out, list)
    finally:
        s.close()


def test_none_inside_dict_vs_putop_none_delete():
    """A None *inside* a value is preserved; PutOp(value=None) is the delete
    sentinel. No confusion between the two."""
    s = fresh()
    try:
        s.put(NS, "keep", {"x": None, "y": [None, None]})
        assert s.get(NS, "keep").value == {"x": None, "y": [None, None]}

        s.put(NS, "del", {"x": 1})
        s.batch([PutOp(NS, "del", None)])
        assert s.get(NS, "del") is None  # deleted, not stored as {"x": None}
    finally:
        s.close()


def test_large_ints_exact():
    """Arbitrary-precision ints survive exactly (JSON has no int width)."""
    s = fresh()
    try:
        big = 2 ** 200 + 12345
        neg = -(2 ** 100)
        s.put(NS, "k", {"n": big, "neg": neg})
        v = s.get(NS, "k").value
        assert v["n"] == big and isinstance(v["n"], int)
        assert v["neg"] == neg
    finally:
        s.close()


def test_unicode_values_byte_identical():
    """emoji ZWJ sequences, RTL overrides, zero-width chars, and combining
    sequences round-trip byte-identical (ensure_ascii=False, no normalization)."""
    s = fresh()
    try:
        samples = {
            "emoji": "hi \U0001f469‍\U0001f469‍\U0001f467‍\U0001f466 fam",
            "rtl": "‮HELLO‬",
            "zw": "a​b‌c‍",
            "combining": "é",  # e + COMBINING ACUTE (NFD form)
        }
        for k, v in samples.items():
            s.put(NS, k, {"t": v})
            got = s.get(NS, k).value["t"]
            assert got == v, f"{k}: {got!r} != {v!r}"
            # byte-identical, not just equal-looking
            assert got.encode("utf-8") == v.encode("utf-8")
    finally:
        s.close()


def test_nfc_nfd_keys_remain_distinct():
    """NFC and NFD forms of the same grapheme are different strings; the store
    does NOT normalize, so put(NFC)/get(NFD) misses. This is CORRECT (matches
    InMemoryStore tuple-key semantics) but documented here as a caller trap."""
    import unicodedata
    s = fresh()
    try:
        nfc = unicodedata.normalize("NFC", "é")   # 1 codepoint
        nfd = unicodedata.normalize("NFD", "é")   # e + combining
        assert nfc != nfd
        s.put(NS, nfc, {"v": 1})
        assert s.get(NS, nfd) is None      # distinct key -> miss (expected)
        assert s.get(NS, nfc).value == {"v": 1}

        ref = InMemoryStore()
        ref.put(NS, nfc, {"v": 1})
        assert ref.get(NS, nfd) is None    # oracle agrees
    finally:
        s.close()


def test_no_value_aliasing():
    """Returned value must not share a mutable reference with the stored data.
    Mutating the put-source or a returned value must not leak into the store.
    SibylStore serializes through JSON, so it is fully copy-isolated (safe)."""
    s = fresh()
    try:
        src = {"list": [1, 2, 3]}
        s.put(NS, "a", src)
        src["list"].append(999)                       # mutate after put
        assert s.get(NS, "a").value == {"list": [1, 2, 3]}

        r = s.get(NS, "a")
        r.value["list"].append(777)                   # mutate returned
        assert s.get(NS, "a").value == {"list": [1, 2, 3]}
    finally:
        s.close()


def test_long_key_clean_error_at_limit():
    """1024-char key stored; 1025 raises a typed ValidationError (clean error,
    no silent truncation of the key)."""
    s = fresh()
    try:
        s.put(NS, "x" * 1024, {"v": 1})
        assert s.get(NS, "x" * 1024).value == {"v": 1}
        with pytest.raises(ValidationError):
            s.put(NS, "x" * 1025, {"v": 1})
    finally:
        s.close()


def test_whitespace_key_preserved_no_trim():
    """Leading/trailing whitespace in a key is preserved verbatim (no silent
    trim that would alias '  k  ' and 'k')."""
    s = fresh()
    try:
        s.put(NS, "  spaced  ", {"v": 1})
        assert s.get(NS, "  spaced  ").value == {"v": 1}
        assert s.get(NS, "spaced") is None
    finally:
        s.close()


def test_nonserializable_value_clean_error():
    """A set (not JSON-serializable) raises a typed ValidationError rather than
    corrupting or partially writing."""
    s = fresh()
    try:
        with pytest.raises(ValidationError):
            s.put(NS, "k", {"s": {1, 2, 3}})
    finally:
        s.close()


def test_nonfinite_float_value_rejected_not_corrupted():
    """NaN / Infinity / -Infinity as VALUES are rejected by the json_valid()
    CHECK constraint (surfaced as StorageError) — the store never persists the
    invalid 'NaN'/'Infinity' JSON tokens. No silent corruption.

    NOTE (diagnosability, low sev): the error is a generic
    'SQLite error: IntegrityError' StorageError, not a ValidationError naming
    non-finite floats. InMemoryStore happily stores NaN; SibylStore rejects.
    Safe divergence (reject > corrupt) but a clearer pre-write guard in the
    store would beat leaking a raw IntegrityError reason.
    """
    s = fresh()
    try:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(StorageError):
                s.put(NS, "k", {"x": bad})
            # nothing persisted
            assert s.get(NS, "k") is None
    finally:
        s.close()


def _nest(depth):
    d = {}
    cur = d
    for _ in range(depth):
        cur["n"] = {}
        cur = cur["n"]
    cur["leaf"] = 1
    return d


def test_deep_nesting_roundtrips_within_limit():
    """Deep nesting round-trips intact at safe depth (no silent truncation of
    the structure)."""
    s = fresh()
    try:
        s.put(NS, "ok", _nest(500))
        c = s.get(NS, "ok").value
        n = 0
        while "n" in c:
            c = c["n"]
            n += 1
        assert n == 500 and c["leaf"] == 1
    finally:
        s.close()


def test_deep_nesting_overlimit_raises_clean_valueerror():
    """Over-limit deep nesting surfaces as a clean ValueError (FIXED).

    _ensure_string_keys is iterative with an explicit depth bound, so a crafted
    ultra-deep value raises a typed ValueError BEFORE it can reach (and
    RecursionError) the client's JSON encoder. Pre-write: nothing is persisted.
    Regression guard for the iterative + depth-bound fix (was: RecursionError)."""
    s = fresh()
    try:
        with pytest.raises(ValueError):
            s.put(NS, "toodeep", _nest(2000))
    finally:
        s.close()


def test_body_cap_boundary_no_truncation():
    """Body just under the ~1 MiB per-value cap stores intact; just over raises
    a clean ValidationError. Never silently truncated."""
    s = fresh()
    try:
        under = "a" * (1024 * 1024 - 2000)
        s.put(NS, "u", {"d": under})
        assert s.get(NS, "u").value["d"] == under

        over = "a" * (1024 * 1024 + 5000)
        with pytest.raises(ValidationError):
            s.put(NS, "o", {"d": over})
    finally:
        s.close()


def test_timestamps_tzaware_monotonic_and_durable():
    """created_at/updated_at are tz-aware UTC, created_at is preserved across
    overwrite, updated_at is non-decreasing, and both survive close()+reopen."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    s = SibylStore(path=path, tier="free")
    try:
        s.put(NS, "k", {"v": 1})
        i1 = s.get(NS, "k")
        assert i1.created_at.tzinfo is not None
        assert i1.updated_at.tzinfo is not None
        created0 = i1.created_at

        import time
        time.sleep(0.01)
        s.put(NS, "k", {"v": 2})
        i2 = s.get(NS, "k")
        assert i2.created_at == created0           # preserved
        assert i2.updated_at >= i1.updated_at      # non-decreasing
    finally:
        s.close()

    # reopen the same DB file
    s2 = SibylStore(path=path, tier="free")
    try:
        i3 = s2.get(NS, "k")
        assert i3 is not None
        assert i3.created_at == created0           # durable across reopen
        assert i3.value == {"v": 2}
    finally:
        s2.close()
