"""sibyl-memory-mcp 0.3.0 account gate (spec account-gated-plugin 2026-09-27, 9.2).

Drives the REAL server dispatch (the lowlevel CallToolRequest handler, the path
stdio/SSE calls take) against the REAL gate in sibyl-memory-client, with the
conftest's isolated, test-entitled home.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error

import pytest
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.exceptions import ENTITLEMENT_MESSAGES
from sibyl_memory_client.testing import write_credentials, write_entitlement

TOOLS = {
    "memory_remember": {"category": "c", "name": "n", "body": {"x": 1}},
    "memory_recall": {"category": "c", "name": "n"},
    "memory_search": {"query": "anything here"},
    "memory_list": {},
    "memory_forget": {"category": "c", "name": "n"},
    "memory_set_state": {"key": "k", "body": {"x": 1}},
    "memory_get_state": {"key": "k"},
    "memory_record_event": {"kind": "decision", "body": {"x": 1}},
}


def _handler():
    mcp = server.build_server()
    return mcp, mcp._mcp_server.request_handlers[CallToolRequest]


def _call(handler, name, args):
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": name, "arguments": args})))
    root = res.root
    text = root.content[0].text if root.content else ""
    return root.isError, text


def _no_credentials(home):
    (home / "credentials.json").unlink()


@pytest.fixture(autouse=True)
def _fresh_notices():
    client_mod._NOTICE_SHOWN.clear()
    yield
    client_mod._NOTICE_SHOWN.clear()


# 15 ------------------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_no_credentials_every_tool_is_account_required(tool, entitled_test_home) -> None:
    _no_credentials(entitled_test_home)
    _, handler = _handler()
    is_error, text = _call(handler, tool, TOOLS[tool])
    assert is_error is True
    payload = json.loads(text)
    assert payload["ok"] is False
    assert payload["code"] == "ACCOUNT_REQUIRED"
    assert payload["reason"] == "no_account"
    assert payload["message"] == ENTITLEMENT_MESSAGES["no_account"]
    assert payload["recovery"] == "Run sibyl init in a terminal, then retry."
    assert payload["account_url"] == "https://account.sibyllabs.org"


def test_short_query_guard_is_gated_too(entitled_test_home) -> None:
    _no_credentials(entitled_test_home)
    _, handler = _handler()
    is_error, text = _call(handler, "memory_search", {"query": "ab"})
    assert is_error and json.loads(text)["code"] == "ACCOUNT_REQUIRED"


def test_tool_discovery_is_never_gated(entitled_test_home) -> None:
    _no_credentials(entitled_test_home)
    mcp, _ = _handler()
    tools = asyncio.run(mcp.list_tools())
    assert sorted(t.name for t in tools) == sorted(TOOLS)


def test_server_starts_without_an_account(entitled_test_home) -> None:
    _no_credentials(entitled_test_home)
    assert server._open_client() is not None


# 16 ------------------------------------------------------------------------

def test_client_is_not_rebuilt_on_every_call(entitled_test_home) -> None:
    assert server._open_client() is server._open_client()


def test_sibyl_init_on_a_running_server_takes_effect(entitled_test_home, test_signer) -> None:
    home = entitled_test_home
    (home / "entitlement.json").unlink()
    _no_credentials(home)
    _, handler = _handler()
    assert _call(handler, "memory_list", {})[0] is True
    write_credentials(home)                                  # what sibyl init writes
    write_entitlement(home, test_signer.mint())
    is_error, text = _call(handler, "memory_remember", TOOLS["memory_remember"])
    assert is_error is False and json.loads(text)["ok"] is True
    # and the cached client was rebuilt on the entitlement.json mtime change
    assert server._client_cache["ent_mtime"] is not None


def test_deleting_credentials_mid_session_is_no_account(entitled_test_home) -> None:
    _, handler = _handler()
    assert _call(handler, "memory_remember", TOOLS["memory_remember"])[0] is False
    _no_credentials(entitled_test_home)
    is_error, text = _call(handler, "memory_recall", TOOLS["memory_recall"])
    assert is_error and json.loads(text)["reason"] == "no_account"


@pytest.mark.parametrize("reason", ["signed_out", "under_review", "account_deleted", "client_too_old"])
def test_locked_reasons_reach_the_agent(reason, entitled_test_home) -> None:
    write_entitlement(entitled_test_home, "", denied={"code": reason, "at": time.time()})
    _, handler = _handler()
    is_error, text = _call(handler, "memory_list", {})
    payload = json.loads(text)
    assert is_error and payload["code"] == "ACCOUNT_REQUIRED" and payload["reason"] == reason
    assert payload["message"] == ENTITLEMENT_MESSAGES[reason]


# 17 ------------------------------------------------------------------------

def _stale_home(home, signer):
    past = time.time() - 8 * 86400
    write_entitlement(home, signer.mint(now=past), received_at=past)


def test_notice_once_per_day_in_the_stale_band(entitled_test_home, test_signer, monkeypatch) -> None:
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="offline"))
    _stale_home(entitled_test_home, test_signer)
    _, handler = _handler()
    first = json.loads(_call(handler, "memory_list", {})[1])
    assert "has not reached the server for 8 days" in first["notice"]
    second = json.loads(_call(handler, "memory_list", {})[1])
    assert "notice" not in second


def test_email_nag_notice(entitled_test_home, test_signer) -> None:
    acct = {"complete": False, "email_verified": False, "email_required": True,
            "grace_until": None, "enforced": False, "purchase_blocked": False}
    write_entitlement(entitled_test_home, test_signer.mint(acct=acct))
    _, handler = _handler()
    out = json.loads(_call(handler, "memory_remember", TOOLS["memory_remember"])[1])
    assert out["notice"].startswith("Your Sibyl Memory account has no confirmed email yet.")


def test_notice_never_on_errors_and_never_a_token(entitled_test_home, test_signer, monkeypatch) -> None:
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="offline"))
    _stale_home(entitled_test_home, test_signer)
    token = json.loads((entitled_test_home / "entitlement.json").read_text())["token"]
    _, handler = _handler()
    is_error, text = _call(handler, "memory_recall", {"category": "no", "name": "such"})
    # non-gate errors keep FastMCP's "Error executing tool <name>: {json}" text
    assert is_error and "notice" not in json.loads(text[text.index("{"):])
    is_error, text = _call(handler, "memory_list", {})
    assert not is_error
    assert token not in text and token.split(".")[1] not in text


# 18 ------------------------------------------------------------------------

class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


@pytest.mark.parametrize("old_server_answer", ["200_ignores_field", "400_unknown_field"])
def test_cap_check_against_an_old_server(old_server_answer, entitled_test_home, monkeypatch) -> None:
    """An api without dual acceptance either ignores the new `entitlement`
    field (200) or rejects the body (400). The first is a normal verdict; the
    second is a verification failure the cap gate already handles (free user
    over the cap: CAP_EXCEEDED, never a crash)."""
    sent = []

    def fake_urlopen(req, timeout=None, context=None):
        sent.append(json.loads(req.data))
        if old_server_answer == "200_ignores_field":
            return _Resp(b'{"ok": true, "tier": "pro", "cap_bytes": null}')
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error": "unknown field"}'))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = server._open_client()
    client._cap_gate._cap = 10                      # put the free user at the cap
    _, handler = _handler()
    is_error, text = _call(handler, "memory_remember",
                           {"category": "c", "name": "n", "body": {"x": "y" * 50}})
    assert sent and "entitlement" in sent[0] and "credentials_claim" not in sent[0]
    if old_server_answer == "200_ignores_field":
        assert is_error is False
    else:
        assert is_error is True and json.loads(text[text.index("{"):])["code"] == "CAP_EXCEEDED"


# 0.9.0 gate fix -------------------------------------------------------------

def test_legacy_credentials_offline_are_setup_incomplete_not_granted(entitled_test_home,
                                                                      monkeypatch) -> None:
    """Credentials, no entitlement, api unreachable: refused with the
    connect-once copy (no 7-day bootstrap grace), and deleting the state file
    changes nothing."""
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="x"))
    for _ in range(2):
        (entitled_test_home / "entitlement.json").unlink(missing_ok=True)
        _, handler = _handler()
        is_error, text = _call(handler, "memory_list", {})
        payload = json.loads(text)
        assert is_error is True
        assert (payload["code"], payload["reason"]) == ("ACCOUNT_REQUIRED", "setup_incomplete")
        assert payload["message"] == ENTITLEMENT_MESSAGES["setup_incomplete"]


def test_pre_signin_rows_are_readable_through_mcp(entitled_test_home) -> None:
    """Rows a pre-account install wrote under DEFAULT_TENANT are listed by the
    signed-in server (first gated call adopts them, locally)."""
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    from sibyl_memory_client.testing import NullEntitlementGate
    old = MemoryClient.local(str(entitled_test_home / "memory.db"), tenant_id=DEFAULT_TENANT,
                             entitlement_gate=NullEntitlementGate())
    old.set_entity("project", "atlas", {"note": "before sign-in"})
    old.storage.close()
    _, handler = _handler()
    is_error, text = _call(handler, "memory_recall", {"category": "project", "name": "atlas"})
    assert is_error is False and "before sign-in" in text


# GATE-15: the automatic pre-sign-in adoption is announced once ----------------

def test_adoption_notice_once_through_the_notice_field(entitled_test_home) -> None:
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    from sibyl_memory_client.testing import NullEntitlementGate
    old = MemoryClient.local(str(entitled_test_home / "memory.db"), tenant_id=DEFAULT_TENANT,
                             entitlement_gate=NullEntitlementGate())
    old.set_entity("project", "atlas", {"note": "before sign-in"})
    old.set_entity("project", "zeppelin", {"note": "before sign-in"})
    old.storage.close()
    _, handler = _handler()
    first = json.loads(_call(handler, "memory_list", {})[1])
    assert first["notice"] == ("2 memories written before sign-in were moved into your account "
                               "on this computer.")
    second = json.loads(_call(handler, "memory_list", {})[1])
    assert "notice" not in second
    # a rebuilt server (new client, same store) does not repeat it
    _, handler2 = _handler()
    third = json.loads(_call(handler2, "memory_list", {})[1])
    assert "notice" not in third


def test_no_adoption_notice_without_pre_signin_rows(entitled_test_home) -> None:
    _, handler = _handler()
    out = json.loads(_call(handler, "memory_list", {})[1])
    assert "notice" not in out
