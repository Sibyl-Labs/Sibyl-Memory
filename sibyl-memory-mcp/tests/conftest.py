"""Shared pytest fixtures for the sibyl-memory-mcp test suite.

Home/env isolation (``_isolate_home``, autouse) — mirrors the sibyl-memory-client
suite's conftest. The client's account-level cap aggregation
(``aggregate_db_size``, client v0.4.18) walks real filesystem locations
(``~/.sibyl-memory/memory.db``, ``$HERMES_HOME/sibyl/...``, ``$SIBYL_MEMORY_DB``)
to sum every store an agent resolves. Without isolation, a developer's real
local memory store leaks into that aggregate and can trip the 2 MB free-tier cap
on every write test (the whole MCP suite writes through the cap gate). Every test
therefore gets HOME / USERPROFILE / HERMES_HOME pointed at a private tmp dir and
SIBYL_MEMORY_DB removed.

The autouse fixture also clears ``SIBYL_MEMORY_AUTO`` so the opt-in auto-memory
tools default to OFF regardless of the developer's ambient environment; tests
that exercise the enabled path set it explicitly.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the real user home + memory-store env, and force
    the opt-in auto-memory flag OFF by default."""
    fake_home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows Path.home()
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))
    monkeypatch.delenv("SIBYL_MEMORY_DB", raising=False)
    monkeypatch.delenv("SIBYL_MEMORY_AUTO", raising=False)
    return fake_home
