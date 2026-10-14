"""Shared pytest fixtures for the sibyl-memory-client test suite.

Three hermeticity guarantees:

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

3. **Entitled test home** (``entitled_test_home``, autouse, 0.9.0 account
   gate, spec 4.4). A throwaway Ed25519 keypair (one per session) is
   monkeypatched into ``sibyl_memory_client._entitlement.TRUSTED_KEYS``, every
   refresh is routed to that signer (no test ever reaches the network), and
   the isolated fake home gets a ``credentials.json`` plus a valid
   ``entitlement.json``. Tests therefore exercise the REAL gate, and the
   existing ``MemoryClient.local(...)`` call sites need no edits.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# (1) Canonical source import: this tree's src/ wins over any installed copy.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from sibyl_memory_client.testing import (  # noqa: E402
    TestSigner,
    forbid_external_network,
    install_entitled_home,
    patch_trusted_keys,
)


@pytest.fixture(scope="session")
def test_signer() -> TestSigner:
    return TestSigner()


@pytest.fixture(scope="session", autouse=True)
def _session_entitled_home(tmp_path_factory: pytest.TempPathFactory,
                           test_signer: TestSigner):
    """Session-wide layer UNDER the per-test fixtures: module/session-scoped
    fixtures build clients before any function fixture runs, so they too must
    see an isolated, entitled home and a patched key (never the real home,
    never the network)."""
    mp = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("session-home")
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("HERMES_HOME", str(home / ".hermes"))
    mp.delenv("SIBYL_MEMORY_DB", raising=False)
    patch_trusted_keys(mp, test_signer)
    forbid_external_network(mp)
    install_entitled_home(home, test_signer)
    yield home
    mp.undo()


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


@pytest.fixture(autouse=True)
def entitled_test_home(_isolate_home: Path, test_signer: TestSigner,
                       monkeypatch: pytest.MonkeyPatch) -> Path:
    """~/.sibyl-memory/{credentials,entitlement}.json signed by the test key."""
    patch_trusted_keys(monkeypatch, test_signer)
    return install_entitled_home(_isolate_home, test_signer)


@pytest.fixture
def entitle_as(entitled_test_home: Path, test_signer: TestSigner):
    """Re-mint the home entitlement with another tier (0.9.0: paid features key
    to the ENTITLEMENT tier, never the ``tier=`` hint alone; spec 3.1, 9.1
    item 9). ``entitle_as("lifetime")`` is what the server would issue after
    an upgrade."""
    from sibyl_memory_client.testing import write_entitlement

    def _entitle(tier: str) -> None:
        paid = tier != "free"
        write_entitlement(entitled_test_home, test_signer.mint(
            tier=tier, cap=None if paid else 5242880, src=tier))
    return _entitle


# ---- network guard (0.9.0 gate fix) -----------------------------------------
# A previous build's suite reached the production api by accident. The guard is
# installed at conftest import (so collection-time code is covered too), stays
# installed for the whole session (the session fixture above re-applies it), and
# EVERY test fails if it made a refused non-loopback connection or name lookup,
# even one it caught and ignored. tests/test_network_guard_2026_09_27.py proves
# the guard itself.
from sibyl_memory_client.testing import (  # noqa: E402
    assert_no_external_attempts as _assert_no_external_attempts,
    forbid_external_network as _forbid_external_network,
    refused_connections as _refused_connections,
)

_IMPORT_TIME_GUARD = pytest.MonkeyPatch()
_forbid_external_network(_IMPORT_TIME_GUARD)


@pytest.fixture(autouse=True)
def _no_external_network_attempts():
    start = len(_refused_connections())
    yield
    _assert_no_external_attempts(start)


# ---- real-home guard (save/debloat build 2026-09-27) --------------------------
# The skills installer and the lifecycle write under the home directory. Every
# test must see a temporary HOME (asserted per test), and at session end none
# of the paths this build writes may have changed under the account's real
# home (sibyl_memory_client.testing.REAL_HOME_WATCHED).
from sibyl_memory_client.testing import (  # noqa: E402
    assert_home_is_not_real as _assert_home_is_not_real,
    real_home_changes as _real_home_changes,
    real_home_snapshot as _real_home_snapshot,
)

_REAL_HOME_BEFORE = _real_home_snapshot()


@pytest.fixture(autouse=True)
def _home_is_never_the_real_home(_isolate_home):
    _assert_home_is_not_real()
    yield
    _assert_home_is_not_real()


def pytest_sessionfinish(session, exitstatus):
    changed = _real_home_changes(_REAL_HOME_BEFORE)
    if changed:
        print(f"\nREAL HOME CHANGED during the test session: {changed}")
        session.exitstatus = 1
