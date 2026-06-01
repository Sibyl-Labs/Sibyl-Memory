"""End-to-end tests for the assembled `run_guided_setup` flow (the orchestrator).
Wirers are injected at fake paths; extraction is stubbed (the real-agent extraction is
validated separately by the live `claude -p` trial). conftest forces no-claude-CLI so
the Claude wirer uses the settings.json fallback at the fake path — never real config."""
from pathlib import Path

import pytest

from sibyl_memory_cli import migrate as M
from sibyl_memory_cli.setup import ClaudeCodeWirer, CodexWirer
from sibyl_memory_client import MemoryClient

BLOAT = """# Project Atlas

## Identity
Atlas build agent. Stay in scope.

## Rules
- run tests before commit

## Accumulated memory
- user prefers tabs over spaces
- API base is https://api.atlas.local
- met Jordan about the Q3 roadmap
"""


def _home(tmp_path):
    h = tmp_path / "home"
    (h / "proj").mkdir(parents=True)
    (h / "proj" / "CLAUDE.md").write_text(BLOAT)
    (h / "AGENTS.md").write_text("user likes concise answers\n")
    (h / ".codex").mkdir()
    (h / ".codex" / "config.toml").write_text('model = "o4"\n')
    return h


def _fake_wirers(h):
    return {
        "claude-code": ClaudeCodeWirer(settings_path=h / ".claude" / "settings.json"),
        "codex": CodexWirer(config_path=h / ".codex" / "config.toml"),
    }


def test_orchestrator_full_flow(tmp_path):
    h = _home(tmp_path); proj = h / "proj"
    db = h / ".sibyl-memory" / "memory.db"; db.parent.mkdir(parents=True)
    original = (proj / "CLAUDE.md").read_text()

    def fake_extract(backup_dir, db_path):
        # the agent reads from the BACKUP (never the live file) and writes to Sibyl
        assert (backup_dir / "proj" / "CLAUDE.md").read_text() == original
        c = MemoryClient.local(str(db_path), tenant_id="qa")
        c.set_entity("preferences", "indent", {"value": "tabs"})
        c.set_entity("facts", "api_base", {"value": "https://api.atlas.local"})
        c.set_entity("relationships", "jordan", {"note": "Q3 roadmap"})

    io = M.GuidedIO(scripted=["y"])   # confirm debloat = yes
    rep = M.run_guided_setup(home=h, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=io, wirers=_fake_wirers(h), extract_fn=fake_extract)

    assert rep["ok"]
    assert rep["phases"]["backup"]["ok"] and rep["phases"]["backup"]["files"] >= 3
    assert rep["phases"]["wire"]["codex"] in ("wired", "already")
    assert rep["phases"]["wire"]["claude-code"] in ("wired", "already")
    assert rep["phases"]["verify"]["new_total"] == 3
    assert rep["phases"]["debloat"]["written"]
    # live file trimmed, backup holds the full original
    assert (proj / "CLAUDE.md").stat().st_size < rep["phases"]["debloat"]["before"]
    bdir = Path(rep["phases"]["backup"]["dir"])
    assert "API base" in (bdir / "proj" / "CLAUDE.md").read_text()
    # codex config really got the block
    assert "[mcp_servers.sibyl_memory]" in (h / ".codex" / "config.toml").read_text()


def test_orchestrator_no_files_aborts(tmp_path):
    h = tmp_path / "empty"; h.mkdir()
    rep = M.run_guided_setup(home=h, cwd=h, db_path=h / "m.db",
                             backup_parent=tmp_path / "bk", io=M.GuidedIO())
    assert rep["ok"] is False


def test_orchestrator_backup_failure_blocks_everything(tmp_path, monkeypatch):
    h = _home(tmp_path); proj = h / "proj"; orig = (proj / "CLAUDE.md").read_text()
    monkeypatch.setattr(M, "run_backup",
                        lambda files, parent, now=None: M.BackupResult(backup_dir=parent / "x", ok=False, error="disk full"))
    rep = M.run_guided_setup(home=h, cwd=proj, db_path=h / "m.db",
                             backup_parent=tmp_path / "bk", io=M.GuidedIO(["y"]))
    assert rep["ok"] is False
    assert "wire" not in rep["phases"] and "debloat" not in rep["phases"]
    assert (proj / "CLAUDE.md").read_text() == orig   # never touched


def test_orchestrator_declined_debloat_keeps_file(tmp_path):
    h = _home(tmp_path); proj = h / "proj"; orig = (proj / "CLAUDE.md").read_text()
    db = h / ".sibyl-memory" / "memory.db"; db.parent.mkdir(parents=True)

    def fx(bk, dbp):
        MemoryClient.local(str(dbp), tenant_id="qa").set_entity("f", "a", {"v": 1})

    rep = M.run_guided_setup(home=h, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=M.GuidedIO(scripted=["n"]),   # decline debloat
                             wirers={"codex": CodexWirer(config_path=h / ".codex" / "config.toml")},
                             extract_fn=fx)
    assert rep["phases"]["verify"]["new_total"] == 1
    assert "debloat" not in rep["phases"]
    assert (proj / "CLAUDE.md").read_text() == orig   # declined -> untouched
