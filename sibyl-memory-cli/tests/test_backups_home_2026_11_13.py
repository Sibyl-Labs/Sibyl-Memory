"""One Sibyl footprint (spec dsh-doctor v2, section 7): every wirer's config
backup lands under ~/.sibyl-memory/backups/<harness>/ (dirs 0700, files 0600),
nothing of ours is left beside a harness config, a failed write leaves no temp
file, and a rewrite keeps the config's mode. Temp HOME and PATH only."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import (
    ClaudeCodeWirer,
    CodexWirer,
    CursorWirer,
    DeepSeekWirer,
    GrokBotWirer,
    HermesWirer,
)

from _harness_fixtures import backups_dir, isolate_path, leftovers


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    monkeypatch.setattr(HermesWirer, "_install_plugin", _stub_plugin)
    return fake


def _stub_plugin(self):
    d = Path(self.hermes_home) / "plugins" / "sibyl"
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("# stub\n")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# --- a real write per harness ------------------------------------------------

def _hermes(tmp_path):
    h = tmp_path / "hermes-home"
    h.mkdir()
    cfg = h / "config.yaml"
    cfg.write_text("memory:\n  provider: mem0\n")
    out = HermesWirer(hermes_home=h).wire(force=True)
    return out, cfg, "hermes", ""


def _claude(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"theme": "dark"}))
    lines = ClaudeCodeWirer(settings_path=cfg).wire_hooks()
    bline = [ln for ln in lines if "backup at" in ln]
    out = S.WireOutcome("claude-code", "wired", "\n".join(lines),
                        backup_path=Path(bline[0].split("backup at ", 1)[1].strip()) if bline else None)
    return out, cfg, "claude-code", ""


def _codex(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('model = "o4"\n')
    return CodexWirer(config_path=cfg).wire(), cfg, "codex", ""


def _cursor(tmp_path, cls=CursorWirer):
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}))
    return cls(config_path=cfg).wire(), cfg, "cursor", ""


def _grokbot(tmp_path):
    return _cursor(tmp_path, GrokBotWirer)


TEMPLATE = "# profile patch layer\n# two\n# three\n[]\n"


def _deepseek_home(tmp_path):
    cfg = Path.home() / ".dsh" / "cordis.patch.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(TEMPLATE)
    return DeepSeekWirer().wire(), cfg, "deepseek", ""


def _deepseek_profile(tmp_path):
    cfg = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(TEMPLATE)
    return DeepSeekWirer(profile="web").wire(), cfg, "deepseek", "profile-web"


WRITES = {"hermes": _hermes, "claude-code-hooks": _claude, "codex": _codex,
          "cursor": _cursor, "grokbot": _grokbot, "deepseek-home": _deepseek_home,
          "deepseek-profile": _deepseek_profile}


@pytest.mark.parametrize("which", sorted(WRITES))
def test_backup_lands_under_sibyl_home_per_harness(which, tmp_path):
    out, cfg, label, sub = WRITES[which](tmp_path)
    assert out.status == "wired", out.message
    assert out.backup_path is not None
    assert out.backup_path.parent == backups_dir(label, sub)
    assert out.backup_path.name.startswith(cfg.name + ".") and out.backup_path.name.endswith(".bak")
    assert out.backup_path.is_file()


@pytest.mark.parametrize("which", sorted(WRITES))
def test_nothing_left_beside_harness_config(which, tmp_path):
    out, cfg, _label, _sub = WRITES[which](tmp_path)
    assert out.status == "wired"
    assert leftovers(cfg.parent) == []


def test_backup_dirs_0700_files_0600(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    os.chmod(cfg, 0o644)
    root = Path.home() / ".sibyl-memory" / "backups"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    b = S._timestamped_backup(cfg, "cursor")
    assert _mode(b) == 0o600
    assert _mode(b.parent) == 0o700
    assert _mode(root) == 0o700
    b2 = S._timestamped_backup(cfg, "deepseek", "profile-web")
    assert _mode(b2.parent) == 0o700 and _mode(b2.parent.parent) == 0o700
    assert _mode(b2) == 0o600
    assert b2.read_bytes() == b"{}"


def test_same_second_backups_do_not_clobber(tmp_path, monkeypatch):
    monkeypatch.setattr(S.time, "strftime", lambda fmt, t=None: "20261113T120000Z")
    cfg = tmp_path / "config.toml"
    cfg.write_text("one\n")
    b1 = S._timestamped_backup(cfg, "codex")
    cfg.write_text("two\n")
    b2 = S._timestamped_backup(cfg, "codex")
    assert b1.name == "config.toml.20261113T120000Z.bak"
    assert b2.name == "config.toml.20261113T120000Z.2.bak"
    assert b1.read_text() == "one\n" and b2.read_text() == "two\n"


def test_backup_failure_blocks_config_write(tmp_path):
    home_dir = Path.home() / ".sibyl-memory"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "backups").write_text("not a directory")
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    original = json.dumps({"mcpServers": {"other": {"command": "npx"}}})
    cfg.write_text(original)
    out = CursorWirer(config_path=cfg).wire()
    assert out.status == "error"
    assert "could not back up" in out.message and "was not modified" in out.message
    assert cfg.read_text() == original
    # the same rule for Codex and Hermes
    ccfg = tmp_path / "config.toml"
    ccfg.write_text('model = "o4"\n')
    assert CodexWirer(config_path=ccfg).wire().status == "error"
    assert ccfg.read_text() == 'model = "o4"\n'
    h = tmp_path / "hh"
    h.mkdir()
    (h / "config.yaml").write_text("memory:\n  provider: mem0\n")
    assert HermesWirer(hermes_home=h).wire(force=True).status == "error"
    assert (h / "config.yaml").read_text() == "memory:\n  provider: mem0\n"
    # and the hooks path reports it instead of raising
    scfg = tmp_path / "settings.json"
    scfg.write_text("{}")
    lines = ClaudeCodeWirer(settings_path=scfg).wire_hooks()
    assert lines and lines[0].startswith("hooks not written: backup failed")
    assert scfg.read_text() == "{}"


def test_backup_follows_isolated_home(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("x: 1\n")
    b = S._timestamped_backup(cfg, "hermes")
    assert str(b).startswith(os.environ["HOME"])
    assert str(b).startswith(str(Path.home()))


def _fail_replace(*a, **k):
    raise OSError("simulated rename failure")


def _write_hermes(tmp_path):
    h = tmp_path / "hermes-home"
    h.mkdir()
    (h / "config.yaml").write_text("a: 1\n")
    import yaml
    w = HermesWirer(hermes_home=h)
    return h, lambda: w._write_config_with_sibyl(yaml)




def _write_codex(tmp_path):
    p = tmp_path / ".codex" / "config.toml"
    p.parent.mkdir()
    p.write_text('model = "o4"\n')
    return p.parent, CodexWirer(config_path=p)._append_block


def _write_cursor(tmp_path):
    p = tmp_path / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text("{}")
    return p.parent, CursorWirer(config_path=p)._write_config


# Panel fix 10 (2026-11-14): Claude Code's settings.json mcpServers writer was
# removed (the claude CLI registers the server); its hooks use _atomic_text.
@pytest.mark.parametrize("writer", [_write_hermes, _write_codex, _write_cursor],
                         ids=["hermes", "codex", "cursor"])
def test_failed_write_leaves_no_tmp_in_harness_dir(writer, tmp_path, monkeypatch):
    d, write = writer(tmp_path)
    before = {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}
    monkeypatch.setattr(S.os, "replace", _fail_replace)
    with pytest.raises(OSError):
        write()
    assert leftovers(d) == []
    assert {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()} == before


def test_config_mode_kept_by_rewrite(tmp_path):
    h = tmp_path / "hermes-home"
    h.mkdir()
    y = h / "config.yaml"
    y.write_text("memory:\n  provider: mem0\n")
    os.chmod(y, 0o600)
    assert HermesWirer(hermes_home=h).wire(force=True).status == "wired"
    assert _mode(y) == 0o600
    m = tmp_path / ".cursor" / "mcp.json"
    m.parent.mkdir()
    m.write_text("{}")
    os.chmod(m, 0o600)
    assert CursorWirer(config_path=m).wire().status == "wired"
    assert _mode(m) == 0o600
