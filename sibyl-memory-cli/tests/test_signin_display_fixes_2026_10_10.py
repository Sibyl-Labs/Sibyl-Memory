"""Plugin fixes (2026-10-10).

F-1 (contract C1): the api's 401 ``signed_out_on_claim`` is named on every
surface that used to say only "signed out . run sibyl init".
`sibyl setup <agent> --dry-run` says "already in" for skills, shims,
hooks and the Codex block that are already installed.
times shown to people carry a zone (`2026-10-09 19:08 UTC`).
`sibyl --version` names every installed Sibyl package.
the USDC-from-a-mobile-wallet path is offered only when the api minted
a USDC-send bind for the session.

Nothing here reaches the network: `cli.http_request` / `_security_request` are
fakes and the gate's refresh transport is scripted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sibyl_memory_cli import _account_gate as G
from sibyl_memory_cli import cli
from sibyl_memory_cli import setup as S
from sibyl_memory_cli._timefmt import utc_hm, utc_minute
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import write_entitlement

from conftest import handoff

CLAIM_BODY = {
    "error": "this computer was signed out when the email was confirmed on the account page. "
             "run sibyl init --force and approve it there.",
    "code": "signed_out_on_claim",
    "signed_out_at": "2026-10-08T18:34:05.123Z",
}
CLAIM_LINE = ("signed out: this computer was signed out when your email was confirmed on the account "
              "page (2026-10-08 18:34 UTC). run: sibyl init --force")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def sm() -> Path:
    return Path.home() / ".sibyl-memory"


def _acct(sm: Path) -> list[str]:
    return ["--credentials", str(sm / "credentials.json"), "--db", str(sm / "memory.db"),
            "--tier-cache", str(sm / "tier_cache.json")]


def _claim_err() -> cli.HttpError:
    return cli.HttpError(401, dict(CLAIM_BODY), "https://api/x")


# ---- F-1 (C1) ---------------------------------------------------------------

def test_access_row_names_the_claim_and_init_force() -> None:
    info = {"state": "locked", "code": "signed_out", "cause": "signed_out_on_claim",
            "signed_out_at": "2026-10-08T18:34:05.123Z"}
    assert G.access_row(info) == CLAIM_LINE
    assert G.access_handoff(info) == ("sibyl init --force", None)


def test_access_row_plain_signed_out_is_unchanged() -> None:
    info = {"state": "locked", "code": "signed_out"}
    assert G.access_row(info) == "signed out · run sibyl init"
    assert G.access_handoff(info)[0] == "sibyl init"


def test_claim_line_without_a_time_has_no_parenthesis() -> None:
    assert G.signed_out_on_claim_line(None) == (
        "signed out: this computer was signed out when your email was confirmed on the account "
        "page. run: sibyl init --force")


def test_gate_http_line_401_signed_out_on_claim() -> None:
    assert G.gate_http_line(401, "signed_out_on_claim", body=CLAIM_BODY) == CLAIM_LINE
    assert G.gate_http_handoff(401, "signed_out_on_claim", body=CLAIM_BODY) == ("sibyl init --force", None)
    assert G.gate_http_line(401, None, body={"error": "session not found"}) is None


def test_status_names_the_claim_after_the_refresh_is_refused(sm, test_signer, monkeypatch, capsys):
    write_entitlement(sm, test_signer.mint())
    monkeypatch.setattr(E, "http_refresh", lambda account_id, session_token: E.classify_denial(401, CLAIM_BODY))

    def server(method, path, **kw):
        raise _claim_err()
    monkeypatch.setattr(cli, "http_request", server)
    cli.main(_acct(sm) + ["status"])
    out = capsys.readouterr().out
    assert CLAIM_LINE in out
    assert "signed out · run sibyl init" not in out
    assert "server error: 401" not in out
    assert handoff(out)["next"] == "sibyl init --force"


def test_devices_names_the_claim(sm, monkeypatch, capsys):
    def boom(*a, **k):
        raise _claim_err()
    monkeypatch.setattr(cli, "_security_request", boom)
    assert cli.main(_acct(sm) + ["devices"]) == 2
    out = capsys.readouterr().out
    assert CLAIM_LINE in out and "Session expired." not in out
    assert handoff(out)["next"] == "sibyl init --force"


def test_email_show_names_the_claim(sm, monkeypatch, capsys):
    def server(method, path, **kw):
        raise _claim_err()
    monkeypatch.setattr(cli, "http_request", server)
    assert cli.main(_acct(sm) + ["email"]) == 1
    out = capsys.readouterr().out
    assert CLAIM_LINE in out and "session expired" not in out
    assert handoff(out)["next"] == "sibyl init --force"


def test_email_verify_error_names_the_claim():
    assert cli._email_error_message(_claim_err()) == CLAIM_LINE
    assert cli._email_error_handoff(_claim_err()) == ("sibyl init --force", None)
    plain = cli.HttpError(401, {"error": "session not found"}, "https://api/x")
    assert cli._email_error_message(plain) == "session expired. re-run sibyl init."


# ---- times carry a zone --------------------------------------------

@pytest.mark.parametrize("value,expect", [
    ("2026-10-09T19:08:12.345Z", "2026-10-09 19:08 UTC"),
    ("2026-10-09T21:08:12+02:00", "2026-10-09 19:08 UTC"),
    ("2026-10-09 19:08:12", "2026-10-09 19:08 UTC"),
    (1791572892, "2026-10-09 19:08 UTC"),
    ("yesterday", None),
    (None, None),
    (True, None),
])
def test_utc_minute(value, expect):
    assert utc_minute(value) == expect


def test_utc_hm():
    assert utc_hm(1791572892) == "19:08 UTC"


def test_devices_last_seen_has_a_zone(sm, test_signer, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_security_request", lambda *a, **k: {"devices": [
        {"device_label": "this-one", "is_this_device": True, "install_method": "cli",
         "last_seen_at": "2026-10-09T19:08:12.345Z", "bearer_id": "b1"}]})
    assert cli.main(_acct(sm) + ["devices"]) == 0
    out = capsys.readouterr().out
    assert "last seen 2026-10-09 19:08 UTC" in out
    assert "19:08:12" not in out


def test_lifecycle_timestamps_have_a_zone():
    from sibyl_memory_cli import lifecycle_cli
    assert lifecycle_cli._fmt_ts(1791572892) == "2026-10-09 19:08 UTC"
    assert lifecycle_cli._fmt_ts(None) == "never"


# ---- --version -------------------------------------------------------

def test_version_names_every_installed_package(monkeypatch, capsys):
    vers = {"sibyl-memory-client": "0.9.0+dev.5", "sibyl-memory-mcp": "0.3.0+dev.5"}
    monkeypatch.setattr(cli, "_client_version", lambda: "0.5.0+dev.5")
    monkeypatch.setattr(cli, "_installed_version", lambda pkg: vers.get(pkg))
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert capsys.readouterr().out == "sibyl cli 0.5.0+dev.5 · client 0.9.0+dev.5 · mcp 0.3.0+dev.5\n"


def test_version_includes_hermes_and_langgraph_when_installed(monkeypatch, capsys):
    vers = {"sibyl-memory-client": "0.9.0", "sibyl-memory-mcp": "0.3.0",
            "sibyl-memory-hermes": "0.5.0", "sibyl-memory-langgraph": "0.3.0"}
    monkeypatch.setattr(cli, "_client_version", lambda: "0.5.0")
    monkeypatch.setattr(cli, "_installed_version", lambda pkg: vers.get(pkg))
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == (
        "sibyl cli 0.5.0 · client 0.9.0 · mcp 0.3.0 · hermes 0.5.0 · langgraph 0.3.0\n")


# ---- USDC path only when the api minted it ---------------------------

class _InitServer:
    def __init__(self, init_resp):
        self.init_resp = init_resp

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        if path.startswith("/api/plugin/session-init"):
            return json.loads(json.dumps(self.init_resp))
        if path.startswith("/api/plugin/check"):
            return {"bound": True, "credentials": {
                "account_id": "11111111-2222-4333-8444-555555555555",
                "tenant_id": "11111111-2222-4333-8444-555555555555", "tier": "free",
                "email": None, "wallet": "0x1234567890abcdef1234567890abcdef12345678",
                "issued_at": "2026-10-10T00:00:00Z",
                "bearer_token": "22222222-3333-4444-8555-666666666666"}}
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")


def _init(tmp_path, monkeypatch, init_resp):
    monkeypatch.setattr(cli, "http_request", _InitServer(init_resp))
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    return cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])


def test_init_offers_no_usdc_path_while_usdc_sign_in_is_off(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch, {"pairing_ttl_seconds": 60, "bind_amount_micros": None,
                                  "bind_bus_wallet": None})
    out = capsys.readouterr().out
    assert "USDC" not in out and "mobile wallet" not in out
    assert "two paths" in out
    ask = next(ln for ln in out.splitlines() if ln.startswith("ask your human: "))
    assert ask.endswith("with a desktop wallet or with email plus the code "
                        + re.search(r"(\d{3} \d{3})", ask).group(1) + "; I pick it up here")


def test_init_offers_the_usdc_path_when_the_api_minted_it(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch, {"pairing_ttl_seconds": 60, "bind_amount_micros": 123456,
                                  "bind_bus_wallet": "0x5ab927893db6f35083a82bd69c2c091fbe488f32"})
    out = capsys.readouterr().out
    assert "or send USDC from any mobile wallet" in out and "three paths" in out


def test_usdc_bind_offered_shape():
    assert cli._usdc_bind_offered({"bind_amount_micros": 1, "bind_bus_wallet": "0xabc"})
    for bad in ({}, None, {"bind_amount_micros": True, "bind_bus_wallet": "0xabc"},
                {"bind_amount_micros": 0, "bind_bus_wallet": "0xabc"},
                {"bind_amount_micros": 5, "bind_bus_wallet": " "}):
        assert not cli._usdc_bind_offered(bad)


# ---- setup --dry-run compares with what is installed ----------------

def _claude(tmp_path, monkeypatch):
    w = S.ClaudeCodeWirer(settings_path=tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr(w, "_ensure_mcp_binary", lambda *a, **k: True)
    return w


def test_claude_dry_run_after_setup_says_already_in(tmp_path, monkeypatch):
    w = _claude(tmp_path, monkeypatch)
    assert w.wire().status == "wired"
    before = (tmp_path / ".claude" / "settings.json").read_text()
    out = w.wire(dry_run=True)
    assert out.status == "already"
    lines = out.lifecycle
    assert any(re.match(r"skills v\S+: SAVE\.md, DEBLOAT\.md already in ", ln) for ln in lines), lines
    assert sum(1 for ln in lines if ln.startswith("shim ") and ln.endswith(": already in")) == 2, lines
    assert any(ln.startswith("hooks: 5 already in ") for ln in lines), lines
    assert not any(ln.startswith("would") for ln in lines), lines
    assert (tmp_path / ".claude" / "settings.json").read_text() == before


def test_claude_dry_run_on_a_fresh_computer_says_would(tmp_path, monkeypatch):
    w = _claude(tmp_path, monkeypatch)
    out = w.wire(dry_run=True)
    assert out.status == "dry-run"
    assert any("would install" in ln for ln in out.lifecycle)
    assert any(ln.startswith("would write shim ") for ln in out.lifecycle)
    assert any(ln.startswith("would add hook ") for ln in out.lifecycle)
    assert not any("already in" in ln for ln in out.lifecycle)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_dry_run_after_one_skill_was_removed_mixes_states(tmp_path, monkeypatch):
    w = _claude(tmp_path, monkeypatch)
    w.wire()
    (Path.home() / ".sibyl-memory" / "skills" / "DEBLOAT.md").unlink()
    lines = w.wire(dry_run=True).lifecycle
    assert any(ln.startswith("skills: SAVE.md already in ") for ln in lines), lines
    assert any("would install" in ln and "DEBLOAT.md" in ln for ln in lines), lines


def test_codex_dry_run_after_setup_says_block_already_in(tmp_path, monkeypatch):
    w = S.CodexWirer(config_path=tmp_path / ".codex" / "config.toml")
    monkeypatch.setattr(w, "_ensure_mcp_binary", lambda *a, **k: True)
    assert w.wire().status == "wired"
    lines = w.wire(dry_run=True).lifecycle
    assert any(ln.startswith("block: already in ") for ln in lines), lines
    assert sum(1 for ln in lines if ln.endswith(": already in")) == 2, lines
