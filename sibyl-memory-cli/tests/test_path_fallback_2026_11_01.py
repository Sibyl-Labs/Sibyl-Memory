"""cli 0.6.1: setup and doctor find sibyl-memory-mcp when ~/.local/bin is not on PATH.

Field report 2026-11-01: on a fresh Grok Bot machine, `sibyl setup grokbot` failed
with "'sibyl-memory-mcp' not on PATH. Install it" although the installer had just
put the server in ~/.local/bin. The Bot fixed it by hand. These tests pin the fix:
the wirers resolve the binary from PATH, then beside the interpreter, then from
~/.local/bin, and write the absolute path into the client config.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import sibyl_memory_cli.setup as S
from sibyl_memory_cli import doctor as D


@pytest.fixture()
def fresh_home(tmp_path, monkeypatch):
    """A HOME whose ~/.local/bin holds the server, with a PATH that does not."""
    home = tmp_path / "home"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "sibyl-memory-mcp"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    # the interpreter's own bin dir must not answer for it in these tests
    monkeypatch.setattr(S.sys, "executable", str(tmp_path / "py" / "bin" / "python"))
    return home, exe


def _no_install(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_install_pkg_or_instruct",
                        lambda pkg: calls.append(pkg) or "should not be called")
    return calls


def test_resolve_finds_user_bin_off_path(fresh_home):
    _home, exe = fresh_home
    assert S.resolve_mcp_binary() == str(exe)
    assert S.mcp_command_path() == str(exe)


def test_resolve_prefers_path(fresh_home, tmp_path, monkeypatch):
    on_path = tmp_path / "onpath"
    on_path.mkdir()
    exe2 = on_path / "sibyl-memory-mcp"
    exe2.write_text("#!/bin/sh\nexit 0\n")
    exe2.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path))
    assert S.resolve_mcp_binary() == str(exe2)


def test_resolve_none_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(S.sys, "executable", str(tmp_path / "py" / "bin" / "python"))
    assert S.resolve_mcp_binary() is None


def test_non_executable_file_is_ignored(fresh_home):
    _home, exe = fresh_home
    exe.chmod(0o644)
    assert S.resolve_mcp_binary() is None


@pytest.mark.parametrize("cls", [S.CursorWirer, S.GrokBotWirer])
def test_cursor_and_grokbot_wire_without_path(fresh_home, monkeypatch, cls):
    home, exe = fresh_home
    calls = _no_install(monkeypatch)
    cfg = home / ".cursor" / "mcp.json"
    out = cls(config_path=cfg).wire()
    assert out.status == "wired", out.message
    assert calls == []  # no reinstall attempt
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["sibyl-memory"]["command"] == str(exe)


def test_cursor_rerun_reports_already(fresh_home, monkeypatch):
    home, _exe = fresh_home
    _no_install(monkeypatch)
    cfg = home / ".cursor" / "mcp.json"
    assert S.CursorWirer(config_path=cfg).wire().status == "wired"
    assert S.CursorWirer(config_path=cfg).wire().status == "already"


def test_cursor_truly_missing_says_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(S.sys, "executable", str(tmp_path / "py" / "bin" / "python"))
    monkeypatch.setattr(S, "_install_pkg_or_instruct", lambda pkg: None)
    out = S.CursorWirer(config_path=tmp_path / "h" / ".cursor" / "mcp.json").wire()
    assert out.status == "error"
    assert "is not installed" in out.message
    assert ".local" in out.message  # names where it looked
    assert "not on PATH" not in out.message


def test_cursor_verify_uses_fallback(fresh_home, monkeypatch):
    _home, exe = fresh_home
    seen = []
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: seen.append(b) or (True, "ok"))
    assert S.CursorWirer().verify_mcp_starts() == (True, "ok")
    assert seen == [str(exe)]


def test_codex_command_uses_fallback(fresh_home):
    _home, exe = fresh_home
    assert S.CodexWirer()._mcp_command() == str(exe)
    assert S.CodexWirer()._mcp_binary_found()


def test_claude_binary_found_uses_fallback(fresh_home):
    assert S.ClaudeCodeWirer()._mcp_binary_found()


def test_doctor_finds_server_off_path(fresh_home):
    _home, exe = fresh_home
    assert D._server_cmd() == [str(exe)]
