"""SQLite storage layer for sibyl-memory-client.

Opens a per-tenant local SQLite database, applies the canonical schema, and
exposes a connection helper plus low-level row IO. Thread-local connection
pool keeps things simple for v1; we revisit if/when concurrent agent
workloads emerge.

Design notes:
- WAL mode for concurrent reads + single writer (default for v1, matches
  the local-first single-agent workload).
- foreign_keys = ON enforced at connection time.
- Schema applied on first open; idempotent via CREATE IF NOT EXISTS.
- ISO 8601 UTC timestamps everywhere (`strftime('%Y-%m-%dT%H:%M:%fZ','now')`).
- All JSON validated at write time via sqlite json_valid() CHECK constraints.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .exceptions import SchemaError, StorageError

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Real #3 (0.4.19): crash-atomic FTS-rebuild marker, stored in PRAGMA
# user_version. It cannot live as a row in ``sibyl_memory_schema_version``:
# schema.sql unconditionally INSERTs versions 1/2/3 there via INSERT OR IGNORE
# BEFORE _migrate_if_needed runs, so a "version-3 row absent" signal is
# impossible. And ``count(*)`` on an external-content FTS5 table delegates to
# its content table (never diverges from the base count), while the FTS5
# 'integrity-check' command does not flag an index that has merely been emptied.
# PRAGMA user_version is a DB-header integer schema.sql never touches, it is
# transactional (rolls back with a failed rebuild), and reading it is O(1):
#   0                     -> FTS never verified/rebuilt under this client
#   _FTS_REBUILD_MARKER   -> the FTS index was (re)built AND its txn committed
_FTS_REBUILD_MARKER = 3

# v0.4.0 (2026-05-18, KAPPA RED finding): the SQLite DB holds every entity
# body, not just credentials. docs.sibyllabs.org/memory/install claims 0600
# but sqlite3.connect inherits the process umask (typically yields 0644).
# Tighten with explicit chmod after the schema apply guarantees the file
# exists. Idempotent: safe to call every time. Also tightens WAL + SHM
# sidecar files if they exist after the first transaction.
_DB_FILE_MODE = 0o600
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 millisecond-precision format.

    Matches the 3-digit precision of SQLite's ``strftime('%f')`` so that
    timestamps produced by Python and by SQL DEFAULTs sort identically in
    lexicographic comparisons.  Prior versions emitted 6-digit microseconds
    which broke cross-tier ``ORDER BY ts`` merges ('Z' > '3' at position 24).
    Fixed in 0.4.3."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def new_id() -> str:
    """Generate a fresh UUID v4 string for primary keys."""
    return str(uuid4())


def dumps(payload: Any) -> str:
    """Canonical JSON serialization for body / payload fields.
    sort_keys=False (preserve insertion order: matters for downstream diff).
    separators tight to keep DB rows compact."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def loads(blob: str | None) -> Any:
    """Inverse of dumps(). Returns None for None input (matches nullable
    JSONB column semantics).

    CORE-7 (2026-06-25 pre-launch audit): a corrupted stored row (truncated
    blob, partial write, manual DB edit) previously raised a raw
    ``json.JSONDecodeError`` out of the public read API (get_entity, search,
    read_events, ...). That is an undeclared exception type that crashes
    callers expecting only the typed SibylMemoryError hierarchy. Now a malformed
    blob raises a typed StorageError with the offending prefix elided (no
    content leak), chained to the original decode error for debugging."""
    if blob is None:
        return None
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, ValueError) as e:
        raise StorageError(
            "A stored memory row contains malformed JSON and could not be "
            "decoded.",
            recovery=(
                "The row was likely corrupted by a partial write or a manual "
                "edit. Run the memory linter to locate invalid-json rows, then "
                "repair or delete the offending row."
            ),
        ) from e


def db_size_bytes(db_path: str | Path) -> int:
    """Return the WAL-inclusive logical footprint of a SQLite database.

    CAP-1 (2026-06-25 pre-launch audit): the free-tier cap must account for data
    that has been committed but still lives in ``memory.db-wal`` (WAL journal
    mode is the default here). Sizing ``memory.db`` alone under-reports during
    write bursts, letting a user grow past the cap before the checkpoint folds
    the WAL back in.

    The authoritative measure is the SQLite *logical* size — ``page_count *
    page_size`` — read over a short-lived connection. ``page_count`` reflects
    every page the database logically holds, including committed pages still in
    the WAL, so it counts WAL-resident data WITHOUT the transient over-count a
    raw ``main + -wal`` file-byte sum produces (the WAL holds rewritten copies of
    existing pages during a burst, not purely net-new bytes). This is the same
    number ``Storage.logical_size_bytes`` reads inside a transaction, so the
    pre-write estimate and the in-transaction CAP-2 check agree.

    Falls back to the file-byte sum (main + -wal + -shm) if the logical read
    fails for any reason (locked DB, pre-open path, non-SQLite file) — a sum
    that is never an UNDER-count, which is the safe direction for a cap.
    """
    main = Path(db_path)
    if main.exists():
        try:
            conn = sqlite3.connect(str(main), timeout=1.0)
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                logical = int(page_count) * int(page_size)
                if logical > 0:
                    return logical
            finally:
                conn.close()
        except (sqlite3.Error, TypeError, IndexError, OSError):
            pass  # fall through to the file-sum lower-effort path
    total = 0
    for path in (main, main.with_name(main.name + "-wal"),
                 main.with_name(main.name + "-shm")):
        try:
            if path.exists():
                total += path.stat().st_size
        except OSError:
            pass
    return total


class Storage:
    """SQLite connection wrapper with schema bootstrap + transaction helpers."""

    def __init__(self, db_path: str | Path):
        raw = Path(db_path).expanduser()
        # SEC-12: reject a symlinked or hardlinked database file before opening.
        # Path.resolve() follows symlinks, and Path.is_symlink() is False for
        # hardlinks, so without this guard a symlinked path or a hardlinked
        # memory.db (st_nlink > 1) could redirect one profile's writes into
        # another profile's database at the SQLite layer (WAL checkpoints into
        # the shared inode on close). We check the final path component AS GIVEN
        # (pre-resolve) so a symlinked *parent* dir — a legitimate containerized
        # / relocated-home setup — is NOT rejected; only the db file itself.
        if raw.is_symlink():
            raise StorageError(
                "Refusing to open a symlinked database file.",
                recovery="Remove the symlink at the database path and point at a real file.",
            )
        if raw.exists():
            try:
                if raw.stat().st_nlink > 1:
                    raise StorageError(
                        "Refusing to open a hardlinked database file (shared inode).",
                        recovery="Use a database file that is not hardlinked to another file.",
                    )
            except OSError:
                pass
        self.db_path = raw.resolve()
        # Hardening #10: reject a symlinked or hardlinked WAL/SHM sidecar BEFORE
        # opening. WAL/SHM are opened by SQLite at the resolved db path + suffix;
        # a planted ``memory.db-wal`` symlink would redirect the write-ahead log
        # (which carries committed rows) into an attacker-chosen file, and the
        # later perms-tightening chmod would retarget through it. Guard the
        # sidecars with the same is_symlink() / st_nlink checks as the main file.
        self._reject_symlinked_sidecars()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Hardening #4b: ``mkdir(mode=0o700)`` is a no-op on an existing dir, so
        # the storage directory can persist at a loose (umask-derived) mode.
        # Tighten explicitly (best-effort; guarded for chmod-less platforms).
        if hasattr(os, "chmod"):
            try:
                os.chmod(self.db_path.parent, 0o700)
            except OSError:
                pass
        # Per-instance thread-local cache (avoids leaking connections across
        # Storage instances pointing at different files).
        self._tls = threading.local()
        # CORE-13 (2026-06-25 pre-launch audit): thread-local connections opened
        # by worker threads were never closed by close() (which only sees the
        # calling thread's TLS slot), leaking a file descriptor + WAL handle per
        # thread for the life of the process. Track every opened connection in a
        # registry guarded by a lock so close() can reap all of them.
        #
        # Real #2 (0.4.19): CORE-13 only reaped at shutdown, so a long-lived
        # Storage under Hermes (a fresh thread per turn) accumulated one open
        # connection PER dead thread → fd exhaustion (EMFILE) → silent write
        # loss mid-session. The registry now holds ``(weakref-to-owning-thread,
        # conn)`` and every new registration sweeps out connections whose owning
        # thread has exited, keeping the live-connection count bounded.
        self._conn_registry: list[tuple[weakref.ref, sqlite3.Connection]] = []
        self._registry_lock = threading.Lock()
        # Bootstrap schema on first open (idempotent)
        self._ensure_schema()
        # v0.4.0 (KAPPA RED finding): tighten file permissions on the main DB
        # file + WAL + SHM sidecars after the schema apply has created them.
        # Default umask leaves 0644 (world-readable); we want 0600 since the
        # DB contains every entity body. Idempotent + tolerant of missing
        # sidecars (WAL/SHM only exist after first write).
        self._tighten_db_file_perms()

    def _reject_symlinked_sidecars(self) -> None:
        """Hardening #10: refuse to open when a WAL/SHM sidecar is a symlink or
        hardlink. SQLite opens ``<db>-wal`` / ``<db>-shm`` at fixed paths beside
        the main file; a planted symlink there would divert the write-ahead log
        (which holds committed rows before checkpoint) to another file, and the
        perms-tightening chmod could retarget through it. Mirrors the main-file
        guard in __init__."""
        for suffix in _DB_SIDECAR_SUFFIXES:
            sidecar = self.db_path.with_name(self.db_path.name + suffix)
            if sidecar.is_symlink():
                raise StorageError(
                    "Refusing to open: a database WAL/SHM sidecar is a symlink.",
                    recovery="Remove the symlinked -wal/-shm sidecar beside the database file and retry.",
                )
            if sidecar.exists():
                try:
                    if sidecar.stat().st_nlink > 1:
                        raise StorageError(
                            "Refusing to open: a database WAL/SHM sidecar is hardlinked (shared inode).",
                            recovery="Remove the hardlinked -wal/-shm sidecar beside the database file and retry.",
                        )
                except OSError:
                    pass

    def _register_and_sweep(self, conn: sqlite3.Connection) -> None:
        """Register ``conn`` for the calling thread and reap connections whose
        owning thread has exited (Real #2).

        Each registry entry is ``(weakref-to-owning-thread, conn)``. A thread's
        SQLite connection lives in that thread's ``threading.local`` slot, which
        is freed when the thread exits — after which the ONLY reference to the
        (still-open) connection is this registry. On every new registration we
        drop and close entries whose owning thread is gone (weakref dead) OR
        finished (``is_alive()`` False), keeping the live-connection count
        bounded even when a caller (Hermes) spawns a fresh thread per turn.
        Closing happens OUTSIDE the lock (conn.close() can block on checkpoint).
        """
        me = weakref.ref(threading.current_thread())
        dead: list[sqlite3.Connection] = []
        with self._registry_lock:
            live: list[tuple[weakref.ref, sqlite3.Connection]] = []
            for thread_ref, existing in self._conn_registry:
                owner = thread_ref()
                if owner is None or not owner.is_alive():
                    dead.append(existing)
                else:
                    live.append((thread_ref, existing))
            live.append((me, conn))
            self._conn_registry = live
        for old in dead:
            try:
                old.close()
            except sqlite3.Error:
                pass

    @staticmethod
    def _conn_is_usable(conn: sqlite3.Connection) -> bool:
        """Cheap liveness probe for a cached TLS connection (Real #2).

        A CLOSED sqlite3 connection raises ``ProgrammingError`` even on plain
        attribute access, so reading ``total_changes`` (no SQL issued)
        distinguishes a live handle from one that another thread's close()
        already shut. Returns False for a poisoned handle so connection() can
        transparently reopen."""
        try:
            conn.total_changes  # noqa: B018  (attribute read is the probe)
            return True
        except sqlite3.Error:
            return False

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection. Callers should prefer connection() context
        manager for proper cleanup.

        SEC-3 hardening (v0.3.3): exception messages do not echo the absolute
        db path: the original exception is chained via `from e` for debugging,
        but the user-visible message stays generic."""
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,  # autocommit; we manage transactions explicitly
                check_same_thread=False,
                detect_types=0,
            )
        except sqlite3.Error as e:
            raise StorageError(
                f"Could not open the local SQLite database: {type(e).__name__}",
                recovery="Check disk space, file permissions, and that no other process holds an exclusive lock.",
            ) from e

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, faster than FULL
        conn.execute("PRAGMA busy_timeout = 5000")  # 5s before SQLITE_BUSY
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager that yields a per-instance, thread-local connection.
        Connection stays open across calls for performance; cleanup happens at
        Storage.close() or at process exit.

        SEC-3 hardening (v0.3.3): wraps sqlite3.Error in a sanitized
        StorageError without leaking db_path or query text."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None and not self._conn_is_usable(conn):
            # Real #2: another thread's close() (or an external close) may have
            # already shut this thread's cached connection. A closed sqlite3
            # handle raises on any use, so drop the poisoned TLS slot and
            # transparently reopen. This is what makes close() safe to call from
            # ANY thread without breaking sibling threads' cached connections.
            conn = None
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
            # CORE-13 + Real #2: register so close() can reap connections opened
            # by other threads (TLS only exposes the calling thread's slot), and
            # sweep out any whose owning thread has already exited.
            self._register_and_sweep(conn)
        try:
            yield conn
        except sqlite3.Error as e:
            raise StorageError(
                f"SQLite error: {type(e).__name__}",
                recovery="See exception cause for detail; consider checking schema version and disk health.",
            ) from e

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic transaction. Rolls back on exception, commits on clean exit.

        CORE-14 (2026-06-25 pre-launch audit):
          - ``BEGIN IMMEDIATE`` is now inside the try so a failure to acquire the
            write lock (SQLITE_BUSY after busy_timeout) propagates cleanly
            instead of escaping the rollback-aware block.
          - The ROLLBACK is wrapped in its own try/except. Previously, if the
            ROLLBACK itself raised (e.g. the connection is already in an aborted
            state), that secondary error MASKED the real exception that caused
            the rollback. Now the rollback failure is chained as __context__ but
            the original error is always the one re-raised, so the caller sees
            the true cause.

        Hardening #14 (0.4.19): the COMMIT itself was unguarded. A COMMIT that
        fails (disk full, I/O error, SQLITE_FULL) left the PERSISTENT per-thread
        connection mid-transaction, so the NEXT write on that thread raised
        "cannot start a transaction within a transaction" — one transient write
        error poisoned the connection for the rest of the session. The COMMIT is
        now wrapped: on failure we attempt a guarded ROLLBACK to return the conn
        to autocommit (chained as __context__), then re-raise the original COMMIT
        error so the caller still sees the true failure.
        """
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # Do not mask the original error with a rollback failure.
                    pass
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except Exception:
                    # Hardening #14: unpoison the persistent connection so a
                    # failed COMMIT does not brick every subsequent write on
                    # this thread. Re-raise the original COMMIT error.
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise

    def _ensure_schema(self) -> None:
        """Apply the canonical schema. Idempotent: safe to call on every open.

        After applying the schema, runs any pending migrations. v2 to v3 (2026-05-18)
        is the only migration currently: it reshapes FTS5 tables from standalone
        (body duplicated) to external-content (body lives in base tables only).
        Migration runs once and is idempotent thereafter."""
        if not _SCHEMA_PATH.exists():
            raise SchemaError(
                "Schema file missing from package install",
                recovery="The package install is corrupted. Reinstall sibyl-memory-client.",
            ) from None
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connection() as conn:
            try:
                conn.executescript(sql)
            except sqlite3.Error as e:
                raise SchemaError(
                    f"Failed to apply schema: {e}",
                    recovery="Check sqlite3 version (need 3.38+ for json_valid). On older systems, upgrade.",
                ) from e
        # Run migrations that need imperative work beyond CREATE IF NOT EXISTS.
        self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        """Run any pending schema migrations + guarantee the FTS index is built.

        Two concerns:

        1. **v2 → v3 reshape.** Examine ``entities_fts``'s declared SQL via
           sqlite_master. If it was created in the v2 standalone shape
           (``entity_id UNINDEXED``) we drop and rebuild every FTS5 table as
           external-content (+ contentless journal).

        2. **Real #3 (0.4.19) — crash-atomic rebuild.** The old migration ran as
           three SEPARATELY-committed steps (drop / re-create / rebuild) with no
           marker. A crash after the DROP committed but before the rebuild
           committed left a v3-SHAPED but EMPTY index; the shape check then read
           it as "already migrated" and search returned nothing FOREVER. We now
           stamp the crash-atomic marker (``PRAGMA user_version`` =
           ``_FTS_REBUILD_MARKER``) in the SAME transaction as the rebuild, so
           the marker exists iff the rebuild committed. On open, a store in v3
           shape whose marker is unset — a crashed migration, OR a healthy DB
           first opened under 0.4.19 — is rebuilt from the intact base tables
           before use. (A rebuild of an already-correct index is idempotent, so
           the one-time rebuild on upgrade is safe; ``count(*)`` on external
           content can't detect the emptied case and neither can
           'integrity-check', which is why the marker is the sole signal.)

        Safe to call repeatedly: once the marker is set and the shape is v3,
        this short-circuits with no scan and no rebuild.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entities_fts'"
            ).fetchone()
            if row is None:
                # entities_fts absent entirely. schema.sql (just applied) creates
                # it, so this is an unexpected state with nothing safe to rebuild.
                return
            sql = (row["sql"] or "").lower()
            needs_v3_shape = (
                "entity_id" in sql
                or "content='entities'" not in sql.replace(" ", "")
            )
            marker = self._fts_marker(conn)

        if not needs_v3_shape and marker >= _FTS_REBUILD_MARKER:
            # v3 external-content shape AND the index was rebuilt + committed
            # under this client. Fast path: no scan, no rebuild.
            return

        try:
            if needs_v3_shape:
                # v2 → v3: drop standalone FTS5 + triggers, then re-create in
                # external-content shape via schema.sql (CREATE IF NOT EXISTS
                # picks up the dropped tables).
                with self.transaction() as conn:
                    conn.execute("DROP TRIGGER IF EXISTS entities_ai_fts")
                    conn.execute("DROP TRIGGER IF EXISTS entities_ad_fts")
                    conn.execute("DROP TRIGGER IF EXISTS entities_au_fts")
                    conn.execute("DROP TABLE IF EXISTS entities_fts")
                    conn.execute("DROP TRIGGER IF EXISTS reference_ai_fts")
                    conn.execute("DROP TRIGGER IF EXISTS reference_ad_fts")
                    conn.execute("DROP TRIGGER IF EXISTS reference_au_fts")
                    conn.execute("DROP TABLE IF EXISTS reference_documents_fts")
                sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
                with self.connection() as conn:
                    conn.executescript(sql_text)
            # Rebuild the FTS indexes from the (intact) base tables and stamp the
            # crash-atomic marker in ONE transaction. If we crash here, the whole
            # transaction — marker included — rolls back, and the next open
            # rebuilds again (the exact failure Real #3 fixes).
            with self.transaction() as conn:
                self._rebuild_fts_indexes(conn)
                conn.execute(f"PRAGMA user_version = {int(_FTS_REBUILD_MARKER)}")
        except (sqlite3.Error, StorageError, SchemaError) as e:
            raise SchemaError(
                f"FTS5 index migration/rebuild failed: {type(e).__name__}",
                recovery="Back up your memory.db, then delete it; the next open will create a fresh v3 DB. Your base-table data is unaffected by an FTS index rebuild failure: the index rebuilds on the next open.",
            ) from e

    def _rebuild_fts_indexes(self, conn: sqlite3.Connection) -> None:
        """Repopulate every FTS5 index from its base table. Idempotent.

        External-content tables (entities / state_documents / reference_
        documents) use the FTS5 ``'rebuild'`` command. journal_events_fts is a
        STANDALONE FTS5 table (4 JSON columns concatenated into one searchable
        payload, no single content column to rebuild against), so 'rebuild' /
        'delete-all' are unavailable — it is cleared with a plain ``DELETE`` then
        backfilled from journal_events. Clearing first keeps the backfill
        duplicate-free when this runs against an already-populated index (the
        rebuild-on-upgrade path)."""
        conn.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO state_documents_fts(state_documents_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO reference_documents_fts(reference_documents_fts) VALUES('rebuild')")
        conn.execute("DELETE FROM journal_events_fts")
        conn.execute(
            """
            INSERT INTO journal_events_fts(rowid, ts, payload, tenant_id, event_id)
            SELECT rowid, ts,
                   COALESCE(evaluated,'') || ' ' || COALESCE(acted,'') || ' ' ||
                   COALESCE(forward,'') || ' ' || COALESCE(extra,''),
                   tenant_id, id
              FROM journal_events
            """
        )

    @staticmethod
    def _fts_marker(conn: sqlite3.Connection) -> int:
        """Read the crash-atomic FTS-rebuild marker (PRAGMA user_version).
        Returns 0 (never rebuilt) on any read failure."""
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.Error, TypeError, IndexError):
            return 0

    def schema_version(self) -> int | None:
        """Return current schema version, or None if uninitialized."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM sibyl_memory_schema_version"
            ).fetchone()
            return row["v"] if row else None

    def _tighten_db_file_perms(self) -> None:
        """Set memory.db (and WAL/SHM sidecars if present) to mode 0600.

        Idempotent. Safe on systems where chmod is a no-op (Windows): we
        guard with hasattr. Errors during chmod are non-fatal: we want
        secure-by-default but won't block a working DB if the chmod call
        races a concurrent process or hits a read-only mount edge case.
        """
        if not hasattr(os, "chmod"):
            return  # platform without POSIX chmod (Windows)
        targets = [self.db_path]
        for suffix in _DB_SIDECAR_SUFFIXES:
            sidecar = self.db_path.with_name(self.db_path.name + suffix)
            if sidecar.exists():
                targets.append(sidecar)
        # Hardening #10: prefer an lchmod-style call that does NOT follow
        # symlinks where the platform supports it. On Linux, os.chmod is not in
        # os.supports_follow_symlinks (the kernel forbids chmod-ing a symlink),
        # so we fall back to a plain chmod AFTER an explicit is_symlink() skip —
        # never retargeting a symlink's chmod onto its victim.
        follow_supported = os.chmod in getattr(os, "supports_follow_symlinks", set())
        for path in targets:
            try:
                if path.is_symlink():
                    # Never chmod THROUGH a symlinked sidecar (a symlinked
                    # sidecar is already rejected at open; this is belt-and-
                    # suspenders for one planted between open and this call).
                    continue
                if follow_supported:
                    os.chmod(path, _DB_FILE_MODE, follow_symlinks=False)
                else:
                    os.chmod(path, _DB_FILE_MODE)
            except OSError:
                # Non-fatal: log nothing, defer to caller noticing if perms
                # are truly broken (write operations will fail downstream).
                pass

    @staticmethod
    def logical_size_bytes(conn: sqlite3.Connection) -> int:
        """Return the logical DB size (page_count * page_size) on a connection.

        CAP-2 (2026-06-25 pre-launch audit): inside an open transaction this
        already reflects the pages the pending INSERT/UPDATE will occupy, so it
        is the reliable "true size immediately before commit" signal that a raw
        file ``stat`` cannot give mid-transaction (WAL has not folded back yet).
        Used by the write paths to gate on the ABSOLUTE resulting footprint
        rather than a pre-write byte estimate.
        """
        try:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            return int(page_count) * int(page_size)
        except (sqlite3.Error, TypeError, IndexError):
            return 0

    def count_rows(self, table: str, tenant_id: str) -> int:
        """Return COUNT(*) for a tenant in one of the canonical tables.

        CORE-6/MH-3 (2026-06-25 pre-launch audit): multi_record_search needs a
        corpus size for IDF weighting. It previously did
        ``len(list_entities(limit=100000))`` — a full materialization of every
        entity row (and a JSON decode of each body) just to count them. This is a
        cheap COUNT(*) that touches no bodies. ``table`` is matched against a
        fixed allowlist (never user input) so the interpolation is injection-safe;
        the tenant value is parameterized.
        """
        allowed = {
            "entities", "state_documents", "journal_events",
            "reference_documents", "archived_entities",
        }
        if table not in allowed:
            raise StorageError(
                f"count_rows: unknown table {table!r}",
                recovery="Pass one of the canonical memory tables.",
            )
        with self.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        """Close all tracked connections (mainly for tests / shutdown).

        CORE-13 (2026-06-25 pre-launch audit): previously only the calling
        thread's TLS connection was closed, leaking every connection opened by
        a worker thread. Now reap the full registry so no fd / WAL handle is
        left open at shutdown.

        Real #2 (0.4.19): the registry now holds ``(weakref, conn)`` tuples.
        close() is safe to call from any thread — it closes every registered
        connection (including other threads') and clears only its OWN TLS slot;
        sibling threads detect their now-closed handle via the liveness probe in
        connection() and transparently reopen, so no thread is left poisoned.
        """
        with self._registry_lock:
            registry = list(self._conn_registry)
            self._conn_registry.clear()
        for _thread_ref, conn in registry:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # Drop the calling thread's TLS slot so a later connection() reopens.
        # Other threads' TLS slots cannot be reached from here (threading.local
        # exposes only the calling thread's slot); they self-heal via the
        # connection() liveness probe.
        if getattr(self._tls, "conn", None) is not None:
            self._tls.conn = None
