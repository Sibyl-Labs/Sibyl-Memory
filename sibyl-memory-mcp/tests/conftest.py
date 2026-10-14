"""Shared fixtures for the sibyl-memory-mcp suite (0.3.0 account gate, spec 4.4).

The account gate lives in sibyl-memory-client and reads
``~/.sibyl-memory/credentials.json`` + ``entitlement.json``. Every test gets an
isolated fake home holding a valid, test-signed entitlement: a throwaway
Ed25519 key is monkeypatched into ``sibyl_memory_client._entitlement.TRUSTED_KEYS``
(the production pinned key is untouched), refreshes are routed to that test
signer, and non-loopback sockets are refused, so no test can reach a live
service. The server module resolves its default paths at import time, so they
are re-pointed at the fake home per test and the cached client is reset.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_memory_client.testing import (
    TestSigner,
    forbid_external_network,
    install_entitled_home,
    patch_trusted_keys,
)


@pytest.fixture(scope="session")
def test_signer() -> TestSigner:
    return TestSigner()


@pytest.fixture(scope="session", autouse=True)
def _session_entitled_home(tmp_path_factory: pytest.TempPathFactory, test_signer: TestSigner):
    mp = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("session-home")
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("HERMES_HOME", str(home / ".hermes"))
    mp.delenv("SIBYL_MEMORY_DB", raising=False)
    mp.delenv("SIBYL_CREDENTIALS", raising=False)
    patch_trusted_keys(mp, test_signer)
    forbid_external_network(mp)
    install_entitled_home(home, test_signer)
    yield home
    mp.undo()


@pytest.fixture(autouse=True)
def entitled_test_home(tmp_path_factory: pytest.TempPathFactory, test_signer: TestSigner,
                       monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("SIBYL_MEMORY_DB", raising=False)
    patch_trusted_keys(monkeypatch, test_signer)
    d = install_entitled_home(home, test_signer)
    import sibyl_memory_mcp.server as server
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", d / "memory.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", d / "credentials.json")
    for key, val in (("client", None), ("creds_mtime", None),
                     ("creds_path_exists", False), ("ent_mtime", None)):
        monkeypatch.setitem(server._client_cache, key, val)
    return d


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
# Every test must see a temporary HOME (asserted per test), and at session end
# none of the paths the save/debloat build writes (skills, lifecycle, hooks,
# shims) may have changed under the account's real home.
from sibyl_memory_client.testing import (  # noqa: E402
    assert_home_is_not_real as _assert_home_is_not_real,
    real_home_changes as _real_home_changes,
    real_home_snapshot as _real_home_snapshot,
)

_REAL_HOME_BEFORE = _real_home_snapshot()


@pytest.fixture(autouse=True)
def _home_is_never_the_real_home(entitled_test_home):
    _assert_home_is_not_real()
    yield
    _assert_home_is_not_real()


def pytest_sessionfinish(session, exitstatus):
    changed = _real_home_changes(_REAL_HOME_BEFORE)
    if changed:
        print(f"\nREAL HOME CHANGED during the test session: {changed}")
        session.exitstatus = 1
