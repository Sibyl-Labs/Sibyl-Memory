# Contributing to Sibyl Memory

Thanks for looking. Bug reports, reproductions and ideas are the most useful things you can send, and they are always read.

## How changes land

Sibyl Memory ships as a paid product as well as an open-source plugin family, so **we do not merge outside code directly**. When a pull request finds a real bug or a good idea, we re-implement it in-house, ship it in the next release, and credit you by GitHub handle in the CHANGELOG and the release notes. A pull request is still the clearest way to show a fix: the diff, the failing case and the test are what we work from.

Please open an issue first for anything larger than a small fix, so we can tell you whether it fits before you spend the time.

## Reporting a bug

Use the bug report form. The most helpful reports include:

- the package versions (`sibyl status` prints them, or `pip show sibyl-memory-client sibyl-memory-cli sibyl-memory-mcp sibyl-memory-hermes`)
- your OS and Python version
- the harness (Claude Code, Codex, Cursor, Hermes, LangGraph, another MCP client)
- the exact command and the full error output
- the output of `sibyl doctor`, if the problem is with setup or the MCP server

Never paste the contents of `~/.sibyl-memory/credentials.json` or any token.

## Security issues

Do not open a public issue. Email sibyl@sibyllabs.org with the details and a way to reach you.

## Working on the code

The repo holds five packages, each with its own `pyproject.toml`, tests and CHANGELOG:

| Package | What it is |
| --- | --- |
| `sibyl-memory-client` | The SDK: local SQLite store, FTS5 search, the memory model |
| `sibyl-memory-cli` | The `sibyl` command: sign-in, setup for each harness, `doctor`, `memory` |
| `sibyl-memory-mcp` | The MCP server for Claude Code, Codex, Cursor and any MCP client |
| `sibyl-memory-hermes` | The Hermes memory provider |
| `sibyl-memory-langgraph` | A LangGraph `BaseStore` |

Setup (Python 3.10 or newer):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e sibyl-memory-client -e sibyl-memory-cli -e sibyl-memory-mcp -e sibyl-memory-hermes pytest
cd sibyl-memory-cli && pytest -q
```

Run the tests with a throwaway `HOME` (`HOME=$(mktemp -d) pytest -q`) so nothing touches your real `~/.sibyl-memory`, `~/.claude`, `~/.codex` or `~/.hermes`.

Every package supports Python 3.10 and newer. Do not use syntax or standard-library features that need 3.11 or 3.12 (for example, reusing an f-string's quote inside `{}`).

## Good first issues

Issues labelled `good first issue` are small, self-contained and have a clear done state. Comment on one before starting so two people do not take the same one.
