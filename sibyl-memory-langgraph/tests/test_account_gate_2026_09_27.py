"""sibyl-memory-langgraph 0.3.0 account gate (spec account-gated-plugin 2026-09-27, 9.4).

SibylStore against the REAL gate in sibyl-memory-client, with the conftest's
isolated, test-entitled home.
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.store.base import GetOp, PutOp

from sibyl_memory_client import DEFAULT_TENANT, EntitlementError, MemoryClient
from sibyl_memory_client.testing import NullEntitlementGate, write_credentials
from sibyl_memory_langgraph import SibylStore


def _signed_out(home):
    (home / "credentials.json").unlink()


def test_store_constructs_without_an_account(entitled_test_home) -> None:
    _signed_out(entitled_test_home)
    SibylStore()                                   # never raises for the gate


@pytest.mark.parametrize("op", ["put", "get", "search", "delete", "list_namespaces"])
def test_every_store_op_raises_entitlement_error(op, entitled_test_home) -> None:
    _signed_out(entitled_test_home)
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
    assert exc.value.code == "no_account"


def test_abatch_propagates(entitled_test_home) -> None:
    _signed_out(entitled_test_home)
    store = SibylStore()
    with pytest.raises(EntitlementError):
        asyncio.run(store.abatch([GetOp(namespace=("m",), key="k")]))


def test_default_tenant_store_is_refused(entitled_test_home) -> None:
    _signed_out(entitled_test_home)
    store = SibylStore(tenant_id=DEFAULT_TENANT)
    with pytest.raises(EntitlementError):
        store.batch([PutOp(namespace=("m",), key="k", value={"x": 1})])


def test_sibling_credentials_without_session_token_are_no_account(tmp_path) -> None:
    write_credentials(tmp_path, session_token=None)
    store = SibylStore(path=str(tmp_path / "memory.db"))
    with pytest.raises(EntitlementError) as exc:
        store.put(("m",), "k", {"x": 1})
    assert exc.value.code == "no_account"


def test_sibling_legacy_credentials_bootstrap_through_the_refresh(tmp_path) -> None:
    write_credentials(tmp_path)                    # legacy file, no entitlement yet
    store = SibylStore(path=str(tmp_path / "memory.db"))
    store.put(("m",), "k", {"x": 1})               # the (test) refresh issued one
    assert (tmp_path / "entitlement.json").exists()
    assert store.get(("m",), "k").value == {"x": 1}


def test_explicit_client_with_null_gate_is_the_embedding_path(entitled_test_home, tmp_path) -> None:
    _signed_out(entitled_test_home)
    client = MemoryClient.local(str(tmp_path / "e.db"), entitlement_gate=NullEntitlementGate())
    store = SibylStore(client=client)
    store.put(("m",), "k", {"x": 1})
    assert store.get(("m",), "k").value == {"x": 1}


def test_signed_in_store_round_trip() -> None:
    store = SibylStore()
    store.put(("m", "u1"), "k", {"text": "prefers dark mode"})
    assert store.get(("m", "u1"), "k").value == {"text": "prefers dark mode"}
