"""F-1 (contract C1): the api's 401 ``signed_out_on_claim``.

When the account's email is first confirmed on the account page, the api signs
out every computer that was set up before it and answers that computer's next
authenticated call with HTTP 401 ``{code: "signed_out_on_claim", signed_out_at,
error}``. The gate keeps the code ``signed_out`` (the MCP ``reason`` is stable)
and carries the cause and time, so every surface can say why and what to run
(``sibyl init --force``). Any other 401 is unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_memory_client import EntitlementError
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.exceptions import (
    SIGNED_OUT_ON_CLAIM,
    clean_iso,
    signed_out_on_claim_message,
    utc_minute,
)
from sibyl_memory_client.testing import TestSigner, write_credentials, write_entitlement

T0 = 1_790_000_000.0
DAY = 86400
CLAIM_BODY = {
    "error": "this computer was signed out when the email was confirmed on the account page. "
             "run sibyl init --force and approve it there.",
    "code": "signed_out_on_claim",
    "signed_out_at": "2026-10-08T18:34:05.123Z",
}


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


def _gate(home: Path, stub, clock=None) -> E.EntitlementGate:
    return E.EntitlementGate(home / "credentials.json", refresh_fn=stub, now_fn=clock or Clock(),
                             background_refresh=False)


# ---- classification ---------------------------------------------------------

def test_401_signed_out_on_claim_keeps_code_and_carries_cause_and_time() -> None:
    out = E.classify_denial(401, CLAIM_BODY)
    assert (out.status, out.code) == ("denied", "signed_out")
    assert out.cause == SIGNED_OUT_ON_CLAIM
    assert out.since == "2026-10-08T18:34:05.123Z"


def test_any_other_401_is_unchanged() -> None:
    out = E.classify_denial(401, {"error": "session not found, revoked, or expired"})
    assert (out.status, out.code, out.cause, out.since) == ("denied", "signed_out", None, None)


def test_a_garbage_signed_out_at_is_dropped_not_trusted() -> None:
    out = E.classify_denial(401, dict(CLAIM_BODY, signed_out_at="<script>" * 10))
    assert out.cause == SIGNED_OUT_ON_CLAIM and out.since is None


# ---- the gate ---------------------------------------------------------------

def test_gate_locks_with_the_cause_and_status_reports_it(home, test_signer: TestSigner) -> None:
    write_entitlement(home, test_signer.mint(now=T0), received_at=T0, skew=0.0)
    g = _gate(home, Stub(E.classify_denial(401, CLAIM_BODY)), Clock(T0 + DAY))
    g.refresh(blocking=True)
    d = g.evaluate()
    assert (d.state, d.code, d.cause, d.since) == ("locked", "signed_out", SIGNED_OUT_ON_CLAIM,
                                                   "2026-10-08T18:34:05.123Z")
    st = json.loads((home / "entitlement.json").read_text())
    assert st["denied"]["cause"] == SIGNED_OUT_ON_CLAIM
    info = g.status()
    assert info["code"] == "signed_out" and info["cause"] == SIGNED_OUT_ON_CLAIM
    assert info["signed_out_at"] == "2026-10-08T18:34:05.123Z"


def test_require_raises_signed_out_with_the_claim_copy_and_init_force(home, test_signer) -> None:
    write_entitlement(home, test_signer.mint(now=T0), received_at=T0, skew=0.0)
    g = _gate(home, Stub(E.classify_denial(401, CLAIM_BODY)), Clock(T0 + DAY))
    g.refresh(blocking=True)
    with pytest.raises(EntitlementError) as ei:
        g.require("set_entity")
    e = ei.value
    assert e.code == "signed_out" and e.reason == "signed_out"
    assert "signed out of your Sibyl Memory account when your email was confirmed on the account page" in e.summary
    assert "(2026-10-08 18:34 UTC)" in e.summary
    assert e.next == "sibyl init --force" and e.human_action is None
    assert str(e).rstrip().endswith("next: sibyl init --force")


def test_plain_401_keeps_the_old_copy(home, test_signer) -> None:
    write_entitlement(home, test_signer.mint(now=T0), received_at=T0, skew=0.0)
    g = _gate(home, Stub(E.classify_denial(401, {"error": "session not found"})), Clock(T0 + DAY))
    g.refresh(blocking=True)
    with pytest.raises(EntitlementError) as ei:
        g.require("set_entity")
    assert ei.value.next == "sibyl init"
    assert "email was confirmed" not in ei.value.summary
    assert "cause" not in g.status()


def test_state_file_cause_and_since_are_shape_checked(home) -> None:
    write_entitlement(home, "", received_at=T0,
                      denied={"code": "signed_out", "at": T0, "cause": "something_else",
                              "since": "2026-10-08T18:34:05Z"})
    d = _gate(home, Stub(E.RefreshOutcome("transient", code="x")), Clock(T0 + 60)).evaluate()
    assert d.code == "signed_out" and d.cause is None and d.since is None
    write_entitlement(home, "", received_at=T0,
                      denied={"code": "signed_out", "at": T0, "cause": SIGNED_OUT_ON_CLAIM,
                              "since": "x" * 80})
    d = _gate(home, Stub(E.RefreshOutcome("transient", code="x")), Clock(T0 + 60)).evaluate()
    assert d.cause == SIGNED_OUT_ON_CLAIM and d.since is None


def test_a_later_successful_refresh_clears_the_cause(home, test_signer) -> None:
    write_entitlement(home, "", received_at=T0,
                      denied={"code": "signed_out", "at": T0, "cause": SIGNED_OUT_ON_CLAIM})
    clock = Clock(T0 + 16 * 60)
    g = _gate(home, Stub(E.RefreshOutcome("ok", token=test_signer.mint(now=clock.t))), clock)
    d = g.evaluate()
    assert d.state == "fresh" and d.cause is None


# ---- copy helpers -----------------------------------------------------------

@pytest.mark.parametrize("value,expect", [
    ("2026-10-08T18:34:05.123Z", "2026-10-08 18:34"),
    ("2026-10-08T20:34:05+02:00", "2026-10-08 18:34"),
    ("2026-10-08T18:34", "2026-10-08 18:34"),
    ("not a time", None),
    (None, None),
    (1790000000, None),
])
def test_utc_minute(value, expect) -> None:
    assert utc_minute(value) == expect


def test_message_without_a_time_has_no_parenthesis() -> None:
    m = signed_out_on_claim_message(None)
    assert "()" not in m and "UTC" not in m and "sibyl init --force" in m
    assert clean_iso("2026-10-08T18:34:05Z") == "2026-10-08T18:34:05Z"
