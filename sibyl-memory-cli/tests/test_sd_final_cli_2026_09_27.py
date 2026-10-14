"""Final fixes on the save/debloat build, CLI side (SD-11.., 2026-09-27).

C   `--root` given through a symlink names the real files (one authorisation,
    one backup slot); a stow-style ``~/.claude`` symlink is trimmed by an
    interactive free apply;
D   `sibyl debloat undo` without a file undoes the exact file the last apply
    trimmed (matched on its path identity), never the same NAME elsewhere.

Each test fails on 4abce3a (the docstring says how).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import state as ST

CURRENT = "# Agent rules\n\n" + "".join(f"- rule {i} stays current\n" for i in range(10)) + "\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def _offline_server(monkeypatch):
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "free", "cap_bytes": 5242880})

    def offline(*a, **k):
        raise cli.HttpError(0, {}, "offline")
    monkeypatch.setattr(cli, "http_request", offline)


@pytest.fixture
def home():
    return Path.home()


@pytest.fixture
def sm(home):
    return home / ".sibyl-memory"


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - 3600
    os.utime(p, (t, t))
    return p


def _acct(sm: Path) -> list[str]:
    return ["--credentials", str(sm / "credentials.json"), "--db", str(sm / "memory.db")]


def test_c_root_through_a_symlink_trims_the_real_file(tmp_path, sm, home, capsys, monkeypatch):
    """4abce3a refused every file under a `--root` reached through a symlink."""
    f = _write(tmp_path / "real" / "proj" / "AGENTS.md", CURRENT + HIST)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes", "--root",
                                 str(link / "proj")]) == 0, capsys.readouterr().out
    assert "old decision number 3" not in f.read_text() and link.is_symlink()
    assert (tmp_path / "real" / "proj" / "AGENTS-history.md").is_file()
    rec = ST.LifecycleState(home).load()["debloat"]["last_apply"]["results"][0]
    assert rec["id"] == f.as_posix() and rec["applied"]
    # The same file from the real root: one backup slot, undo finds it.
    assert cli.main(_acct(sm) + ["debloat", "undo", "AGENTS.md", "--yes", "--root",
                                 str(tmp_path / "real" / "proj")]) == 0
    assert "old decision number 3" in f.read_text()


def test_c_auto_through_a_symlinked_root_stores_the_real_path(tmp_path, sm, home, capsys, monkeypatch):
    f = _write(tmp_path / "real" / "proj" / "AGENTS.md", CURRENT + HIST)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert cli.main(_acct(sm) + ["debloat", "auto", "AGENTS.md", "--yes", "--root",
                                 str(link / "proj")]) == 0
    assert P.load(home)["auto_files"] == [f.as_posix()]
    assert cli.main(_acct(sm) + ["debloat", "manual", str(link / "proj" / "AGENTS.md"),
                                 "--root", str(tmp_path)]) == 0
    assert P.load(home)["auto_files"] == []


def test_c_stow_dot_claude_is_trimmed_by_a_free_interactive_apply(tmp_path, sm, home, capsys, monkeypatch):
    """4abce3a: 'refused: ~/.claude is a symlink' for every stow user."""
    f = _write(home / "dotfiles" / "claude" / "CLAUDE.md", CURRENT + HIST)
    (home / ".claude").symlink_to(home / "dotfiles" / "claude", target_is_directory=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert cli.main(_acct(sm) + ["debloat", "apply", "~/.claude/CLAUDE.md", "--yes"]) == 0, \
        capsys.readouterr().out
    assert "old decision number 3" not in f.read_text() and (home / ".claude").is_symlink()
    assert (home / "dotfiles" / "claude" / "CLAUDE-history.md").is_file()


def test_d_bare_undo_never_restores_the_same_name_in_another_project(tmp_path, sm, home, capsys,
                                                                      monkeypatch):
    """4abce3a matched the last apply on its bare label: after trimming
    repo-a/AGENTS.md, a bare `sibyl debloat undo` in repo-b restored
    repo-b's AGENTS.md from repo-b's own backup."""
    a = _write(tmp_path / "repo-a" / "AGENTS.md", CURRENT + HIST)
    b = _write(tmp_path / "repo-b" / "AGENTS.md", CURRENT + HIST)
    a_orig = a.read_bytes()
    monkeypatch.chdir(b.parent)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    b_trimmed = b.read_bytes()
    monkeypatch.chdir(a.parent)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(b.parent)
    assert cli.main(_acct(sm) + ["debloat", "undo", "--yes"]) == 1
    out = capsys.readouterr().out
    assert a.as_posix() in out and "run `sibyl debloat undo <file>` where it lives" in out
    assert b.read_bytes() == b_trimmed
    monkeypatch.chdir(a.parent)
    assert cli.main(_acct(sm) + ["debloat", "undo", "--yes"]) == 0
    assert a.read_bytes() == a_orig and b.read_bytes() == b_trimmed


def test_d_a_record_without_an_id_asks_for_the_file(tmp_path, sm, home, capsys, monkeypatch):
    f = _write(tmp_path / "proj" / "AGENTS.md", CURRENT + HIST)
    monkeypatch.chdir(f.parent)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    trimmed = f.read_bytes()

    def strip(st):
        for r in st["debloat"]["last_apply"]["results"]:
            r.pop("id", None)
    ST.LifecycleState(home).update(strip)
    capsys.readouterr()
    assert cli.main(_acct(sm) + ["debloat", "undo", "--yes"]) == 1
    assert "name the file" in capsys.readouterr().out and f.read_bytes() == trimmed
