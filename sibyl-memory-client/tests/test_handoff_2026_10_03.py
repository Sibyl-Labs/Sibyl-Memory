"""Agent handoff in the client exceptions (spec agent-handoff-messages 2026-10-03,
rows C1 to C17, section 1.6).

The two tables beside ENTITLEMENT_MESSAGES / ENTITLEMENT_RECOVERY are the single
source the MCP server, the Hermes adapter and a LangGraph graph read.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import (
    CapExceededError,
    DEVICES_READ_ONLY_HUMAN_ACTION,
    DEVICES_READ_ONLY_NEXT,
    ENTITLEMENT_CODES,
    ENTITLEMENT_HUMAN_ACTION,
    ENTITLEMENT_MESSAGES,
    ENTITLEMENT_NEXT,
    ENTITLEMENT_RECOVERY,
    EntitlementError,
    TierAuthError,
    TierGateError,
    TierVerificationError,
    format_handoff,
)
from sibyl_memory_client.testing import write_entitlement

UPDATE = "pip install -U sibyl-memory-client sibyl-memory-mcp sibyl-memory-hermes sibyl-memory-cli"

ROWS = {
    # code: (next, human prefix or None)
    "no_account": ("sibyl init", None),
    "signed_out": ("sibyl init", None),
    "expired_offline": ("sibyl refresh", "connect this computer to the internet once; nothing was deleted"),
    "setup_incomplete": ("sibyl refresh", "connect this computer to the internet once; nothing was deleted"),
    "under_review": ("done", "your account is under review; email sibyl@sibyllabs.org"),
    "account_deleted": ("sibyl init", "this account was deleted; signing in creates or picks an account"),
    "invalid_entitlement": ("sibyl init --force", None),
    "wrong_device": ("sibyl init --force", None),
    "policy_read_only": ("sibyl email add <address>",
                         "an email address to confirm for the account; reading still works"),
    "policy_none": ("sibyl email add <address>", "an email address to confirm for the account"),
    "client_too_old": (UPDATE, None),
}


def test_the_tables_cover_every_code() -> None:
    assert set(ENTITLEMENT_NEXT) == set(ENTITLEMENT_HUMAN_ACTION) == set(ENTITLEMENT_CODES) == set(ROWS)


@pytest.mark.parametrize("code", sorted(ROWS))
def test_each_code_carries_its_handoff(code) -> None:
    nxt, human = ROWS[code]
    e = EntitlementError(code)
    assert e.next == ENTITLEMENT_NEXT[code] == nxt
    assert e.human_action == ENTITLEMENT_HUMAN_ACTION[code]
    if human is None:
        assert e.human_action is None
    else:
        assert e.human_action.startswith(human)
    # summary is the section 8 copy; str(e) is summary + the block, next last
    assert e.summary == ENTITLEMENT_MESSAGES[code]
    assert str(e).startswith(e.summary + "\n")
    lines = str(e).splitlines()
    assert lines[-1] == f"next: {e.next}"
    if e.human_action:
        assert lines[-2] == f"ask your human: {e.human_action}"
    assert e.handoff_lines() == format_handoff(e.next, e.human_action)
    # the existing contract is unchanged
    assert e.recovery == ENTITLEMENT_RECOVERY[code] and e.code == e.reason == code
    assert e.surface_code == "ACCOUNT_REQUIRED"
    assert "\u2014" not in str(e)
    # a placeholder always comes with the sentence that names it
    if "<" in e.next:
        assert e.human_action


def test_a_message_override_is_the_summary() -> None:
    e = EntitlementError("signed_out", message="custom words")
    assert e.summary == "custom words" and str(e).splitlines()[0] == "custom words"
    assert e.next == "sibyl init"


def test_an_unknown_reason_falls_back_to_invalid_entitlement() -> None:
    e = EntitlementError("nonsense")
    assert e.code == "invalid_entitlement" and e.next == "sibyl init --force"


def test_explicit_next_and_human_win() -> None:
    e = EntitlementError("policy_read_only", message="m", next="sibyl devices", human_action=None)
    assert e.next == "sibyl devices" and e.human_action is None
    assert str(e).splitlines()[-1] == "next: sibyl devices"


def test_c12_computer_limit_read_only_hands_off_to_devices(tmp_path: Path, test_signer, entitled_test_home) -> None:
    acct = {"complete": True, "email_verified": True, "email_required": False,
            "devices": {"n": 3, "limit": 1, "over": True}}
    write_entitlement(entitled_test_home, test_signer.mint(mem="read_only", acct=acct))
    client = MemoryClient.local(tmp_path / "m.db")
    with pytest.raises(EntitlementError) as ei:
        client.set_entity("x", "y", {"a": 1})
    e = ei.value
    assert e.code == "policy_read_only"
    assert e.next == DEVICES_READ_ONLY_NEXT == "sibyl devices"
    assert e.human_action == DEVICES_READ_ONLY_HUMAN_ACTION
    assert e.human_action.startswith("more computers are signed in than the plan allows")
    assert "email" not in str(e)                     # the device copy never names the email
    assert str(e).splitlines()[-1] == "next: sibyl devices"


@pytest.mark.parametrize("exc,nxt,human", [
    (CapExceededError("m", current_size=6, cap=5), "sibyl upgrade",
     "the free 5 MB memory cap is full; Pro is a purchase"),
    (TierGateError("m", feature="learner"), "sibyl upgrade", "this feature is on Pro"),
    (TierVerificationError("m"), "retry", "connect this computer to the internet once"),
    (TierAuthError("m"), "sibyl init --force", None),
])
def test_c14_to_c17_class_defaults(exc, nxt, human) -> None:
    assert exc.next == nxt
    if human is None:
        assert exc.human_action is None
    else:
        assert exc.human_action.startswith(human)
    assert str(exc) == "m"                              # str() of these is unchanged
    assert exc.handoff_lines().splitlines()[-1] == f"next: {nxt}"


def test_m2_client_too_old_cap_denial_hands_off_to_the_update(tmp_path: Path) -> None:
    from sibyl_memory_client._capcheck import CapGate, TierCache

    def refused(url, payload, timeout=4.0):
        e = TierAuthError("refused")
        e.server_code = "client_too_old"
        raise e
    gate = CapGate(account_id="a", session_token="s", db_size_fn=lambda: 6 * 1024 * 1024,
                   local_tier_hint="free", cache=TierCache(tmp_path / "tc.json"),
                   check_fn=refused, cap_bytes=5 * 1024 * 1024)
    with pytest.raises(CapExceededError) as ei:
        gate.check(1024)
    assert ei.value.next == UPDATE and ei.value.human_action is None
    assert "too old" in str(ei.value)


def test_other_cap_denials_keep_the_upgrade_handoff(tmp_path: Path) -> None:
    from sibyl_memory_client._capcheck import CapGate, TierCache

    def refused(url, payload, timeout=4.0):
        raise TierAuthError("refused")
    gate = CapGate(account_id="a", session_token="s", db_size_fn=lambda: 6 * 1024 * 1024,
                   local_tier_hint="free", cache=TierCache(tmp_path / "tc.json"),
                   check_fn=refused, cap_bytes=5 * 1024 * 1024)
    with pytest.raises(CapExceededError) as ei:
        gate.check(1024)
    assert ei.value.next == "sibyl upgrade"
