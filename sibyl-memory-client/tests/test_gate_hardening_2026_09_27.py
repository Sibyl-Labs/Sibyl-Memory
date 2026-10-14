"""Gate bypasses without code edits (0.9.0 gate fix, defect 2).

Before the fix:
* the legacy bootstrap grace (D10, 7 days) was anchored on a first-sight time
  stored in entitlement.json: fabricated credentials with the api blocked got 7
  days of memory tools, and deleting the file restarted them;
* the skew (a local number) moved server_now back without bound, so an edited
  skew, or an entitlement.json holding only a once-valid token, kept that token
  fresh forever;
* an edited retry_after_until suppressed every refresh.

Every scenario here uses local files only: nothing a user needs a code edit for.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from sibyl_memory_client import EntitlementError, MemoryClient
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import TestSigner, write_credentials, write_entitlement

REAL_HTTP_REFRESH = E.http_refresh   # captured at collection (see test_entitlement)
T0 = 1_790_000_000.0
DAY = 86400.0

FAKE_ACCOUNT = "deadbeef-0000-4000-8000-000000000001"
FAKE_SESSION = "cafebabe-0000-4000-8000-000000000002"


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Count:
    def __init__(self, outcome: E.RefreshOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self, account_id: str, session_token: str) -> E.RefreshOutcome:
        self.calls += 1
        return self.outcome


TRANSIENT = E.RefreshOutcome("transient", code="unreachable")


@pytest.fixture
def closed_api(monkeypatch) -> str:
    """SIBYL_API_BASE on a loopback port nothing listens on: the api is
    'blocked' and the real HTTP transport is exercised."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("SIBYL_API_BASE", base)
    return base


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    d = tmp_path / "fake"
    write_credentials(d, account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION)
    return d


def _gate(home: Path, clock=None, refresh=None) -> E.EntitlementGate:
    return E.EntitlementGate(home / "credentials.json", refresh_fn=refresh or Count(TRANSIENT),
                             now_fn=clock or Clock(), background_refresh=False)


# ----------------------------------------------------------------------
# Bootstrap: one verified token is required
# ----------------------------------------------------------------------

def test_fabricated_credentials_with_blocked_api_are_locked(fake_home, tmp_path, closed_api) -> None:
    gate = E.EntitlementGate(fake_home / "credentials.json", refresh_fn=REAL_HTTP_REFRESH,
                             background_refresh=False)
    c = MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=gate)
    with pytest.raises(EntitlementError) as ei:
        c.set_entity("project", "x", {"a": 1})
    assert ei.value.code == "setup_incomplete"
    assert ei.value.surface_code == "ACCOUNT_REQUIRED"
    with pytest.raises(EntitlementError):
        c.list_entities()


def test_fabricated_credentials_default_gate_is_locked(fake_home, tmp_path, closed_api,
                                                        monkeypatch) -> None:
    """The gate every agent surface builds (no injected refresh_fn), with the
    api blocked."""
    monkeypatch.setattr(E, "http_refresh", REAL_HTTP_REFRESH)
    c = MemoryClient.local(str(tmp_path / "m.db"),
                           credentials_path=fake_home / "credentials.json")
    with pytest.raises(EntitlementError) as ei:
        c.get_entity("project", "x")
    assert ei.value.code == "setup_incomplete"


def test_locked_for_days_not_granted_later(fake_home) -> None:
    clock = Clock(T0)
    g = _gate(fake_home, clock)
    for dt in (0, 60, 3600, DAY, 3 * DAY, 8 * DAY, 30 * DAY):
        clock.t = T0 + dt
        d = g.evaluate("set_entity")
        assert (d.state, d.code) == ("locked", "setup_incomplete"), dt
        assert not d.allowed


def test_deleting_entitlement_json_does_not_restart_anything(fake_home) -> None:
    clock = Clock(T0)
    for i in range(5):
        clock.t = T0 + i * DAY
        g = _gate(fake_home, clock)          # a new process each time
        assert g.evaluate().code == "setup_incomplete"
        (fake_home / "entitlement.json").unlink(missing_ok=True)
        assert _gate(fake_home, clock).evaluate().code == "setup_incomplete"


def test_edited_state_file_grants_nothing_without_a_token(fake_home) -> None:
    for st in ({"first_seen_legacy": T0 + 365 * DAY},
               {"first_seen_legacy": T0, "hwm": T0, "received_at": T0, "skew": -30 * DAY},
               {"last_attempt": T0 + 365 * DAY, "retry_after_until": T0 + 365 * DAY},
               {"token": "SE1.e30.AAAA", "received_at": T0}):
        (fake_home / "entitlement.json").write_text(json.dumps(st))
        d = _gate(fake_home, Clock(T0)).evaluate()
        assert d.state == "locked", st
        assert d.code in ("setup_incomplete", "invalid_entitlement"), st


def test_genuine_token_leaves_bootstrap(fake_home, test_signer) -> None:
    ok = Count(E.RefreshOutcome("ok", token=test_signer.mint(
        account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0)))
    g = _gate(fake_home, Clock(T0), ok)
    assert g.evaluate().state == "fresh" and ok.calls == 1


def test_offline_upgrade_message_says_connect_once(fake_home) -> None:
    with pytest.raises(EntitlementError) as ei:
        _gate(fake_home).require("recall")
    text = str(ei.value)
    assert "Connect to the internet" in text and "sibyl init" in text
    assert "memory" in text.lower() and FAKE_SESSION not in text


def test_denied_then_file_deleted_stays_locked(fake_home) -> None:
    denied = Count(E.RefreshOutcome("denied", code="signed_out"))
    clock = Clock(T0)
    assert _gate(fake_home, clock, denied).evaluate().code == "signed_out"
    (fake_home / "entitlement.json").unlink()
    # api now blocked: the deletion buys nothing
    assert _gate(fake_home, clock).evaluate().code == "setup_incomplete"


# ----------------------------------------------------------------------
# Genuine token, then offline: the stale band as specified (2.1, 2.6)
# ----------------------------------------------------------------------

def _install(home: Path, signer: TestSigner, *, iat: float = T0, skew: float = 0.0,
             **kw) -> str:
    tok = signer.mint(account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=iat)
    write_entitlement(home, tok, received_at=iat + skew, skew=skew, **kw)
    return tok


def test_stale_band_after_genuine_token(fake_home, test_signer) -> None:
    _install(fake_home, test_signer)
    clock = Clock(T0)
    g = _gate(fake_home, clock)
    assert g.evaluate().state == "fresh"
    clock.t = T0 + 7 * DAY                              # exp
    assert g.evaluate().state == "fresh"
    clock.t = T0 + 8 * DAY
    d = g.evaluate()
    assert d.state == "stale" and d.allowed and "keeps working for 6 more days" in d.notice
    clock.t = T0 + 14 * DAY                             # exp + og
    assert g.evaluate().state == "stale"
    clock.t = T0 + 14 * DAY + 1
    assert g.evaluate().code == "expired_offline"
    clock.t = T0 + 2 * DAY                              # rollback: hwm holds
    assert g.evaluate().code == "expired_offline"


def test_stale_band_survives_restart(fake_home, test_signer) -> None:
    _install(fake_home, test_signer)
    assert _gate(fake_home, Clock(T0 + 10 * DAY)).evaluate().state == "stale"
    assert _gate(fake_home, Clock(T0 + 15 * DAY)).evaluate().code == "expired_offline"


def test_clock_behind_server_is_absorbed(fake_home, test_signer) -> None:
    _install(fake_home, test_signer, skew=-3600.0)       # local clock 1 h behind
    g = _gate(fake_home, Clock(T0 - 3600 + 60))
    assert g.evaluate().state == "fresh"
    g2 = _gate(fake_home, Clock(T0 - 3600 + 7 * DAY + 60))   # server time: exp + 60 s
    assert g2.evaluate().state == "stale"


# ----------------------------------------------------------------------
# Skew: a local number cannot move server time back without bound
# ----------------------------------------------------------------------

def test_token_only_state_file_is_not_fresh_forever(fake_home, test_signer) -> None:
    tok = test_signer.mint(account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0)
    (fake_home / "entitlement.json").write_text(json.dumps({"token": tok}))
    d = _gate(fake_home, Clock(T0 + 60 * DAY)).evaluate()
    assert d.code == "expired_offline"


def test_edited_skew_does_not_extend(fake_home, test_signer) -> None:
    _install(fake_home, test_signer)
    st = json.loads((fake_home / "entitlement.json").read_text())
    st["skew"] = 30 * DAY
    st["hwm"] = T0
    (fake_home / "entitlement.json").write_text(json.dumps(st))
    assert _gate(fake_home, Clock(T0 + 20 * DAY)).evaluate().code == "expired_offline"


def test_edited_received_at_does_not_extend(fake_home, test_signer) -> None:
    tok = test_signer.mint(account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0)
    (fake_home / "entitlement.json").write_text(json.dumps(
        {"token": tok, "received_at": T0 + 30 * DAY}))
    assert _gate(fake_home, Clock(T0 + 20 * DAY)).evaluate().code == "expired_offline"


def test_file_skew_lead_is_bounded_but_refresh_restores_exact(fake_home, test_signer) -> None:
    """A clock genuinely 30 days ahead: from the file alone the lead is bounded
    (the token reads as expired), but online the refresh this process makes is
    trusted as measured, so the device works."""
    ahead = 30 * DAY
    tok = test_signer.mint(account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0)
    (fake_home / "entitlement.json").write_text(json.dumps(
        {"token": tok, "received_at": T0 + ahead, "hwm": T0 + ahead}))
    offline = _gate(fake_home, Clock(T0 + ahead + 60))
    assert offline.evaluate().code == "expired_offline"
    ok = Count(E.RefreshOutcome("ok", token=test_signer.mint(
        account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0 + 120)))
    online = _gate(fake_home, Clock(T0 + ahead + 120), ok)
    assert online.evaluate().state == "fresh" and ok.calls == 1


# ----------------------------------------------------------------------
# Throttle state is not a switch
# ----------------------------------------------------------------------

def test_edited_retry_after_cannot_suppress_refresh(fake_home, test_signer) -> None:
    _install(fake_home, test_signer, retry_after_until=T0 + 3650 * DAY)
    ok = Count(E.RefreshOutcome("ok", token=test_signer.mint(
        account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0 + 20 * DAY)))
    g = _gate(fake_home, Clock(T0 + 20 * DAY), ok)
    assert g.evaluate().state == "fresh" and ok.calls == 1


def test_genuine_retry_after_is_still_honoured(fake_home, test_signer) -> None:
    _install(fake_home, test_signer, retry_after_until=T0 + 20 * DAY + 600)
    ok = Count(E.RefreshOutcome("ok", token=test_signer.mint(
        account_id=FAKE_ACCOUNT, session_token=FAKE_SESSION, now=T0 + 20 * DAY)))
    g = _gate(fake_home, Clock(T0 + 20 * DAY), ok)
    assert g.evaluate().code == "expired_offline" and ok.calls == 0


def test_deleting_the_file_after_a_denial_does_not_unlock(fake_home, test_signer) -> None:
    _install(fake_home, test_signer)
    denied = Count(E.RefreshOutcome("denied", code="signed_out"))
    g = _gate(fake_home, Clock(T0 + 20 * DAY), denied)
    assert g.evaluate().code == "signed_out"
    (fake_home / "entitlement.json").unlink()
    assert _gate(fake_home, Clock(T0 + 20 * DAY)).evaluate().code == "setup_incomplete"


def test_bootstrap_grace_constant_is_zero() -> None:
    assert E.BOOTSTRAP_GRACE_SECONDS == 0
    assert "first_seen_legacy" not in E._STATE_NUM_FIELDS
