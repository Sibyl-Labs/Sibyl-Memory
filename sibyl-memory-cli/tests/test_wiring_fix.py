"""Tests for the bug fix: Claude Code MCP registration via `claude mcp add` (not the
stale settings.json), and Codex auto-wiring its config.toml. The Claude CLI is fully
MOCKED here — no test ever runs the real `claude mcp` against this machine's config."""
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import ClaudeCodeWirer, CodexWirer, ALL_WIRERS


def _with_cli(monkeypatch):
    monkeypatch.setattr(ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: "/usr/bin/claude"))


def _mock_run(monkeypatch, *, get_rc=1, add_rc=0, add_err="boom"):
    calls = []
    def fake(cmd, *, timeout=20.0):
        calls.append(cmd)
        if cmd[:3] == ["claude", "mcp", "get"]:
            return (get_rc, "", "")
        if cmd[:3] == ["claude", "mcp", "add"]:
            return (add_rc, "", "" if add_rc == 0 else add_err)
        if cmd[:3] == ["claude", "mcp", "remove"]:
            return (0, "", "")
        return (0, "", "")
    monkeypatch.setattr(S, "_run", fake)
    return calls


# ---------------------------------------------------------------- Claude CLI path

def test_claude_wire_uses_mcp_add_user_scope(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: True)
    calls = _mock_run(monkeypatch, get_rc=1, add_rc=0)            # not registered -> add
    out = ClaudeCodeWirer().wire()
    assert out.status == "wired"
    add = [c for c in calls if c[:3] == ["claude", "mcp", "add"]]
    assert len(add) == 1
    c = add[0]
    assert c[:6] == ["claude", "mcp", "add", "--scope", "user", "sibyl-memory"]
    assert c[6] == "--" and c[7].endswith("sibyl-memory-mcp")   # resolved abspath or bare


def test_claude_wire_already_registered_is_noop(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: True)
    calls = _mock_run(monkeypatch, get_rc=0)                      # get -> registered
    out = ClaudeCodeWirer().wire()
    assert out.status == "already"
    assert not [c for c in calls if c[:3] == ["claude", "mcp", "add"]]


def test_claude_wire_force_reregisters(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: True)
    calls = _mock_run(monkeypatch, get_rc=0, add_rc=0)
    out = ClaudeCodeWirer().wire(force=True)
    assert out.status == "wired"
    assert any(c[:3] == ["claude", "mcp", "remove"] for c in calls)
    assert any(c[:3] == ["claude", "mcp", "add"] for c in calls)


def test_claude_wire_add_failure_is_error(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: True)
    _mock_run(monkeypatch, get_rc=1, add_rc=2, add_err="permission denied")
    out = ClaudeCodeWirer().wire()
    assert out.status == "error" and "permission denied" in out.message


def test_claude_wire_dry_run_does_not_add(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: True)
    calls = _mock_run(monkeypatch, get_rc=1)
    out = ClaudeCodeWirer().wire(dry_run=True)
    assert out.status == "dry-run" and "claude mcp add" in out.message
    assert not [c for c in calls if c[:3] == ["claude", "mcp", "add"]]


def test_claude_wire_binary_missing_errors(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_ensure_mcp_binary", lambda self, **k: False)
    _mock_run(monkeypatch, get_rc=1)
    out = ClaudeCodeWirer().wire()
    assert out.status == "error" and "not on PATH" in out.message


def test_claude_current_state_reflects_cli(monkeypatch):
    _with_cli(monkeypatch)
    monkeypatch.setattr(ClaudeCodeWirer, "_mcp_binary_found", lambda self: True)
    _mock_run(monkeypatch, get_rc=0)
    st = ClaudeCodeWirer().current_state()
    assert st["claude_cli"] is True and st["cli_registered"] is True and st["wired_with_sibyl"] is True
    _mock_run(monkeypatch, get_rc=1)
    assert ClaudeCodeWirer().current_state()["wired_with_sibyl"] is False


def test_claude_no_cli_falls_back_to_settings(tmp_path):
    # autouse conftest already forces no-CLI -> settings.json path
    p = tmp_path / "settings.json"
    out = ClaudeCodeWirer(settings_path=p).wire()
    assert out.status in ("wired", "error")     # wired if binary present
    if out.status == "wired":
        import json
        assert "sibyl-memory" in json.loads(p.read_text())["mcpServers"]


# ---------------------------------------------------------------- Codex auto-wire

def test_codex_in_registry():
    assert "codex" in ALL_WIRERS and ALL_WIRERS["codex"] is CodexWirer


def test_codex_wire_fresh_creates_config(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    out = CodexWirer(config_path=cfg).wire()
    assert out.status == "wired" and out.backup_path is None
    txt = cfg.read_text()
    assert "[mcp_servers.sibyl_memory]" in txt
    # command is the RESOLVED absolute path (or bare name fallback) — matches
    # codex's own `mcp add` behavior; never connect-fails on spawn PATH.
    cmd_line = [l for l in txt.splitlines() if l.startswith("command = ")][0]
    assert cmd_line.rstrip().endswith('sibyl-memory-mcp"')


def test_codex_wire_appends_and_preserves(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "o4"\n[other]\nx = 1\n')
    out = CodexWirer(config_path=cfg).wire()
    assert out.status == "wired" and out.backup_path is not None
    txt = cfg.read_text()
    assert 'model = "o4"' in txt and "[other]" in txt           # preserved
    assert "[mcp_servers.sibyl_memory]" in txt                  # appended
    assert cfg.with_suffix(".toml.bak").exists()


def test_codex_wire_idempotent(tmp_path):
    cfg = tmp_path / "config.toml"; cfg.write_text('model = "o4"\n')
    CodexWirer(config_path=cfg).wire()
    after_first = cfg.read_text()
    out2 = CodexWirer(config_path=cfg).wire()
    assert out2.status == "already" and cfg.read_text() == after_first   # no double-append


def test_codex_wire_dry_run_untouched(tmp_path):
    cfg = tmp_path / "config.toml"; cfg.write_text('model = "o4"\n')
    out = CodexWirer(config_path=cfg).wire(dry_run=True)
    assert out.status == "dry-run" and "[mcp_servers.sibyl_memory]" not in cfg.read_text()


def test_codex_result_is_valid_toml(tmp_path):
    cfg = tmp_path / "config.toml"; cfg.write_text('model = "o4"\nfoo = "bar"\n')
    CodexWirer(config_path=cfg).wire()
    try:
        import tomllib
        parsed = tomllib.loads(cfg.read_text())
        assert parsed["mcp_servers"]["sibyl_memory"]["command"].endswith("sibyl-memory-mcp")
        assert parsed["model"] == "o4"
    except ModuleNotFoundError:
        pytest.skip("tomllib not available (<3.11)")


def test_codex_binary_missing_errors(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"; cfg.write_text("model='o4'\n")
    monkeypatch.setattr(CodexWirer, "_mcp_binary_found", lambda self: False)
    monkeypatch.setattr(S.subprocess, "check_call", lambda *a, **k: None)  # pip install no-op
    out = CodexWirer(config_path=cfg).wire()
    assert out.status == "error" and "not on PATH" in out.message
    assert "[mcp_servers.sibyl_memory]" not in cfg.read_text()   # not written on error
