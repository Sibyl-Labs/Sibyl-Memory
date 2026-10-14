"""Release fix batch (independent check of the account-gated release, GATE-15).

* ``mem: none`` is a locked branch like the others: it schedules the throttled
  background refresh, so a server-side policy change (email confirmed, policy
  lifted) reaches the device without a restart.
* The automatic pre-sign-in adoption is not silent: a one-time notice goes out
  through the same ``_consume_notice`` path as every other account notice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.testing import (
    TEST_ACCOUNT_ID,
    NullEntitlementGate,
    TestSigner,
    write_credentials,
    write_entitlement,
)

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Stub:
    def __init__(self, outcome: E.RefreshOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self, account_id: str, session_token: str) -> E.RefreshOutcome:
        self.calls += 1
        return self.outcome


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "sm"
    d.mkdir()
    write_credentials(d)
    return d


def _gate(home: Path, clock: Clock, stub: Stub) -> E.EntitlementGate:
    return E.EntitlementGate(home / "credentials.json", refresh_fn=stub, now_fn=clock,
                             background_refresh=False)


# ----------------------------------------------------------------------
# mem: none schedules the throttled refresh
# ----------------------------------------------------------------------

def test_mem_none_schedules_a_throttled_refresh(home, test_signer: TestSigner) -> None:
    write_entitlement(home, test_signer.mint(now=T0, mem="none"), received_at=T0, skew=0.0)
    clock = Clock(T0)
    stub = Stub(E.RefreshOutcome("transient", code="unreachable"))
    g = _gate(home, clock, stub)
    d = g.evaluate()
    assert (d.state, d.code) == ("locked", "policy_none")
    assert stub.calls == 1, "a locked policy must ask the server again"
    g.evaluate()
    assert stub.calls == 1, "throttled"
    clock.t += E.LOCKED_RETRY_SECONDS + 1
    assert g.evaluate().code == "policy_none"
    assert stub.calls == 2


def test_mem_none_lifts_after_the_background_refresh(home, test_signer: TestSigner) -> None:
    write_entitlement(home, test_signer.mint(now=T0, mem="none"), received_at=T0, skew=0.0)
    clock = Clock(T0)
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint(now=T0, mem="full")))
    g = _gate(home, clock, stub)
    assert g.evaluate().code == "policy_none"        # this call still answers from the old token
    assert stub.calls == 1
    assert g.evaluate().state == "fresh"             # the refreshed token lifts the policy


def test_mem_none_forced_refresh_ignores_the_throttle(home, test_signer: TestSigner) -> None:
    write_entitlement(home, test_signer.mint(now=T0, mem="none"), received_at=T0, skew=0.0)
    clock = Clock(T0)
    stub = Stub(E.RefreshOutcome("transient", code="unreachable"))
    g = _gate(home, clock, stub)
    g.evaluate()
    assert stub.calls == 1
    g.notify_server_rejected("entitlement_invalid")
    g.evaluate()
    assert stub.calls == 2


def test_mem_none_non_blocking_evaluation_still_refreshes(home, test_signer: TestSigner) -> None:
    write_entitlement(home, test_signer.mint(now=T0, mem="none"), received_at=T0, skew=0.0)
    stub = Stub(E.RefreshOutcome("transient", code="unreachable"))
    g = _gate(home, Clock(T0), stub)
    assert g.evaluate(allow_blocking=False).code == "policy_none"
    assert stub.calls == 1


# ----------------------------------------------------------------------
# adoption notice
# ----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_notices():
    client_mod._NOTICE_SHOWN.clear()
    yield
    client_mod._NOTICE_SHOWN.clear()


def _seed_pre_signin(db: Path, n_entities: int = 2) -> None:
    c = MemoryClient.local(str(db), tenant_id=DEFAULT_TENANT, entitlement_gate=NullEntitlementGate())
    for i in range(n_entities):
        c.set_entity("project", f"p{i}", {"note": f"before sign-in {i}"})
    c.set_state("session", {"focus": "x"})
    c.storage.close()


def test_auto_adoption_emits_a_one_time_notice(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db, 2)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()                                  # first gated op adopts
    assert c.adoption is not None and c.adoption.moved == 3
    n = c.entitlement_notice()
    assert n == ("3 memories written before sign-in were moved into your account "
                 "on this computer.")
    assert c.entitlement_notice() is None, "one time"
    # A new client on the same store (MCP rebuilds its client) does not repeat it.
    c2 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c2.list_entities()
    assert c2.entitlement_notice() is None


def test_adoption_notice_singular(tmp_path) -> None:
    db = tmp_path / "memory.db"
    c0 = MemoryClient.local(str(db), tenant_id=DEFAULT_TENANT, entitlement_gate=NullEntitlementGate())
    c0.set_entity("project", "only", {"note": "one"})
    c0.storage.close()
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()
    assert c.entitlement_notice() == ("1 memory written before sign-in was moved into your "
                                      "account on this computer.")


def test_adoption_notice_is_also_available_on_its_own(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db, 1)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()
    assert c.adoption_notice() == ("2 memories written before sign-in were moved into your "
                                   "account on this computer.")
    assert c.adoption_notice() is None
    assert c.entitlement_notice() is None


def test_nothing_to_move_says_nothing(tmp_path) -> None:
    db = tmp_path / "memory.db"
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()
    assert c.entitlement_notice() is None and c.adoption_notice() is None


def test_adoption_notice_never_carries_content(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db, 2)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()
    n = c.entitlement_notice()
    assert n
    assert "before sign-in 0" not in n and "project" not in n and TEST_ACCOUNT_ID not in n
