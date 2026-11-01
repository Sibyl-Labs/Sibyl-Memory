"""Grok Bot / Cursor onboarding (A2, A3): CursorWirer, `sibyl setup grokbot`,
the no-detect snippet, and the non-interactive `--yes` path. Every test uses a
temp HOME; nothing touches a real ~/.cursor."""
import argparse
import json
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import CursorWirer, GrokBotWirer, ALL_WIRERS


@pytest.fixture(autouse=True)
def _fake_mcp_binary(monkeypatch, tmp_path):
    """A resolvable sibyl-memory-mcp that is not the real one; no pip, no spawn."""
    fake = tmp_path / "bin" / "sibyl-memory-mcp"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:/usr/bin:/bin")
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    return fake


def _cfg(tmp_path):
    return tmp_path / ".cursor" / "mcp.json"


def test_registered_and_target_only():
    assert ALL_WIRERS["cursor"] is CursorWirer
    assert "grokbot" not in ALL_WIRERS            # never auto-detected
    assert S.EXPLICIT_WIRERS["grokbot"] is GrokBotWirer
    assert "grokbot" in S.SETUP_TARGETS


def test_cursor_fresh_file(tmp_path, _fake_mcp_binary):
    cfg = _cfg(tmp_path)
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "wired" and out.backup_path is None
    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["sibyl-memory"]
    assert entry["command"] == str(_fake_mcp_binary)
    assert entry["type"] == "stdio"
    assert not list(cfg.parent.glob("*.tmp"))


def test_cursor_merge_preserves_foreign_entries_and_backs_up(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir()
    original = {"mcpServers": {"other": {"command": "npx", "args": ["x"]}}, "extra": 1}
    cfg.write_text(json.dumps(original))
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "wired"
    assert out.backup_path is not None and out.backup_path.exists()
    assert json.loads(out.backup_path.read_text()) == original
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert data["extra"] == 1
    assert "sibyl-memory" in data["mcpServers"]


def test_cursor_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    assert CursorWirer(config_path=cfg).wire().status == "wired"
    before = cfg.read_text()
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "already" and out.backup_path is None
    assert cfg.read_text() == before


def test_cursor_foreign_sibyl_entry_not_clobbered_without_force(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "/opt/other-server"}}}))
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "skipped"
    assert json.loads(cfg.read_text())["mcpServers"]["sibyl-memory"]["command"] == "/opt/other-server"
    assert CursorWirer(config_path=cfg).wire(force=True).status == "wired"


def test_cursor_corrupt_json_never_replaced(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.parent.mkdir()
    cfg.write_text("{not json")
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "error"
    assert cfg.read_text() == "{not json"


def test_cursor_dry_run_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    out = CursorWirer(config_path=cfg).wire(dry_run=True)
    assert out.status == "dry-run" and not cfg.exists()


def test_cursor_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not CursorWirer().is_present()
    (tmp_path / ".cursor").mkdir()
    assert CursorWirer().is_present()


def _args(**kw):
    base = dict(target=None, yes=False, force=False, dry_run=False, hermes_home=None,
                claude_settings=None, codex_config=None, cursor_config=None, print_config=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _env_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    # no real `claude`/`codex`/`cursor` on the stripped PATH, so only fixtures detect


def test_no_detect_prints_path_and_snippet(tmp_path, monkeypatch, capsys, _fake_mcp_binary):
    _env_home(tmp_path, monkeypatch)
    rc = S.cmd_setup(_args(yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No agent frameworks detected" in out
    assert str(_fake_mcp_binary) in out
    start = out.index('{\n  "mcpServers"')
    snippet = json.loads(out[start:out.index("\n}\n", start) + 2])
    assert snippet["mcpServers"]["sibyl-memory"]["command"] == str(_fake_mcp_binary)
    assert "sibyl setup grokbot" in out


def test_print_config_flag(tmp_path, monkeypatch, capsys, _fake_mcp_binary):
    _env_home(tmp_path, monkeypatch)
    assert S.cmd_setup(_args(print_config=True)) == 0
    assert str(_fake_mcp_binary) in capsys.readouterr().out


def test_yes_autodetects_cursor_and_never_prompts(tmp_path, monkeypatch, capsys):
    _env_home(tmp_path, monkeypatch)
    (tmp_path / ".cursor").mkdir()
    def boom(*a, **k):
        raise AssertionError("prompted in --yes mode")
    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    rc = S.cmd_setup(_args(yes=True))
    assert rc == 0
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert "sibyl-memory" in data["mcpServers"]
    assert "Cursor" in capsys.readouterr().out


def test_setup_grokbot_wires_and_confirms(tmp_path, monkeypatch, capsys, _fake_mcp_binary):
    _env_home(tmp_path, monkeypatch)
    rc = S.cmd_setup(_args(target="grokbot", yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Grok Bot confirmation" in out
    assert str(_fake_mcp_binary) in out and "sibyl-memory" in out
    assert str(tmp_path / ".cursor" / "mcp.json") in out
    assert "sibyl-memory" in json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]


def test_cli_parser_accepts_grokbot_cursor_doctor():
    from sibyl_memory_cli import cli
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is None:
        pytest.skip("no build_parser")
    for argv in (["setup", "grokbot", "--yes"], ["setup", "cursor"], ["doctor"],
                 ["setup", "--print-config"]):
        assert p.parse_args(argv)
