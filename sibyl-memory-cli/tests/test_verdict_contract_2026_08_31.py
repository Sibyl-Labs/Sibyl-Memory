"""The verdict contract in the terminal (stage 3, 2026-08-31).

`sibyl memory search` used to print "(no matches for 'x')" and stop. That is the
terminal-shaped version of the defect this stage closes: a zero that cannot
explain itself, and in the CLI's case the person reading it is a new user
deciding whether the product works. "No matches" against an EMPTY store is the
single most misleading thing this command could say, and it was what it said.

These tests pin that every zero prints a cause in plain language, that the cause
is the canonical one from `sibyl_memory_client.verdicts` rather than a
CLI-local string, and that nothing about a successful search changed.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.verdicts import VerdictCode
from sibyl_memory_cli import cli


def _store(tmp_path: Path) -> Path:
    d = tmp_path / "memory.db"
    c = MemoryClient.local(path=d)
    c.set_entity("partner", "Blocktronics",
                 {"stage": "active", "note": "token forensics suite"})
    c.set_entity("partner", "Reppo", {"stage": "negotiation"})
    return d


def _empty_store(tmp_path: Path) -> Path:
    d = tmp_path / "empty.db"
    MemoryClient.local(path=d)
    return d


def test_a_zero_result_prints_a_cause_not_just_no_matches(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "search", "qwzjvxzzyplm"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no matches" in out
    assert f"cause: {VerdictCode.NO_MATCH.value}" in out
    assert "genuine miss" in out


def test_an_empty_store_says_so_instead_of_blaming_the_query(tmp_path, capsys):
    """The one that matters most to a new user: this is a fixable state, and
    'no matches' hid that behind what looks like a failed search."""
    d = _empty_store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "search", "anything at all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"cause: {VerdictCode.EMPTY_STORE.value}" in out
    assert "no rows yet" in out


def test_the_cause_is_plain_language_a_human_can_act_on(tmp_path, capsys):
    d = _empty_store(tmp_path)
    cli.main(["--db", str(d), "memory", "search", "anything at all"])
    out = capsys.readouterr().out
    # a sentence, not an enum dump
    assert "Nothing was returned" in out


def test_a_successful_search_is_unchanged(tmp_path, capsys):
    d = _store(tmp_path)
    rc = cli.main(["--db", str(d), "memory", "search", "forensics"])
    out = capsys.readouterr().out
    assert rc == 0 and "Blocktronics" in out
    assert "cause:" not in out          # no noise on the success path


def test_the_cli_imports_the_vocabulary_and_declares_none():
    """One vocabulary, one module. The CLI prints causes; it does not own them."""
    src = inspect.getsource(cli)
    assert "from sibyl_memory_client.verdicts import refine_zero" in src
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    parts = code.replace('"""', "\x00").split("\x00")
    code_only = "".join(parts[0::2])
    for lit in ("abstained_on", "negation_abstain", "coverage_floor",
                "anchor_gate", "prep_filter", "empty_store", "no_match"):
        assert f'"{lit}"' not in code_only, lit
