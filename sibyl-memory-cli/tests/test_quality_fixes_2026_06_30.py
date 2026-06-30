"""Regression tests for the 2026-06-30 quality fix-pass (audit #13, #19, #15).

All LOW severity; these prove the new robustness/diagnostic behavior:
  - #13 (B001): `status` reports the WAL-inclusive logical DB size (the same
    measure the cap gate enforces), not the raw `memory.db` file size.
  - #19 (B001): `migrate` SQLite connections always close, even on query error.
  - #19 (B005): `migrate._tree_size` skips an un-statable entry instead of
    aborting.
  - #19 (B005): `setup` config backups are timestamped and never clobber a
    prior backup.
  - #19 (B005): `setup._verify_mcp_starts` is a standalone helper (no cross-class
    dispatch).
  - #15: `status` store discovery survives a PermissionError on the profiles dir.
"""
from __future__ import annotations

import sqlite3
import time
from argparse import Namespace
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import migrate as M
from sibyl_memory_cli import setup as S


def _status_args(tmp_path, *, creds="credentials.json", db="memory.db",
                 tier_cache="tier_cache.json") -> Namespace:
    return Namespace(
        credentials=str(tmp_path / creds),
        db=str(tmp_path / db),
        tier_cache=str(tmp_path / tier_cache),
    )


# ----------------------------------------------------------------------
# #13 (B001) — status uses db_size_bytes (WAL-inclusive logical), not st_size
# ----------------------------------------------------------------------

def test_b001_status_uses_logical_db_size(tmp_path, monkeypatch, capsys):
    import json
    from sibyl_memory_client import MemoryClient

    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"account_id": "a"}), encoding="utf-8")
    db = tmp_path / "memory.db"
    MemoryClient.local(str(db), tenant_id="qa")  # real SQLite DB

    # Force the cap measure to a sentinel so we can prove status renders THAT
    # number, not the raw file st_size.
    sentinel = 1_234_567
    monkeypatch.setattr(
        "sibyl_memory_client.storage.db_size_bytes",
        lambda p: sentinel,
    )
    rc = cli.cmd_status(_status_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{sentinel:,}" in out          # logical size rendered
    raw = db.stat().st_size
    if raw != sentinel:
        assert f"{raw:,} bytes (" not in out  # raw st_size NOT used for the gate label


def test_b001_status_size_matches_gate_measure(tmp_path, capsys):
    """End-to-end (no monkeypatch): the rendered size equals db_size_bytes()."""
    import json
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.storage import db_size_bytes

    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"account_id": "a"}), encoding="utf-8")
    db = tmp_path / "memory.db"
    MemoryClient.local(str(db), tenant_id="qa")

    expected = db_size_bytes(db)
    rc = cli.cmd_status(_status_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{expected:,} bytes (" in out


# ----------------------------------------------------------------------
# #19 (B001) — migrate connections always close, even on query error
# ----------------------------------------------------------------------

def _make_db(path: Path, *, with_entities: bool) -> None:
    con = sqlite3.connect(str(path))
    try:
        if with_entities:
            con.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, category TEXT)")
            con.execute("INSERT INTO entities (category) VALUES ('people')")
            con.commit()
        else:
            # a real SQLite DB WITHOUT an entities table → COUNT(*) raises
            con.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
            con.commit()
    finally:
        con.close()


class _CountingConnection(sqlite3.Connection):
    """Connection subclass that records every close() into a class-level counter.

    sqlite3.Connection.close is a read-only C attribute (can't be monkeypatched
    per-instance), so we subclass and pass this as connect(factory=...).
    """

    closes = 0

    def close(self):  # noqa: D401 - thin override
        type(self).closes += 1
        return super().close()


def _tracking_connect(real_connect):
    def connect(*a, **k):
        k.setdefault("factory", _CountingConnection)
        return real_connect(*a, **k)
    return connect


def test_b001_verify_new_entries_closes_connection_on_error(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _make_db(db, with_entities=False)  # query path will raise OperationalError

    _CountingConnection.closes = 0
    monkeypatch.setattr(M.sqlite3, "connect", _tracking_connect(sqlite3.connect))
    out = M.verify_new_entries(db, baseline_total=0)
    assert out["ok"] is False
    assert "error" in out               # the missing-table error surfaced
    # _is_readable_db opens+closes once; the fix ensures the query connection is
    # ALSO closed despite the error → at least two closes total.
    assert _CountingConnection.closes >= 2


def test_b001_db_baseline_closes_connection_on_error(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    _make_db(db, with_entities=False)

    _CountingConnection.closes = 0
    monkeypatch.setattr(M.sqlite3, "connect", _tracking_connect(sqlite3.connect))
    # readable SQLite, no entities table → baseline 0, not unreadable
    assert M.db_baseline(db) == 0
    assert _CountingConnection.closes >= 2


def test_verify_new_entries_happy_path_still_works(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db, with_entities=True)
    out = M.verify_new_entries(db, baseline_total=0)
    assert out["ok"] is True
    assert out["new_total"] == 1
    assert out["by_category"] == {"people": 1}


# ----------------------------------------------------------------------
# #19 (B005) — _tree_size tolerates an unreadable / un-statable entry
# ----------------------------------------------------------------------

def test_b005_tree_size_skips_unreadable_entry(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    good = root / "good.txt"
    good.write_bytes(b"hello")        # 5 bytes
    bad = root / "bad.txt"
    bad.write_bytes(b"xxxxxxxxxx")    # would be 10 bytes if statable

    real_stat = Path.stat

    def flaky_stat(self, *a, **k):
        if self.name == "bad.txt":
            raise PermissionError("denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    # must NOT raise; the bad entry is skipped, only the good one counts
    assert M._tree_size(root) == 5


def test_b005_tree_size_tolerates_broken_symlink(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "real.txt").write_bytes(b"abc")  # 3 bytes
    broken = root / "dangling"
    try:
        broken.symlink_to(root / "does-not-exist")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # is_file()/stat() on a broken symlink must not abort the sum
    assert M._tree_size(root) == 3


# ----------------------------------------------------------------------
# #19 (B005) — timestamped backups never clobber a prior backup
# ----------------------------------------------------------------------

def test_b005_timestamped_backup_is_unique(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("v1\n")
    times = iter(["20260630T010000Z", "20260630T020000Z"])
    monkeypatch.setattr(
        S.time, "strftime",
        lambda fmt, t=None: next(times),
    )
    b1 = S._timestamped_backup(cfg)
    cfg.write_text("v2\n")
    b2 = S._timestamped_backup(cfg)
    assert b1 is not None and b2 is not None
    assert b1 != b2                       # distinct backup files
    assert b1.exists() and b2.exists()
    assert b1.read_text() == "v1\n"       # first backup not overwritten
    assert b2.read_text() == "v2\n"
    assert b1.name.endswith(".bak")
    assert ".20260630T010000Z." in b1.name
    # original extension preserved in the backup name
    assert b1.name.startswith("config.yaml.")


def test_b005_timestamped_backup_none_when_missing(tmp_path):
    assert S._timestamped_backup(tmp_path / "nope.yaml") is None


# ----------------------------------------------------------------------
# #19 (B005) — _verify_mcp_starts is a shared standalone helper
# ----------------------------------------------------------------------

def test_b005_verify_mcp_starts_is_module_helper():
    assert callable(S._verify_mcp_starts)
    # binary-not-found short-circuits without spawning anything
    ok, msg = S._verify_mcp_starts(None)
    assert ok is False
    assert "not found" in msg.lower()


def test_b005_both_wirers_delegate_to_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: calls.append(b) or (True, "ok"))
    monkeypatch.setattr(S.shutil, "which", lambda name: "/usr/bin/" + name)
    claude = S.ClaudeCodeWirer(settings_path=Path("/tmp/x.json"))
    codex = S.CodexWirer(config_path=Path("/tmp/x.toml"))
    assert claude.verify_mcp_starts() == (True, "ok")
    assert codex.verify_mcp_starts() == (True, "ok")
    # both routed through the shared helper with the resolved binary path
    assert calls == ["/usr/bin/sibyl-memory-mcp", "/usr/bin/sibyl-memory-mcp"]


# ----------------------------------------------------------------------
# #15 — _discover_stores survives a PermissionError on the profiles dir
# ----------------------------------------------------------------------

def test_hygiene_discover_stores_survives_restricted_profiles(tmp_path, monkeypatch):
    primary = tmp_path / ".sibyl-memory" / "memory.db"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"x" * 10)
    hermes = tmp_path / ".hermes"
    profiles = hermes / "sibyl" / "profiles"
    profiles.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.delenv("SIBYL_MEMORY_DB", raising=False)

    real_iterdir = Path.iterdir

    def flaky_iterdir(self):
        if self == profiles:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", flaky_iterdir)
    # must NOT raise; profiles sweep skipped, default store still discovered
    stores = cli._discover_stores(primary)
    assert any(s["label"] == "default (SDK/CLI/MCP)" for s in stores)
