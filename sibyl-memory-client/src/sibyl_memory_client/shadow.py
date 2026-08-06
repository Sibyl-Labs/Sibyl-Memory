"""Folded-trigram search shadow (v0.5.0 multi-language search, spec §4.2).

A single standalone FTS5 ``trigram`` virtual table (``search_shadow``) holding a
FOLDED rendering of the searchable text across all four tiers (entity / state /
reference / journal), maintained by DB-side triggers and consulted ONLY as a
zero-hit fallback in ``MemoryClient.search()`` (client.py §4.3). The primary
``porter unicode61`` pipeline is never touched, so the fallback is strictly
additive: it fires only where today's answer is ``[]``.

Why a shadow (and why THIS shape):
  * ``trigram`` gives SUBSTRING semantics, which is the only thing that can match
    inside an unbroken indexed token — CJK scriptio-continua (``北京`` inside
    ``北京烤鸭``), Thai fragment glue, Zulu/Bantu locative compounds, German
    compounds — none of which any query-side change to ``porter unicode61`` can
    reach (a prefix ``*`` covers leading substrings only).
  * A FOLDED copy closes the non-decomposable gap (``ł ß ø æ đ ı œ þ ð``): no
    ``remove_diacritics`` setting folds these, so ``Belzyce`` cannot find a stored
    ``Bełżyce`` without an explicit fold map applied on BOTH sides. External-
    content trigram can't carry a divergent folded copy; a standalone table can.
  * ``trigram`` is built into SQLite (>= 3.34; >= 3.45 for ``remove_diacritics``);
    no native/loadable extension, so the plugin keeps shipping pure-Python wheels.

The fold map + runtime tokenizer clause are the single source of truth for both
the trigger/backfill SQL (index side) and the query side (``fold_py``); keeping
them here — not in the static ``schema.sql`` — is why the shadow DDL is generated.

Business-key linkage (NOT ``content_rowid``): shadow rows carry the base-table
business key, so a VACUUM that renumbers rowids can never desync the shadow from
its base table.

TRAP (do not reintroduce): the shadow tables are maintained with PLAIN INSERT and
PLAIN DELETE. The external-content ``INSERT INTO x(x, rowid, ...) VALUES('delete',
...)`` idiom fed by a ``SELECT`` from the shadow FAILS inside a trigger body
("SQL logic error": an FTS5 table cannot be read from within a trigger). A
standalone FTS5 table supports native ``DELETE`` — use it. And NEVER write to the
base tables with ``INSERT OR REPLACE``: with ``recursive_triggers`` off it skips
the DELETE triggers and silently desyncs the shadow. The existing write paths are
safe (set_entity does explicit INSERT-else-UPDATE; state/reference use
``ON CONFLICT ... DO UPDATE`` -> a true UPDATE fires the AU trigger).
"""
from __future__ import annotations

import json as _json
import re
import sqlite3

# ---------------------------------------------------------------------------
# Fold map — non-decomposables ONLY. Decomposable diacritics (é ñ ö ż ...) are
# the tokenizer's job (``remove_diacritics``), and fold_py deliberately does NOT
# touch them so the query side and the index side fold IDENTICALLY (both get the
# tokenizer's decomposable folding, or neither does on pre-3.45 SQLite). This map
# is applied identically by fold_sql (index side, in trigger + backfill SQL) and
# fold_py (query side).
# ---------------------------------------------------------------------------
FOLD_MAP = {
    "ł": "l",  "Ł": "l",    # ł Ł
    "ß": "ss", "ẞ": "ss",   # ß ẞ
    "ø": "o",  "Ø": "o",    # ø Ø
    "æ": "ae", "Æ": "ae",   # æ Æ
    "đ": "d",  "Đ": "d",    # đ Đ
    "ı": "i",                     # ı (dotless i; İ handled by tokenizer fold)
    "œ": "oe", "Œ": "oe",   # œ Œ
    "þ": "th", "Þ": "th",   # þ Þ
    "ð": "d",  "Ð": "d",    # ð Ð
}

SHADOW_TABLE = "search_shadow"

# The four searchable tiers this shadow mirrors.
_TIERS = ("entity", "state", "reference", "journal")


def fold_sql(expr: str) -> str:
    """SQL expression applying ASCII ``lower()`` + FOLD_MAP to ``expr``.

    Wraps ``lower(expr)`` in one nested ``replace()`` per FOLD_MAP entry. SQLite's
    built-in ``lower()`` folds ASCII only; the trigram tokenizer applies unicode
    case folding (and, on >= 3.45, diacritic removal) on top when it indexes the
    stored text, which is exactly the symmetry ``fold_py`` preserves on the query
    side. FOLD_MAP sources/targets are fixed ASCII/Latin-1 literals (never user
    input), so this string interpolation is injection-safe.
    """
    out = f"lower({expr})"
    for src, dst in FOLD_MAP.items():
        out = f"replace({out}, '{src}', '{dst}')"
    return out


def fold_py(text: str) -> str:
    """Query-side twin of ``fold_sql``: ASCII-lower + FOLD_MAP, nothing else.

    NEVER strips decomposable accents — that stays the tokenizer's job on BOTH
    sides (see FOLD_MAP note). ASCII case-folds; non-ASCII letters are left as-is
    for the trigram tokenizer to case/diacritic fold, keeping query and index
    folding identical.
    """
    out = text.lower() if text.isascii() else "".join(
        c.lower() if c.isascii() else c for c in text)
    for src, dst in FOLD_MAP.items():
        out = out.replace(src, dst)
    return out


def trigram_tokenizer_clause() -> str:
    """Runtime-selected tokenizer clause across the SQLite 3.45 boundary.

    ``remove_diacritics`` reached the ``trigram`` tokenizer in 3.45.0. On older
    SQLite the option is rejected at vtable construction ("unrecognized"), so we
    fall back to a bare ``trigram``. The only degradation on pre-3.45 SQLite is
    accent-insensitive SUBSTRING matching in the fallback; whole-word accented
    matching still works via the primary porter-unicode61 index, and fold_py's
    non-decomposable map is unaffected.
    """
    if sqlite3.sqlite_version_info >= (3, 45, 0):
        return "tokenize = 'trigram remove_diacritics 1'"
    return "tokenize = 'trigram'"


# ---------------------------------------------------------------------------
# DDL / DML generators (single source of truth, consumed by storage.py migration)
# ---------------------------------------------------------------------------
def create_table_sql() -> str:
    """CREATE for the unified shadow table with the runtime tokenizer clause."""
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {SHADOW_TABLE} USING fts5(\n"
        "  txt, tier UNINDEXED, k1 UNINDEXED, k2 UNINDEXED, tenant_id UNINDEXED,\n"
        f"  {trigram_tokenizer_clause()}\n"
        ")"
    )


# Per-tier fold expressions, IDENTICAL on the trigger side (new./old.) and the
# backfill side (bare columns). Mirrors the base-table columns each tier's
# primary FTS5 index feeds on; journal reuses the exact concat of
# journal_events_ai_fts.
def _entity_txt(p: str) -> str:
    return fold_sql(f"{p}name || ' ' || {p}category || ' ' || {p}body")


def _state_txt(p: str) -> str:
    return fold_sql(f"{p}document_key || ' ' || {p}body")


def _reference_txt(p: str) -> str:
    return fold_sql(f"{p}doc_key || ' ' || COALESCE({p}body, '')")


def _journal_txt(p: str) -> str:
    return fold_sql(
        f"COALESCE({p}evaluated, '') || ' ' || COALESCE({p}acted, '') || ' ' || "
        f"COALESCE({p}forward, '') || ' ' || COALESCE({p}extra, '')"
    )


def trigger_sqls() -> list[str]:
    """The shadow-maintenance triggers.

    entity / state / reference each get AFTER INSERT / UPDATE / DELETE; journal
    gets AFTER INSERT only (append-only — mirrors ``journal_events_ai_fts``, which
    also has no AU/AD). All use PLAIN INSERT / PLAIN DELETE keyed on the tier's
    BUSINESS key (see module docstring TRAP note). Ordering the DELETE before the
    re-INSERT in the AU triggers keeps a rename/rekey clean.
    """
    stmts: list[str] = []

    # --- entities: business key (tenant_id, category, name) = (tenant, k1, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_ai_shadow
AFTER INSERT ON entities BEGIN
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_entity_txt('new.')}, 'entity', new.category, new.name, new.tenant_id);
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_ad_shadow
AFTER DELETE ON entities BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'entity' AND k1 = old.category AND k2 = old.name
         AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_au_shadow
AFTER UPDATE ON entities BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'entity' AND k1 = old.category AND k2 = old.name
         AND tenant_id = old.tenant_id;
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_entity_txt('new.')}, 'entity', new.category, new.name, new.tenant_id);
END""")

    # --- state_documents: business key (tenant_id, document_key) = (tenant, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_ai_shadow
AFTER INSERT ON state_documents BEGIN
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_state_txt('new.')}, 'state', '', new.document_key, new.tenant_id);
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_ad_shadow
AFTER DELETE ON state_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'state' AND k2 = old.document_key AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_au_shadow
AFTER UPDATE ON state_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'state' AND k2 = old.document_key AND tenant_id = old.tenant_id;
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_state_txt('new.')}, 'state', '', new.document_key, new.tenant_id);
END""")

    # --- reference_documents: business key (tenant_id, doc_key) = (tenant, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_ai_shadow
AFTER INSERT ON reference_documents BEGIN
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_reference_txt('new.')}, 'reference', '', new.doc_key, new.tenant_id);
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_ad_shadow
AFTER DELETE ON reference_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'reference' AND k2 = old.doc_key AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_au_shadow
AFTER UPDATE ON reference_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'reference' AND k2 = old.doc_key AND tenant_id = old.tenant_id;
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_reference_txt('new.')}, 'reference', '', new.doc_key, new.tenant_id);
END""")

    # --- journal_events: append-only, AI only. Business key (tenant_id, id).
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS journal_events_ai_shadow
AFTER INSERT ON journal_events BEGIN
  INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)
  VALUES ({_journal_txt('new.')}, 'journal', '', new.id, new.tenant_id);
END""")

    return stmts


def backfill_sqls() -> list[str]:
    """One INSERT..SELECT per tier, folding with the SAME expressions the
    triggers use so a backfilled row is byte-identical to a trigger-written one."""
    return [
        f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id) "
        f"SELECT {_entity_txt('')}, 'entity', category, name, tenant_id FROM entities",
        f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id) "
        f"SELECT {_state_txt('')}, 'state', '', document_key, tenant_id FROM state_documents",
        f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id) "
        f"SELECT {_reference_txt('')}, 'reference', '', doc_key, tenant_id FROM reference_documents",
        f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id) "
        f"SELECT {_journal_txt('')}, 'journal', '', id, tenant_id FROM journal_events",
    ]


def shadow_table_exists(conn: sqlite3.Connection) -> bool:
    """True iff the shadow virtual table is present (cheap sqlite_master lookup)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (SHADOW_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def apply_shadow_migration(conn: sqlite3.Connection) -> None:
    """Create the shadow table + triggers and (re)backfill all four tiers.

    Runs via INDIVIDUAL ``execute`` statements (NOT ``executescript``, which
    issues an implicit COMMIT and would break the caller's BEGIN IMMEDIATE). The
    caller (storage.py) wraps this in one transaction and stamps the schema
    marker in the SAME transaction, so a crash anywhere rolls the whole thing
    back and the next open retries — idempotent by construction. Clearing with
    ``DELETE`` before the backfill keeps a re-run (heal / crash-retry)
    duplicate-free.
    """
    conn.execute(create_table_sql())
    for stmt in trigger_sqls():
        conn.execute(stmt)
    conn.execute(f"DELETE FROM {SHADOW_TABLE}")
    for stmt in backfill_sqls():
        conn.execute(stmt)


def rebuild_shadow(conn: sqlite3.Connection) -> None:
    """Clear + re-backfill the shadow from the base tables (heal/rebuild path).

    No-op when the shadow table is absent (e.g. a fresh DB whose shadow is
    created by the later v4 migration step, so the v3 FTS-rebuild that also calls
    this must not fail). Idempotent."""
    if not shadow_table_exists(conn):
        return
    conn.execute(f"DELETE FROM {SHADOW_TABLE}")
    for stmt in backfill_sqls():
        conn.execute(stmt)


def drop_shadow(conn: sqlite3.Connection) -> None:
    """Drop the shadow table + all its triggers. Used by the portability heal and
    documented as the rollback recovery (restores exact v3 behaviour when paired
    with ``PRAGMA user_version = 3``). Base-table data is never touched."""
    for name in (
        "entities_ai_shadow", "entities_au_shadow", "entities_ad_shadow",
        "state_documents_ai_shadow", "state_documents_au_shadow",
        "state_documents_ad_shadow",
        "reference_documents_ai_shadow", "reference_documents_au_shadow",
        "reference_documents_ad_shadow",
        "journal_events_ai_shadow",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP TABLE IF EXISTS {SHADOW_TABLE}")


# Portability / corruption error markers: a DB created on SQLite >= 3.45 (tokenizer
# clause 'trigram remove_diacritics 1') opened on < 3.45 fails vtable construction
# with an "unrecognized"-class message; a corrupt shadow surfaces as a malformed /
# vtable-constructor error. Both are HEALABLE by dropping and recreating the
# shadow with the locally-supported clause. Matched case-insensitively.
_HEALABLE_MARKERS = (
    "unrecognized", "no such tokenize", "no such module",
    "vtable constructor", "malformed", "not a database",
)


def _is_healable(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _HEALABLE_MARKERS)


def _heal(conn: sqlite3.Connection) -> None:
    """Best-effort portability heal: rebuild the shadow with the local tokenizer
    clause. Runs inside the §4.2 containment so a failure here can never crash
    search. Wrapped in its own transaction; swallows any error."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        drop_shadow(conn)
        conn.execute(create_table_sql())
        for stmt in trigger_sqls():
            conn.execute(stmt)
        for stmt in backfill_sqls():
            conn.execute(stmt)
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


_LIKE_ESC = re.compile(r"([%_\\])")


def _tier_filter(allowed: set[str]) -> tuple[str, list[str]]:
    """Return (`` AND tier IN (?,...)``, params) restricting to ``allowed`` tiers,
    or ("", []) when all four tiers are allowed (no clause needed)."""
    if not allowed or allowed >= set(_TIERS):
        return "", []
    ordered = [t for t in _TIERS if t in allowed]
    placeholders = ", ".join("?" for _ in ordered)
    return f" AND tier IN ({placeholders})", ordered


def _shape_hit(conn: sqlite3.Connection, tenant_id: str, tier: str,
               k1: str, k2: str, txt: str, rank) -> dict | None:
    """Join a shadow row back to its base table by business key and return the
    exact dict shape ``_search_strict`` produces for that tier. Returns None when
    the base record no longer resolves (the shadow raced a delete) — the caller
    skips it. ``snippet`` = first ~120 chars of the folded text; ``rank`` = shadow
    BM25 (only ever surfaced when the result set would otherwise be empty, so
    cross-scale comparison with the primary index never arises)."""
    snippet = txt[:120]
    if tier == "entity":
        row = conn.execute(
            "SELECT body, updated_at FROM entities "
            "WHERE tenant_id = ? AND category = ? AND name = ?",
            (tenant_id, k1, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "entity", "key": k2, "category": k1,
                "body": _json.loads(row[0]), "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "state":
        row = conn.execute(
            "SELECT body, updated_at FROM state_documents "
            "WHERE tenant_id = ? AND document_key = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "state", "key": k2, "category": None,
                "body": _json.loads(row[0]), "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "reference":
        row = conn.execute(
            "SELECT body, updated_at FROM reference_documents "
            "WHERE tenant_id = ? AND doc_key = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "reference", "key": k2, "category": None,
                "body": row[0], "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "journal":
        row = conn.execute(
            "SELECT ts, evaluated, acted, forward, extra FROM journal_events "
            "WHERE tenant_id = ? AND id = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "journal", "key": k2, "category": None,
                "body": {
                    "evaluated": _json.loads(row[1]) if row[1] else None,
                    "acted": _json.loads(row[2]) if row[2] else None,
                    "forward": _json.loads(row[3]) if row[3] else None,
                    "extra": _json.loads(row[4]) if row[4] else None,
                },
                "snippet": snippet, "rank": rank, "ts": row[0]}
    return None


def shadow_search(conn: sqlite3.Connection, tenant_id: str, query: str,
                  *, limit: int = 20, tiers: tuple[str, ...] | None = None) -> list[dict]:
    """Folded-trigram substring fallback. Returns ``_search_strict``-shaped dicts.

    Substring semantics via the trigram MATCH (>=3-char folded tokens) with a
    bounded LIKE post-filter/scan for 1-2 char non-ASCII tokens (the CJK 2-char
    case — trigram MATCH cannot see below 3 chars). Short ASCII tokens are
    stopword-grade noise and dropped. A no-op ``[]`` when the shadow table is
    absent (pre-migration DB) so it is safe to call unconditionally.
    """
    if limit <= 0:  # CORE-5: a non-positive limit must never broaden — mirror clamp
        return []
    if not shadow_table_exists(conn):
        return []
    allowed = set(tiers) if tiers else set(_TIERS)
    allowed &= set(_TIERS)
    if not allowed:
        return []

    folded = fold_py(query)
    toks = [t for t in re.findall(r"\w+", folded) if t]
    match_toks = [t for t in toks if len(t) >= 3]
    # short non-ASCII tokens are real words (2-char CJK/Hangul); short ASCII is noise
    like_toks = [t for t in toks if len(t) < 3 and not t.isascii()]
    if not match_toks and not like_toks:
        return []

    tier_clause, tier_params = _tier_filter(allowed)
    fetch = max(limit, 1) * 4

    try:
        rows: list = []
        if match_toks:
            mq = " ".join('"' + t.replace('"', '""') + '"' for t in match_toks)
            # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
            # `AND tenant_id = ?` is the ONLY thing keeping this query inside the
            # caller's tenant. tenant_id is UNINDEXED in the shadow FTS5 table, so
            # this is a trailing post-filter, NOT index-enforced isolation. DO NOT
            # remove, reorder, or make this clause conditional. Covered by the
            # cross-tenant leak test (test_trigram_shadow_2026_08_06).
            rows = conn.execute(
                f"SELECT txt, tier, k1, k2, rank FROM {SHADOW_TABLE} "
                f"WHERE {SHADOW_TABLE} MATCH ? AND tenant_id = ?" + tier_clause +
                " ORDER BY rank LIMIT ?",
                [mq, tenant_id, *tier_params, fetch],
            ).fetchall()
            if like_toks:
                rows = [r for r in rows if all(t in r[0] for t in like_toks)]
        elif like_toks:
            conds = " AND ".join(f"txt LIKE ? ESCAPE '\\'" for _ in like_toks)
            like_params = ["%" + _LIKE_ESC.sub(r"\\\1", t) + "%" for t in like_toks]
            # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
            # `tenant_id = ?` is the ONLY tenant boundary (tenant_id UNINDEXED ->
            # trailing post-filter, not index-enforced). DO NOT remove/reorder/
            # conditionalize. Covered by test_trigram_shadow_2026_08_06.
            rows = conn.execute(
                f"SELECT txt, tier, k1, k2, 0.0 AS rank FROM {SHADOW_TABLE} "
                f"WHERE tenant_id = ?" + tier_clause + " AND " + conds + " LIMIT ?",
                [tenant_id, *tier_params, *like_params, fetch],
            ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        # A broken shadow must NEVER take down search: contain the error, return
        # the primary path's empty result, and heal the shadow if the failure is
        # the portability/corruption class (next query then succeeds).
        if _is_healable(err):
            _heal(conn)
        return []

    # Hit shaping. Journal is capped at max(1, limit//4) for symmetry with
    # _search_strict (contentless journal rows share many common terms and would
    # otherwise dominate). Rows whose base record no longer resolves are skipped.
    journal_cap = max(1, limit // 4) if limit > 0 else 0
    journal_used = 0
    hits: list[dict] = []
    for txt, tier, k1, k2, rank in rows:
        if tier == "journal":
            if journal_used >= journal_cap:
                continue
        shaped = _shape_hit(conn, tenant_id, tier, k1, k2, txt, rank)
        if shaped is None:
            continue
        if tier == "journal":
            journal_used += 1
        hits.append(shaped)
        if len(hits) >= limit:
            break
    return hits
