"""Agent handoff through SibylStore (spec agent-handoff-messages 2026-10-03, rows L1, L2).

The store propagates EntitlementError unchanged; the exception carries the
handoff: `e.next`, `e.human_action`, and `str(e)` ending with the block.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from langgraph.store.base import GetOp

from sibyl_memory_client import EntitlementError
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_langgraph import SibylStore
from sibyl_memory_langgraph import store as store_mod


@pytest.mark.parametrize("op", ["put", "get", "search", "delete", "list_namespaces"])
def test_l1_no_account_hands_off_to_sibyl_init(op, entitled_test_home) -> None:
    (entitled_test_home / "credentials.json").unlink()
    store = SibylStore()
    calls = {
        "put": lambda: store.put(("m", "u1"), "k", {"text": "x"}),
        "get": lambda: store.get(("m", "u1"), "k"),
        "search": lambda: store.search(("m",), query="anything"),
        "delete": lambda: store.delete(("m", "u1"), "k"),
        "list_namespaces": lambda: store.list_namespaces(prefix=("m",)),
    }
    with pytest.raises(EntitlementError) as exc:
        calls[op]()
    assert exc.value.next == "sibyl init" and exc.value.human_action is None
    assert str(exc.value).splitlines()[-1] == "next: sibyl init"


def test_l1_abatch_and_a_human_step(entitled_test_home) -> None:
    write_entitlement(entitled_test_home, "", denied={"code": "under_review", "at": time.time()})
    store = SibylStore()
    with pytest.raises(EntitlementError) as exc:
        asyncio.run(store.abatch([GetOp(namespace=("m",), key="k")]))
    e = exc.value
    assert e.next == "done" and e.human_action.startswith("your account is under review")
    lines = str(e).splitlines()
    assert lines[-2] == f"ask your human: {e.human_action}" and lines[-1] == "next: done"


def test_l2_the_docstring_names_the_attributes() -> None:
    doc = store_mod.SibylStore.__doc__
    assert "e.next" in doc and "e.human_action" in doc
