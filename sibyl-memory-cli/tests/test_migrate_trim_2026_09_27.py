"""`sibyl migrate` trim step: every instruction file the scan found, not a
hardcoded CLAUDE.md (spec plugin-save-debloat 6.1-6.2, tests 8.4 items 27-29
and the Codex-only adversarial case).

On the pre-change tree: the Codex-only, two-file, .debloat.json-surface and
second-file backup tests FAIL (only CLAUDE.md was ever considered and the
phase was a single dict); CONTEXT_FILES / trim_candidates did not exist
(ImportError). test_29 and test_config_files_are_never_candidates have no
pre-change equivalent beyond the ImportError."""
from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_cli import cli
from sibyl_memory_cli import migrate as M

HISTORY = "".join(f"- 2025-0{1 + i % 8}-1{i % 9} decided thing {i} for the project history\n"
                  for i in range(8))
CURRENT = "# Agent rules\n\nRun the tests first.\nUse the venv.\nKeep answers short.\nNo secrets.\n\n"
BIG = CURRENT + "## Changelog\n" + HISTORY


def _extract_ok(backup_dir, db_path):
    c = MemoryClient.local(str(db_path), tenant_id="qa")
    c.set_entity("facts", "a", {"v": 1})


def _run(home: Path, proj: Path, tmp_path: Path, answers, **kw):
    db = home / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    io = M.GuidedIO(scripted=list(answers))
    rep = M.run_guided_setup(home=home, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=io, wirers={}, extract_fn=_extract_ok, **kw)
    return rep, io


def _asked(io: M.GuidedIO) -> list[str]:
    return [ln for ln in io.lines if ln.startswith("Trimmed ")]


def test_adv_codex_only_project_gets_agents_md_trimmed(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / "AGENTS.md").write_text(BIG)
    questions: list[str] = []

    class IO(M.GuidedIO):
        def confirm(self, q, *, default=True):
            questions.append(q)
            return True

    db = home / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    rep = M.run_guided_setup(home=home, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=IO(), wirers={}, extract_fn=_extract_ok)
    assert any(q.startswith("Trim AGENTS.md to lean now?") for q in questions), questions
    recs = rep["phases"]["debloat"]
    assert recs == [dict(recs[0], file="AGENTS.md", written=True)]
    txt = (proj / "AGENTS.md").read_text()
    assert "decided thing 5" not in txt and "Run the tests first." in txt
    assert "sibyl migrate" in txt and str(tmp_path / "bk") in txt


def test_27_claude_md_and_cursorrules_two_questions_two_records(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text(BIG)
    (proj / ".cursorrules").write_text("".join(f"Rule {i}: keep this current rule exactly as written.\n"
                                               for i in range(5)) + "\n"
                                       "History\n" + "old rule text that was replaced long ago\n" * 12)
    rep, io = _run(home, proj, tmp_path, ["y", "y"])
    recs = {r["file"]: r for r in rep["phases"]["debloat"]}
    assert set(recs) == {"CLAUDE.md", ".cursorrules"} and all(r["written"] for r in recs.values())
    # The summary prints one `trimmed` line per file, with its name.
    monkeypatch.setattr(M, "run_guided_setup", lambda **kw: rep)
    monkeypatch.setattr(M, "scan_memory_files", lambda h, c: [M.FoundFile("x", proj / "CLAUDE.md",
                                                                            "proj/CLAUDE.md", False, 1)])
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "y")
    monkeypatch.chdir(proj)
    cli.main(["migrate", "--yes"])
    out = capsys.readouterr().out
    trimmed = [ln for ln in out.splitlines() if "trimmed" in ln]
    assert len(trimmed) == 2
    assert any("CLAUDE.md" in ln for ln in trimmed) and any(".cursorrules" in ln for ln in trimmed)


def test_config_files_are_never_candidates_and_cursor_rules_dir_is_not_trimmed(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    (proj / ".cursor" / "rules").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "x"\n' * 200)
    (home / ".claude" / "settings.json").write_text(json.dumps({"k": "v" * 5000}))
    (proj / ".cursor" / "rules" / "a.mdc").write_text(BIG)
    files = M.scan_memory_files(home, proj)
    cands = M.trim_candidates(files, home=home, cwd=proj)
    names = {f.matched: why for f, why in cands}
    assert ".codex/config.toml" not in names and ".claude/settings.json" not in names
    assert names[".cursor/rules"].startswith("a directory")
    rep, io = _run(home, proj, tmp_path, [])
    assert "debloat" not in rep["phases"]
    assert (proj / ".cursor" / "rules" / "a.mdc").read_text() == BIG


def test_27_debloat_json_surface_restricts_and_outside_entries_are_ignored(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.md").write_text(BIG)
    (proj / "CLAUDE.md").write_text(BIG)
    (proj / "AGENTS.md").write_text(BIG)
    (proj / ".debloat.json").write_text(json.dumps({"surface": ["AGENTS.md", "../../other/file.md",
                                                                 str(other / "file.md")]}))
    rep, io = _run(home, proj, tmp_path, ["y"])
    assert [r["file"] for r in rep["phases"]["debloat"]] == ["AGENTS.md"]
    assert (proj / "CLAUDE.md").read_text() == BIG
    assert (other / "file.md").read_text() == BIG


def test_27_no_debloat_asks_nothing(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / "AGENTS.md").write_text(BIG)

    class IO(M.GuidedIO):
        def confirm(self, q, *, default=True):
            raise AssertionError("asked: " + q)

    db = home / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    rep = M.run_guided_setup(home=home, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=IO(), wirers={}, extract_fn=_extract_ok, debloat=False)
    assert "debloat" not in rep["phases"] and (proj / "AGENTS.md").read_text() == BIG


def test_27_small_file_is_no_safe_trim_and_not_asked(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / "AGENTS.md").write_text("# one\nline\n")
    rep, io = _run(home, proj, tmp_path, [])
    assert "debloat" not in rep["phases"]
    assert any("no safe trim found" in ln for ln in io.lines)


def test_28_backup_reverification_guards_the_second_file(tmp_path):
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text(BIG)
    (proj / "AGENTS.md").write_text(BIG)

    class IO(M.GuidedIO):
        def confirm(self, q, *, default=True):
            if "AGENTS.md" in q:
                for d in (tmp_path / "bk").glob("sibyl-migration-backup-*"):
                    bc = d / "proj" / "AGENTS.md"
                    if bc.exists():
                        bc.unlink()
            return True

    db = home / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    rep = M.run_guided_setup(home=home, cwd=proj, db_path=db, backup_parent=tmp_path / "bk",
                             io=IO(), wirers={}, extract_fn=_extract_ok)
    recs = {r["file"]: r for r in rep["phases"]["debloat"]}
    assert recs["CLAUDE.md"]["written"] is True
    assert recs["AGENTS.md"]["written"] is False
    assert recs["AGENTS.md"]["error"] == "backup re-verification failed"
    assert (proj / "AGENTS.md").read_text() == BIG


def test_29_context_and_config_files_cover_harness_files_exactly():
    every = {rel for rels in M.HARNESS_FILES.values() for rel in rels}
    assert M.CONTEXT_FILES | M.CONFIG_FILES == every
    assert not (M.CONTEXT_FILES & M.CONFIG_FILES)


def test_lean_text_for_prefers_the_splitter_and_falls_back(tmp_path):
    lean = M.lean_text_for(BIG, "CLAUDE.md", tmp_path / "bk")
    assert lean is not None and "Run the tests first." in lean and "decided thing" not in lean
    assert M.lean_text_for(BIG, "notes.json", tmp_path) is None
    flat = "# T\n## identity\nx\n## later\n" + "later stuff that is not history\n" * 20
    lean2 = M.lean_text_for(flat, "CLAUDE.md", tmp_path)
    assert lean2 is not None and "later stuff" not in lean2 and "a full copy of this file is in the pre-migration backup" in lean2
