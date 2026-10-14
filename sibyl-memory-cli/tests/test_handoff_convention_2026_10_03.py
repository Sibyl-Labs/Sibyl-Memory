"""The agent handoff convention lint (spec agent-handoff-messages 2026-10-03, 4.2).

1. The lint itself (`conftest.assert_convention`) accepts the convention and
   refuses each way of breaking it.
2. The sweep: every scenario of test_handoff_rows_2026_10_03.py, run again
   and checked against the lint only.
3. A source check: no handoff sentence or command in the CLI or Hermes
   install source carries an em dash, a glyph or ANSI.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import assert_convention, handoff
import test_handoff_rows_2026_10_03 as rows
from test_handoff_rows_2026_10_03 import ctx, refresh, server, _quiet  # noqa: F401  (fixtures)


# ---- 1. the lint ------------------------------------------------------------------

GOOD = [
    "Activated.\n\nnext: sibyl setup\n",
    "x\nlater: sibyl memory adopt\nask your human: an email address\nnext: sibyl email add <address>\n\n",
    "x\nnext: done",
    "x\nnext: retry in 5 minutes",
    "x\nask your human: connect\nnext: retry",
]

BAD = [
    ("no next", "Activated.\n"),
    ("next not last", "next: done\nmore text\n"),
    ("two asks", "ask your human: a\nask your human: b\nnext: done\n"),
    ("ask not directly above next", "ask your human: a\nlater: sibyl x\nnext: done\n"),
    ("placeholder without ask", "x\nnext: sibyl email confirm <code>\n"),
    ("placeholder in later without ask", "x\nlater: sibyl claim <cs_code>\nnext: done\n"),
    ("em dash", "x\nask your human: a \u2014 b\nnext: done\n"),
    ("ansi", "x\nnext: \x1b[32mdone\x1b[0m\n"),
    ("unknown next grammar", "x\nnext: rm -rf /\n"),
    ("retry grammar", "x\nnext: retry soon\n"),
]


@pytest.mark.parametrize("out", GOOD)
def test_lint_accepts(out) -> None:
    assert_convention(out)


@pytest.mark.parametrize("why,out", BAD, ids=[b[0] for b in BAD])
def test_lint_refuses(why, out) -> None:
    with pytest.raises(AssertionError):
        assert_convention(out)


def test_lint_refuses_stderr() -> None:
    with pytest.raises(AssertionError):
        assert_convention("x\nnext: done\n", "a warning\n")


def test_handoff_parser_reads_waiting_lines_anywhere() -> None:
    h = handoff("ask your human: open it\nwaiting: for the bind (up to 1:00)\nActivated.\nnext: sibyl setup\n")
    assert h["waiting"] == ["waiting: for the bind (up to 1:00)"]
    assert h["human"] is None and h["next"] == "sibyl setup"


# ---- 2. the sweep -------------------------------------------------------------------

@pytest.mark.parametrize("sid", list(rows.SCENARIOS))
def test_every_stop_ends_with_the_convention(sid, ctx, capsys) -> None:  # noqa: F811
    cap, *_ = rows.run_scenario(sid, ctx, capsys)
    err = "\n".join(ln for ln in cap.err.splitlines() if "(defaults used)" not in ln)
    assert_convention(cap.out, err)


# ---- 3. the source --------------------------------------------------------------------

SRC = Path(__file__).resolve().parents[1] / "src" / "sibyl_memory_cli"
HERMES_SRC = Path(__file__).resolve().parents[2] / "sibyl-memory-hermes" / "src" / "sibyl_memory_hermes"
_CALLS = {"handoff", "waiting", "json_handoff", "_handoff", "_declined", "_refusal_handoff"}
_NAMES_ENDING = ("_HUMAN",)


def _strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value


def _handoff_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _CALLS:
                yield from _strings(n)
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.endswith(_NAMES_ENDING):
                    yield from _strings(n.value)


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")) + [HERMES_SRC / "install_plugin.py"],
                         ids=lambda p: p.name)
def test_no_em_dash_glyph_or_ansi_in_handoff_copy(path) -> None:
    for s in _handoff_strings(path):
        assert "\u2014" not in s, (path.name, s)
        assert "\x1b" not in s, (path.name, s)
        assert "✓" not in s and "⚠" not in s, (path.name, s)
