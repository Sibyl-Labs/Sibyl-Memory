"""Every package declares requires-python >=3.10, so no source file may use syntax
that only parses on 3.12+ (issue #32).

The one form that has shipped is PEP 701 quote reuse: an f-string whose replacement
field contains a string literal using the f-string's own quote character, e.g.
f"{x or "-"}". Python 3.12's parser accepts it even with feature_version=(3, 10), so
this test walks the token stream instead. It needs the 3.12 tokenizer (FSTRING_START
tokens) and is skipped on older interpreters, where such a file fails to import anyway.
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="needs the 3.12 tokenizer (FSTRING_START tokens)"
)

_CLI_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _CLI_ROOT.parent
_PACKAGES = (
    "sibyl-memory-cli",
    "sibyl-memory-client",
    "sibyl-memory-mcp",
    "sibyl-memory-hermes",
    "sibyl-memory-langgraph",
)


def _source_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _PACKAGES:
        root = _REPO_ROOT / pkg
        for sub in ("src", "hermes-catalog"):
            if (root / sub).is_dir():
                files.extend(sorted((root / sub).rglob("*.py")))
    return files


def _quote_of(token_text: str) -> str:
    body = token_text.lstrip("rRbBuUfF")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def _quote_reuse(source: str) -> list[int]:
    """Line numbers where a string inside an f-string reuses an enclosing quote."""
    hits: list[int] = []
    enclosing: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_START:
            quote = _quote_of(tok.string)
            if quote in enclosing:
                hits.append(tok.start[0])
            enclosing.append(quote)
        elif tok.type == tokenize.FSTRING_END:
            enclosing.pop()
        elif tok.type == tokenize.STRING and enclosing:
            if _quote_of(tok.string) in enclosing:
                hits.append(tok.start[0])
    return hits


def test_detector_catches_the_issue_32_line():
    bad = 'print(f"  {a.bold(acct or "x")}")\n'
    good = "print(f\"  {a.bold(acct or 'x')}\")\n"
    assert _quote_reuse(bad) == [1]
    assert _quote_reuse(good) == []


def test_scan_finds_source_files():
    files = _source_files()
    assert any(f.name == "cli.py" for f in files)


def test_no_fstring_quote_reuse_in_any_package():
    offenders = []
    for path in _source_files():
        for line in _quote_reuse(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line}")
    assert offenders == [], (
        "f-string reuses its own quote inside a replacement field (3.12+ only, "
        "breaks requires-python >=3.10): " + ", ".join(offenders)
    )
