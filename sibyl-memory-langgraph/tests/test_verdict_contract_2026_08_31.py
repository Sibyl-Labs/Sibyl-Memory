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

import ast
import inspect
import logging
from pathlib import Path

from langgraph.store.base import SearchOp

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.verdicts import GateCause, VerdictCode
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
    # `search_entities` is the raw primitive and deliberately does not pay the
    # EMPTY_STORE probe, so the honest cause at this level is exactly NO_MATCH.
    # Asserting a set of two would accept either direction and so could not
    # catch a regression in which one is reported.
    assert v.code is VerdictCode.NO_MATCH
    assert v.explain()


def test_a_page_this_store_emptied_never_ships_as_ok(tmp_path):
    """REGRESSION (adversarial review, 2026-08-31). The verdict was recorded
    from the RAW FTS result, before the namespace-prefix filter, the value
    filter and the offset/limit slice. A page emptied by any of those three
    shipped `code: ok` with `explain(): "5 row(s) matched."` while the caller
    held zero rows — a confidently wrong explanation, which is worse than the
    silence this contract exists to delete."""
    s = _store(tmp_path)
    for i in range(5):
        s.put(("memories",), f"k{i}", {"text": "shared token row", "n": i})

    # A) the engine really did match, rows returned: OK is honest
    assert s.search(("memories",), query="shared")
    assert s.last_search_verdict.code is VerdictCode.OK

    # B) the namespace prefix filters every row away
    assert s.search(("nothing-here",), query="shared") == []
    assert s.last_search_verdict.code is VerdictCode.NO_MATCH

    # C) offset past the end
    assert s.search(("memories",), query="shared", offset=99) == []
    assert s.last_search_verdict.code is VerdictCode.NO_MATCH

    # D) a value filter removes every row
    assert s.search(("memories",), query="shared", filter={"n": 999}) == []
    assert s.last_search_verdict.code is VerdictCode.NO_MATCH
    assert "5 row(s) matched" not in s.last_search_verdict.explain()


def test_the_verdict_is_never_stale_from_a_previous_call(tmp_path):
    """Two branches never touched `_last_search_verdict` at all, so it kept the
    previous call's answer — a verdict about a search that already happened,
    stated confidently."""
    s = _store(tmp_path)
    s.put(("memories",), "a", {"text": "warehouse lodz shelving"})
    assert s.search(("memories",), query="warehouse")
    assert s.last_search_verdict.code is VerdictCode.OK

    # limit=0: the early return
    assert s.search(("memories",), query="warehouse", limit=0) == []
    assert s.last_search_verdict.code is VerdictCode.NO_MATCH

    # the non-query listing branch, filtered to nothing
    assert s.search(("memories",), query="warehouse")            # re-arm OK
    assert s.last_search_verdict.code is VerdictCode.OK
    assert s.search(("nothing-here",)) == []
    assert s.last_search_verdict.code is VerdictCode.NO_MATCH


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

# --------------------------------------------------------------------------
# one vocabulary
# --------------------------------------------------------------------------
def _code_only(text: str) -> str:
    """Strip docstrings and comments, leaving code.

    Parsed, not split on `\"\"\"`. The first version of this helper did
    `text.replace('\"\"\"', chr(0)).split(chr(0))[0::2]`, which silently INVERTS
    which half it scans when the marker count is odd — the test would then check
    the prose and skip the code, and still pass.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:                                # pragma: no cover
        return text
    spans = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):                 # ast.IfExp.body is an expr
            continue
        for child in body:
            if (isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)):
                spans.append((child.lineno, child.end_lineno))
    drop = {i for a, b in spans for i in range(a, b + 1)}
    return "\n".join(ln.split("#", 1)[0]
                     for i, ln in enumerate(text.splitlines(), 1) if i not in drop)


#: Derived from the enums, so renaming a cause updates the guard automatically.
CAUSE_LITERALS = tuple([c.value for c in VerdictCode if c is not VerdictCode.OK]
                       + [g.value for g in GateCause])


def _assert_declares_no_vocabulary(*modules):
    """BOTH quote styles. These tests originally checked only double quotes, and
    an adversarial review slipped `_LOCAL_CAUSE_COPY = 'abstained_on'` into the
    MCP server past a green suite — exactly the drift the guard exists to stop."""
    offenders = []
    for mod in modules:
        code = _code_only(inspect.getsource(mod))
        for lit in CAUSE_LITERALS:
            if f'"{lit}"' in code or f"'{lit}'" in code:
                offenders.append((mod.__name__, lit))
    assert offenders == [], f"cause strings re-declared outside verdicts.py: {offenders}"


def test_this_package_declares_no_cause_vocabulary_of_its_own():
    _assert_declares_no_vocabulary(store_mod)
