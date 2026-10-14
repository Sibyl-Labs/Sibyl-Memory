"""QA findings F1 and F2 (2026-10-08), client side.

F1: a verified, bound pass that arrives on a refresh (foreground or the
background re-check) rewrites the saved ``tier`` in credentials.json, so every
reader agrees with the pass that drives enforcement; ``tier_for_display`` and
``free_tier_status`` show the pass's tier, the saved one only without a pass.

F2: ``status()`` passes the server's additive ``acct.plan.ends_at`` /
``renews_at`` through, and a payload with keys this version does not know is
still a valid pass (older clients ignore the new fields).
"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import (
    TEST_ACCOUNT_ID,
    TEST_SESSION_TOKEN,
    NullEntitlementGate,
    write_credentials,
    write_entitlement,
)

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def ok(signer, **mint):
    def fn(account_id: str, session_token: str) -> E.RefreshOutcome:
        return E.RefreshOutcome("ok", token=signer.mint(account_id=account_id,
                                                        session_token=session_token, **mint))
    return fn


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "sm"
    d.mkdir()
    return d


def creds(home: Path) -> dict:
    return json.loads((home / "credentials.json").read_text())


# ---- F1: refresh rewrites the saved tier ---------------------------------

def test_blocking_refresh_rewrites_the_saved_tier_keeping_mode(home, test_signer) -> None:
    p = write_credentials(home, tier="free", signature="sig", signed_at="2026-09-27T00:00:00Z")
    os.chmod(p, 0o640)
    (home / "tier_cache.json").write_text(json.dumps({"tier": "free"}))
    gate = E.EntitlementGate(p, refresh_fn=ok(test_signer, tier="pro", cap=None, src="subscription"),
                             background_refresh=False)
    assert gate.refresh(blocking=True).status == "ok"
    c = creds(home)
    assert c["tier"] == "pro"
    assert "signature" not in c and "signed_at" not in c
    assert c["account_id"] == TEST_ACCOUNT_ID and c["session_token"] == TEST_SESSION_TOKEN
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert not (home / "tier_cache.json").exists()
    assert not list(home.glob("credentials.json.*.tmp"))
    assert gate.tier == "pro"                      # the gate still reads the same sign-in


def test_background_recheck_rewrites_the_saved_tier(home, test_signer) -> None:
    # A Pro pass past its ra: the next evaluation re-checks (inline here) and the
    # server now says Free (the plan ended).
    p = write_credentials(home, tier="pro")
    write_entitlement(home, test_signer.mint(now=T0, tier="pro", cap=None, src="subscription", ra=3600),
                      received_at=T0, skew=0.0)
    clock = Clock(T0 + 7200)
    gate = E.EntitlementGate(p, refresh_fn=ok(test_signer, now=T0 + 7200, tier="free"),
                             now_fn=clock, background_refresh=False)
    gate.evaluate()                                # triggers the re-check (background_refresh=False: inline)
    assert creds(home)["tier"] == "free"
    assert gate.tier == "free"


def test_no_rewrite_when_the_tier_already_matches_or_is_unknown(home, test_signer) -> None:
    p = write_credentials(home, tier="stake")      # a legacy alias of pro
    before = p.stat().st_mtime_ns, p.stat().st_ino
    gate = E.EntitlementGate(p, refresh_fn=ok(test_signer, tier="pro", cap=None), background_refresh=False)
    gate.refresh(blocking=True)
    assert creds(home)["tier"] == "stake" and (p.stat().st_mtime_ns, p.stat().st_ino) == before
    # an unrecognised label is never written (it is treated as Free until sibyl update)
    gate2 = E.EntitlementGate(p, refresh_fn=ok(test_signer, tier="platinum", cap=None),
                              background_refresh=False)
    gate2.refresh(blocking=True)
    assert creds(home)["tier"] == "stake"


def test_sync_refuses_another_sign_in_a_symlink_or_garbage(home, test_signer, tmp_path) -> None:
    ent = E.parse_and_verify(test_signer.mint(tier="pro", cap=None))
    # another session (a re-init on this computer while the refresh was in flight)
    p = write_credentials(home, tier="free", session_token="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    assert E.sync_credentials_tier(p, ent) is False and creds(home)["tier"] == "free"
    # another account
    write_credentials(home, tier="free", account_id="22222222-2222-4333-8444-555555555555")
    assert E.sync_credentials_tier(p, ent) is False
    # a symlink is never written through
    real = write_credentials(tmp_path / "real", tier="free")
    p.unlink()
    p.symlink_to(real)
    assert E.sync_credentials_tier(p, ent) is False
    assert json.loads(real.read_text())["tier"] == "free"
    p.unlink()
    # garbage and absence: no exception, no write
    p.write_text("{not json")
    assert E.sync_credentials_tier(p, ent) is False
    p.unlink()
    assert E.sync_credentials_tier(p, ent) is False
    # the happy path for the same sign-in
    write_credentials(home, tier="free")
    assert E.sync_credentials_tier(p, ent) is True and creds(home)["tier"] == "pro"


def test_a_refused_refresh_never_touches_the_saved_tier(home, test_signer) -> None:
    p = write_credentials(home, tier="pro")
    for out in (E.RefreshOutcome("transient", code="offline"), E.RefreshOutcome("denied", code="signed_out")):
        gate = E.EntitlementGate(p, refresh_fn=lambda a, s, o=out: o, background_refresh=False)
        gate.refresh(blocking=True)
        assert creds(home)["tier"] == "pro"
    # a 200 with a token for another device is ignored, and so is its tier
    other = test_signer.mint(session_token="cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee", tier="free")
    gate = E.EntitlementGate(p, refresh_fn=lambda a, s: E.RefreshOutcome("ok", token=other),
                             background_refresh=False)
    gate.refresh(blocking=True)
    assert creds(home)["tier"] == "pro"


def test_tier_for_display_and_free_tier_status_follow_the_pass(home, test_signer, tmp_path) -> None:
    p = write_credentials(home, tier="free")
    write_entitlement(home, test_signer.mint(tier="pro", cap=None, src="subscription"))
    c = MemoryClient.local(str(tmp_path / "m.db"), tenant_id=TEST_ACCOUNT_ID, tier="free",
                           account_id=TEST_ACCOUNT_ID, session_token=TEST_SESSION_TOKEN,
                           credentials_path=p)
    assert c.tier_for_display() == "pro"
    st = c.free_tier_status()
    assert st["tier"] == "pro" and st["uncapped"] is True
    # no pass: the saved tier
    c2 = MemoryClient.local(str(tmp_path / "n.db"), tier="free", entitlement_gate=NullEntitlementGate())
    assert c2.tier_for_display() == "free" and c2.free_tier_status()["tier"] == "free"


# ---- F2: plan end passthrough + tolerance of unknown keys ------------------

def _plan_acct(**plan):
    return {"complete": True, "email_verified": True, "email_required": False,
            "plan": dict({"tier": "pro", "src": "card"}, **plan)}


def test_status_passes_ends_at_and_renews_at_through(home, test_signer) -> None:
    p = write_credentials(home, tier="pro")
    write_entitlement(home, test_signer.mint(tier="pro", cap=None, src="subscription",
                                             acct=_plan_acct(ends_at="2027-10-08T12:00:00.000Z")))
    st = E.EntitlementGate(p, background_refresh=False, allow_refresh=False).status()
    assert st["plan"] == {"tier": "pro", "src": "card", "ends_at": "2027-10-08T12:00:00.000Z"}
    write_entitlement(home, test_signer.mint(tier="pro", cap=None, src="subscription",
                                             acct=_plan_acct(renews_at="2027-10-05T12:00:00.000Z")))
    st = E.EntitlementGate(p, background_refresh=False, allow_refresh=False).status()
    assert st["plan"] == {"tier": "pro", "src": "card", "renews_at": "2027-10-05T12:00:00.000Z"}
    # a non-string / oversize value is dropped, never trusted
    write_entitlement(home, test_signer.mint(tier="pro", cap=None, src="subscription",
                                             acct=_plan_acct(ends_at=12345, renews_at="x" * 200)))
    st = E.EntitlementGate(p, background_refresh=False, allow_refresh=False).status()
    assert st["plan"] == {"tier": "pro", "src": "card"}


def test_unknown_payload_keys_are_tolerated(home, test_signer) -> None:
    p = write_credentials(home, tier="pro")
    acct = _plan_acct(ends_at="2027-10-08T12:00:00Z", some_future_key={"x": 1})
    acct["another_future_key"] = [1, 2]
    write_entitlement(home, test_signer.mint(tier="pro", cap=None, src="subscription", acct=acct,
                                             future_top_level="ignored"))
    gate = E.EntitlementGate(p, background_refresh=False, allow_refresh=False)
    d = gate.peek()
    assert d.state == "fresh" and d.entitlement.tier == "pro"
