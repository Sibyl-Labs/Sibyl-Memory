"""Regression tests for the 2026-06-02 bundled UserSignal/beta fixes (v0.4.7).

Covers:
  - SEC-13: forged null-account uncapped tier cache must not bypass the cap.
  - SEC-12: symlinked / hardlinked DB files are refused; a symlinked PARENT
    directory (legit relocated home) is still allowed.
  - Search quality: the journal tier cannot dominate cross-tier search.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    MemoryClient,
    Storage,
    TierCache,
    TierCacheEntry,
)
from sibyl_memory_client.exceptions import StorageError

from test_capcheck import FakeServer  # reuse the fake /check-write transport


# ----------------------------------------------------------------------
# SEC-13 — forged null-account uncapped cache cannot bypass the cap
# ----------------------------------------------------------------------

def test_forged_null_account_uncapped_cache_does_not_bypass(tmp_path: Path) -> None:
    """A pre-activation user (account_id=None) writes a forged tier_cache.json
    with account_id:null + cap_bytes:null. Pre-fix this matched the fast-path and
    returned 'uncapped'. Now it must be distrusted and the server consulted."""
    server = FakeServer(tier="free")  # would block if (correctly) consulted
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id=None, tier="free", checked_at=time.time(), cap_bytes=None,
    ))
    gate = CapGate(
        account_id=None,              # pre-activation / free user
        session_token=None,
        db_size_fn=lambda: 50 * 1024 * 1024,  # way past the free cap
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    # Pre-fix: the null+null cache hit the uncapped fast-path and returned with
    # NO exception (write allowed = bypass). Post-fix: the forged cache is
    # distrusted, the path falls through, and the cap is enforced -> raises.
    # (A no-account user is enforced locally, so the server may not be consulted;
    # the raise itself is the proof the bypass is closed.)
    with pytest.raises(CapExceededError):
        gate.check(proposed_delta_bytes=10_000)


def test_legit_paid_uncapped_cache_still_skips_server(tmp_path: Path) -> None:
    """The fix must NOT regress a real paid account: a fresh uncapped cache with
    a real account_id still short-circuits without a server call."""
    server = FakeServer(tier="free")  # would say no if called
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 100 * 1024 * 1024,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=10_000)
    assert len(server.calls) == 0


# ----------------------------------------------------------------------
# SEC-12 — DB-path link guard (symlink + hardlink), parent-symlink allowed
# ----------------------------------------------------------------------

def test_storage_rejects_symlinked_db(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    Storage(str(real))  # create a real DB file
    link = tmp_path / "link.db"
    link.symlink_to(real)
    with pytest.raises(StorageError):
        Storage(str(link))


def test_storage_rejects_hardlinked_db(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    Storage(str(real))  # create a real DB file (st_nlink=1)
    hard = tmp_path / "hard.db"
    os.link(str(real), str(hard))  # st_nlink -> 2 on both
    with pytest.raises(StorageError):
        Storage(str(hard))


def test_storage_allows_symlinked_parent_dir(tmp_path: Path) -> None:
    """A symlinked PARENT directory (relocated / containerized home) must still
    work — only the db file itself is guarded, not its parents."""
    realdir = tmp_path / "realhome"
    realdir.mkdir()
    linkdir = tmp_path / "homelink"
    linkdir.symlink_to(realdir, target_is_directory=True)
    s = Storage(str(linkdir / "memory.db"))  # must NOT raise
    assert s is not None


# ----------------------------------------------------------------------
# Search quality — journal cannot drown out structured tiers
# ----------------------------------------------------------------------

def test_journal_does_not_dominate_search(tmp_path: Path) -> None:
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    for i in range(5):
        c.set_entity("projects", f"proj-{i}", {"note": "budget planning decision"})
    for i in range(20):
        c.write_event(acted=[f"budget planning decision iteration {i}"])
    res = c.search("budget", limit=8)
    journal_hits = [h for h in res if h["tier"] == "journal"]
    # journal capped at limit // 4 == 2
    assert len(journal_hits) <= 2
    # real entities still surface (were being buried pre-fix)
    assert any(h["tier"] == "entity" for h in res)
