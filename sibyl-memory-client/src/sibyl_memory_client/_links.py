"""Relations: a node graph across tiers (spec tiered-retrieval 2.B, schema v6).

``memory_links`` holds typed, directed edges between nodes addressed by their
BUSINESS KEYS, not by row ids:

* ``entity``    -> (category, name)
* ``reference`` -> doc_key           (category stored as '')
* ``state``     -> document_key      (category stored as '')

Why business keys: reference and state rows have no id, and ``archive_entity``
deletes the entity row, so id foreign keys with ``ON DELETE CASCADE`` would
silently erase a hub's edges on ``memory_forget``. Business keys survive archive
and re-creation (re-creating an archived entity under the same key restores its
edges). The price: a rename must rewrite edges. The only renamer is adoption
(``_adopt._resolve_conflicts``), which calls :func:`rename_endpoint` inside its
transaction.

Identity stamp: entity endpoints also carry ``from_entity_id`` /
``to_entity_id``, the row id the edge was written against. A business key now
held by a DIFFERENT row (archive + re-create, a namesake) makes the edge
``stale`` (see :data:`STALE_SIDE_SQL` / :func:`stale_ids`, the read-side
contract); the edge still survives, as decided, but no longer passes for a
fact about the new row.

This module holds the SQL; the gated public surface is on ``MemoryClient``
(``link`` / ``unlink`` / ``neighbors`` / ``links_of`` and the ``links=``
argument of ``set_entity`` / ``set_reference``). Every statement here carries
``tenant_id`` (CORE-3); nothing joins across tenants. Edges are never
FTS-indexed, so they add no MATCH surface.
"""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any, Iterable

from . import _flags
from .exceptions import NotFoundError, ValidationError
from .storage import dumps, loads, new_id

TIERS = ("entity", "reference", "state")
DIRECTIONS = ("out", "in", "both")

RELATION_MAX_CHARS = 64
EDGE_METADATA_MAX_BYTES = 4 * 1024
# Hard bound on links= per single write call (one transaction). A hub with more
# members is written with several calls or with link(); neighbors paginates.
MAX_LINKS_PER_WRITE = 1000
NEIGHBORS_LIMIT_MAX = 200
LINKS_OF_MAX_NODES = 200
LINKS_OF_PER_NODE_MAX = 20
PREVIEW_CHARS = 200
# Lint: warn above this many out-edges from one node (spec 2.B, `hub-degree`).
HUB_DEGREE_WARN = 500
# Rough per-edge row + two index entries, for the pre-write cap estimate. The
# CAP-2 in-transaction recheck measures the real footprint either way.
EDGE_OVERHEAD_BYTES = 200

# The v6 DDL. schema.sql carries the SAME statements (fresh stores get them from
# the schema script); Storage applies these, together with the one-time
# entity_relations copy, in ONE transaction when it opens a pre-v6 store, so the
# existence of the table is itself the crash-atomic "copied" marker. A test
# asserts the two copies are identical.
V6_DDL = (
    """CREATE TABLE IF NOT EXISTS memory_links (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  from_tier     TEXT NOT NULL CHECK (from_tier IN ('entity','reference','state')),
  from_category TEXT NOT NULL,
  from_key      TEXT NOT NULL,
  to_tier       TEXT NOT NULL CHECK (to_tier IN ('entity','reference','state')),
  to_category   TEXT NOT NULL,
  to_key        TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  metadata      TEXT CHECK (metadata IS NULL OR json_valid(metadata)),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  from_entity_id TEXT,
  to_entity_id   TEXT,
  updated_at    TEXT,
  CHECK ((from_tier = 'entity') = (from_category <> '')),
  CHECK ((to_tier = 'entity') = (to_category <> '')),
  UNIQUE (tenant_id, from_tier, from_category, from_key, to_tier, to_category, to_key, relation_type)
)""",
    """CREATE INDEX IF NOT EXISTS memory_links_from
  ON memory_links (tenant_id, from_tier, from_category, from_key, relation_type)""",
    """CREATE INDEX IF NOT EXISTS memory_links_to
  ON memory_links (tenant_id, to_tier, to_category, to_key, relation_type)""",
    """CREATE INDEX IF NOT EXISTS memory_links_created
  ON memory_links (tenant_id, created_at, id)""",
)

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def needs_v6_migration(conn: sqlite3.Connection) -> bool:
    """A pre-v6 store: base tables exist, memory_links does not. A brand-new
    file (no ``entities``) gets the table from schema.sql instead."""
    return _table_exists(conn, "entities") and not _table_exists(conn, "memory_links")


# v6 identity stamp (operator decision 1, panel theme A). Entity endpoints are
# addressed by business key AND stamped with the entity row id they named when
# the edge was written; reference/state endpoints carry NULL (they have no id).
# ``updated_at`` is set on insert and on every change. Stores created by an
# earlier V1 build hold memory_links WITHOUT these columns: Storage adds them
# (with tenant_adoptions.links_audit) in ONE transaction on open, see
# :func:`apply_identity_columns`. NULL stamp = provenance unknown, never stale.
IDENTITY_COLUMNS = (
    ("memory_links", "from_entity_id", "TEXT"),
    ("memory_links", "to_entity_id", "TEXT"),
    ("memory_links", "updated_at", "TEXT"),
    ("tenant_adoptions", "links_audit",
     "TEXT CHECK (links_audit IS NULL OR json_valid(links_audit))"),
)

# STALE CONTRACT (for every read path that renders edges; neighbors/links_of
# render it as ``stale: true``). An edge side is stale when it carries a stamp
# AND a live entity holds its (category, key) under a DIFFERENT id: the name was
# archived/deleted and re-created, or the edge was carried onto a namesake. A
# side with no live row is dangling (``exists: false``), not stale; a NULL
# stamp is never stale. ``{a}`` is the memory_links alias. Indexed: one probe of
# the entities UNIQUE (tenant_id, category, name) index per side.
STALE_SIDE_SQL = (
    "({a}.{side}_entity_id IS NOT NULL AND EXISTS (SELECT 1 FROM entities se "
    "WHERE se.tenant_id = {a}.tenant_id AND se.category = {a}.{side}_category "
    "AND se.name = {a}.{side}_key AND se.id <> {a}.{side}_entity_id))"
)


def stale_sql(alias: str = "l") -> str:
    """SQL boolean: the edge aliased ``alias`` is stale on either side."""
    return ("(" + STALE_SIDE_SQL.format(a=alias, side="from") + " OR "
            + STALE_SIDE_SQL.format(a=alias, side="to") + ")")


def stale_ids(conn: sqlite3.Connection, tenant: str, ids: Iterable[str]) -> set[str]:
    """The subset of edge ``ids`` (this tenant only) that are stale. One
    statement per 500 ids; O(page) index probes. Read paths call this on the
    page of edges they return and set ``edge["stale"] = edge["id"] in result``."""
    ids = list(dict.fromkeys(ids))
    if not ids or not _has_column(conn, "memory_links", "from_entity_id"):
        return set()
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        out.update(r[0] for r in conn.execute(
            # `+l.tenant_id`: keep the tenant check (CORE-3) out of index
            # selection, so the lookup is by primary key. Without it SQLite
            # drove this through memory_links_created, a scan of every edge in
            # the tenant per page (14 ms per neighbors call at 42k edges).
            f"SELECT l.id FROM memory_links l WHERE +l.tenant_id = ? AND l.id IN ({marks}) "
            f"AND {stale_sql('l')}", [tenant, *chunk]))
    return out


def stale_column(conn: sqlite3.Connection, alias: str = "l") -> str:
    """The stale contract as a SELECT expression for read paths that fetch
    edges themselves (neighbors, links_of): :func:`stale_sql` when the stamp
    columns exist, else the constant ``0``. Checked per statement (PRAGMA, no
    cache), so it can never lag the open-time identity-column migration."""
    if _has_column(conn, "memory_links", "from_entity_id"):
        return stale_sql(alias)
    return "0"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def needs_identity_columns(conn: sqlite3.Connection) -> bool:
    return any(_table_exists(conn, t) and not _has_column(conn, t, c)
               for t, c, _ in IDENTITY_COLUMNS)


def apply_identity_columns(conn: sqlite3.Connection) -> int:
    """Add the stamp / updated_at / links_audit columns that are missing and
    backfill the stamps of existing edges. Caller holds ONE write transaction
    (SQLite DDL is transactional, so a crash leaves the store as it was).
    Returns the number of edge sides stamped.

    Backfill (Fable consult): a side is stamped with the live entity's id only
    when that entity existed when the edge was written (entity created_at <=
    edge created_at); otherwise with the original id of the newest archived
    row of that name archived at or after the edge was written (the entity the
    edge was actually about); otherwise NULL (unknown). Stamping the live id
    unconditionally would launder exactly the inherited edges the stamp exists
    to flag."""
    added_stamp = False
    for table, column, decl in IDENTITY_COLUMNS:
        if _table_exists(conn, table) and not _has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added_stamp = added_stamp or column == "from_entity_id"
    if not _table_exists(conn, "memory_links"):
        return 0
    conn.execute("UPDATE memory_links SET updated_at = created_at WHERE updated_at IS NULL")
    if not added_stamp:
        return 0
    n = 0
    for side in ("from", "to"):
        n += int(conn.execute(
            f"""UPDATE memory_links SET {side}_entity_id = COALESCE(
                  (SELECT e.id FROM entities e WHERE e.tenant_id = memory_links.tenant_id
                     AND e.category = memory_links.{side}_category
                     AND e.name = memory_links.{side}_key
                     AND e.created_at <= memory_links.created_at),
                  (SELECT a.original_entity_id FROM archived_entities a
                    WHERE a.tenant_id = memory_links.tenant_id
                      AND a.category = memory_links.{side}_category
                      AND a.name = memory_links.{side}_key
                      AND a.archived_at >= memory_links.created_at
                    ORDER BY a.archived_at DESC, a.rowid DESC LIMIT 1))
                WHERE {side}_tier = 'entity' AND {side}_entity_id IS NULL"""
        ).rowcount or 0)
    return n


# One-time copy of the pre-v6 id-based edges, in Python so every relation goes
# through the SAME norm_relation as link() (trim + casefold + 64-char bound);
# an invalid row is skipped and counted, never truncated into a different
# relation. Both ids must resolve to an entity of the relation row's tenant
# (cross-tenant and orphan ids are skipped). Rows that fold to one edge keep the
# earliest row's id / created_at and merge their metadata objects in
# created_at order (a later key wins). Each side is stamped with the entity id
# the relation named. The receipt goes into the v6 version row's description
# and the log.
V6_DESCRIPTION = (
    "sibyl-memory-client v6. Adds memory_links: typed, directed edges between entity / "
    "reference / state nodes keyed by business keys (link, unlink, neighbors, links_of, "
    "links= on set_entity and set_reference). Pre-v6 entity_relations rows are copied once, "
    "in the same transaction that creates the table; entity_relations stays, unwritten, until "
    "it is dropped in a later major release. Additive."
)
RECEIPT_KEYS = ("source_rows", "copied", "folded", "skipped_orphan", "skipped_cross_tenant",
                "skipped_bad_endpoint", "skipped_bad_relation", "metadata_dropped")


def _copy_entity_relations(conn: sqlite3.Connection) -> dict[str, int]:
    rec = {k: 0 for k in RECEIPT_KEYS}
    rows = conn.execute(
        "SELECT r.id, r.tenant_id, r.from_id, r.to_id, r.relation_type, r.metadata, r.created_at, "
        "f.tenant_id, f.category, f.name, t.tenant_id, t.category, t.name "
        "FROM entity_relations r LEFT JOIN entities f ON f.id = r.from_id "
        "LEFT JOIN entities t ON t.id = r.to_id ORDER BY r.created_at, r.id").fetchall()
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        rec["source_rows"] += 1
        if r[7] is None or r[10] is None:
            rec["skipped_orphan"] += 1
            continue
        if r[7] != r[1] or r[10] != r[1]:
            rec["skipped_cross_tenant"] += 1
            continue
        try:
            fc = _validate_identifier(r[8], "category")
            fk = _validate_identifier(r[9], "name")
            tc = _validate_identifier(r[11], "category")
            tk = _validate_identifier(r[12], "name")
        except ValidationError:
            rec["skipped_bad_endpoint"] += 1
            continue
        try:
            rel = norm_relation(r[4])
        except ValidationError:
            rec["skipped_bad_relation"] += 1
            continue
        meta: dict | None = None
        if r[5] is not None:
            try:
                val = loads(r[5])
            except (TypeError, ValueError):
                val = None
            if isinstance(val, dict):
                meta = val
            else:
                rec["metadata_dropped"] += 1
        key = (r[1], fc, fk, tc, tk, rel)
        g = groups.get(key)
        if g is None:
            groups[key] = {"id": r[0], "created_at": r[6], "updated_at": r[6], "meta": meta,
                           "from_id": r[2], "to_id": r[3]}
            continue
        rec["folded"] += 1
        g["updated_at"] = r[6] or g["updated_at"]
        if meta:
            g["meta"] = {**(g["meta"] or {}), **meta}
    for (tenant, fc, fk, tc, tk, rel), g in groups.items():
        meta_json = None
        if g["meta"] is not None:
            meta_json = dumps(g["meta"])
            if len(meta_json.encode("utf-8")) > EDGE_METADATA_MAX_BYTES:
                meta_json = None
                rec["metadata_dropped"] += 1
        cur = conn.execute(
            "INSERT OR IGNORE INTO memory_links (id, tenant_id, from_tier, from_category, from_key, "
            "to_tier, to_category, to_key, relation_type, metadata, created_at, from_entity_id, "
            "to_entity_id, updated_at) VALUES (?, ?, 'entity', ?, ?, 'entity', ?, ?, ?, ?, "
            "COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), ?, ?, "
            "COALESCE(?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))",
            (g["id"], tenant, fc, fk, tc, tk, rel, meta_json, g["created_at"], g["from_id"],
             g["to_id"], g["updated_at"], g["created_at"]))
        rec["copied"] += int(cur.rowcount or 0)
    return rec


def apply_v6_migration(conn: sqlite3.Connection) -> dict[str, int]:
    """Create memory_links and copy entity_relations into it. Caller holds a
    write transaction; returns the copy receipt (also written into the v6
    version row's description). entity_relations is left in place, unwritten
    (frozen; dropped in a later major release)."""
    for stmt in V6_DDL:
        conn.execute(stmt)
    rec = {k: 0 for k in RECEIPT_KEYS}
    if _table_exists(conn, "entity_relations"):
        rec = _copy_entity_relations(conn)
    if _table_exists(conn, "sibyl_memory_schema_version"):
        conn.execute(
            "INSERT OR IGNORE INTO sibyl_memory_schema_version (version, description) "
            "VALUES (6, ?)",
            (V6_DESCRIPTION + " Migration receipt: " + json.dumps(rec, sort_keys=True),))
    return rec


# ----------------------------------------------------------------------
# Validation / normalisation
# ----------------------------------------------------------------------

def _validate_identifier(value: Any, field_name: str) -> str:
    from .client import validate_identifier
    return validate_identifier(value, field_name=field_name)


def norm_node(node: Any, *, side: str = "node") -> tuple[str, str, str]:
    """NodeRef -> (tier, category, key) with category '' for reference/state.

    Accepts ``{"tier", "category"?, "key"}`` and, leniently, a bare entity
    ``{"category": c, "name": n}`` (no tier) as ``tier: entity``; ``name`` is
    accepted as an alias of ``key`` for entities."""
    if not isinstance(node, dict):
        raise ValidationError(
            f"{side} must be a NodeRef object like "
            '{"tier": "entity", "category": "person", "key": "Maya Patel"}, '
            f"got {type(node).__name__}",
            recovery='Pass {"tier": "entity"|"reference"|"state", "category": ..., "key": ...}.',
        )
    tier = node.get("tier")
    key = node.get("key")
    if key is None and "name" in node:
        key = node.get("name")
    if tier is None and "category" in node and node.get("category") not in (None, ""):
        tier = "entity"
    if tier not in TIERS:
        raise ValidationError(
            f"{side}.tier must be one of entity, reference, state",
            recovery="Set tier to entity, reference or state.",
        )
    key = _validate_identifier(key, f"{side}.key")
    category = node.get("category")
    if tier == "entity":
        category = _validate_identifier(category, f"{side}.category")
    else:
        if category not in (None, ""):
            raise ValidationError(
                f"{side}.category applies to entity nodes only ({tier} nodes are addressed by key)",
                recovery=f"Drop category for a {tier} node.",
            )
        category = ""
    return tier, category, key


def node_ref(tier: str, category: str, key: str) -> dict[str, Any]:
    return {"tier": tier, "category": category if tier == "entity" else None, "key": key}


def norm_relation(relation: Any, field_name: str = "relation") -> str:
    rel = _validate_identifier(relation, field_name)
    rel = rel.strip().casefold()
    if not rel:
        raise ValidationError(f"{field_name} must be non-empty")
    if len(rel) > RELATION_MAX_CHARS:
        raise ValidationError(
            f"{field_name} is longer than {RELATION_MAX_CHARS} characters",
            recovery="Use a short verb like works_at, part_of, source_of, about.",
        )
    return rel


def norm_metadata(metadata: Any) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValidationError(
            f"edge metadata must be an object, got {type(metadata).__name__}",
            recovery='Pass a small object, e.g. {"date": "2026-03-10", "role": "owner"}.',
        )
    try:
        encoded = dumps(metadata)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"edge metadata is not JSON-serializable: {e}") from e
    if len(encoded.encode("utf-8")) > EDGE_METADATA_MAX_BYTES:
        raise ValidationError(
            f"edge metadata is larger than {EDGE_METADATA_MAX_BYTES // 1024} KB",
            recovery="Keep edge metadata to a few short fields; put long text in a reference document.",
        )
    return encoded


def norm_link_specs(links: Any) -> list[tuple[tuple[str, str, str], str, str | None]]:
    """links= argument -> [((tier, category, key), relation, metadata_json)]."""
    if links is None:
        return []
    if not isinstance(links, (list, tuple)):
        raise ValidationError(
            "links must be a list of {\"to\": NodeRef, \"relation\": str, \"metadata\"?: object}",
        )
    if len(links) > MAX_LINKS_PER_WRITE:
        raise ValidationError(
            f"links has {len(links)} edges; at most {MAX_LINKS_PER_WRITE} per write",
            recovery="Write the rest with link() / memory_link, or across several calls.",
        )
    out = []
    for i, spec in enumerate(links):
        if not isinstance(spec, dict):
            raise ValidationError(f"links[{i}] must be an object")
        dst = norm_node(spec.get("to"), side=f"links[{i}].to")
        rel = norm_relation(spec.get("relation"), f"links[{i}].relation")
        meta = norm_metadata(spec.get("metadata"))
        out.append((dst, rel, meta))
    return out


def spec_bytes(specs: Iterable[tuple[tuple[str, str, str], str, str | None]],
               src: tuple[str, str, str]) -> int:
    """Pre-write byte estimate for the cap gate (edges count toward the cap)."""
    total = 0
    for (t, c, k), rel, meta in specs:
        total += (len(src[1]) + len(src[2]) + len(c) + len(k) + len(rel)
                  + (len(meta) if meta else 0) + EDGE_OVERHEAD_BYTES)
    return total


def edge_bytes(src: tuple[str, str, str], dst: tuple[str, str, str], rel: str,
               meta: str | None) -> int:
    return spec_bytes([(dst, rel, meta)], src)


# ----------------------------------------------------------------------
# Existence (inside the caller's tenant only)
# ----------------------------------------------------------------------

def node_exists(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str]) -> bool:
    tier, category, key = node
    if tier == "entity":
        sql = "SELECT 1 FROM entities WHERE tenant_id = ? AND category = ? AND name = ?"
        args: tuple = (tenant, category, key)
    elif tier == "reference":
        sql = "SELECT 1 FROM reference_documents WHERE tenant_id = ? AND doc_key = ?"
        args = (tenant, key)
    else:
        sql = "SELECT 1 FROM state_documents WHERE tenant_id = ? AND document_key = ?"
        args = (tenant, key)
    return conn.execute(sql, args).fetchone() is not None


def _describe(node: tuple[str, str, str]) -> str:
    tier, category, key = node
    return f"entity {category}/{key}" if tier == "entity" else f"{tier} {key}"


def require_exists(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str],
                   side: str) -> None:
    if not node_exists(conn, tenant, node):
        raise NotFoundError(
            f"link {side} not found: {_describe(node)}. Write the node first "
            "(memory_remember / memory_set_reference / memory_set_state); "
            "edges are only created between existing nodes.",
        )


# ----------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------

def entity_id(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str]) -> str | None:
    """The live row id behind an entity endpoint (the stamp); None otherwise."""
    if node[0] != "entity":
        return None
    row = conn.execute("SELECT id FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                       (tenant, node[1], node[2])).fetchone()
    return row[0] if row is not None else None


def count_touching(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str]) -> int:
    """Edges touching ``node`` in either direction (a self-loop counts once)."""
    tier, category, key = node
    return int(conn.execute(
        "SELECT COUNT(*) FROM memory_links WHERE tenant_id = ? AND "
        "((from_tier = ? AND from_category = ? AND from_key = ?) OR "
        " (to_tier = ? AND to_category = ? AND to_key = ?))",
        (tenant, tier, category, key, tier, category, key)).fetchone()[0])


def upsert_edge(conn: sqlite3.Connection, tenant: str, src: tuple[str, str, str],
                dst: tuple[str, str, str], rel: str, meta: str | None) -> dict[str, Any]:
    """Insert, or upsert metadata on, one edge. Caller has checked existence and
    holds the write transaction. ``meta`` None keeps any existing metadata.

    Writing an edge (again) is an assertion about the LIVE endpoints, so both
    entity sides are (re-)stamped with the current row ids; that clears
    ``stale``. Returns ``{"id", "created", "changed"}``: ``changed`` is True
    only when an existing edge's metadata or stamps actually differed (an
    identical re-link changes nothing, bumps no ``updated_at`` and is not
    counted as a write)."""
    fid = entity_id(conn, tenant, src)
    tid = entity_id(conn, tenant, dst)
    row = conn.execute(
        "SELECT id, metadata, from_entity_id, to_entity_id FROM memory_links WHERE tenant_id = ? "
        "AND from_tier = ? AND from_category = ? AND from_key = ? AND to_tier = ? "
        "AND to_category = ? AND to_key = ? AND relation_type = ?",
        (tenant, *src, *dst, rel),
    ).fetchone()
    if row is not None:
        new_meta = row[1] if meta is None else meta
        meta_changed = meta is not None and (row[1] is None or loads(row[1]) != loads(meta))
        changed = meta_changed or row[2] != fid or row[3] != tid
        if changed:
            conn.execute(
                "UPDATE memory_links SET metadata = ?, from_entity_id = ?, to_entity_id = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (new_meta, fid, tid, row[0]))
        return {"id": row[0], "created": False, "changed": bool(changed)}
    edge_id = new_id()
    conn.execute(
        "INSERT INTO memory_links (id, tenant_id, from_tier, from_category, from_key, "
        "to_tier, to_category, to_key, relation_type, metadata, from_entity_id, to_entity_id, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
        (edge_id, tenant, *src, *dst, rel, meta, fid, tid),
    )
    return {"id": edge_id, "created": True, "changed": False}


def write_specs(conn: sqlite3.Connection, tenant: str, src: tuple[str, str, str],
                specs: list[tuple[tuple[str, str, str], str, str | None]]) -> list[dict[str, Any]]:
    """links= on a node write: strict (every target must exist in this tenant,
    after the node itself was staged in the same transaction)."""
    out = []
    for i, (dst, rel, meta) in enumerate(specs):
        if dst != src:
            require_exists(conn, tenant, dst, f"target (links[{i}].to)")
        res = upsert_edge(conn, tenant, src, dst, rel, meta)
        res.update({"relation": rel, "to": node_ref(*dst)})
        out.append(res)
    return out


def write_summary(results: list[dict[str, Any]]) -> dict[str, int]:
    """``{created, updated, unchanged}`` over the edges ``write_specs`` touched
    (Fable consult, V1 merge): created = new edge; updated = existed and its
    metadata or identity stamps differed (``updated_at`` bumped, the same
    ``changed`` link() reports); unchanged = existed, identical, a no-op.
    created + updated + unchanged == len(results); all three keys always
    present."""
    created = sum(1 for r in results if r.get("created"))
    updated = sum(1 for r in results if not r.get("created") and r.get("changed"))
    return {"created": created, "updated": updated,
            "unchanged": len(results) - created - updated}


def delete_edges(conn: sqlite3.Connection, tenant: str, src: tuple[str, str, str],
                 dst: tuple[str, str, str], rel: str | None) -> int:
    sql = ("DELETE FROM memory_links WHERE tenant_id = ? AND from_tier = ? AND from_category = ? "
           "AND from_key = ? AND to_tier = ? AND to_category = ? AND to_key = ?")
    args: list[Any] = [tenant, *src, *dst]
    if rel is not None:
        sql += " AND relation_type = ?"
        args.append(rel)
    return int(conn.execute(sql, args).rowcount or 0)


def cascade_node(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str]) -> int:
    """Delete every edge touching ``node`` (either endpoint). Called inside the
    HARD-delete transaction of the node (delete_entity, delete_reference).
    archive_entity does NOT call this: archived endpoints keep their edges."""
    tier, category, key = node
    n = conn.execute(
        "DELETE FROM memory_links WHERE tenant_id = ? AND "
        "((from_tier = ? AND from_category = ? AND from_key = ?) OR "
        " (to_tier = ? AND to_category = ? AND to_key = ?))",
        (tenant, tier, category, key, tier, category, key),
    ).rowcount
    return int(n or 0)


def drop_adoption_duplicates(conn: sqlite3.Connection, source: str, target: str,
                             exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Adoption: a ``source`` edge identical (all seven key columns) to an edge the
    ``target`` tenant already holds is the same fact; the target row is never
    touched, so the source copy is dropped and returned (audit shape, reason
    ``duplicate``). Without this the tenant move would hit the UNIQUE and roll
    the whole adoption back, forever (Fable consult Q2). ``exclude``: edge ids
    that are held back (not moved), so they cannot collide."""
    if not _table_exists(conn, "memory_links"):
        return []
    skip = set(exclude)
    rows = conn.execute(
        f"SELECT {', '.join('s.' + c.strip() for c in _EDGE_COLS.split(','))} "
        "FROM memory_links s WHERE s.tenant_id = ? AND EXISTS ("
        "SELECT 1 FROM memory_links t WHERE t.tenant_id = ? AND t.from_tier = s.from_tier "
        "AND t.from_category = s.from_category AND t.from_key = s.from_key "
        "AND t.to_tier = s.to_tier AND t.to_category = s.to_category AND t.to_key = s.to_key "
        "AND t.relation_type = s.relation_type)", (source, target)).fetchall()
    dropped = []
    for r in rows:
        if r[0] in skip:
            continue
        conn.execute("DELETE FROM memory_links WHERE id = ?", (r[0],))
        dropped.append(_edge_dict(r, "duplicate"))
    return dropped


def _edge_dict(r: Any, reason: str) -> dict[str, Any]:
    """Audit shape of one edge (id, endpoints, relation, metadata, reason)."""
    return {"id": r[0], "from": node_ref(r[1], r[2], r[3]), "to": node_ref(r[4], r[5], r[6]),
            "relation": r[7], "metadata": loads(r[8]), "reason": reason}


_EDGE_COLS = ("id, from_tier, from_category, from_key, to_tier, to_category, to_key, "
              "relation_type, metadata")


def rekey_edge(conn: sqlite3.Connection, edge_id: str, *, from_key: str | None = None,
               to_key: str | None = None, dropped: list | None = None,
               reason: str = "rename_collision") -> bool:
    """Rewrite one edge's endpoint key(s) in place. If the rewritten edge would
    duplicate an edge already in the store (UNIQUE), the moving copy is the
    same fact: it is deleted and appended to ``dropped`` (audit shape) instead
    of failing the caller's transaction. Returns True when rewritten."""
    sets, args = [], []
    if from_key is not None:
        sets.append("from_key = ?")
        args.append(from_key)
    if to_key is not None:
        sets.append("to_key = ?")
        args.append(to_key)
    if not sets:
        return True
    try:
        conn.execute(f"UPDATE memory_links SET {', '.join(sets)} WHERE id = ?", (*args, edge_id))
        return True
    except sqlite3.IntegrityError:
        r = conn.execute(f"SELECT {_EDGE_COLS} FROM memory_links WHERE id = ?", (edge_id,)).fetchone()
        conn.execute("DELETE FROM memory_links WHERE id = ?", (edge_id,))
        if dropped is not None and r is not None:
            dropped.append(_edge_dict(r, reason))
        return False


def rename_endpoint(conn: sqlite3.Connection, tenant: str, tier: str, category: str,
                    old_key: str, new_key: str, *, entity_id: str | None = None,
                    dropped: list | None = None) -> int:
    """Rewrite BOTH endpoint columns for a renamed node, inside the caller's
    transaction. EVERY code path that renames a business key (UPDATE entities
    SET name / reference_documents SET doc_key / state_documents SET
    document_key) must call this in the same function (a test enforces it).

    ``entity_id`` (entity renames): only edges whose stamp is NULL or equals
    the renamed row's id follow it; an edge stamped with another entity of the
    same name (an archived namesake) stays behind. A rewritten edge that would
    duplicate an existing one (UNIQUE) is dropped and appended to ``dropped``
    instead of failing (and, in adoption, rolling back forever) the caller.
    Returns the number of edge sides rewritten."""
    if not _table_exists(conn, "memory_links"):
        return 0
    stamped = tier == "entity" and entity_id is not None and _has_column(
        conn, "memory_links", "from_entity_id")
    n = 0
    for side in ("from", "to"):
        sql = (f"SELECT id, from_tier, from_category, from_key, to_tier, to_category, to_key "
               f"FROM memory_links WHERE tenant_id = ? AND {side}_tier = ? "
               f"AND {side}_category = ? AND {side}_key = ?")
        args: list[Any] = [tenant, tier, category, old_key]
        if stamped:
            sql += f" AND ({side}_entity_id IS NULL OR {side}_entity_id = ?)"
            args.append(entity_id)
        for r in conn.execute(sql, args).fetchall():
            self_loop = (r[1], r[2], r[3]) == (r[4], r[5], r[6])
            if side == "to" and self_loop:
                continue   # rewritten with its from side, both columns at once
            kw = {"from_key": new_key} if side == "from" else {"to_key": new_key}
            if self_loop:
                kw["to_key"] = new_key
            if rekey_edge(conn, r[0], dropped=dropped, **kw):
                n += 2 if self_loop else 1
    return n


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------

def encode_cursor(created_at: str, edge_id: str) -> str:
    raw = json.dumps([created_at, edge_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Any) -> tuple[str, str] | None:
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ValidationError("cursor is not a value returned by neighbors()")
    try:
        pad = "=" * (-len(cursor) % 4)
        val = json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as e:
        raise ValidationError("cursor is not a value returned by neighbors()") from e
    if (not isinstance(val, list) or len(val) != 2
            or not all(isinstance(v, str) for v in val)):
        raise ValidationError("cursor is not a value returned by neighbors()")
    return val[0], val[1]


def _norm_relations(relation: Any) -> list[str] | None:
    if relation is None:
        return None
    if isinstance(relation, str):
        return [norm_relation(relation)]
    if isinstance(relation, (list, tuple)):
        if not relation:
            return None
        if len(relation) > 50:
            raise ValidationError("relation filter takes at most 50 values")
        return [norm_relation(r) for r in relation]
    raise ValidationError("relation must be a string or a list of strings")


# ``stale`` on rendered edges (neighbors edges, links_of samples) has ONE
# implementation: :data:`STALE_SIDE_SQL` above. neighbors applies it to the
# returned page through :func:`stale_ids`; links_of selects it inline on the
# sampled rows through :func:`stale_column` (stamp set
# AND a live entity at that (category, key) under a different id; dangling and
# NULL-stamp sides are never stale). It reads the columns on every call (no
# per-connection cache), so a store whose stamp columns are added by the
# open-time migration, or by another process, can never read without them.


def _endpoint_states(conn: sqlite3.Connection, tenant: str,
                     nodes: list[tuple[str, str, str]], preview: bool) -> dict[tuple, dict]:
    """Batch existence / status / updated_at (and optional preview) for the
    other endpoints of a page of edges. One query per tier, never per edge.
    Underscore keys are internal and never rendered."""
    out: dict[tuple, dict] = {}
    by_tier: dict[str, set] = {"entity": set(), "reference": set(), "state": set()}
    for n in nodes:
        by_tier[n[0]].add(n)
    if by_tier["entity"]:
        pairs = sorted(by_tier["entity"])
        vals = ",".join("(?,?)" for _ in pairs)
        args: list[Any] = [x for (_t, c, k) in pairs for x in (c, k)]
        cols = ("e.category, e.name, e.status, e.updated_at"
                + (", substr(e.body, 1, ?)" if preview else ""))
        q_args = ([PREVIEW_CHARS * 2] if preview else []) + args + [tenant]
        rows = conn.execute(
            f"WITH want(category, name) AS (VALUES {vals}) "
            f"SELECT {cols} FROM want w JOIN entities e "
            f"ON e.category = w.category AND e.name = w.name AND e.tenant_id = ?",
            q_args,
        ).fetchall()
        for r in rows:
            d = {"exists": True, "status": r[2], "updated_at": r[3]}
            if preview:
                d["preview"] = _preview_text(r[4])
            out[("entity", r[0], r[1])] = d
        missing = [n for n in pairs if n not in out]
        if missing:
            vals = ",".join("(?,?)" for _ in missing)
            args = [x for (_t, c, k) in missing for x in (c, k)]
            rows = conn.execute(
                f"WITH want(category, name) AS (VALUES {vals}) "
                f"SELECT a.category, a.name, MAX(a.archived_at) FROM want w JOIN archived_entities a "
                f"ON a.category = w.category AND a.name = w.name AND a.tenant_id = ? "
                f"GROUP BY a.category, a.name",
                args + [tenant],
            ).fetchall()
            for r in rows:
                out[("entity", r[0], r[1])] = {"exists": False, "archived": True,
                                               "status": "archived", "archived_at": r[2],
                                               "updated_at": r[2]}
    for tier, table, col in (("reference", "reference_documents", "doc_key"),
                             ("state", "state_documents", "document_key")):
        keys = sorted(k for (_t, _c, k) in by_tier[tier])
        if not keys:
            continue
        marks = ",".join("?" for _ in keys)
        extra = ", substr(body, 1, ?)" if preview else ""
        rows = conn.execute(
            f"SELECT {col}, updated_at{extra} FROM {table} WHERE tenant_id = ? AND {col} IN ({marks})",
            ([PREVIEW_CHARS * 2] if preview else []) + [tenant] + keys,
        ).fetchall()
        for r in rows:
            d = {"exists": True, "status": None, "updated_at": r[1]}
            if preview:
                d["preview"] = _preview_text(r[2])
            out[(tier, "", r[0])] = d
    for n in nodes:
        out.setdefault(n, {"exists": False, "status": None, "updated_at": None})
    return out


def _public(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "preview" and not k.startswith("_")}


def _preview_text(raw: Any) -> str:
    if raw is None:
        return ""
    text = " ".join(str(raw).split())
    return text[:PREVIEW_CHARS]


def _node_degree(conn: sqlite3.Connection, tenant: str,
                 node: tuple[str, str, str]) -> tuple[int, int]:
    """(out, in) for one node, index-only, in one statement. A self-loop counts
    once, as out (the same rule neighbors(direction="both") lists it by), so
    out + in is the number of distinct edges touching the node."""
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM memory_links INDEXED BY memory_links_from"
        "         WHERE tenant_id = ? AND from_tier = ? AND from_category = ? AND from_key = ?),"
        "       (SELECT COUNT(*) FROM memory_links INDEXED BY memory_links_to"
        "         WHERE tenant_id = ? AND to_tier = ? AND to_category = ? AND to_key = ?),"
        "       (SELECT COUNT(*) FROM memory_links"
        "         WHERE tenant_id = ? AND from_tier = ? AND from_category = ? AND from_key = ?"
        "           AND to_tier = ? AND to_category = ? AND to_key = ?)",
        (tenant, *node, tenant, *node, tenant, *node, *node)).fetchone()
    return int(r[0]), int(r[1]) - int(r[2])


def neighbors(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str], *,
              relations: list[str] | None, direction: str, tier: str | None,
              category: str | None, limit: int, cursor: tuple[str, str] | None,
              preview: bool) -> dict[str, Any]:
    """One page of ``node``'s edges.

    Each branch is pinned to its endpoint index (``INDEXED BY``): without the
    hint SQLite (no ANALYZE stats) drove the union through
    memory_links_created, a scan of every edge in the tenant per call (83.8 ms
    vs 0.24 ms for one node at 40k edges; 0.3-0.5 s per hop at 200k). The
    cursor predicate sits inside each branch so only rows past the cursor are
    sorted. The envelope's ``node`` carries the queried node's own
    exists/status (archived: true when only an archived row matches), so a
    mistyped or wrong-case name is distinguishable from an isolated node."""
    tier_n, cat_n, key_n = node
    branches = []
    args: list[Any] = []

    def _filters(other: str) -> tuple[str, list[Any]]:
        sql, fargs = "", []
        if relations:
            sql += f" AND relation_type IN ({','.join('?' for _ in relations)})"
            fargs += relations
        if tier is not None:
            sql += f" AND {other}_tier = ?"
            fargs.append(tier)
        if category is not None:
            sql += f" AND {other}_category = ?"
            fargs.append(category)
        if cursor is not None:
            sql += " AND (created_at > ? OR (created_at = ? AND id > ?))"
            fargs += [cursor[0], cursor[0], cursor[1]]
        return sql, fargs

    if direction in ("out", "both"):
        f_sql, f_args = _filters("to")
        branches.append(
            "SELECT id, 'out' AS direction, relation_type, to_tier AS o_tier, "
            "to_category AS o_cat, to_key AS o_key, metadata, created_at"
            " FROM memory_links INDEXED BY memory_links_from "
            "WHERE tenant_id = ? AND from_tier = ? AND from_category = ? AND from_key = ?" + f_sql)
        args += [tenant, tier_n, cat_n, key_n] + f_args
    if direction in ("in", "both"):
        f_sql, f_args = _filters("from")
        # A self-loop is listed once (as "out") when both directions are asked.
        self_skip = (" AND NOT (from_tier = to_tier AND from_category = to_category "
                     "AND from_key = to_key)") if direction == "both" else ""
        branches.append(
            "SELECT id, 'in', relation_type, from_tier, from_category, from_key, "
            "metadata, created_at"
            " FROM memory_links INDEXED BY memory_links_to "
            "WHERE tenant_id = ? AND to_tier = ? AND to_category = ? AND to_key = ?"
            + self_skip + f_sql)
        args += [tenant, tier_n, cat_n, key_n] + f_args
    sql = " UNION ALL ".join(branches) + " ORDER BY created_at, id LIMIT ?"
    args.append(limit + 1)
    rows = conn.execute(sql, args).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    states = _endpoint_states(conn, tenant, [node] + [(r[3], r[4], r[5]) for r in rows], preview)
    me = states.get(node, {})
    # stale for the returned page only (O(page) probes, not O(degree): a
    # SELECT-list subquery would run for every row the sorter sees).
    stale = stale_ids(conn, tenant, [r[0] for r in rows])
    edges = []
    for r in rows:
        other = (r[3], r[4], r[5])
        st = states.get(other, {})
        node_out = node_ref(*other)
        node_out.update(_public(st))
        e = {"id": r[0], "direction": r[1], "relation": r[2], "node": node_out,
             "metadata": loads(r[6]), "created_at": r[7], "stale": r[0] in stale}
        if preview:
            e["preview"] = st.get("preview", "")
        edges.append(e)
    deg_out, deg_in = _node_degree(conn, tenant, node)
    nxt = encode_cursor(rows[-1][7], rows[-1][0]) if more and rows else None
    head = node_ref(*node)
    head.update(_public(me))
    head.setdefault("archived", False)
    return {"node": head, "edges": edges,
            "degree": {"out": deg_out, "in": deg_in}, "next_cursor": nxt}


def links_of(conn: sqlite3.Connection, tenant: str, nodes: list[tuple[str, str, str]],
             per_node: int) -> dict[tuple, dict[str, Any]]:
    """Search-hit edge summary for a list of nodes, index-only, O(degree) per
    node and a constant number of statements whatever the number of nodes (2.C:
    hit decoration must never be N queries).

    Statement 1 groups each node's edges by (direction, relation) on the
    covering endpoint indexes (COUNT + MAX(rowid), nothing read from the table)
    plus a third branch on the UNIQUE autoindex that finds self-loops, which
    count once, as out (parity with neighbors). Statement 2 reads ONE
    representative row per group by rowid (the newest written: operator
    decision 3, one sample edge per relation, replacing the full-partition
    window sort that cost 68 ms per hit at a 5,000-edge hub). ``hub: true`` is
    set when either direction exceeds the lint ``hub-degree`` threshold.
    ``sample`` is capped at ``per_node`` groups, largest group first, out
    before in, then relation name; ``by_relation`` follows the same order."""
    uniq = list(dict.fromkeys(nodes))
    out: dict[tuple, dict[str, Any]] = {
        n: {"degree": {"out": 0, "in": 0}, "by_relation": {}, "sample": []} for n in uniq}
    if not uniq:
        return out
    per = max(0, int(per_node))
    vals = ",".join("(?,?,?)" for _ in uniq)
    nargs: list[Any] = [x for n in uniq for x in n]
    # CORE-3: tenant clause on every branch. CROSS JOIN keeps `want` the outer
    # loop (SQLite never reorders a CROSS JOIN); INDEXED BY pins the per-node
    # lookup to the covering endpoint index (without it the planner drove the
    # "in" half through memory_links_created, a tenant scan).
    sql = (
        f"WITH want(t, c, k) AS (VALUES {vals}) "
        "SELECT w.t, w.c, w.k, 'out', l.relation_type, COUNT(*), MAX(l.rowid)"
        "  FROM want w CROSS JOIN memory_links l INDEXED BY memory_links_from"
        "  ON l.tenant_id = ? AND l.from_tier = w.t AND l.from_category = w.c AND l.from_key = w.k"
        "  GROUP BY w.t, w.c, w.k, l.relation_type"
        " UNION ALL "
        "SELECT w.t, w.c, w.k, 'in', l.relation_type, COUNT(*), MAX(l.rowid)"
        "  FROM want w CROSS JOIN memory_links l INDEXED BY memory_links_to"
        "  ON l.tenant_id = ? AND l.to_tier = w.t AND l.to_category = w.c AND l.to_key = w.k"
        "  GROUP BY w.t, w.c, w.k, l.relation_type"
        " UNION ALL "
        "SELECT w.t, w.c, w.k, 'self', l.relation_type, COUNT(*), MAX(l.rowid)"
        "  FROM want w CROSS JOIN memory_links l"
        "  ON l.tenant_id = ? AND l.from_tier = w.t AND l.from_category = w.c AND l.from_key = w.k"
        "  AND l.to_tier = w.t AND l.to_category = w.c AND l.to_key = w.k"
        "  GROUP BY w.t, w.c, w.k, l.relation_type")
    groups: dict[tuple, dict[tuple, list]] = {n: {} for n in uniq}
    selfs: dict[tuple, dict[str, tuple[int, int]]] = {n: {} for n in uniq}
    for r in conn.execute(sql, nargs + [tenant, tenant, tenant]).fetchall():
        n = (r[0], r[1], r[2])
        if r[3] == "self":
            selfs[n][r[4]] = (int(r[5]), int(r[6]))
        else:
            groups[n][(r[3], r[4])] = [int(r[5]), int(r[6])]
    redo: list[tuple[tuple, str]] = []
    for n in uniq:
        g = groups[n]
        for rel, (sn, srow) in selfs[n].items():
            grp = g.get(("in", rel))
            if grp is None:
                continue
            grp[0] -= sn
            if grp[0] <= 0:
                del g[("in", rel)]
            elif grp[1] == srow or sn > 1:
                # The newest in-edge of this relation may be a self-loop; pick
                # the newest non-self one instead (rare: a node with self-loops
                # AND in-edges of the same relation).
                redo.append((n, rel))
        s = out[n]
        # One order for by_relation and sample (Fable consult Q3): largest
        # group first, out before in, then relation name. Deterministic and
        # independent of rowid, so unrelated writes never reshuffle it.
        ranked = sorted(g.items(), key=lambda kv: (-kv[1][0], kv[0][0] != "out", kv[0][1]))
        groups[n] = dict(ranked)
        for (d, rel), (cnt, _row) in ranked:
            s["degree"][d] += cnt
            s["by_relation"][rel] = s["by_relation"].get(rel, 0) + cnt
        if s["degree"]["out"] > HUB_DEGREE_WARN or s["degree"]["in"] > HUB_DEGREE_WARN:
            s["hub"] = True
    for n, rel in redo:
        r = conn.execute(
            "SELECT MAX(rowid) FROM memory_links INDEXED BY memory_links_to"
            " WHERE tenant_id = ? AND to_tier = ? AND to_category = ? AND to_key = ?"
            " AND relation_type = ? AND rowid NOT IN ("
            "  SELECT rowid FROM memory_links WHERE tenant_id = ? AND from_tier = ?"
            "  AND from_category = ? AND from_key = ? AND to_tier = ? AND to_category = ?"
            "  AND to_key = ? AND relation_type = ?)",
            (tenant, *n, rel, tenant, *n, *n, rel)).fetchone()
        groups[n][("in", rel)][1] = int(r[0])
    if per == 0:
        return out
    picks: dict[tuple, list[tuple[str, str, int]]] = {}
    for n in uniq:
        picks[n] = [(d, rel, row) for (d, rel), (_c, row) in list(groups[n].items())[:per]]
    rowids = sorted({row for p in picks.values() for (_d, _r, row) in p})
    if not rowids:
        return out
    cols = ("rowid, from_tier, from_category, from_key, to_tier, to_category, to_key, "
            + stale_column(conn, "memory_links"))
    rows: dict[int, Any] = {}
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        for r in conn.execute(
                # `+tenant_id`: keep the tenant check (CORE-3) but out of index
                # selection, so the lookup is by rowid, not a tenant scan.
                f"SELECT {cols} FROM memory_links WHERE +tenant_id = ? AND rowid IN "
                f"({','.join('?' for _ in chunk)})", [tenant] + chunk).fetchall():
            rows[int(r[0])] = r
    for n in uniq:
        for d, rel, row in picks[n]:
            r = rows.get(row)
            if r is None:
                continue
            src, dst = (r[1], r[2], r[3]), (r[4], r[5], r[6])
            e = {"relation": rel, "direction": d, "node": node_ref(*(dst if d == "out" else src)),
                 "stale": bool(r[7])}
            out[n]["sample"].append(e)
    if _flags.on("SIBYL_MEMORY_HOP_OWNERS"):
        ents = [n for n in uniq if n[0] == "entity" and ("in", "source_of") in groups[n]]
        if ents:
            own = owners_of(conn, tenant, ents)
            for n in ents:
                lst = own.get(n) or []
                if not lst:
                    continue
                top = [{"relation": "source_of", "direction": "in",
                        "node": node_ref("entity", o["kind"], o["key"]),
                        "doc_count": o["doc_count"]} for o in lst[:3]]
                rest = [e for e in out[n]["sample"]
                        if not (e["relation"] == "source_of" and e["direction"] == "in")]
                out[n]["sample"] = top + rest
                out[n]["owners_total"] = len(lst)
    return out


# --- owner primitives: a document's owning hubs and small-hub siblings ---------

OWNER_KIND_RANK = {"ticket": 0, "customer": 1, "project": 2, "person": 3}
_VALUES_CHUNK = 400
_OWNER_COUNT_CAP = 1000


def owners_of(conn: sqlite3.Connection, tenant: str,
              nodes: list[tuple[str, str, str]]) -> dict[tuple, list[dict]]:
    """For each entity node, its source_of owners (in-edges), each with its doc_count
    (index-only COUNT of the owner's source_of out-edges), sorted by
    (doc_count asc, OWNER_KIND_RANK asc, key asc). Two statements for any number of nodes."""
    res: dict[tuple, list[dict]] = {n: [] for n in nodes}
    ents = list(dict.fromkeys(n for n in nodes if n[0] == "entity"))
    if not ents:
        return res
    by_ck: dict[tuple[str, str], list[tuple]] = {}
    for n in ents:
        by_ck.setdefault((n[1], n[2]), []).append(n)
    owners: dict[tuple[str, str], list[tuple[str, str]]] = {}
    pairs = list(by_ck)
    for i in range(0, len(pairs), _VALUES_CHUNK):
        chunk = pairs[i:i + _VALUES_CHUNK]
        vals = ",".join("(?,?)" for _ in chunk)
        sql = (f"WITH want(c, k) AS (VALUES {vals}) "
               "SELECT w.c, w.k, l.from_category, l.from_key"
               "  FROM want w CROSS JOIN memory_links l INDEXED BY memory_links_to"
               "  ON l.tenant_id = ? AND l.to_tier = 'entity' AND l.to_category = w.c AND l.to_key = w.k"
               "     AND l.relation_type = 'source_of'"
               " WHERE l.from_tier = 'entity'")
        for r in conn.execute(sql, [x for ck in chunk for x in ck] + [tenant]).fetchall():
            owners.setdefault((r[0], r[1]), []).append((r[2], r[3]))
    distinct = list(dict.fromkeys(o for lst in owners.values() for o in lst))
    deg: dict[tuple[str, str], int] = {}
    # Latency (2026-10-25): the exact COUNT is index-only but O(hub size); 30 document hits can
    # reach ~100 distinct owners summing ~216k edges (165 ms). The COUNT is therefore capped at
    # _OWNER_COUNT_CAP + 1 entries; an owner that reaches the cap takes the hub card's body.doc_count
    # (an importer that writes hub cards keeps it equal to the edge count), and falls back to the exact
    # COUNT when the card has none. Every count <= the cap is the exact COUNT, so the ordering and all
    # small-hub decisions are unchanged.
    capped: list[tuple[str, str]] = []
    for i in range(0, len(distinct), _VALUES_CHUNK):
        chunk = distinct[i:i + _VALUES_CHUNK]
        vals = ",".join("(?,?)" for _ in chunk)
        sql = (f"WITH want(c, k) AS (VALUES {vals}) "
               "SELECT w.c, w.k, (SELECT COUNT(*) FROM (SELECT 1 FROM memory_links l INDEXED BY memory_links_from"
               "  WHERE l.tenant_id = ? AND l.from_tier = 'entity' AND l.from_category = w.c AND l.from_key = w.k"
               "     AND l.relation_type = 'source_of' LIMIT ?))"
               "  FROM want w")
        for r in conn.execute(sql, [x for ck in chunk for x in ck] + [tenant, _OWNER_COUNT_CAP + 1]).fetchall():
            n = int(r[2])
            if n > _OWNER_COUNT_CAP:
                capped.append((r[0], r[1]))
            elif n:
                deg[(r[0], r[1])] = n
    for i in range(0, len(capped), _VALUES_CHUNK):
        chunk = capped[i:i + _VALUES_CHUNK]
        vals = ",".join("(?,?)" for _ in chunk)
        for c, k, dc in conn.execute(
                f"WITH want(c, k) AS (VALUES {vals}) "
                "SELECT w.c, w.k, json_extract(e.body, '$.doc_count')"
                "  FROM want w CROSS JOIN entities e"
                "  ON e.tenant_id = ? AND e.category = w.c AND e.name = w.k",
                [x for ck in chunk for x in ck] + [tenant]).fetchall():
            if isinstance(dc, int) and dc > _OWNER_COUNT_CAP:
                deg[(c, k)] = dc
        for ck in chunk:
            if ck not in deg:   # no usable card count: the exact COUNT
                deg[ck] = int(conn.execute(
                    "SELECT COUNT(*) FROM memory_links INDEXED BY memory_links_from"
                    " WHERE tenant_id = ? AND from_tier = 'entity' AND from_category = ? AND from_key = ?"
                    "   AND relation_type = 'source_of'", (tenant, ck[0], ck[1])).fetchone()[0])
    for ck, ns in by_ck.items():
        lst = [{"kind": c, "key": k, "doc_count": deg.get((c, k), 0)}
               for (c, k) in dict.fromkeys(owners.get(ck, []))]
        lst.sort(key=lambda o: (o["doc_count"], OWNER_KIND_RANK.get(o["kind"], 4), o["key"]))
        for n in ns:
            res[n] = [dict(o) for o in lst]
    return res


def hub_members(conn: sqlite3.Connection, tenant: str, kind: str, key: str, *,
                limit: int, exclude_key: str | None) -> list[dict]:
    """source_of out-edges of one hub in neighbors() order, as
    {"dsid": to_key, "category": to_category, "date", "role", "title"} (title cut to 100 chars),
    excluding exclude_key. Reads limit+1 rows so the caller can tell truncation."""
    rows = conn.execute(
        "SELECT to_category, to_key, metadata FROM memory_links INDEXED BY memory_links_from"
        " WHERE tenant_id = ? AND from_tier = 'entity' AND from_category = ? AND from_key = ?"
        "   AND relation_type = 'source_of'"
        " ORDER BY created_at, id LIMIT ?", (tenant, kind, key, int(limit) + 1)).fetchall()
    out: list[dict] = []
    for cat, k, meta in rows:
        if exclude_key is not None and k == exclude_key:
            continue
        m = loads(meta) if meta else {}
        if not isinstance(m, dict):
            m = {}
        title = m.get("title")
        if isinstance(title, str):
            title = title[:100]
        out.append({"dsid": k, "category": cat, "date": m.get("date"),
                    "role": m.get("role"), "title": title})
    return out


def doc_neighborhood(conn: sqlite3.Connection, tenant: str, node: tuple[str, str, str], *,
                     small_hub_max: int, sibling_cap: int) -> dict | None:
    """The recall neighborhood of one document entity ({hubs, siblings, siblings_omitted}),
    or None when it has no source_of owner."""
    owners = owners_of(conn, tenant, [node]).get(node) or []
    if not owners:
        return None
    hubs = [dict(o) for o in owners[:10]]
    siblings: list[dict] = []
    omitted = 0
    used = 0
    for o in owners:
        if o["doc_count"] > small_hub_max:
            continue
        rows = hub_members(conn, tenant, o["kind"], o["key"], limit=o["doc_count"],
                           exclude_key=node[2])
        if len(rows) + used <= sibling_cap:
            siblings.append({"hub": dict(o), "rows": rows, "total": o["doc_count"] - 1})
            used += len(rows)
        else:
            omitted += 1
    return {"hubs": hubs, "siblings": siblings, "siblings_omitted": omitted}


__all__ = [
    "DIRECTIONS", "EDGE_METADATA_MAX_BYTES", "HUB_DEGREE_WARN", "MAX_LINKS_PER_WRITE",
    "RELATION_MAX_CHARS", "TIERS", "V6_DDL", "apply_v6_migration", "cascade_node",
    "links_of", "needs_v6_migration", "neighbors", "norm_link_specs", "norm_node",
    "rename_endpoint", "IDENTITY_COLUMNS", "apply_identity_columns", "needs_identity_columns",
    "rekey_edge", "stale_column", "stale_ids", "stale_sql", "STALE_SIDE_SQL",
    "OWNER_KIND_RANK", "owners_of", "hub_members", "doc_neighborhood",
]
