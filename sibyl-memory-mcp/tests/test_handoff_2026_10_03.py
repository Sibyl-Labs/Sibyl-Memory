"""Agent handoff in the MCP error envelopes (spec agent-handoff-messages
2026-10-03, rows M1 to M5 and M7).

The handoff points (ACCOUNT_REQUIRED, CAP_EXCEEDED, TIER_GATED,
TIER_VERIFICATION_FAILED) gain `next` and `human_action`; every existing key
and value is unchanged. NOT_FOUND / VALIDATION_ERROR / ERROR are not handoffs
and carry neither key.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.exceptions import (
    ENTITLEMENT_HUMAN_ACTION,
    ENTITLEMENT_MESSAGES,
    ENTITLEMENT_NEXT,
    ENTITLEMENT_RECOVERY,
    CapExceededError,
    EntitlementError,
    NotFoundError,
    TierAuthError,
    TierGateError,
    TierVerificationError,
    ValidationError,
)
from sibyl_memory_client.testing import write_entitlement

NINE_KEYS = {"ok", "error", "code", "reason", "message", "recovery", "account_url", "next", "human_action"}


def _payload(exc) -> dict:
    with pytest.raises(ToolError) as ei:
        server._err(exc)
    return json.loads(str(ei.value))


@pytest.fixture(autouse=True)
def _fresh_notices():
    client_mod._NOTICE_SHOWN.clear()
    yield
    client_mod._NOTICE_SHOWN.clear()


@pytest.mark.parametrize("reason", sorted(ENTITLEMENT_NEXT))
def test_m1_account_required_carries_the_handoff(reason) -> None:
    p = _payload(EntitlementError(reason))
    assert set(p) == NINE_KEYS                       # future drift is loud
    assert p["ok"] is False and p["error"] == "EntitlementError" and p["code"] == "ACCOUNT_REQUIRED"
    assert p["reason"] == reason
    assert p["message"] == ENTITLEMENT_MESSAGES[reason]           # unchanged: the summary, no block
    assert "next: " not in p["message"]
    assert p["recovery"] == ENTITLEMENT_RECOVERY[reason]
    assert p["account_url"] == "https://account.sibyllabs.org"
    assert p["next"] == ENTITLEMENT_NEXT[reason]
    assert p["human_action"] == ENTITLEMENT_HUMAN_ACTION[reason]
    assert list(p)[:7] == ["ok", "error", "code", "reason", "message", "recovery", "account_url"]


def test_m1_end_to_end_through_the_tool_call(entitled_test_home) -> None:
    (entitled_test_home / "credentials.json").unlink()
    mcp = server.build_server()
    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": "memory_list", "arguments": {}})))
    text = res.root.content[0].text
    p = json.loads(text)                              # M7: still bare JSON after the unwrap
    assert res.root.isError is True
    assert p["code"] == "ACCOUNT_REQUIRED" and p["next"] == "sibyl init" and p["human_action"] is None


@pytest.mark.parametrize("reason", ["under_review", "client_too_old"])
def test_m1_locked_reasons_through_the_tool_call(reason, entitled_test_home) -> None:
    write_entitlement(entitled_test_home, "", denied={"code": reason, "at": time.time()})
    mcp = server.build_server()
    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": "memory_list", "arguments": {}})))
    p = json.loads(res.root.content[0].text)
    assert p["reason"] == reason and p["next"] == ENTITLEMENT_NEXT[reason]
    assert p["human_action"] == ENTITLEMENT_HUMAN_ACTION[reason]


def test_m1_computer_limit_read_only() -> None:
    from sibyl_memory_client.exceptions import DEVICES_READ_ONLY_HUMAN_ACTION
    e = EntitlementError("policy_read_only", message="device copy", next="sibyl devices",
                         human_action=DEVICES_READ_ONLY_HUMAN_ACTION)
    p = _payload(e)
    assert p["message"] == "device copy" and p["next"] == "sibyl devices"
    assert p["human_action"] == DEVICES_READ_ONLY_HUMAN_ACTION


def test_m2_cap_exceeded() -> None:
    p = _payload(CapExceededError("over the cap", current_size=6, cap=5))
    assert p["code"] == "CAP_EXCEEDED" and p["message"] == "over the cap"
    assert p["recovery"] == "Run `sibyl upgrade` to lift the 5 MB free-tier cap."
    assert p["upgrade_url"].startswith("https://")
    assert p["next"] == "sibyl upgrade"
    assert p["human_action"].startswith("the free 5 MB memory cap is full; Pro is a purchase")


def test_m2_cap_exceeded_client_too_old_denial() -> None:
    e = CapExceededError("too old", current_size=6, cap=5)
    e.next = ENTITLEMENT_NEXT["client_too_old"]
    e.human_action = None
    p = _payload(e)
    assert p["next"].startswith("pip install -U sibyl-memory-client") and p["human_action"] is None


def test_m3_tier_gated() -> None:
    p = _payload(TierGateError("paid", feature="self_learning"))
    assert p["code"] == "TIER_GATED" and p["next"] == "sibyl upgrade"
    assert p["human_action"].startswith("this feature is on Pro")


def test_m4_tier_verification_and_auth() -> None:
    p = _payload(TierVerificationError("offline"))
    assert p["code"] == "TIER_VERIFICATION_FAILED" and p["next"] == "retry"
    assert p["human_action"] == "connect this computer to the internet once"
    p = _payload(TierAuthError("refused"))
    assert p["code"] == "TIER_VERIFICATION_FAILED" and p["next"] == "sibyl init --force"
    assert p["human_action"] is None


@pytest.mark.parametrize("exc,code", [(NotFoundError("x"), "NOT_FOUND"),
                                      (ValidationError("x"), "VALIDATION_ERROR"),
                                      (RuntimeError("x"), "ERROR")])
def test_m5_not_handoffs_have_no_next(exc, code) -> None:
    p = _payload(exc)
    assert p["code"] == code and "next" not in p and "human_action" not in p


def test_m7_unwrap_still_matches_with_the_new_keys() -> None:
    body = json.dumps({"ok": False, "code": "ACCOUNT_REQUIRED", "message": "a\nb",
                       "next": "sibyl init", "human_action": None})
    assert server._unwrap_account_required(f"Error executing tool memory_list: {body}") == body


def test_an_older_client_exception_still_gets_a_handoff() -> None:
    class OldEntitlementError(EntitlementError):
        pass
    e = OldEntitlementError("no_account")
    del e.next
    p = _payload(e)
    assert p["next"] == "sibyl init" and p["human_action"] is None
