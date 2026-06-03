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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .exceptions import SchemaError, StorageError

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

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
    JSONB column semantics)."""
    if blob is None:
        return None
    return json.loads(blob)


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
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Per-instance thread-local cache (avoids leaking connections across
        # Storage instances pointing at different files).
        self._tls = threading.local()
        # Bootstrap schema on first open (idempotent)
        self._ensure_schema()
        # v0.4.0 (KAPPA RED finding): tighten file permissions on the main DB
        # file + WAL + SHM sidecars after the schema apply has created them.
        # Default umask leaves 0644 (world-readable); we want 0600 since the
        # DB contains every entity body. Idempotent + tolerant of missing
        # sidecars (WAL/SHM only exist after first write).
        self._tighten_db_file_perms()

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
        if conn is None:
            conn = self._connect()
            self._tls.conn = conn
        try:
            yield conn
        except sqlite3.Error as e:
            raise StorageError(
                f"SQLite error: {type(e).__name__}",
                recovery="See exception cause for detail; consider checking schema version and disk health.",
            ) from e

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic transaction. Rolls back on exception, commits on clean exit."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

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
        """Run any pending schema migrations.

        Detection: examine `entities_fts`'s declared SQL via sqlite_master. If
        the table was created in the v2 standalone shape (`entity_id UNINDEXED`)
        we need to drop and rebuild it as external-content. The migration also
        backfills state_documents_fts + journal_events_fts + the new
        reference_documents_fts shape for existing v2 databases.

        Safe to call repeatedly: operations short-circuit once v3 is in place.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entities_fts'"
            ).fetchone()
            if row is None:
                # Fresh DB. Schema.sql already created v3 shape correctly.
                return
            sql = (row["sql"] or "").lower()
            needs_v3 = "entity_id" in sql or "content='entities'" not in sql.replace(" ", "")
            if not needs_v3:
                # Already v3 shape: nothing to do.
                return

        # v2 → v3: drop standalone FTS5 + triggers, re-create in external-content
        # shape, rebuild from base table data. The CREATE statements in
        # schema.sql will pick up after the DROP because they're CREATE IF
        # NOT EXISTS.
        try:
            with self.transaction() as conn:
                # Drop old FTS5 tables + their triggers
                conn.execute("DROP TRIGGER IF EXISTS entities_ai_fts")
                conn.execute("DROP TRIGGER IF EXISTS entities_ad_fts")
                conn.execute("DROP TRIGGER IF EXISTS entities_au_fts")
                conn.execute("DROP TABLE IF EXISTS entities_fts")
                conn.execute("DROP TRIGGER IF EXISTS reference_ai_fts")
                conn.execute("DROP TRIGGER IF EXISTS reference_ad_fts")
                conn.execute("DROP TRIGGER IF EXISTS reference_au_fts")
                conn.execute("DROP TABLE IF EXISTS reference_documents_fts")
            # Re-run schema.sql so the v3 external-content tables + triggers
            # land (CREATE IF NOT EXISTS picks up the dropped tables).
            sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
            with self.connection() as conn:
                conn.executescript(sql_text)
            # Rebuild FTS5 indexes from base tables for any pre-existing data.
            with self.transaction() as conn:
                conn.execute("INSERT INTO entities_fts(entities_fts) VALUES('rebuild')")
                conn.execute("INSERT INTO state_documents_fts(state_documents_fts) VALUES('rebuild')")
                conn.execute("INSERT INTO reference_documents_fts(reference_documents_fts) VALUES('rebuild')")
                # journal_events_fts is contentless: can't 'rebuild' from
                # outside. Backfill manually for any existing journal rows.
                conn.execute(
                    """
                    INSERT INTO journal_events_fts(rowid, ts, payload, tenant_id)
                    SELECT rowid, ts,
                           COALESCE(evaluated,'') || ' ' || COALESCE(acted,'') || ' ' ||
                           COALESCE(forward,'') || ' ' || COALESCE(extra,''),
                           tenant_id
                      FROM journal_events
                    """
                )
        except sqlite3.Error as e:
            raise SchemaError(
                f"FTS5 v2 to v3 migration failed: {e}",
                recovery="Back up your memory.db, then delete it; the next open will create a fresh v3 DB. Your base-table data is unaffected by this migration failure: the FTS5 index will rebuild.",
            ) from e

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
        for path in targets:
            try:
                os.chmod(path, _DB_FILE_MODE)
            except OSError:
                # Non-fatal: log nothing, defer to caller noticing if perms
                # are truly broken (write operations will fail downstream).
                pass

    def close(self) -> None:
        """Close the thread-local connection (mainly for tests / shutdown)."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None
