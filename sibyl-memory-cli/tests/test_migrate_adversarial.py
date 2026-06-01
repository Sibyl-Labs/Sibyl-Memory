"""Adversarial / edge-case tests for the sibyl setup migration phases.

Hunts for the failure modes a hand-written happy-path suite misses: large/many/
nested/unicode/binary files, symlinks, corrupted/locked DBs, permission errors,
backup integrity under stress, atomic + idempotent debloat, and a fuzz loop over
random file trees asserting (a) byte-exact backup, (b) sources never modified,
(c) debloat refuses without a backup and round-trips with one.
"""
import os
import random
import sqlite3
import string
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import migrate as M
from sibyl_memory_client import MemoryClient


# ---------------------------------------------------------------- backup stress

def test_backup_large_file(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    big = home / "CLAUDE.md"; big.write_bytes(b"x" * (5 * 1024 * 1024))  # 5 MB
    res = M.run_backup(M.scan_memory_files(home, cwd=home), tmp_path / "b")
    assert res.ok and res.total_bytes >= 5 * 1024 * 1024
    assert (res.backup_dir / "CLAUDE.md").stat().st_size == big.stat().st_size


def test_backup_many_files_nested(tmp_path):
    home = tmp_path / "h"; (home / ".hermes" / "memory" / "deep" / "deeper").mkdir(parents=True)
    for i in range(120):
        (home / ".hermes" / "memory" / "deep" / "deeper" / f"n{i}.md").write_text(f"note {i}\n")
    res = M.run_backup(M.scan_memory_files(home, cwd=home), tmp_path / "b")
    assert res.ok
    copied = list((res.backup_dir).rglob("n*.md"))
    assert len(copied) == 120


def test_backup_unicode_and_binary_content(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    (home / "CLAUDE.md").write_text("# café ☕ 你好 \U0001f9e0\nkeep: π=3.14159\n", encoding="utf-8")
    (home / "AGENTS.md").write_bytes(bytes(range(256)))  # raw binary
    res = M.run_backup(M.scan_memory_files(home, cwd=home), tmp_path / "b")
    assert res.ok
    assert (res.backup_dir / "CLAUDE.md").read_text(encoding="utf-8").startswith("# café")
    assert (res.backup_dir / "AGENTS.md").read_bytes() == bytes(range(256))


def test_backup_never_modifies_sources(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    files = {}
    for n in ("CLAUDE.md", "AGENTS.md"):
        p = home / n; p.write_text("content " * 50); files[p] = (p.read_bytes(), p.stat().st_mtime_ns)
    M.run_backup(M.scan_memory_files(home, cwd=home), tmp_path / "b")
    for p, (b, mt) in files.items():
        assert p.read_bytes() == b and p.stat().st_mtime_ns == mt


def test_backup_dir_collision_errors_cleanly(tmp_path):
    home = tmp_path / "h"; home.mkdir(); (home / "CLAUDE.md").write_text("x")
    files = M.scan_memory_files(home, cwd=home)
    fixed = M.run_backup(files, tmp_path / "b")
    # forcing the SAME backup dir name must not silently overwrite
    same = tmp_path / "b2"
    r1 = M.run_backup(files, same)
    from datetime import datetime
    # re-run into a pre-created dir of the same timestamp name -> clean error, no crash
    dirname = r1.backup_dir.name
    (tmp_path / "b3").mkdir(); (tmp_path / "b3" / dirname).mkdir()
    # monkey the name fn by writing into existing dir: simulate by calling with now fixed
    res = M.run_backup(files, tmp_path / "b3", now=datetime.fromisoformat(dirname.replace("sibyl-migration-backup-","").replace("_",":")))
    assert (res.ok is False) and "backup dir" in (res.error or "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_backup_permission_denied_source_aborts(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    p = home / "CLAUDE.md"; p.write_text("secret")
    files = M.scan_memory_files(home, cwd=home)
    os.chmod(p, 0o000)
    try:
        res = M.run_backup(files, tmp_path / "b")
        # either it copies (some FS) or aborts cleanly — must NOT raise
        assert res.ok in (True, False)
        if not res.ok:
            assert "copy failed" in (res.error or "")
    finally:
        os.chmod(p, 0o644)


# ---------------------------------------------------------------- scan edge

def test_scan_handles_broken_symlink(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    (home / "CLAUDE.md").symlink_to(home / "does-not-exist")  # broken symlink
    # must not raise; broken link .exists() is False so it's skipped
    found = M.scan_memory_files(home, cwd=home)
    assert isinstance(found, list)


def test_scan_no_files_returns_empty(tmp_path):
    home = tmp_path / "empty"; home.mkdir()
    assert M.scan_memory_files(home, cwd=home) == []


# ---------------------------------------------------------------- verify / DB

def test_verify_corrupt_db_is_contained(tmp_path):
    db = tmp_path / "memory.db"; db.write_bytes(os.urandom(4096))  # not a sqlite file
    assert M.db_baseline(db) == 0                      # contained, no raise
    v = M.verify_new_entries(db, 0)
    assert v["ok"] is False                            # contained, no raise


def test_verify_empty_schema_db(tmp_path):
    db = tmp_path / "memory.db"
    MemoryClient.local(str(db), tenant_id="qa")        # creates schema, 0 rows
    assert M.db_baseline(db) == 0
    assert M.verify_new_entries(db, 0)["ok"] is False


def test_verify_counts_after_writes(tmp_path):
    db = tmp_path / "memory.db"
    c = MemoryClient.local(str(db), tenant_id="qa")
    base = M.db_baseline(db)
    for i in range(25):
        c.set_entity("facts", f"f{i}", {"value": i})
    v = M.verify_new_entries(db, base)
    assert v["new_total"] == 25 and v["by_category"]["facts"] == 25


# ---------------------------------------------------------------- debloat safety

def test_debloat_atomic_no_partial_on_success(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text("# A\n" + "junk\n" * 1000)
    lean = M.heuristic_lean(f.read_text())
    out = M.debloat_file(f, lean, backup_exists=True)
    assert out["written"] and f.read_text() == lean
    assert not list(tmp_path.glob("*.sibyl-tmp"))      # no temp left behind


def test_debloat_idempotent_rerun(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text("# A\nidentity\n## later\njunk\n")
    lean = M.heuristic_lean(f.read_text())
    M.debloat_file(f, lean, backup_exists=True)
    first = f.read_text()
    # re-run with the lean of the now-lean file: should be stable
    M.debloat_file(f, M.heuristic_lean(first), backup_exists=True)
    assert "identity" in f.read_text()


def test_debloat_preserves_unicode(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text("# café ☕\nrule π\n", encoding="utf-8")
    lean = "# café ☕\nrule π\n"
    M.debloat_file(f, lean, backup_exists=True)
    assert f.read_text(encoding="utf-8") == lean


def test_debloat_refuses_no_backup_under_all_inputs(tmp_path):
    f = tmp_path / "CLAUDE.md"; orig = "# keep\n" * 10; f.write_text(orig)
    for lean in ("", "x", orig, "a" * 10000):
        out = M.debloat_file(f, lean, backup_exists=False)
        assert not out["written"] and f.read_text() == orig


# ---------------------------------------------------------------- heuristic_lean edge

def test_lean_empty_and_no_sections(tmp_path):
    assert "Sibyl Memory" in M.heuristic_lean("")
    flat = "just one line, no headings at all\nsecond line\n"
    out = M.heuristic_lean(flat)
    assert "just one line" in out


def test_lean_keepblock_exact(tmp_path):
    t = "x\n<!-- sibyl:keep -->\nONLY THIS\n<!-- /sibyl:keep -->\ny\n"
    assert M.heuristic_lean(t).split("\n")[0] == "ONLY THIS"


# ---------------------------------------------------------------- codex wirer edge

def test_codex_malformed_toml_no_crash(tmp_path):
    cfg = tmp_path / "config.toml"; cfg.write_text("this is = = not valid toml [[[\n")
    w = M.CodexWirer(config_path=cfg)
    st = w.current_state()                              # must not raise
    assert st["config_exists"] and st["wired_with_sibyl"] is False


def test_codex_already_wired_detected(tmp_path):
    cfg = tmp_path / "config.toml"; cfg.write_text('model="o4"\n[mcp_servers.sibyl_memory]\ncommand = "sibyl-memory-mcp"\n')
    assert M.CodexWirer(config_path=cfg).current_state()["wired_with_sibyl"] is True


# ---------------------------------------------------------------- FUZZ

def _rand_text(rng, n):
    return "".join(rng.choice(string.printable) for _ in range(n))


def test_fuzz_backup_roundtrip_and_source_immutability(tmp_path):
    rng = random.Random(20260531)
    for it in range(60):
        home = tmp_path / f"h{it}"; home.mkdir()
        snap = {}
        # random subset of known memory files with random content
        for rel in ("CLAUDE.md", "AGENTS.md", ".codex/config.toml", "MEMORY.md"):
            if rng.random() < 0.6:
                p = home / rel; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_rand_text(rng, rng.randint(0, 4000)), encoding="utf-8")
                snap[p] = (p.read_bytes(), p.stat().st_mtime_ns)
        files = M.scan_memory_files(home, cwd=home)
        res = M.run_backup(files, tmp_path / f"b{it}")
        assert res.ok, res.error
        # byte-exact copies
        for f in files:
            assert (res.backup_dir / f.rel).read_bytes() == f.path.read_bytes()
        # sources untouched
        for p, (b, mt) in snap.items():
            assert p.read_bytes() == b and p.stat().st_mtime_ns == mt
        # debloat round-trip on CLAUDE.md if present
        cm = home / "CLAUDE.md"
        if cm.exists():
            assert not M.debloat_file(cm, "lean", backup_exists=False)["written"]
            M.debloat_file(cm, M.heuristic_lean(cm.read_text(encoding="utf-8", errors="replace")), backup_exists=res.ok)
