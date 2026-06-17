"""PKG-4 (VRTX/deadguy beta): read-only `sibyl memory list/search/recall` CLI.
Lets a tester inspect what's actually stored without writing through an agent."""
from __future__ import annotations

from pathlib import Path

from sibyl_memory_client import MemoryClient
from sibyl_memory_cli import cli


def _store(tmp_path: Path) -> Path:
    d = tmp_path / "memory.db"
    c = MemoryClient.local(path=d)
    c.set_entity("partner", "Blocktronics", {"stage": "active", "note": "token forensics suite"})
    c.set_entity("partner", "Reppo", {"stage": "negotiation"})
    return d


def test_memory_list(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Blocktronics" in out and "Reppo" in out


def test_memory_list_category_filter(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "list", "partner", "--limit", "1"])
    assert rc == 0


def test_memory_search(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "search", "forensics"])
    out = capsys.readouterr().out
    assert rc == 0 and "Blocktronics" in out


def test_memory_recall(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "recall", "partner", "Blocktronics"])
    out = capsys.readouterr().out
    assert rc == 0 and "forensics" in out


def test_memory_recall_missing_returns_1(tmp_path):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "recall", "partner", "Nope"])
    assert rc == 1


def test_memory_no_store_returns_1(tmp_path):
    rc = cli.main(["--db", str(tmp_path / "absent.db"), "memory", "list"])
    assert rc == 1
