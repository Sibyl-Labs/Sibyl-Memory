"""Release fix batch (GATE-15): the CLI's session calls and the setup copy.

`_bearer_headers` (used by /access, /email-verify/*, /upgrade-nonce,
/stripe-claim) refreshes first when this computer holds no usable token (the
same ensure-fresh path the security calls use). When the api answers 403
client_too_old and this CLI still holds no token, the plugin is not too old:
setup is not finished, so the line says to connect once, not to update.
"""
from __future__ import annotations

import json
import time

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import _account_gate as G
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import write_entitlement

from test_account_gate_2026_09_27 import (  # noqa: F401  (fixtures)
    ACCESS,
    BEARER,
    _fast,
    err,
    mint,
    refresh,
    run,
    server,
    write_creds,
)

CS = "cs_test_" + "a" * 40


def _setup(tmp_path, monkeypatch, *, token=None, past=False, signer=None):
    cred = write_creds(tmp_path / "credentials.json")
    monkeypatch.setattr(cli, "_ENT_CRED_PATH", cred)
    if token == "past":
        t = time.time() - 20 * 86400
        write_entitlement(tmp_path, mint(signer, now=t), received_at=t)
    elif token == "fresh":
        write_entitlement(tmp_path, mint(signer))
    return cred


# ---- refresh first -----------------------------------------------------

def test_bearer_headers_refresh_first_when_the_token_is_past_grace(tmp_path, monkeypatch, test_signer, refresh) -> None:
    cred = _setup(tmp_path, monkeypatch, token="past", signer=test_signer)
    fresh = mint(test_signer)
    refresh["outcome"] = E.RefreshOutcome("ok", token=fresh)
    h = cli._bearer_headers(json.loads(cred.read_text()))
    assert refresh["calls"] == 1
    assert h["X-Sibyl-Entitlement"] == fresh and h["Authorization"] == f"Bearer {BEARER}"


def test_bearer_headers_refresh_first_when_there_is_no_token(tmp_path, monkeypatch, test_signer, refresh) -> None:
    cred = _setup(tmp_path, monkeypatch)
    fresh = mint(test_signer)
    refresh["outcome"] = E.RefreshOutcome("ok", token=fresh)
    h = cli._bearer_headers(json.loads(cred.read_text()))
    assert refresh["calls"] == 1 and h["X-Sibyl-Entitlement"] == fresh


def test_bearer_headers_do_not_refresh_a_usable_token(tmp_path, monkeypatch, test_signer, refresh) -> None:
    cred = _setup(tmp_path, monkeypatch, token="fresh", signer=test_signer)
    h = cli._bearer_headers(json.loads(cred.read_text()))
    assert refresh["calls"] == 0 and "X-Sibyl-Entitlement" in h


def test_bearer_headers_refresh_failure_sends_no_token(tmp_path, monkeypatch, test_signer, refresh) -> None:
    cred = _setup(tmp_path, monkeypatch, token="past", signer=test_signer)
    refresh["outcome"] = E.RefreshOutcome("transient", code="offline")
    h = cli._bearer_headers(json.loads(cred.read_text()))
    assert refresh["calls"] == 1
    assert "X-Sibyl-Entitlement" not in h and h["Authorization"] == f"Bearer {BEARER}"


@pytest.mark.parametrize("which", ["access", "email_request", "email_confirm", "upgrade_nonce", "stripe_claim"])
def test_every_bearer_call_carries_the_refreshed_token(which, tmp_path, monkeypatch, server, test_signer, refresh) -> None:
    cred = _setup(tmp_path, monkeypatch, token="past", signer=test_signer)
    fresh = mint(test_signer)
    refresh["outcome"] = E.RefreshOutcome("ok", token=fresh)
    creds = json.loads(cred.read_text())
    server.on("POST", "/api/plugin/access", ACCESS)
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True, "sent": True})
    server.on("POST", "/api/plugin/email-verify/confirm", {"ok": True})
    server.on("POST", "/api/plugin/upgrade-nonce", {"exchange_url": "https://account.sibyllabs.org/x"})
    server.on("POST", "/api/plugin/stripe-claim", {"ok": True})
    path = {
        "access": ("/api/plugin/access", lambda: cli._fetch_access_ex(creds)),
        "email_request": ("/api/plugin/email-verify/request", lambda: cli._email_request(creds, None)),
        "email_confirm": ("/api/plugin/email-verify/confirm",
                          lambda: cli._email_confirm_once_h(creds, "123456", cred)[:2]),
        "upgrade_nonce": ("/api/plugin/upgrade-nonce",
                          lambda: cli._upgrade_exchange_url(creds, creds["account_id"])),
        "stripe_claim": ("/api/plugin/stripe-claim", lambda: run(["claim", CS], cred, tmp_path)),
    }[which]
    path[1]()
    call = server.calls_to(path[0])[0]
    assert call["headers"].get("X-Sibyl-Entitlement") == fresh


# ---- client_too_old without a token = setup not finished ------------------

def test_client_too_old_without_a_token_prints_the_setup_line(tmp_path, monkeypatch, server, refresh, capsys) -> None:
    cred = _setup(tmp_path, monkeypatch)
    refresh["outcome"] = E.RefreshOutcome("transient", code="offline")
    server.on("POST", "/api/plugin/email-verify/confirm", err(403, {"code": "client_too_old", "error": "x"}))
    run(["email", "confirm", "123456"], cred, tmp_path)
    out = capsys.readouterr().out
    assert "connect to the internet once to finish setup" in out
    assert "pip install -U" not in out and "update the plugin" not in out


def test_client_too_old_with_a_token_still_says_update(tmp_path, monkeypatch, server, test_signer, capsys) -> None:
    cred = _setup(tmp_path, monkeypatch, token="fresh", signer=test_signer)
    server.on("POST", "/api/plugin/email-verify/confirm", err(403, {"code": "client_too_old", "error": "x"}))
    run(["email", "confirm", "123456"], cred, tmp_path)
    out = capsys.readouterr().out
    assert "pip install -U" in out and "finish setup" not in out


@pytest.mark.parametrize("surface", ["devices", "claim", "upgrade_nonce"])
def test_setup_line_on_every_gate_surface(surface, tmp_path, monkeypatch, server, refresh, capsys) -> None:
    cred = _setup(tmp_path, monkeypatch)
    refresh["outcome"] = E.RefreshOutcome("transient", code="offline")
    too_old = err(403, {"code": "client_too_old", "error": "x"})
    server.on("GET", "/api/plugin/devices", too_old)
    server.on("POST", "/api/plugin/stripe-claim", too_old)
    server.on("POST", "/api/plugin/upgrade-nonce", too_old)
    if surface == "devices":
        run(["devices"], cred, tmp_path)
    elif surface == "claim":
        run(["claim", CS], cred, tmp_path)
    else:
        cli._upgrade_exchange_url(json.loads(cred.read_text()), "x")
    out = capsys.readouterr().out
    assert "connect to the internet once to finish setup" in out and "pip install -U" not in out


def test_gate_http_line_contract(tmp_path, test_signer) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    assert G.gate_http_line(403, "client_too_old") == G.CLIENT_TOO_OLD_LINE
    assert G.gate_http_line(403, "client_too_old", cred_path=cred) == G.SETUP_INCOMPLETE_LINE
    write_entitlement(tmp_path, mint(test_signer))
    assert G.gate_http_line(403, "client_too_old", cred_path=cred) == G.CLIENT_TOO_OLD_LINE
    assert "connect to the internet once to finish setup" in G.SETUP_INCOMPLETE_LINE
    assert G.gate_http_line(403, "entitlement_invalid", cred_path=cred) == G.ENTITLEMENT_INVALID_LINE
