"""Follow-up fixes D1 to D6 after the review panel pass (2026-11-14).

D1  an untrusted SIBYL_API_BASE is a configuration answer, never "retry" or
    "connect to the internet", and the explanation prints at most once.
D3  the CLI refuses backslash, whitespace, control and userinfo URLs itself,
    so it is safe with client 0.10.0 (whose rule alone allows them).
D4  a Codex config with a UTF-8 BOM is valid TOML.
D5  Hermes: the config edit is proven before the plugin install and backup;
    doctor reports `memory: honcho` as foreign.
D6  tests for checks that a revert used to survive.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from sibyl_memory_cli import _account_gate as G
from sibyl_memory_cli import cli
from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import ClaudeCodeWirer, CodexWirer, HermesWirer

from _harness_fixtures import backups_dir, isolate_path

try:
    import tomllib as REAL_TOML
except ImportError:  # pragma: no cover - Python 3.10
    try:
        import tomli as REAL_TOML  # type: ignore[no-redef]
    except ImportError:
        REAL_TOML = None


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    return fake


# =========================================================================
# D1. untrusted SIBYL_API_BASE, end to end in a fresh process
# =========================================================================

UNTRUSTED = "https://staging.example.internal"


def _run_sibyl(home: Path, *argv: str) -> tuple:
    env = {
        "HOME": str(home),
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "SIBYL_API_BASE": UNTRUSTED,
        "SIBYL_NONINTERACTIVE": "1",
    }
    p = subprocess.run([sys.executable, "-c",
                        "import sys; from sibyl_memory_cli._entry import main; sys.exit(main())",
                        *argv], env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture()
def signed_in_home(tmp_path):
    home = tmp_path / "home"
    d = home / ".sibyl-memory"
    d.mkdir(parents=True)
    cred = d / "credentials.json"
    cred.write_text(json.dumps({"account_id": "acct_fake", "session_token": "st_fake",
                                "bearer_token": "bt_fake", "tier": "free"}))
    cred.chmod(0o600)
    return home


@pytest.mark.parametrize("argv", [
    ("status",), ("devices",), ("devices", "revoke", "0"), ("upgrade",), ("refresh",),
    ("email",), ("email", "verify"), ("claim", "cs_test_abcdefghijkl"),
])
def test_untrusted_base_is_never_a_network_fault(signed_in_home, argv):
    rc, out, err = _run_sibyl(signed_in_home, *argv)
    text = out + err
    assert "next: done" in out, text
    assert G.UNTRUSTED_BASE_HUMAN in out, text
    for wrong in ("next: retry", "connect this computer to the internet",
                  "connect to the internet", "could not reach the server", "server error: 0",
                  "try again"):
        assert wrong not in text, (wrong, text)
    # The explanation is printed at most once per process.
    assert err.count("so the session token was not sent") <= 1, err
    assert "bt_fake" not in text and "st_fake" not in text


def test_untrusted_upgrade_prints_the_gate_once_and_stops(signed_in_home):
    rc, out, err = _run_sibyl(signed_in_home, "upgrade")
    assert rc == 1
    assert err.count("SIBYL_API_BASE (https://staging.example.internal)") == 1, err
    assert "could not start the upgrade" not in out


def test_untrusted_upgrade_base_on_the_legacy_link(monkeypatch, capsys):
    monkeypatch.setattr(cli, "UPGRADE_BASE", "https://evil.example/up")
    monkeypatch.setattr(cli, "_UNTRUSTED_NOTED", set())
    assert cli._legacy_upgrade_url("st") is None
    assert cli._legacy_upgrade_url("st") is None
    assert cli._UPGRADE_GATE_HANDOFF == ("done", G.UNTRUSTED_UPGRADE_HUMAN)
    assert capsys.readouterr().err.count("SIBYL_UPGRADE_BASE") == 1


def test_gate_line_and_handoff_for_the_untrusted_code():
    assert G.gate_http_line(0, cli.UNTRUSTED_BASE_CODE) == G.UNTRUSTED_BASE_LINE
    assert G.gate_http_handoff(0, cli.UNTRUSTED_BASE_CODE) == ("done", G.UNTRUSTED_BASE_HUMAN)
    e = cli.HttpError(0, {"error": "x", "code": cli.UNTRUSTED_BASE_CODE}, "u")
    assert cli._email_error_handoff(e) == ("done", G.UNTRUSTED_BASE_HUMAN)
    assert cli._devices_error_handoff(e) == ("done", G.UNTRUSTED_BASE_HUMAN)
    assert cli._email_rc(e) == 1
    # A plain network failure keeps its retry handoff.
    net = cli.HttpError(0, {"error": "timed out"}, "u")
    assert cli._devices_error_handoff(net) == ("retry", cli.OFFLINE_HUMAN)


def test_access_row_and_handoff_after_an_unsent_refresh():
    info = {"state": "bootstrap", "code": "setup_incomplete", "grace_left_seconds": 86400,
            "refresh_code": G.UNTRUSTED_BASE_CODE}
    assert "connect" not in G.access_row(info)
    assert G.access_handoff(info) == ("done", G.UNTRUSTED_BASE_HUMAN)


# =========================================================================
# D3. the CLI's own URL refusals
# =========================================================================

def _client_0_10_0_rule(url: str) -> bool:
    """sibyl-memory-client 0.10.0's _bearer_allowed, verbatim."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    if host in ("127.0.0.1", "localhost", "::1") and scheme in ("http", "https"):
        return True
    return scheme == "https" and (host == "sibyllabs.org" or host.endswith(".sibyllabs.org"))


BAD_URLS = [
    "https://evil.com\\@sibyllabs.org/x",
    "https://evil.com@sibyllabs.org/x",
    "https://u:p@api.sibyllabs.org/x",
    "https://api.sibyllabs.org /x",
    "https://api.sibyllabs.org/x\r\n",
    "https://api.sibyllabs.org/\x01",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_cli_refuses_userinfo_backslash_and_controls_with_client_0_10_0(monkeypatch, url):
    monkeypatch.setattr(cli, "_client_bearer_allowed", _client_0_10_0_rule)
    assert cli._bearer_allowed(url) is False


@pytest.mark.parametrize("url", ["https://api.sibyllabs.org/api/plugin/access",
                                 "http://127.0.0.1:8787/x"])
def test_cli_still_allows_the_api_and_loopback(monkeypatch, url):
    monkeypatch.setattr(cli, "_client_bearer_allowed", _client_0_10_0_rule)
    assert cli._bearer_allowed(url) is True


def test_http_request_never_sends_to_a_userinfo_base(monkeypatch):
    sent = []
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cli, "_client_bearer_allowed", _client_0_10_0_rule)
    monkeypatch.setattr(cli, "API_BASE", "https://evil.com\\@sibyllabs.org")
    with pytest.raises(cli.HttpError) as ei:
        cli.http_request("POST", "/api/plugin/access", body={"session_token": "t"})
    assert ei.value.code == cli.UNTRUSTED_BASE_CODE and sent == []


def test_entitlement_refresh_is_guarded_by_the_cli(monkeypatch):
    from sibyl_memory_client import _entitlement as E
    called = []
    monkeypatch.setattr(E, "http_refresh", lambda *a: called.append(a))
    monkeypatch.setattr(E, "_api_base", lambda: "https://evil.com\\@sibyllabs.org")
    out = G._guarded_refresh("acct", "tok")
    assert (out.status, out.code) == ("transient", G.UNTRUSTED_BASE_CODE) and called == []


# =========================================================================
# D4. Codex config with a UTF-8 BOM
# =========================================================================

@pytest.mark.parametrize("text", [
    '﻿model = "o4"\n',
    '﻿model = "o4"\n\n[mcp_servers.sibyl_memory]\ncommand = "npx"\n',
])
def test_codex_bom_file_is_wired_and_keeps_the_bom(tmp_path, text):
    if REAL_TOML is None:
        pytest.skip("needs a TOML parser")
    p = tmp_path / "config.toml"
    p.write_bytes(text.encode("utf-8"))
    w = CodexWirer(config_path=p)
    assert w.doctor_probe()[0]["entry"] != "unreadable"
    out = w.wire(force=True)
    assert out.status == "wired", out.message
    data = p.read_bytes().decode("utf-8")
    assert data.startswith("﻿model = \"o4\"\n")
    assert data.count("﻿") == 1
    assert REAL_TOML.loads(data[1:])["mcp_servers"]["sibyl_memory"]["command"] == S.mcp_command_path()
    assert w.current_state()["wired_with_sibyl"] is True
    probe = w.doctor_probe()[0]
    assert probe["entry"] == "present" and not probe.get("broken")


# =========================================================================
# D5. Hermes: prove the edit before installing or backing up
# =========================================================================

def test_hermes_unprovable_edit_installs_nothing_and_backs_up_nothing(tmp_path, monkeypatch):
    h = tmp_path / "hermes-home"
    h.mkdir()
    cfg = h / "config.yaml"
    text = "memory: {provider: x}\n"
    cfg.write_text(text)
    installs = []
    monkeypatch.setattr(HermesWirer, "_install_plugin", lambda self: installs.append(1))
    out = HermesWirer(hermes_home=h).wire(force=True)
    assert out.status == "error" and out.manual_block
    assert out.handoff[0] == "done"
    assert installs == []
    assert out.backup_path is None and not backups_dir("hermes").exists()
    assert cfg.read_text() == text


def test_hermes_doctor_reports_a_scalar_memory_value_as_foreign(tmp_path):
    h = tmp_path / "hermes-home"
    h.mkdir()
    (h / "config.yaml").write_text("memory: honcho\n")
    probe = HermesWirer(hermes_home=h).doctor_probe()[0]
    assert probe["entry"] == "foreign"
    assert "honcho" in probe["detail"]


# =========================================================================
# D6. checks a revert used to survive
# =========================================================================

def test_hermes_zero_return_install_without_init_stops_before_the_config(tmp_path, monkeypatch):
    import sibyl_memory_hermes.install_plugin as IP
    h = tmp_path / "hermes-home"
    h.mkdir()
    cfg = h / "config.yaml"
    text = "model: x\n"
    cfg.write_text(text)
    monkeypatch.setattr(IP, "install", lambda **kw: 0)
    with contextlib.redirect_stdout(io.StringIO()):
        out = HermesWirer(hermes_home=h).wire()
    assert out.status == "error" and "did not install the plugin" in out.message
    assert cfg.read_text() == text
    assert not backups_dir("hermes").exists()


def test_codex_doctor_unreadable_on_duplicate_command_keys(tmp_path):
    if REAL_TOML is None:
        pytest.skip("needs a TOML parser")
    cmd = S.mcp_command_path()
    text = f'[mcp_servers.sibyl_memory]\ncommand = "{cmd}"\ncommand = "{cmd}"\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    probe = CodexWirer(config_path=p).doctor_probe()[0]
    assert probe["entry"] == "unreadable" and "not valid TOML" in probe["detail"]


def test_codex_open_string_refusal_names_the_cause(tmp_path, monkeypatch):
    # (c) the check is kept and pinned. Without a TOML parser it is the only
    # one that names the cause: the scan below it still refuses, but with a
    # generic "could not add the table safely" (with a parser, the earlier
    # "not valid TOML" refusal comes first).
    monkeypatch.setattr(S, "_tomllib", None)
    text = 'model = "o4"\nnote = """\nnever closed\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "error"
    assert "ends inside an unterminated string or array" in out.message
    assert p.read_text() == text


# =========================================================================
# Minor
# =========================================================================

def test_claude_repair_manual_command_quotes_the_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "mcp_command_path", lambda: "/opt/my tools/sibyl-memory-mcp")
    w = ClaudeCodeWirer(settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(w, "_claude_cli", lambda: None)
    out = w.repair_mcp({})
    assert out.handoff[0].endswith("-- '/opt/my tools/sibyl-memory-mcp'")


def test_hermes_refusal_prints_only_setups_handoff(tmp_path, capsys):
    h = tmp_path / "hermes-home"
    (h / "plugins" / "sibyl" / "__pycache__").mkdir(parents=True)
    (h / "config.yaml").write_text("model: x\n")
    out = HermesWirer(hermes_home=h).wire()
    printed = capsys.readouterr().out
    assert out.status == "error" and "refused (code 2)" in out.message
    assert "next:" not in printed
    assert "Refused" in printed


def test_handoff_hold_passes_a_successful_install_through():
    real = io.StringIO()
    hold = S._HandoffHold(real)
    hold.write("a\nnext: x\nb\n")
    hold.write("later: y\nnext: done\n")
    hold.release(True)
    assert real.getvalue() == "a\nnext: x\nb\nlater: y\nnext: done\n"
    real2 = io.StringIO()
    hold2 = S._HandoffHold(real2)
    hold2.write("Refused\nnext: sibyl-memory-hermes install-plugin --force\n")
    hold2.release(False)
    assert real2.getvalue() == "Refused\n"
