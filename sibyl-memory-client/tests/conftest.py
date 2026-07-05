"""Shared pytest fixtures for the sibyl-memory-client test suite.

Two hermeticity guarantees:

1. **Canonical source import.** This repo is the canonical source tree for
   the published package. Prepend ``src/`` to ``sys.path`` so the suite
   always exercises THIS tree, even on machines where an (older) editable
   install of ``sibyl_memory_client`` resolves to a different checkout.

2. **Home/env isolation** (``_isolate_home``, autouse). The account-level
   cap aggregation (``aggregate_db_size``, 0.4.18) walks real filesystem
   locations — ``~/.sibyl-memory/memory.db``, ``$HERMES_HOME/sibyl/...``,
   and ``$SIBYL_MEMORY_DB`` — to sum every store an agent resolves. Without
   isolation, a developer's real local memory store would leak into the
   aggregate and skew (or spuriously trip) the cap in tests. Every test
   therefore gets HOME/USERPROFILE/HERMES_HOME pointed at a private tmp dir
   and SIBYL_MEMORY_DB removed, keeping the aggregate-cap tests hermetic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# (1) Canonical source import: this tree's src/ wins over any installed copy.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the real user home and memory-store env.

    Keeps ``aggregate_db_size``'s candidate walk (SDK default store, Hermes
    adapter + profiles, SIBYL_MEMORY_DB override) confined to a per-test
    tmp home, so no real local store can leak into cap-size aggregates.
    """
    fake_home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows Path.home()
    monkeypatch.setenv("HERMES_HOME", str(fake_home / ".hermes"))
    monkeypatch.delenv("SIBYL_MEMORY_DB", raising=False)
    return fake_home
