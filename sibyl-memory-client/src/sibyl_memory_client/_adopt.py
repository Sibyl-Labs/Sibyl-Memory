"""Pre-sign-in memory adoption: DEFAULT_TENANT rows -> the account's tenant.

The product promise (0.9.0): memory written on this computer before sign-in is
never deleted and is readable again the moment you sign in. Before accounts,
unactivated installs wrote under ``DEFAULT_TENANT``; after sign-in every
surface resolves the account tenant (``tenant_id -> account_id``), so those
rows were invisible. This module moves them, LOCALLY:

* ONE local SQLite transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``). A crash or
  error anywhere inside rolls the whole move back; there is no partial state.
* Nothing is deleted. Every DEFAULT_TENANT row in every tenant-scoped table is
  re-keyed to the target tenant; ids, bodies and timestamps are unchanged.
* Conflict rule (same business key in both tenants): KEEP BOTH, overwrite
  neither. The row already under the account keeps its key; the moved row gets
  ``<key> (before sign-in)`` (then ``(before sign-in 2)``, ...). Business keys
  are ``entities (category, name)``, ``state_documents document_key`` and
  ``reference_documents doc_key``; every other table is keyed by a random id and
  cannot conflict. Each rename is recorded in the audit row.
* Derived search state follows: the external-content FTS tables and the search
  shadow re-index entities/state/reference through their UPDATE triggers; the
  standalone journal FTS and the journal rows of the search shadow (append-only
  tables with no UPDATE trigger) are re-keyed explicitly in the same
  transaction.
* An audit row lands in ``tenant_adoptions`` (same transaction).
* No network, ever. Nothing here sends memory content (or anything else)
  anywhere.

Who may trigger it (spec deviation, documented in the 0.5.0 release notes):
``target_tenant`` must be the ``ten`` or ``sub`` of an entitlement the gate has
JUST verified and bound to this device. Automatic adoption (the client's first
gated operation for an account) runs at most once per account per store, and
ONLY when the store has never held another account (no data under another
tenant, no audit row for another account). Otherwise it is left to the explicit
``sibyl memory adopt`` command, which asks for confirmation.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from .storage import Storage, new_id

# Kept in sync with client.DEFAULT_TENANT (imported lazily there to avoid a
# cycle; a test asserts the two are equal).
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"

RENAME_SUFFIX = " (before sign-in)"

# Every tenant-scoped base table in schema.sql. A test asserts this list equals
# every table with a tenant_id column that is not an FTS/shadow table, so a new
# table cannot be forgotten.
TENANT_TABLES = (
    "entities", "entity_relations", "state_documents", "journal_events",
    "revenue_events", "error_events", "reference_documents", "archived_entities",
    "flagged_actors", "skill_proposals", "learning_runs",
)

# Tables whose tenant_id is a derived copy that no UPDATE trigger maintains.
# (entities_fts / state_documents_fts / reference_documents_fts are
# external-content: their tenant_id column is read from the base row and their
# AU triggers re-index it.)
_JOURNAL_FTS = "journal_events_fts"
_SHADOW = "search_shadow"

TRIGGERS = ("auto", "command", "seen")


class AdoptionRefused(Exception):
    """The move was not attempted. ``reason`` is one of ``not_verified``,
    ``tenant_mismatch``, ``default_target``, ``other_account``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class AdoptionResult:
    status: str                       # moved | nothing_to_move | skipped
    target_tenant: str
    counts: dict[str, int] = field(default_factory=dict)
    renamed: list[dict[str, str]] = field(default_factory=list)
    reason: str | None = None         # why skipped (auto path)

    @property
    def moved(self) -> int:
        return sum(self.counts.values())


# ----------------------------------------------------------------------
# Read-only inspection
# ----------------------------------------------------------------------

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def pending_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per tenant table still under DEFAULT_TENANT (counts only)."""
    out: dict[str, int] = {}
    for t in TENANT_TABLES:
        n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id = ?",
                         (DEFAULT_TENANT,)).fetchone()[0]
        if n:
            out[t] = int(n)
    return out


def _has_pending(conn: sqlite3.Connection) -> bool:
    for t in TENANT_TABLES:
        if conn.execute(f"SELECT 1 FROM {t} WHERE tenant_id = ? LIMIT 1",
                        (DEFAULT_TENANT,)).fetchone():
            return True
    return False


def other_accounts(conn: sqlite3.Connection, own: set[str]) -> bool:
    """True when this store has ever held an account other than ``own``: data
    under another tenant, or an audit row (sign-in or move) for another one."""
    allowed = set(own) | {DEFAULT_TENANT}
    marks = ",".join("?" for _ in allowed)
    params = tuple(allowed)
    for t in TENANT_TABLES:
        if conn.execute(f"SELECT 1 FROM {t} WHERE tenant_id NOT IN ({marks}) LIMIT 1",
                        params).fetchone():
            return True
    if _table_exists(conn, "tenant_adoptions"):
        if conn.execute(
            f"SELECT 1 FROM tenant_adoptions WHERE to_tenant NOT IN ({marks}) "
            f"OR account_id NOT IN ({marks}) LIMIT 1", params + params,
        ).fetchone():
            return True
    return False


def _account_seen(conn: sqlite3.Connection, account_id: str, target: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tenant_adoptions WHERE account_id = ? OR to_tenant = ? LIMIT 1",
        (account_id, target),
    ).fetchone() is not None


# ----------------------------------------------------------------------
# The move
# ----------------------------------------------------------------------

def _free_name(taken: Callable[[str], bool], base: str) -> str:
    cand = base + RENAME_SUFFIX
    n = 2
    while taken(cand):
        cand = f"{base} (before sign-in {n})"
        n += 1
    return cand


def _resolve_conflicts(conn: sqlite3.Connection, target: str) -> list[dict[str, str]]:
    """Rename every DEFAULT_TENANT row whose business key already exists under
    ``target``. The target's row is never touched."""
    renamed: list[dict[str, str]] = []

    def _both(sql: str, *args: Any) -> bool:
        return conn.execute(sql, args).fetchone() is not None

    rows = conn.execute(
        "SELECT d.id, d.category, d.name FROM entities d JOIN entities t "
        "ON t.category = d.category AND t.name = d.name "
        "WHERE d.tenant_id = ? AND t.tenant_id = ?", (DEFAULT_TENANT, target),
    ).fetchall()
    for r in rows:
        cat = r[1]
        new = _free_name(lambda c: _both(
            "SELECT 1 FROM entities WHERE category = ? AND name = ? AND tenant_id IN (?, ?)",
            cat, c, DEFAULT_TENANT, target), r[2])
        conn.execute("UPDATE entities SET name = ? WHERE id = ?", (new, r[0]))
        renamed.append({"table": "entities", "category": cat, "from": r[2], "to": new})

    for table, col in (("state_documents", "document_key"), ("reference_documents", "doc_key")):
        keys = [row[0] for row in conn.execute(
            f"SELECT d.{col} FROM {table} d JOIN {table} t ON t.{col} = d.{col} "
            f"WHERE d.tenant_id = ? AND t.tenant_id = ?", (DEFAULT_TENANT, target))]
        for key in keys:
            new = _free_name(lambda c: _both(
                f"SELECT 1 FROM {table} WHERE {col} = ? AND tenant_id IN (?, ?)",
                c, DEFAULT_TENANT, target), key)
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE tenant_id = ? AND {col} = ?",
                         (new, DEFAULT_TENANT, key))
            renamed.append({"table": table, "from": key, "to": new})
            if table == "reference_documents":
                # An accepted skill proposal points at its reference doc by key.
                conn.execute(
                    "UPDATE skill_proposals SET accepted_doc_key = ? "
                    "WHERE tenant_id = ? AND accepted_doc_key = ?",
                    (new, DEFAULT_TENANT, key))
    return renamed


def _move_table(conn: sqlite3.Connection, table: str, target: str) -> int:
    cur = conn.execute(f"UPDATE {table} SET tenant_id = ? WHERE tenant_id = ?",
                       (target, DEFAULT_TENANT))
    return int(cur.rowcount or 0)


def _move_derived(conn: sqlite3.Connection, target: str) -> None:
    """Re-key the derived copies no UPDATE trigger maintains (journal FTS and
    the journal rows of the search shadow)."""
    if _table_exists(conn, _JOURNAL_FTS):
        conn.execute(f"UPDATE {_JOURNAL_FTS} SET tenant_id = ? WHERE tenant_id = ?",
                     (target, DEFAULT_TENANT))
    if _table_exists(conn, _SHADOW):
        conn.execute(f"UPDATE {_SHADOW} SET tenant_id = ? "
                     f"WHERE tier = 'journal' AND tenant_id = ?", (target, DEFAULT_TENANT))


def _record(conn: sqlite3.Connection, *, target: str, account_id: str, trigger: str,
            counts: dict[str, int], renamed: list[dict[str, str]]) -> None:
    conn.execute(
        "INSERT INTO tenant_adoptions (id, from_tenant, to_tenant, account_id, kind, "
        "counts, renamed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id(), DEFAULT_TENANT, target, account_id, trigger,
         json.dumps(counts, sort_keys=True), json.dumps(renamed) if renamed else None),
    )


def adopt_default_tenant(
    storage: Storage,
    target_tenant: str,
    entitlement: Any,
    *,
    trigger: str = "command",
    confirm_other_accounts: bool = False,
) -> AdoptionResult:
    """Move every DEFAULT_TENANT row of ``storage`` to ``target_tenant``.

    ``entitlement`` must be the verified, device-bound Entitlement the gate
    just returned (``EntitlementGate.require``); ``target_tenant`` must be its
    ``ten`` or ``sub``. ``trigger``:

    * ``"auto"``: the client's first gated operation. Runs only when this store
      has no audit row for the account yet AND has never held another account;
      otherwise returns ``skipped`` (never raises for those). Always leaves an
      audit row for the account (``seen`` when nothing moved), so it is the
      FIRST sign-in only.
    * ``"command"``: ``sibyl memory adopt``. Moves whatever is under
      DEFAULT_TENANT now (idempotent: a re-run finds nothing and changes
      nothing). Raises ``AdoptionRefused("other_account")`` unless
      ``confirm_other_accounts`` when the store has held another account.
    """
    if trigger not in ("auto", "command"):
        raise ValueError("trigger must be 'auto' or 'command'")
    ten = getattr(entitlement, "ten", None)
    sub = getattr(entitlement, "sub", None)
    if not isinstance(ten, str) or not isinstance(sub, str) or not ten or not sub:
        raise AdoptionRefused("not_verified",
                              "No verified sign-in on this computer. Run sibyl init first.")
    if target_tenant == DEFAULT_TENANT:
        raise AdoptionRefused("default_target", "The target is the default tenant itself.")
    if target_tenant not in (ten, sub):
        raise AdoptionRefused("tenant_mismatch",
                              "The target tenant does not belong to the signed-in account.")
    own = {ten, sub}

    # Cheap pre-check outside the write lock: the common case (nothing under
    # DEFAULT_TENANT, account already seen) costs two indexed probes.
    with storage.connection() as conn:
        pending = _has_pending(conn)
        seen = _account_seen(conn, sub, target_tenant)
    if trigger == "auto" and seen:
        return AdoptionResult("skipped", target_tenant, reason="already_seen")
    if trigger == "command" and not pending:
        return AdoptionResult("nothing_to_move", target_tenant)

    with storage.transaction() as conn:
        # Re-check inside the lock (another process may have moved them).
        if trigger == "auto" and _account_seen(conn, sub, target_tenant):
            return AdoptionResult("skipped", target_tenant, reason="already_seen")
        if not _has_pending(conn):
            if trigger == "auto":
                _record(conn, target=target_tenant, account_id=sub, trigger="seen",
                        counts={}, renamed=[])
            return AdoptionResult("nothing_to_move", target_tenant)
        if other_accounts(conn, own):
            if trigger == "auto":
                # Not ours to merge automatically. No audit row: the explicit
                # command (with its confirmation) stays the way forward.
                return AdoptionResult("skipped", target_tenant, reason="other_account")
            if not confirm_other_accounts:
                raise AdoptionRefused(
                    "other_account",
                    "This computer's memory store also holds another account's memory. "
                    "Confirm before moving the rows written before sign-in into this account.")
        renamed = _resolve_conflicts(conn, target_tenant)
        counts: dict[str, int] = {}
        for table in TENANT_TABLES:
            n = _move_table(conn, table, target_tenant)
            if n:
                counts[table] = n
        _move_derived(conn, target_tenant)
        _record(conn, target=target_tenant, account_id=sub, trigger=trigger,
                counts=counts, renamed=renamed)
    return AdoptionResult("moved", target_tenant, counts=counts, renamed=renamed)


def adoption_history(storage: Storage) -> list[dict[str, Any]]:
    """The audit rows, newest first (no memory content beyond renamed keys)."""
    with storage.connection() as conn:
        rows = conn.execute(
            "SELECT from_tenant, to_tenant, account_id, kind, moved_at, counts, renamed "
            "FROM tenant_adoptions ORDER BY moved_at DESC, rowid DESC").fetchall()
    out = []
    for r in rows:
        out.append({
            "from_tenant": r[0], "to_tenant": r[1], "account_id": r[2], "trigger": r[3],
            "moved_at": r[4], "counts": json.loads(r[5] or "{}"),
            "renamed": json.loads(r[6]) if r[6] else [],
        })
    return out


__all__ = [
    "AdoptionRefused", "AdoptionResult", "DEFAULT_TENANT", "RENAME_SUFFIX",
    "TENANT_TABLES", "adopt_default_tenant", "adoption_history", "other_accounts",
    "pending_counts",
]
