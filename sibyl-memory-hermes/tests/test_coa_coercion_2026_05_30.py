"""Coerce-on-Adapter (CoA) regression tests — 2026-05-30.

The client (sibyl-memory-client >= 0.4.5) hard-enforces dict/list entity+state
bodies. The hermes adapter coerces agent-supplied primitives into
{"value": body} so agent ergonomics never break against that contract.
Paired with the client-side enforcement (EoC). See provider._coerce_body.
"""
import tempfile, os
import pytest
from sibyl_memory_hermes import SibylMemoryProvider


def _p(tmp_path):
    return SibylMemoryProvider(db_path=tmp_path / "m.db", tenant_id="qa",
                               autoload_credentials=False)


@pytest.mark.parametrize("val", ["a fact", 42, 3.14, True, False, None])
def test_remember_coerces_primitive(tmp_path, val):
    p = _p(tmp_path)
    p.remember("notes", "k", val)
    assert p.recall("notes", "k")["body"] == {"value": val}


@pytest.mark.parametrize("val", ["s", 7, None, False])
def test_set_state_coerces_primitive(tmp_path, val):
    p = _p(tmp_path)
    p.set_state("key", val)
    assert p.get_state("key")["body"] == {"value": val}


def test_dict_and_list_pass_through_uncoerced(tmp_path):
    p = _p(tmp_path)
    p.remember("notes", "d", {"k": "v", "n": 3})
    p.set_state("s", ["a", "b"])
    assert p.recall("notes", "d")["body"] == {"k": "v", "n": 3}
    assert p.get_state("s")["body"] == ["a", "b"]


def test_coerced_primitive_is_searchable(tmp_path):
    p = _p(tmp_path)
    p.remember("notes", "f", "the quick brown fox")
    hits = p.search("fox")
    assert any(h.get("key") == "f" for h in hits)
