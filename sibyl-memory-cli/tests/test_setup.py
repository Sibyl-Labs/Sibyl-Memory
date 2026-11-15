"""Tests for the v0.1.4 `sibyl setup` command.

Covers:
- Hermes wirer: fresh, existing-no-memory, existing-sibyl, existing-other-provider,
  force-overwrite, dry-run, plugin-install side-effect.
- Claude Code wirer: fresh-no-file, fresh-with-other-mcps, existing-sibyl,
  existing-sibyl-mismatch, force-overwrite, dry-run.
- Detection: is_present logic for both wirers.
- Outcomes: WireOutcome status field correctness.
- Atomic writes + backup files land at the expected paths.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sibyl_memory_cli.setup import (  # noqa: E402
    ALL_WIRERS,
    HOOK_EVENTS,
    ClaudeCodeWirer,
    HermesWirer,
    WireOutcome,
    _accept_defaults_prompt,
    _interactive_prompt,
    count_our_hooks,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _backups(harness: str) -> Path:
    """cli 0.7.0: where a wirer's config backups land (under the test HOME)."""
    return Path.home() / ".sibyl-memory" / "backups" / harness


def _stub_install_plugin(hermes_home: str):
    """Replacement for sibyl_memory_hermes.install_plugin.install: drops a fake
    adapter file so the wirer sees plugin_installed=True afterwards."""
    plugin_dir = Path(hermes_home) / "plugins" / "sibyl"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text("# stub plugin\n")


# ----------------------------------------------------------------------
# WireOutcome basics
# ----------------------------------------------------------------------

def test_outcome_dataclass_basic():
    o = WireOutcome("hermes", "wired", "test")
    assert o.name == "hermes" and o.status == "wired" and o.backup_path is None
    o2 = WireOutcome("claude-code", "skipped", "no", backup_path=Path("/tmp/x.bak"))
    assert o2.backup_path == Path("/tmp/x.bak")


# ----------------------------------------------------------------------
# Prompt helpers
# ----------------------------------------------------------------------

def test_interactive_prompt_default_y_empty_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _interactive_prompt("Q?", default="Y") == "y"


def test_interactive_prompt_default_n_empty_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _interactive_prompt("Q?", default="N") == "n"


def test_interactive_prompt_explicit_y(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _interactive_prompt("Q?", default="N") == "y"


def test_interactive_prompt_explicit_n(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert _interactive_prompt("Q?", default="Y") == "n"


def test_accept_defaults_prompt_returns_default():
    assert _accept_defaults_prompt("Q?", default="Y") == "y"
    assert _accept_defaults_prompt("Q?", default="N") == "n"


# ----------------------------------------------------------------------
# HermesWirer
# ----------------------------------------------------------------------

def test_hermes_wirer_auto_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom-hermes"))
    w = HermesWirer()
    assert w.hermes_home == tmp_path / "custom-hermes"


def test_hermes_wirer_auto_home_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    w = HermesWirer()
    assert w.hermes_home == Path.home() / ".hermes"


def test_hermes_is_present_false_when_no_dir_no_bin(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    w = HermesWirer(hermes_home=tmp_path / "nope")
    with patch("sibyl_memory_cli.setup.shutil.which", return_value=None):
        assert not w.is_present()


def test_hermes_is_present_true_when_dir_exists(tmp_path):
    (tmp_path / "hermes-home").mkdir()
    w = HermesWirer(hermes_home=tmp_path / "hermes-home")
    assert w.is_present()


def test_hermes_state_fresh(tmp_path):
    w = HermesWirer(hermes_home=tmp_path / "hermes-home")
    st = w.current_state()
    assert st["config_exists"] is False
    assert st["plugin_installed"] is False
    assert st["memory_provider"] is None
    assert st["wired_with_sibyl"] is False


def test_hermes_state_existing_sibyl(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: sibyl\n")
    w = HermesWirer(hermes_home=home)
    st = w.current_state()
    assert st["memory_provider"] == "sibyl"
    assert st["wired_with_sibyl"] is True


def test_hermes_state_existing_other_provider(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: mem0\nother: thing\n")
    w = HermesWirer(hermes_home=home)
    st = w.current_state()
    assert st["memory_provider"] == "mem0"
    assert st["wired_with_sibyl"] is False


def test_hermes_wire_fresh_creates_config_and_installs_plugin(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    w = HermesWirer(hermes_home=home)
    # Stub the install_plugin import via the wirer's _install_plugin override
    monkeypatch.setattr(
        HermesWirer, "_install_plugin",
        lambda self: _stub_install_plugin(str(self.hermes_home)),
    )
    outcome = w.wire()
    assert outcome.status == "wired"
    # Config now has memory.provider: sibyl
    import yaml
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg == {"memory": {"provider": "sibyl"}}
    # Plugin "installed" (stub created the file)
    assert (home / "plugins" / "sibyl" / "__init__.py").exists()


def test_hermes_wire_existing_sibyl_is_noop(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: sibyl\n")
    # Also pre-install the plugin so the noop path is true end-to-end
    (home / "plugins" / "sibyl").mkdir(parents=True)
    (home / "plugins" / "sibyl" / "__init__.py").write_text("# stub\n")
    w = HermesWirer(hermes_home=home)
    outcome = w.wire()
    assert outcome.status == "already"


def test_hermes_wire_existing_other_provider_refused_without_force(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: mem0\n")
    monkeypatch.setattr(
        HermesWirer, "_install_plugin",
        lambda self: _stub_install_plugin(str(self.hermes_home)),
    )
    w = HermesWirer(hermes_home=home)
    # No prompt_fn means non-interactive refusal
    outcome = w.wire()
    assert outcome.status == "skipped"
    # Config UNCHANGED
    assert "mem0" in (home / "config.yaml").read_text()


def test_hermes_wire_existing_other_provider_with_force(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: mem0\n")
    monkeypatch.setattr(
        HermesWirer, "_install_plugin",
        lambda self: _stub_install_plugin(str(self.hermes_home)),
    )
    w = HermesWirer(hermes_home=home)
    outcome = w.wire(force=True)
    assert outcome.status == "wired"
    import yaml
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["memory"]["provider"] == "sibyl"
    # Backup landed (audit #19: timestamped suffix, not a fixed .bak). cli 0.7.0:
    # backups live under ~/.sibyl-memory/backups/<harness>/, never beside the config.
    assert list(home.glob("config.yaml.*.bak")) == []
    backups = list((Path.home() / ".sibyl-memory" / "backups" / "hermes").glob("config.yaml.*.bak"))
    assert len(backups) == 1
    assert "mem0" in backups[0].read_text()


def test_hermes_wire_existing_other_provider_prompt_y_accepts(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: mem0\n")
    monkeypatch.setattr(
        HermesWirer, "_install_plugin",
        lambda self: _stub_install_plugin(str(self.hermes_home)),
    )
    w = HermesWirer(hermes_home=home)
    outcome = w.wire(prompt_fn=lambda q, *, default: "y")
    assert outcome.status == "wired"


def test_hermes_wire_dry_run_no_writes(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    w = HermesWirer(hermes_home=home)
    outcome = w.wire(dry_run=True)
    assert outcome.status == "dry-run"
    assert not (home / "config.yaml").exists()
    assert not (home / "plugins" / "sibyl" / "__init__.py").exists()


def test_hermes_wire_preserves_other_top_level_keys(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  name: gpt-4\ntools:\n  - search\n  - file\n"
    )
    monkeypatch.setattr(
        HermesWirer, "_install_plugin",
        lambda self: _stub_install_plugin(str(self.hermes_home)),
    )
    w = HermesWirer(hermes_home=home)
    w.wire()
    import yaml
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["model"]["name"] == "gpt-4"
    assert cfg["tools"] == ["search", "file"]
    assert cfg["memory"]["provider"] == "sibyl"


# ----------------------------------------------------------------------
# ClaudeCodeWirer
# ----------------------------------------------------------------------

def test_claude_is_present_false_when_no_settings_no_bin(monkeypatch, tmp_path):
    w = ClaudeCodeWirer(settings_path=tmp_path / "no.json")
    with patch("sibyl_memory_cli.setup.shutil.which", return_value=None):
        assert not w.is_present()


def test_claude_is_present_true_when_settings_exists(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{}")
    w = ClaudeCodeWirer(settings_path=p)
    assert w.is_present()


def test_claude_state_fresh(tmp_path):
    w = ClaudeCodeWirer(settings_path=tmp_path / "settings.json")
    st = w.current_state()
    assert st["settings_exists"] is False
    assert st["mcp_servers_count"] == 0
    assert st["sibyl_mcp"] is None
    assert st["wired_with_sibyl"] is False


def test_claude_state_existing_sibyl(tmp_path):
    # Panel fix 10 (2026-11-14): without the claude CLI an mcpServers entry in
    # settings.json is not a registration Claude Code reads, so it is never
    # reported as wired.
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "sibyl-memory-mcp"}}}))
    w = ClaudeCodeWirer(settings_path=p)
    st = w.current_state()
    assert st["wired_with_sibyl"] is False and st["cli_registered"] is None


def test_claude_state_existing_other_mcps_no_sibyl(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "mcpServers": {"github": {"command": "gh-mcp"}, "filesystem": {"command": "fs-mcp"}}
    }))
    w = ClaudeCodeWirer(settings_path=p)
    st = w.current_state()
    assert st["mcp_servers_count"] == 2
    assert st["sibyl_mcp"] is None
    assert st["wired_with_sibyl"] is False


def _assert_not_registered_outcome(outcome):
    assert outcome.status == "skipped"
    assert "not registered" in outcome.message
    nxt = outcome.handoff[0]
    assert nxt.startswith("claude mcp add --scope user sibyl-memory -- ")
    assert nxt in outcome.message


def test_claude_wire_without_cli_writes_no_mcp_entry(tmp_path):
    p = tmp_path / "subdir" / "settings.json"  # parent doesn't exist yet
    w = ClaudeCodeWirer(settings_path=p)
    outcome = w.wire()
    _assert_not_registered_outcome(outcome)
    cfg = json.loads(p.read_text())          # the hooks were still written
    assert "mcpServers" not in cfg
    assert count_our_hooks(cfg) == len(HOOK_EVENTS)


def test_claude_wire_without_cli_preserves_other_mcps(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "mcpServers": {"github": {"command": "gh-mcp"}},
        "theme": "dark",
    }))
    w = ClaudeCodeWirer(settings_path=p)
    outcome = w.wire()
    _assert_not_registered_outcome(outcome)
    cfg = json.loads(p.read_text())
    assert cfg["mcpServers"] == {"github": {"command": "gh-mcp"}}
    assert cfg["theme"] == "dark"
    # cli 0.7.0: backups under ~/.sibyl-memory/backups/claude-code/, nothing
    # beside the config; one backup, before the lifecycle hooks write.
    assert list(tmp_path.glob("settings.json.*.bak")) == []
    assert len(list(_backups("claude-code").glob("settings.json.*.bak"))) == 1


def test_claude_wire_existing_settings_entry_is_not_already(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "sibyl-memory-mcp"}}}))
    w = ClaudeCodeWirer(settings_path=p)
    _assert_not_registered_outcome(w.wire())
    first = list(_backups("claude-code").glob("settings.json.*.bak"))
    # The second run changes nothing and writes no further backup.
    _assert_not_registered_outcome(w.wire())
    assert list(_backups("claude-code").glob("settings.json.*.bak")) == first
    assert json.loads(p.read_text())["mcpServers"] == {
        "sibyl-memory": {"command": "sibyl-memory-mcp"}}


def test_claude_wire_mismatched_sibyl_untouched_even_with_force(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "/some/other/path"}}}))
    w = ClaudeCodeWirer(settings_path=p)
    for force in (False, True):
        _assert_not_registered_outcome(w.wire(force=force))
        assert json.loads(p.read_text())["mcpServers"]["sibyl-memory"]["command"] == "/some/other/path"


def test_claude_wire_dry_run_no_writes(tmp_path):
    p = tmp_path / "settings.json"
    w = ClaudeCodeWirer(settings_path=p)
    outcome = w.wire(dry_run=True)
    assert outcome.status == "dry-run"
    assert not p.exists()


def test_claude_wire_dry_run_without_cli_names_the_command(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "/old"}}}))
    w = ClaudeCodeWirer(settings_path=p, )
    outcome = w.wire(dry_run=True, force=True)
    assert outcome.status == "dry-run"
    assert "claude mcp add --scope user sibyl-memory" in outcome.message
    # Still no write
    assert "/old" in p.read_text()


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------

def test_registry_has_all_wirers():
    assert set(ALL_WIRERS) == {"hermes", "claude-code", "codex", "cursor", "deepseek"}
    assert ALL_WIRERS["hermes"] is HermesWirer
    assert ALL_WIRERS["claude-code"] is ClaudeCodeWirer
