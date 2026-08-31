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

import ast
import inspect
from pathlib import Path

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.verdicts import GateCause, VerdictCode
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

# --------------------------------------------------------------------------
# one vocabulary
# --------------------------------------------------------------------------
def _code_only(text: str) -> str:
    """Strip docstrings and comments, leaving code.

    Parsed, not split on `\"\"\"`. The first version of this helper did
    `text.replace('\"\"\"', chr(0)).split(chr(0))[0::2]`, which silently INVERTS
    which half it scans when the marker count is odd — the test would then check
    the prose and skip the code, and still pass.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:                                # pragma: no cover
        return text
    spans = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):                 # ast.IfExp.body is an expr
            continue
        for child in body:
            if (isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)):
                spans.append((child.lineno, child.end_lineno))
    drop = {i for a, b in spans for i in range(a, b + 1)}
    return "\n".join(ln.split("#", 1)[0]
                     for i, ln in enumerate(text.splitlines(), 1) if i not in drop)


#: Derived from the enums, so renaming a cause updates the guard automatically.
CAUSE_LITERALS = tuple([c.value for c in VerdictCode if c is not VerdictCode.OK]
                       + [g.value for g in GateCause])


def _assert_declares_no_vocabulary(*modules):
    """BOTH quote styles. These tests originally checked only double quotes, and
    an adversarial review slipped `_LOCAL_CAUSE_COPY = 'abstained_on'` into the
    MCP server past a green suite — exactly the drift the guard exists to stop."""
    offenders = []
    for mod in modules:
        code = _code_only(inspect.getsource(mod))
        for lit in CAUSE_LITERALS:
            if f'"{lit}"' in code or f"'{lit}'" in code:
                offenders.append((mod.__name__, lit))
    assert offenders == [], f"cause strings re-declared outside verdicts.py: {offenders}"


def test_the_cli_imports_the_vocabulary_and_declares_none():
    """One vocabulary, one module. The CLI prints causes; it does not own them."""
    assert "from sibyl_memory_client.verdicts import refine_zero" in inspect.getsource(cli)
    _assert_declares_no_vocabulary(cli)
