"""The verdict contract in the langgraph store (stage 3, 2026-08-31).

langgraph's ``BaseStore.search`` signature is fixed by langgraph: it returns
``list[SearchItem]`` and there is nowhere in it to hand a caller a verdict
object. So this package's obligation under the contract is narrower than the
MCP / hermes / CLI surfaces, and stating it honestly is better than pretending
otherwise:

  1. the cause is no longer THROWN AWAY — ``SibylStore.last_search_verdict``
     exposes it and the debug log carries it, so an empty result is explainable
     even where the API cannot carry the explanation;
  2. this package declares NO cause vocabulary of its own;
  3. the ``SearchResults`` carrier is list-compatible enough that the store's
     row handling (len, slicing, comprehension, filtering) is untouched by it —
     which is the property the whole additive design rests on.
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

from langgraph.store.base import SearchOp

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.verdicts import VerdictCode
from sibyl_memory_langgraph import store as store_mod
from sibyl_memory_langgraph.store import SibylStore


def _store(tmp_path: Path) -> SibylStore:
    c = MemoryClient.local(tmp_path / "lg.db", tenant_id="t1")
    return SibylStore(client=c)


def test_a_zero_result_records_the_canonical_cause(tmp_path):
    s = _store(tmp_path)
    assert s.last_search_verdict is None
    out = s.search(("memories",), query="qwzjvxzzyplm")
    assert out == []
    v = s.last_search_verdict
    assert v is not None
    # The store is genuinely empty here; the raw primitive does not pay the
    # EMPTY_STORE probe, so the honest cause at this level is NO_MATCH.
    assert v.code in (VerdictCode.NO_MATCH, VerdictCode.EMPTY_STORE)
    assert v.explain()


def test_a_non_empty_result_records_ok(tmp_path):
    s = _store(tmp_path)
    s.put(("memories",), "a", {"text": "warehouse lodz shelving"})
    out = s.search(("memories",), query="warehouse")
    assert out
    assert s.last_search_verdict.code is VerdictCode.OK


def test_the_cause_reaches_the_log_for_an_operator_who_holds_no_object(tmp_path, caplog):
    s = _store(tmp_path)
    s.put(("memories",), "a", {"text": "warehouse lodz shelving"})
    with caplog.at_level(logging.DEBUG, logger=store_mod._log.name):
        s.search(("memories",), query="qwzjvxzzyplm")
    assert any("returned no rows" in r.message or "returned no rows" in r.getMessage()
               for r in caplog.records)


def test_the_carrier_does_not_disturb_the_stores_row_handling(tmp_path):
    """The whole additive design rests on SearchResults being a list. The store
    slices it, filters it, and comprehends over it without knowing."""
    s = _store(tmp_path)
    for i in range(5):
        s.put(("memories",), f"k{i}", {"text": f"shared token row {i}"})
    page = s.search(("memories",), query="shared", limit=2, offset=1)
    assert len(page) == 2
    assert all(hasattr(it, "key") for it in page)
    # the raw client call the store makes, exercised directly
    rows = s._client.search_entities("shared", limit=10)
    assert isinstance(rows, list)
    assert rows[:2] == list(rows)[:2]


def test_search_entities_stamps_a_verdict_on_an_invalid_query(tmp_path):
    """The third SDK search entry point. Its `return []` on an unsanitizable
    query was another silent zero."""
    c = MemoryClient.local(tmp_path / "se.db", tenant_id="t1")
    out = c.search_entities("", limit=10)
    assert out == []
    assert out.verdict.code is VerdictCode.NO_MATCH
    assert out.verdict.tokens_total == 0


def test_this_package_declares_no_cause_vocabulary_of_its_own():
    src = inspect.getsource(store_mod)
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    parts = code.replace('"""', "\x00").split("\x00")
    code_only = "".join(parts[0::2])
    for lit in ("abstained_on", "negation_abstain", "coverage_floor",
                "anchor_gate", "prep_filter", "empty_store", "no_match"):
        assert f'"{lit}"' not in code_only, lit
