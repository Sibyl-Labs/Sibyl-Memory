"""Regression tests for the 2026-07-05 super-patch build units C1 + C2.

Each test is tagged with its finding ID from
memory/research/plugin-hardening-superpatch-plan-2026-07-05.md (§4 Units C1/C2)
and PROVES the new behavior.

C1 — src/sibyl_memory_client/_capcheck.py:
  Real #5   TierCache.store fixed-name .tmp race → unique mkstemp + graceful degrade
  Hard #3   TierCache symlink guard was dead code → refuse symlinked cache path
  Hard #4a  cache dir mode not umask-enforced → chmod 0o700 after mkdir
  Hard #13  _refresh_and_check_total monkey-patched db_size_fn → explicit arg

C2 — src/sibyl_memory_client/storage.py:
  Real #2   per-thread conn registry leak → weakref sweep + close() safe cross-thread
  Real #3   FTS v2→v3 migration not crash-atomic → marker + rebuild-on-open
  Hard #4b  storage dir mode not umask-enforced → chmod 0o700 after mkdir
  Hard #10  WAL/SHM sidecars not symlink-guarded → refuse symlinked sidecar
  Hard #14  failed COMMIT poisons the persistent conn → guarded rollback
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapGate,
    MemoryClient,
    Storage,
    StorageError,
    TierCache,
    TierCacheEntry,
)
from sibyl_memory_client import storage as storage_mod


# ======================================================================
# C1 · Real #5 — TierCache.store: unique temp name, no cross-writer race,
#                and a persist failure degrades instead of failing the write
# ======================================================================

def test_real5_concurrent_store_never_races(tmp_path: Path) -> None:
    """Many threads storing to the SAME cache file concurrently must never
    raise (the old fixed-name <name>.tmp let writers unlink each other's temp
    and crash os.replace) and must never leave a partial/torn cache or stray
    temp file behind."""
    cache = TierCache(tmp_path / "tc.json")
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(25):
                cache.store(TierCacheEntry(
                    account_id=f"acc-{i}", tier="free",
                    checked_at=time.time(), cap_bytes=2_000_000,
                ))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # The cache is complete, valid JSON (atomic rename → never a torn write).
    assert cache.load() is not None
    # No stray mkstemp temp files left in the directory.
    leftover = [p for p in tmp_path.glob("tc.json.*")]
    assert leftover == [], leftover


def test_real5_store_oserror_degrades_the_write(tmp_path: Path) -> None:
    """A cache-persist OSError (disk full, perms, lost rename race) must be
    swallowed at the CapGate call site so the caller's memory write succeeds
    rather than blowing up. The authoritative server decision already applied."""
    class BoomCache(TierCache):
        def store(self, entry: TierCacheEntry) -> None:
            raise OSError("simulated disk full")

    def server(url, payload, timeout=4.0):
        return {"ok": True, "tier": "sync", "cap_bytes": None}

    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 1000,
        local_tier_hint="sync",          # paid hint → forces the server refresh path
        cache=BoomCache(tmp_path / "tc.json"),
        check_fn=server,
    )
    # Must NOT raise despite cache.store() raising OSError internally.
    gate.check(proposed_delta_bytes=100)


# ======================================================================
# C1 · Hardening #3 — a symlinked cache path is refused on load AND store
# ======================================================================

def test_hardening3_symlinked_cache_refused_on_load_and_store(tmp_path: Path) -> None:
    real_target = tmp_path / "real_cache.json"
    TierCache(real_target).store(TierCacheEntry(
        account_id="acc-x", tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    link = tmp_path / "link.json"
    os.symlink(real_target, link)

    cache = TierCache(link)
    # load() refuses the symlink (returns None, NOT the target's contents).
    assert cache.load() is None

    # store() refuses too: it never writes THROUGH the link.
    before = real_target.read_bytes()
    cache.store(TierCacheEntry(
        account_id="acc-y", tier="free", checked_at=time.time(), cap_bytes=123,
    ))
    assert real_target.read_bytes() == before      # target untouched
    assert link.is_symlink()                        # link not replaced by a real file


# ======================================================================
# C1 · Hardening #4a — a pre-existing loose cache dir is tightened to 0o700
# ======================================================================

def test_hardening4a_cache_dir_tightened_to_700(tmp_path: Path) -> None:
    d = tmp_path / "loose-cache-dir"
    d.mkdir()
    os.chmod(d, 0o755)  # loose despite mkdir(mode=)
    assert oct(d.stat().st_mode)[-3:] == "755"

    TierCache(d / "tc.json")
    assert oct(d.stat().st_mode)[-3:] == "700"


# ======================================================================
# C1 · Hardening #13 — check_total passes an explicit total, never swaps db_size_fn
# ======================================================================

def test_hardening13_absolute_total_passed_not_swapped(tmp_path: Path) -> None:
    """The absolute-footprint recheck must feed the total to _refresh_and_check
    as an argument, not by mutating self._db_size_fn. Prove the configured
    db_size_fn sentinel is NEVER consulted and the object is never swapped."""
    sizes_seen: list[int] = []

    def server(url, payload, timeout=4.0):
        sizes_seen.append(payload["current_size_bytes"])
        return {"ok": True, "tier": "free", "cap_bytes": None}

    sentinel = lambda: 999_999  # must never be called on the absolute-total path
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=sentinel,
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),
        check_fn=server,
        cap_bytes=1000,
    )
    original = gate._db_size_fn

    gate.check_total(5000)  # 5000 > 1000 cap → routes through _refresh_and_check_total

    assert gate._db_size_fn is original        # never swapped (thread-unsafe pattern gone)
    assert sizes_seen == [5000]                # used the passed total...
    assert 999_999 not in sizes_seen           # ...never the db_size_fn sentinel


def test_hardening13_concurrent_check_total_thread_safe(tmp_path: Path) -> None:
    """Two+ threads calling check_total with different totals must never crash
    or leave a residual patched db_size_fn (the old swap-and-restore could cross
    fns between threads)."""
    def server(url, payload, timeout=4.0):
        return {"ok": True, "tier": "sync", "cap_bytes": None}

    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 0,
        local_tier_hint="sync",
        cache=TierCache(tmp_path / "tc.json"),
        check_fn=server,
        cap_bytes=1000,
    )
    original = gate._db_size_fn
    errors: list[BaseException] = []

    def worker(total: int) -> None:
        try:
            for _ in range(100):
                gate.check_total(total)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in (2000, 3000, 4000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert gate._db_size_fn is original  # no residual patched db_size_fn


# ======================================================================
# C2 · Real #2 — dead-thread connections are reaped; close() is cross-thread safe
# ======================================================================

def test_real2_dead_thread_connections_are_reaped(tmp_path: Path) -> None:
    """Hermes opens a fresh thread per turn. The connection registry must prune
    connections whose owning thread has exited, so the live-connection count
    stays BOUNDED instead of growing ~1 per (dead) worker thread."""
    storage = Storage(str(tmp_path / "memory.db"))

    def worker(i: int) -> None:
        with storage.transaction() as conn:
            conn.execute(
                "INSERT INTO entities (id,tenant_id,category,name,body) "
                "VALUES (?,?,?,?,?)",
                (f"id{i}", "qa", "c", f"n{i}", "{}"),
            )

    N = 40
    for i in range(N):
        t = threading.Thread(target=worker, args=(i,))
        t.start()
        t.join()

    # Force one more registration from a fresh thread to trigger a sweep (the
    # main thread already holds a conn from construction, so it won't re-register).
    def sweeper() -> None:
        with storage.connection() as conn:
            conn.execute("SELECT 1")

    st = threading.Thread(target=sweeper)
    st.start()
    st.join()

    with storage._registry_lock:
        remaining = len(storage._conn_registry)
    # Bounded (main-thread conn + a little slack), NOT ~40.
    assert remaining < 10, remaining
    storage.close()


def test_real2_close_from_other_thread_does_not_poison_tls(tmp_path: Path) -> None:
    """close() called from the main thread closes a worker's registered conn.
    The worker must transparently reopen on its next op instead of using the
    poisoned (closed) handle cached in its TLS."""
    storage = Storage(str(tmp_path / "memory.db"))
    opened = threading.Event()
    proceed = threading.Event()
    result: dict[str, object] = {}

    def worker() -> None:
        with storage.connection() as conn:  # caches a conn in this thread's TLS
            conn.execute("SELECT 1")
        opened.set()
        proceed.wait(5)
        try:
            with storage.connection() as conn:  # must reopen after cross-thread close()
                conn.execute("SELECT 1")
            result["ok"] = True
        except BaseException as e:  # noqa: BLE001
            result["ok"] = False
            result["err"] = repr(e)

    t = threading.Thread(target=worker)
    t.start()
    assert opened.wait(5)
    storage.close()      # closes the worker's registered conn from the main thread
    proceed.set()
    t.join()

    assert result.get("ok") is True, result
    storage.close()


# ======================================================================
# C2 · Real #3 — a crashed FTS migration is detected on open and rebuilt
# ======================================================================

def test_real3_fresh_open_stamps_rebuild_marker(tmp_path: Path) -> None:
    """A fresh DB stamps the crash-atomic FTS marker (PRAGMA user_version) so
    subsequent opens take the fast path."""
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa")
    c.set_entity("notes", "seed", {"text": "hello"})
    with c._storage.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    c._storage.close()


def test_real3_crashed_migration_rebuilds_fts_on_open(tmp_path: Path) -> None:
    """Simulate a crash mid v2→v3 migration: the FTS index is emptied and the
    marker never written, while the base tables stay intact. The next open must
    detect the unset marker on a non-empty store and rebuild the FTS so search
    returns rows again (the old shape-check treated the empty v3 index as
    'already migrated' → search stayed permanently empty)."""
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa")
    c.set_entity("notes", "findme", {"text": "unique_zebra_token_xyz"})
    c.write_event(acted=["did unique_journal_thing_qpr"])
    assert c.search("unique_zebra_token_xyz"), "sanity: searchable before the crash"
    c._storage.close()

    # Crash simulation: empty the FTS index + reset the marker, base data intact.
    raw = sqlite3.connect(str(db))
    raw.execute("INSERT INTO entities_fts(entities_fts) VALUES('delete-all')")
    raw.execute("DELETE FROM journal_events_fts")
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    # Prove the index is genuinely empty (search would return nothing now).
    assert raw.execute(
        "SELECT count(*) FROM entities_fts WHERE entities_fts MATCH 'unique_zebra_token_xyz'"
    ).fetchone()[0] == 0
    raw.close()

    # Reopen → _migrate_if_needed rebuilds the FTS from the base tables.
    c2 = MemoryClient.local(db, tenant_id="qa")
    assert c2.search("unique_zebra_token_xyz"), "entity FTS was not rebuilt after crash"
    assert c2.search("unique_journal_thing_qpr"), "journal FTS was not rebuilt after crash"
    with c2._storage.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3  # marker restamped
    c2._storage.close()


# ======================================================================
# C2 · Hardening #4b — a pre-existing loose storage dir is tightened to 0o700
# ======================================================================

def test_hardening4b_storage_dir_tightened_to_700(tmp_path: Path) -> None:
    d = tmp_path / "loose-store-dir"
    d.mkdir()
    os.chmod(d, 0o755)
    assert oct(d.stat().st_mode)[-3:] == "755"

    Storage(str(d / "memory.db"))
    assert oct(d.stat().st_mode)[-3:] == "700"


# ======================================================================
# C2 · Hardening #10 — a symlinked WAL/SHM sidecar is refused at open
# ======================================================================

def test_hardening10_symlinked_wal_sidecar_refused(tmp_path: Path) -> None:
    outside = tmp_path / "victim-wal.txt"
    outside.write_text("sensitive")
    db = tmp_path / "memory.db"
    os.symlink(outside, db.with_name(db.name + "-wal"))
    with pytest.raises(StorageError):
        Storage(str(db))
    # The symlink target was not chmod-retargeted (open refused before any chmod).
    assert outside.read_text() == "sensitive"


def test_hardening10_symlinked_shm_sidecar_refused(tmp_path: Path) -> None:
    outside = tmp_path / "victim-shm.txt"
    outside.write_text("sensitive")
    db = tmp_path / "memory.db"
    os.symlink(outside, db.with_name(db.name + "-shm"))
    with pytest.raises(StorageError):
        Storage(str(db))


def test_hardening10_tighten_perms_skips_symlinked_sidecar(tmp_path: Path) -> None:
    """_tighten_db_file_perms must NEVER chmod through a symlinked sidecar (a
    symlink planted between open and the perms pass must not retarget a victim
    file's mode)."""
    db = tmp_path / "memory.db"
    storage = Storage(str(db))  # opens cleanly (no sidecar symlink yet)

    victim = tmp_path / "victim.txt"
    victim.write_text("x")
    os.chmod(victim, 0o644)

    # Point a -wal symlink at the victim (remove any real -wal first).
    wal = db.with_name(db.name + "-wal")
    if wal.exists() or wal.is_symlink():
        wal.unlink()
    os.symlink(victim, wal)

    storage._tighten_db_file_perms()  # must skip the symlinked sidecar
    assert oct(victim.stat().st_mode)[-3:] == "644", "chmod retargeted through a symlink"
    storage.close()


# ======================================================================
# C2 · Hardening #14 — a failed COMMIT does not poison the persistent conn
# ======================================================================

def test_hardening14_failed_commit_does_not_poison_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A COMMIT that fails (disk full / I/O error) must leave the persistent
    per-thread connection usable: the guarded ROLLBACK returns it to autocommit
    so the NEXT write on the same thread succeeds instead of raising 'cannot
    start a transaction within a transaction'."""
    fail = {"commit": False}

    class FlakyConn(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if (fail["commit"] and isinstance(sql, str)
                    and sql.strip().upper().startswith("COMMIT")):
                fail["commit"] = False  # fail exactly once
                raise sqlite3.OperationalError("simulated disk-full on COMMIT")
            return super().execute(sql, *args, **kwargs)

    real_connect = sqlite3.connect

    def fake_connect(*a, **k):
        k["factory"] = FlakyConn
        return real_connect(*a, **k)

    monkeypatch.setattr(storage_mod.sqlite3, "connect", fake_connect)
    # Build with commits WORKING (schema apply must succeed), then arm the
    # one-shot COMMIT failure for the first user transaction.
    storage = storage_mod.Storage(str(tmp_path / "memory.db"))
    fail["commit"] = True

    # The COMMIT failure surfaces as StorageError (connection() wraps the
    # re-raised OperationalError, whose cause chain still carries the original).
    with pytest.raises(StorageError):
        with storage.transaction() as conn:
            conn.execute(
                "INSERT INTO entities (id,tenant_id,category,name,body) "
                "VALUES ('a','qa','c','n1','{}')"
            )

    # The connection is NOT poisoned: the next write on the same thread commits.
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO entities (id,tenant_id,category,name,body) "
            "VALUES ('b','qa','c','n2','{}')"
        )

    with storage.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE tenant_id='qa'"
        ).fetchone()["n"]
    assert n == 1  # only the second write survived; the first rolled back
    storage.close()
