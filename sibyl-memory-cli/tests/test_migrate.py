"""Tests for the `sibyl setup` guided-flow phases (migrate.py).

Exercises every DETERMINISTIC phase against a fake home: scan, backup (+byte verify
+ source-untouched), Codex wirer, extraction prompt, DB verify, heuristic lean,
and the confirmed-debloat safety gate (refuses without a backup).
"""
import os
from pathlib import Path

import pytest

from sibyl_memory_cli import migrate as M
from sibyl_memory_client import MemoryClient

BLOATED_CLAUDE = """# Project Atlas

## Identity
You are the Atlas build agent. Stay in scope.

## Rules
- never force-push
- run tests before commit

## Accumulated memory
- user prefers tabs over spaces
- API base is https://api.atlas.local
- met with Jordan about the Q3 roadmap on 2026-04-02
- the staging DB password rotates monthly
- learned: the flaky test is test_pipeline::test_retry
- project uses pnpm not npm
""" * 1  # ~ real-ish bloat


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "myproj").mkdir(parents=True)
    (home / "myproj" / "CLAUDE.md").write_text(BLOATED_CLAUDE, encoding="utf-8")
    (home / "AGENTS.md").write_text("# Agents\nuser likes concise answers\n", encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "o4"\n', encoding="utf-8")
    (home / ".hermes" / "memory").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("memory:\n  provider: flatfile\n", encoding="utf-8")
    (home / ".hermes" / "memory" / "notes.md").write_text("remembered: deploy on fridays\n", encoding="utf-8")
    return home


def test_scan_finds_files_across_harnesses(tmp_path):
    home = _fake_home(tmp_path)
    found = M.scan_memory_files(home, cwd=home / "myproj")
    rels = {f.rel for f in found}
    assert any("CLAUDE.md" in r for r in rels)
    assert "AGENTS.md" in rels
    assert ".codex/config.toml" in rels
    assert ".hermes/config.yaml" in rels
    # the hermes memory dir is captured as a directory
    assert any(f.is_dir and "memory" in f.rel for f in found)


def test_backup_copies_verifies_and_leaves_sources_untouched(tmp_path):
    home = _fake_home(tmp_path)
    src = home / "myproj" / "CLAUDE.md"
    src_bytes, src_mtime = src.read_bytes(), src.stat().st_mtime
    found = M.scan_memory_files(home, cwd=home / "myproj")
    res = M.run_backup(found, tmp_path / "backups")
    assert res.ok, res.error
    assert res.backup_dir.name.startswith("sibyl-migration-backup-")
    assert res.total_bytes > 0 and len(res.files) >= 4
    # backup contains a copy of CLAUDE.md
    assert any((res.backup_dir / r).exists() for r in res.files)
    # SOURCES UNTOUCHED
    assert src.read_bytes() == src_bytes
    assert src.stat().st_mtime == src_mtime


def test_codex_wirer_detect_and_instructions(tmp_path):
    home = _fake_home(tmp_path)
    w = M.CodexWirer(config_path=home / ".codex" / "config.toml")
    assert w.is_present()
    st = w.current_state()
    assert st["config_exists"] and not st["wired_with_sibyl"]
    instr = w.instructions()
    assert any("mcp_servers.sibyl_memory" in ln for ln in instr)


def test_wire_instructions_cover_all_harnesses():
    for h in ("claude-code", "codex", "hermes", "something-else"):
        assert isinstance(M.wire_instructions(h), list) and M.wire_instructions(h)
    assert "claude mcp add" in " ".join(M.wire_instructions("claude-code"))


def test_extraction_prompt_reads_from_backup_only(tmp_path):
    p = M.extraction_prompt("claude-code", tmp_path / "bk")
    assert "Read ONLY from the backup" in p
    assert "Do not edit, trim, or delete any live file" in p
    assert str(tmp_path / "bk") in p


def test_db_baseline_and_verify_new(tmp_path):
    db = tmp_path / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    assert M.db_baseline(db) == 0  # no DB rows yet
    c = MemoryClient.local(str(db), tenant_id="qa")
    baseline = M.db_baseline(db)
    c.set_entity("facts", "api_base", {"value": "https://api.atlas.local"})
    c.set_entity("preferences", "indent", {"value": "tabs"})
    c.set_entity("relationships", "jordan", {"note": "Q3 roadmap"})
    v = M.verify_new_entries(db, baseline)
    assert v["ok"] and v["new_total"] == 3
    assert set(v["by_category"]) >= {"facts", "preferences", "relationships"}


def test_heuristic_lean_keepblock_and_first_section():
    # explicit keep-block wins
    t = "junk\n<!-- sibyl:keep -->\nCORE RULES\n<!-- /sibyl:keep -->\nmore junk\n"
    lean = M.heuristic_lean(t)
    assert "CORE RULES" in lean and "junk" not in lean
    # else keep first ## section, trim later ones
    lean2 = M.heuristic_lean(BLOATED_CLAUDE)
    assert "Identity" in lean2
    assert "Accumulated memory" not in lean2     # later section trimmed
    assert len(lean2) < len(BLOATED_CLAUDE)
    assert "lives in Sibyl Memory" in lean2       # pointer appended


def test_debloat_refuses_without_backup(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text(BLOATED_CLAUDE, encoding="utf-8")
    out = M.debloat_file(f, "lean", backup_exists=False)
    assert not out["written"] and "refused" in out["error"]
    assert f.read_text(encoding="utf-8") == BLOATED_CLAUDE   # untouched


def test_debloat_trims_with_backup_and_dry_run(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text(BLOATED_CLAUDE, encoding="utf-8")
    lean = M.heuristic_lean(BLOATED_CLAUDE)
    # dry-run does not write
    dry = M.debloat_file(f, lean, backup_exists=True, dry_run=True)
    assert not dry["written"] and f.read_text(encoding="utf-8") == BLOATED_CLAUDE
    assert dry["after"] < dry["before"]
    # real write trims
    real = M.debloat_file(f, lean, backup_exists=True)
    assert real["written"] and f.read_text(encoding="utf-8") == lean
    assert f.stat().st_size < real["before"]


def test_detect_state_snapshot(tmp_path):
    home = _fake_home(tmp_path)
    st = M.detect_state(home, cwd=home / "myproj", db_path=home / ".sibyl-memory" / "memory.db")
    assert "files" in st and len(st["files"]) >= 4
    assert set(st["harnesses"]) == {"claude-code", "codex", "hermes"}
    assert st["db_entries"] == 0
