"""Tiered retrieval 2.A (2026-10-22): cold-document access in the SDK.

read_reference (paged), list_references (prefix + keyset cursor),
delete_reference (hard delete), and the 1 MiB bound on str bodies that the str
branch of set_reference used to skip.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_memory_client.client import _MAX_BODY_BYTES, MemoryClient
from sibyl_memory_client.exceptions import EntitlementError, ValidationError


@pytest.fixture
def c(tmp_path):
    return MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")


# ---------------------------------------------------------------- 1 MiB bound

def test_str_body_over_1mib_rejected(c):
    with pytest.raises(ValidationError) as ei:
        c.set_reference("doc/big", "x" * (_MAX_BODY_BYTES + 1))
    assert "1 MiB" in str(ei.value)
    assert c.get_reference("doc/big") is None


def test_str_body_bound_is_utf8_bytes_not_chars(c):
    # 400k three-byte chars = 1.2 MB in UTF-8 while only 400k Python chars.
    with pytest.raises(ValidationError):
        c.set_reference("doc/wide", "€" * 400_000)


def test_str_body_exactly_at_bound_accepted(c):
    c.set_reference("doc/edge", "y" * _MAX_BODY_BYTES)
    assert c.read_reference("doc/edge", max_chars=10)["chars"] == _MAX_BODY_BYTES


def test_dict_body_over_1mib_rejected(c):
    with pytest.raises(ValidationError) as ei:
        c.set_reference("doc/bigdict", {"text": "z" * (_MAX_BODY_BYTES + 10)})
    assert "too large" in str(ei.value)
    assert c.get_reference("doc/bigdict") is None


def test_lone_surrogate_rejected_typed(c):
    with pytest.raises(ValidationError):
        c.set_reference("doc/sur", "ok \ud800 bad")


# ---------------------------------------------------------------- read_reference

def test_read_round_trip(c):
    c.set_reference("doc/a", "hello world", metadata={"title": "A"})
    r = c.read_reference("doc/a")
    assert r == {"key": "doc/a", "body": "hello world", "offset": 0, "chars": 11,
                 "next_offset": None, "metadata": {"title": "A"},
                 "updated_at": r["updated_at"]}
    assert r["updated_at"]
    # get_reference is unchanged
    g = c.get_reference("doc/a")
    assert set(g) == {"body", "metadata", "updated_at"} and g["body"] == "hello world"


def test_read_unknown_key_is_none(c):
    assert c.read_reference("doc/missing") is None


def test_read_paging_walks_whole_body(c):
    body = "".join(chr(0x41 + i % 26) for i in range(2_500))
    c.set_reference("doc/p", body)
    out, off, pages = [], 0, 0
    while off is not None:
        r = c.read_reference("doc/p", offset=off, max_chars=1_000)
        assert r["offset"] == off and r["chars"] == 2_500
        out.append(r["body"])
        off = r["next_offset"]
        pages += 1
    assert pages == 3 and "".join(out) == body


def test_read_paging_exact_multiple_has_no_empty_tail(c):
    c.set_reference("doc/m", "a" * 2_000)
    r = c.read_reference("doc/m", offset=1_000, max_chars=1_000)
    assert len(r["body"]) == 1_000 and r["next_offset"] is None


def test_read_paging_unicode_counts_code_points(c):
    body = "\U0001F600" * 5 + "é" * 5 + "中" * 5  # 4-, 2-, 3-byte chars
    c.set_reference("doc/u", body)
    r = c.read_reference("doc/u", offset=3, max_chars=4)
    assert r["chars"] == 15
    assert r["body"] == "\U0001F600\U0001F600éé"
    assert r["next_offset"] == 7
    joined = "".join(c.read_reference("doc/u", offset=o, max_chars=1)["body"] for o in range(15))
    assert joined == body


def test_read_offset_at_or_past_end_is_empty_not_error(c):
    c.set_reference("doc/e", "abc")
    for off in (3, 4, 10_000):
        r = c.read_reference("doc/e", offset=off)
        assert r["body"] == "" and r["next_offset"] is None and r["chars"] == 3


def test_read_empty_body(c):
    c.set_reference("doc/empty", "")
    r = c.read_reference("doc/empty")
    assert r["body"] == "" and r["chars"] == 0 and r["next_offset"] is None


@pytest.mark.parametrize("kwargs", [{"offset": -1}, {"max_chars": 0}, {"max_chars": -5},
                                    {"offset": "1"}, {"max_chars": 1.5}, {"offset": True}])
def test_read_bad_paging_args_raise(c, kwargs):
    c.set_reference("doc/x", "abc")
    with pytest.raises(ValidationError):
        c.read_reference("doc/x", **kwargs)


# ---------------------------------------------------------------- list_references

def test_list_items_shape_and_order(c):
    c.set_reference("doc/b", "bb", metadata={"title": "B"})
    c.set_reference("doc/a", "a")
    out = c.list_references()
    assert [i["key"] for i in out["items"]] == ["doc/a", "doc/b"]
    assert out["items"][1] == {"key": "doc/b", "chars": 2, "metadata": {"title": "B"},
                               "updated_at": out["items"][1]["updated_at"]}
    assert out["next_cursor"] is None


def test_list_cursor_pages_without_gaps_or_repeats(c):
    keys = [f"doc/{i:03d}" for i in range(23)]
    for k in reversed(keys):
        c.set_reference(k, k)
    seen, cursor, pages = [], None, 0
    while True:
        out = c.list_references(limit=10, cursor=cursor)
        seen += [i["key"] for i in out["items"]]
        pages += 1
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert seen == keys and pages == 3


def test_list_cursor_exact_page_boundary_has_no_extra_page(c):
    for i in range(10):
        c.set_reference(f"k/{i}", "x")
    out = c.list_references(limit=10)
    assert len(out["items"]) == 10 and out["next_cursor"] is None


def test_list_prefix_is_literal_and_case_sensitive(c):
    for k in ("doc/a", "doc/b", "Doc/C", "docx/d", "do%/e", "dox/f", "do_/g", "note/h"):
        c.set_reference(k, "x")
    assert [i["key"] for i in c.list_references(prefix="doc/")["items"]] == ["doc/a", "doc/b"]
    assert [i["key"] for i in c.list_references(prefix="do%")["items"]] == ["do%/e"]
    assert [i["key"] for i in c.list_references(prefix="do_")["items"]] == ["do_/g"]
    assert [i["key"] for i in c.list_references(prefix="Doc/")["items"]] == ["Doc/C"]
    assert c.list_references(prefix="zzz")["items"] == []


def test_list_prefix_with_cursor(c):
    for i in range(5):
        c.set_reference(f"doc/{i}", "x")
    c.set_reference("doc0", "x")  # "/" < "0": sorts right after the doc/ block
    first = c.list_references(prefix="doc/", limit=3)
    second = c.list_references(prefix="doc/", limit=3, cursor=first["next_cursor"])
    assert [i["key"] for i in first["items"] + second["items"]] == [f"doc/{i}" for i in range(5)]
    assert second["next_cursor"] is None


def test_list_prefix_top_code_points(c):
    c.set_reference("a\U0010FFFF", "x")
    c.set_reference("a\U0010FFFFb", "x")
    c.set_reference("b", "x")
    keys = [i["key"] for i in c.list_references(prefix="a\U0010FFFF")["items"]]
    assert keys == ["a\U0010FFFF", "a\U0010FFFFb"]
    c.set_reference("퟿z", "x") if False else None  # surrogate edge covered below
    c.set_reference("p퟿", "x")
    c.set_reference("p", "x")
    assert [i["key"] for i in c.list_references(prefix="p퟿")["items"]] == ["p퟿"]


def test_list_does_not_load_bodies_into_items(c):
    c.set_reference("doc/big", "q" * 50_000)
    item = c.list_references()["items"][0]
    assert "body" not in item and item["chars"] == 50_000


def test_list_limit_clamped(c):
    for i in range(3):
        c.set_reference(f"k{i}", "x")
    assert len(c.list_references(limit=-1)["items"]) >= 1
    assert len(c.list_references(limit=10**9)["items"]) == 3


# ---------------------------------------------------------------- delete_reference

def test_delete_round_trip_and_search_index(c):
    c.set_reference("doc/del", "zebraquartz content")
    assert c.search("zebraquartz", tiers=("reference",))
    assert c.delete_reference("doc/del") is True
    assert c.get_reference("doc/del") is None
    assert c.read_reference("doc/del") is None
    assert c.list_references()["items"] == []
    assert c.search("zebraquartz", tiers=("reference",)) == []
    assert c.delete_reference("doc/del") is False


# ---------------------------------------------------------------- tenant isolation

def test_tenant_isolation_on_every_new_method(c):
    c.set_tenant("alice")
    c.set_reference("doc/secret", "alice only", metadata={"title": "S"})
    c.set_tenant("bob")
    assert c.read_reference("doc/secret") is None
    assert c.list_references()["items"] == []
    assert c.list_references(prefix="doc/")["items"] == []
    assert c.delete_reference("doc/secret") is False
    c.set_reference("doc/secret", "bob's own")
    c.set_tenant("alice")
    assert c.read_reference("doc/secret")["body"] == "alice only"
    assert [i["key"] for i in c.list_references()["items"]] == ["doc/secret"]


def test_list_cursor_cannot_cross_tenants(c):
    c.set_tenant("alice")
    c.set_reference("a/1", "x")
    c.set_reference("z/9", "x")
    c.set_tenant("bob")
    c.set_reference("m/5", "x")
    from sibyl_memory_client.client import _encode_ref_cursor
    assert [i["key"] for i in c.list_references(cursor=_encode_ref_cursor("a/1"))["items"]] == ["m/5"]


@pytest.mark.parametrize("bad", ["", "!!!", "\u00e9", 5, "_w"])
def test_list_bad_cursor_is_validation_error(c, bad):
    with pytest.raises(ValidationError):
        c.list_references(cursor=bad)


# ---------------------------------------------------------------- account gate

@pytest.fixture
def locked(tmp_path):
    cl = MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    return cl


@pytest.mark.parametrize("name,args,kwargs", [
    ("read_reference", ("k",), {}),
    ("list_references", (), {}),
    ("delete_reference", ("k",), {}),
])
def test_new_methods_are_gated(locked, name, args, kwargs):
    with pytest.raises(EntitlementError):
        getattr(locked, name)(*args, **kwargs)


def test_delete_reference_is_a_write_op_everywhere():
    from sibyl_memory_client._entitlement import WRITE_OPS
    from sibyl_memory_client.client import _LIFECYCLE_WRITE_KINDS
    from sibyl_memory_client.lifecycle import state as ST
    assert "delete_reference" in WRITE_OPS
    assert ST.WRITE_OPS == WRITE_OPS == _LIFECYCLE_WRITE_KINDS
    assert "read_reference" not in WRITE_OPS and "list_references" not in WRITE_OPS
