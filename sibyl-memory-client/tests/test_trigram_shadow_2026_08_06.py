"""Folded-trigram search shadow — DDL, triggers, migration, heal, isolation.

v0.5.0 multi-language search (spec §4.2 / §5 / §7). Covers:
  * DDL + trigger integrity: insert/update/delete keep ``search_shadow`` ==
    fold(base) across all four tiers.
  * Fold map both directions for EVERY FOLD_MAP char.
  * v3-fixture migration: backfill correct, marker stamped v4, crash mid-migration
    rolls back and leaves the marker at 3.
  * Old-client simulation: raw 0.4.19-shaped writes are mirrored by the
    DB-resident triggers (true backward compatibility).
  * Heal path: dropped/corrupt shadow is rebuilt on the next open.
  * Cross-tenant isolation on the shadow MATCH and LIKE paths (CORE-3).
  * LIKE metacharacter escaping (%, _, \\).
  * Runtime tokenizer-clause selection across the SQLite 3.45 boundary.
"""
from __future__ import annotations

import sqlite3

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import SchemaError
from sibyl_memory_client import shadow
from sibyl_memory_client.shadow import (
    FOLD_MAP, SHADOW_TABLE, fold_py, shadow_search, trigram_tokenizer_clause,
    create_table_sql,
)
from sibyl_memory_client.storage import _FTS_REBUILD_MARKER, _SHADOW_MARKER


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _raw(path):
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _user_version(path) -> int:
    conn = _raw(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _shadow_txt(conn, tier, k2, tenant):
    row = conn.execute(
        f"SELECT txt FROM {SHADOW_TABLE} WHERE tier=? AND k2=? AND tenant_id=?",
        (tier, k2, tenant),
    ).fetchone()
    return row[0] if row else None


def _shadow_count(conn) -> int:
    return conn.execute(f"SELECT count(*) FROM {SHADOW_TABLE}").fetchone()[0]


# --------------------------------------------------------------------------
# DDL + trigger integrity: shadow == fold(base) across all four tiers
# --------------------------------------------------------------------------

def test_entity_triggers_keep_shadow_in_sync(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("città", "Anteṙ", {"note": "Straße + Łódź"})
    with c.storage.connection() as conn:
        row = conn.execute(
            "SELECT name, category, body FROM entities WHERE name='Anteṙ'").fetchone()
        expected = fold_py(f"{row[0]} {row[1]} {row[2]}")
        assert _shadow_txt(conn, "entity", "Anteṙ", "t1") == expected
    # UPDATE (set_entity on existing key -> real UPDATE -> AU trigger)
    c.set_entity("città", "Anteṙ", {"note": "moved to Kraków"})
    with c.storage.connection() as conn:
        row = conn.execute(
            "SELECT name, category, body FROM entities WHERE name='Anteṙ'").fetchone()
        assert _shadow_txt(conn, "entity", "Anteṙ", "t1") == fold_py(
            f"{row[0]} {row[1]} {row[2]}")
        # exactly one shadow row for this key (no stale duplicate from the update)
        n = conn.execute(
            f"SELECT count(*) FROM {SHADOW_TABLE} WHERE tier='entity' AND k2='Anteṙ'"
        ).fetchone()[0]
        assert n == 1
    # DELETE (AD trigger)
    c.delete_entity("città", "Anteṙ")
    with c.storage.connection() as conn:
        assert _shadow_txt(conn, "entity", "Anteṙ", "t1") is None


def test_state_triggers_keep_shadow_in_sync(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_state("cfg-北京", {"note": "北京烤鸭"})
    with c.storage.connection() as conn:
        body = conn.execute(
            "SELECT body FROM state_documents WHERE document_key='cfg-北京'").fetchone()[0]
        assert _shadow_txt(conn, "state", "cfg-北京", "t1") == fold_py(f"cfg-北京 {body}")
    c.set_state("cfg-北京", {"note": "上海"})  # ON CONFLICT DO UPDATE -> AU
    with c.storage.connection() as conn:
        body = conn.execute(
            "SELECT body FROM state_documents WHERE document_key='cfg-北京'").fetchone()[0]
        assert _shadow_txt(conn, "state", "cfg-北京", "t1") == fold_py(f"cfg-北京 {body}")
    # AD via raw DELETE (no public delete_state API)
    raw = _raw(c.storage.db_path)
    raw.execute("DELETE FROM state_documents WHERE tenant_id='t1' AND document_key='cfg-北京'")
    raw.close()
    with c.storage.connection() as conn:
        assert _shadow_txt(conn, "state", "cfg-北京", "t1") is None


def test_reference_triggers_keep_shadow_in_sync(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_reference("doc-łódź", "Notes about Łódź, Poland")
    with c.storage.connection() as conn:
        assert _shadow_txt(conn, "reference", "doc-łódź", "t1") == fold_py(
            "doc-łódź Notes about Łódź, Poland")
    c.set_reference("doc-łódź", "Updated Łódź notes")  # ON CONFLICT DO UPDATE -> AU
    with c.storage.connection() as conn:
        assert _shadow_txt(conn, "reference", "doc-łódź", "t1") == fold_py(
            "doc-łódź Updated Łódź notes")
    raw = _raw(c.storage.db_path)
    raw.execute("DELETE FROM reference_documents WHERE tenant_id='t1' AND doc_key='doc-łódź'")
    raw.close()
    with c.storage.connection() as conn:
        assert _shadow_txt(conn, "reference", "doc-łódź", "t1") is None


def test_journal_trigger_keeps_shadow_in_sync(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    ev_id = c.write_event(evaluated={"place": "Bełżyce"}, acted={"note": "北京"})
    with c.storage.connection() as conn:
        row = conn.execute(
            "SELECT evaluated, acted, forward, extra FROM journal_events WHERE id=?",
            (ev_id,)).fetchone()
        expected = fold_py(
            f"{row[0] or ''} {row[1] or ''} {row[2] or ''} {row[3] or ''}")
        assert _shadow_txt(conn, "journal", ev_id, "t1") == expected


# --------------------------------------------------------------------------
# Fold map both directions for every FOLD_MAP char (measured end-to-end)
# --------------------------------------------------------------------------

def test_fold_map_py_every_char():
    for src, dst in FOLD_MAP.items():
        assert fold_py(src) == dst, (src, dst)
        assert fold_py(f"x{src}y") == f"x{dst}y", (src, dst)


def test_fold_map_both_directions_end_to_end(tmp_path):
    """For every non-decomposable char: the ASCII (dst) spelling finds a stored
    src word, AND the src spelling finds a stored dst word — via the shadow."""
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    # unique per-char surrounding text so no two folded forms collide (which would
    # let the strict porter-unicode61 pass satisfy the query and suppress the
    # additive shadow before we can observe it).
    for i, (src, dst) in enumerate(FOLD_MAP.items()):
        c.set_entity("fold", f"src{i}", {"w": f"q{i}x{src}yz"})
        c.set_entity("fold", f"dst{i}", {"w": f"r{i}x{dst}yz"})
    for i, (src, dst) in enumerate(FOLD_MAP.items()):
        # dst spelling -> src-stored (Belzyce -> Bełżyce direction)
        a = {h["key"] for h in c.search(f"q{i}x{dst}yz", limit=20)}
        assert f"src{i}" in a, (src, dst, a)
        # src spelling -> dst-stored (Bełżyce -> Belzyce direction)
        b = {h["key"] for h in c.search(f"r{i}x{src}yz", limit=20)}
        assert f"dst{i}" in b, (src, dst, b)


# --------------------------------------------------------------------------
# v3-fixture migration (backfill, marker) + crash rollback
# --------------------------------------------------------------------------

def _make_v3_fixture(path):
    """Build a genuine pre-shadow v3 DB: apply schema.sql, populate the four tiers
    via raw SQL (fires the v3 FTS triggers, NOT the shadow triggers — they don't
    exist yet), stamp the v3 FTS-rebuild marker, no shadow table."""
    from sibyl_memory_client.storage import _SCHEMA_PATH
    conn = _raw(path)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("DELETE FROM sibyl_memory_schema_version WHERE version=4")
    conn.execute(
        "INSERT INTO entities (id, tenant_id, category, name, status, body) "
        "VALUES ('e1','t1','places','beijing',NULL,'{\"t\":\"北京烤鸭\"}')")
    conn.execute(
        "INSERT INTO entities (id, tenant_id, category, name, status, body) "
        "VALUES ('e2','t1','places','belzyce',NULL,'{\"a\":\"Bełżyce\"}')")
    conn.execute(
        "INSERT INTO state_documents (tenant_id, document_key, body) "
        "VALUES ('t1','s1','{\"n\":\"上海\"}')")
    conn.execute(
        "INSERT INTO reference_documents (tenant_id, doc_key, body) "
        "VALUES ('t1','r1','Łódź notes')")
    conn.execute(
        "INSERT INTO journal_events (id, tenant_id, ts, evaluated) "
        "VALUES ('j1','t1','2026-08-06T00:00:00.000Z','{\"p\":\"Kraków\"}')")
    conn.execute(f"PRAGMA user_version = {int(_FTS_REBUILD_MARKER)}")
    conn.close()


def test_v3_to_v4_migration_backfills_and_stamps(tmp_path):
    path = tmp_path / "v3.db"
    _make_v3_fixture(path)
    assert _user_version(path) == 3
    # opening under 0.5.0 migrates v3 -> v4
    c = MemoryClient.local(path, tenant_id="t1")
    assert _user_version(path) == _SHADOW_MARKER == 4
    with c.storage.connection() as conn:
        assert shadow.shadow_table_exists(conn)
        # backfill: one shadow row per base row across all four tiers
        base = sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                   for t in ("entities", "state_documents",
                             "reference_documents", "journal_events"))
        assert _shadow_count(conn) == base == 5
        # a backfilled folded row is queryable and byte-identical to fold_py
        assert _shadow_txt(conn, "entity", "beijing", "t1") == fold_py(
            'beijing places {"t":"北京烤鸭"}')
    # the folded content is reachable through the fallback (was 0 under v3)
    assert any(h["key"] == "beijing" for h in c.search("北京", limit=10))
    assert any(h["key"] == "belzyce" for h in c.search("Belzyce", limit=10))


def test_v3_to_v4_crash_rollback_leaves_marker_3(tmp_path, monkeypatch):
    path = tmp_path / "v3crash.db"
    _make_v3_fixture(path)

    def boom(conn):
        conn.execute(create_table_sql())          # partial work inside the txn
        raise sqlite3.OperationalError("simulated crash mid-migration")

    monkeypatch.setattr(shadow, "apply_shadow_migration", boom)
    with pytest.raises(SchemaError):
        MemoryClient.local(path, tenant_id="t1")
    # crash-atomic: the whole v4 transaction rolled back
    assert _user_version(path) == 3
    conn = _raw(path)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (SHADOW_TABLE,)).fetchone() is None
    finally:
        conn.close()
    # next clean open completes the migration
    monkeypatch.undo()
    c = MemoryClient.local(path, tenant_id="t1")
    assert _user_version(path) == 4
    with c.storage.connection() as conn:
        assert shadow.shadow_table_exists(conn)


def test_fast_path_no_rebuild_when_v4(tmp_path, monkeypatch):
    """A fully-migrated v4 DB must NOT re-run apply_shadow_migration on reopen."""
    path = tmp_path / "fast.db"
    MemoryClient.local(path, tenant_id="t1").set_entity("c", "n", {"v": 1})
    calls = {"n": 0}
    orig = shadow.apply_shadow_migration

    def counting(conn):
        calls["n"] += 1
        return orig(conn)

    monkeypatch.setattr(shadow, "apply_shadow_migration", counting)
    MemoryClient.local(path, tenant_id="t1")
    assert calls["n"] == 0


# --------------------------------------------------------------------------
# Old-client simulation: raw 0.4.19-shaped writes -> triggers maintain shadow
# --------------------------------------------------------------------------

def test_old_client_raw_writes_are_mirrored(tmp_path):
    path = tmp_path / "compat.db"
    MemoryClient.local(path, tenant_id="t1")  # migrate to v4, then act like 0.4.19
    conn = _raw(path)
    try:
        # exact 0.4.19 set_entity INSERT shape
        conn.execute(
            "INSERT INTO entities (id, tenant_id, category, name, status, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("x1", "t1", "places", "gdansk", None, '{"a":"Gdańsk"}'))
        assert _shadow_txt(conn, "entity", "gdansk", "t1") == fold_py(
            'gdansk places {"a":"Gdańsk"}')
        # exact 0.4.19 UPDATE shape
        conn.execute(
            "UPDATE entities SET status = ?, body = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (None, '{"a":"Wrocław"}', "x1"))
        assert _shadow_txt(conn, "entity", "gdansk", "t1") == fold_py(
            'gdansk places {"a":"Wrocław"}')
        # exact 0.4.19 DELETE shape
        conn.execute(
            "DELETE FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
            ("t1", "places", "gdansk"))
        assert _shadow_txt(conn, "entity", "gdansk", "t1") is None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Heal path: dropped/corrupt shadow rebuilt on next open
# --------------------------------------------------------------------------

def test_dropped_shadow_rebuilt_on_next_open(tmp_path):
    path = tmp_path / "heal.db"
    c = MemoryClient.local(path, tenant_id="t1")
    c.set_entity("places", "beijing", {"t": "北京烤鸭"})
    c.storage.close()
    # simulate corruption: drop the shadow + triggers, leave marker at 4
    conn = _raw(path)
    shadow.drop_shadow(conn)
    conn.close()
    assert _user_version(path) == 4
    # reopen: marker>=4 but shadow missing -> migration rebuilds it
    c2 = MemoryClient.local(path, tenant_id="t1")
    with c2.storage.connection() as conn:
        assert shadow.shadow_table_exists(conn)
        assert _shadow_txt(conn, "entity", "beijing", "t1") is not None
    assert any(h["key"] == "beijing" for h in c2.search("北京", limit=10))


def test_dropped_shadow_trigger_self_heals_on_next_open(tmp_path, monkeypatch):
    """F1 (Fable hardening 2026-08-06): the v4 fast path requires the shadow
    TABLE *and* all 10 maintenance triggers. An out-of-band drop of ONE trigger
    (table intact, marker still 4, other 9 triggers present) must NOT be read as
    'already migrated' — otherwise the shadow silently stops being maintained
    and a later query risks a stale/false-positive fallback hit. The next open
    must fall through to the idempotent apply_shadow_migration: recreate the
    trigger (count back to 10) and re-backfill so shadow == base again."""
    path = tmp_path / "trigheal.db"
    c = MemoryClient.local(path, tenant_id="t1")
    c.set_entity("places", "beijing", {"t": "北京烤鸭"})
    c.storage.close()

    # out-of-band: drop exactly ONE shadow trigger; leave table, marker, others.
    conn = _raw(path)
    conn.execute("DROP TRIGGER IF EXISTS entities_ai_shadow")
    dropped_count = shadow.shadow_trigger_count(conn)
    table_present = shadow.shadow_table_exists(conn)
    conn.close()
    assert _user_version(path) == 4                       # marker untouched
    assert table_present                                  # table still there
    assert dropped_count == len(shadow.SHADOW_TRIGGER_NAMES) - 1 == 9
    assert not (dropped_count == len(shadow.SHADOW_TRIGGER_NAMES))  # incomplete

    # reopen: table present + marker>=4, but triggers incomplete -> self-heal.
    c2 = MemoryClient.local(path, tenant_id="t1")
    with c2.storage.connection() as conn:
        assert shadow.shadow_trigger_count(conn) == 10           # recreated
        assert shadow.shadow_triggers_complete(conn)
        assert shadow.shadow_table_exists(conn)
        # existing row survived the DELETE+re-backfill (shadow still consistent)
        assert _shadow_txt(conn, "entity", "beijing", "t1") is not None
    # the recreated AI trigger propagates NEW writes to the shadow again
    c2.set_entity("places", "shanghai", {"t": "上海"})
    with c2.storage.connection() as conn:
        assert _shadow_txt(conn, "entity", "shanghai", "t1") is not None
    # and the healed folded content is reachable through the fallback
    assert any(h["key"] == "shanghai" for h in c2.search("上海", limit=10))
    c2.storage.close()

    # third open: trigger set complete again -> fast path, no migration re-run.
    calls = {"n": 0}
    orig = shadow.apply_shadow_migration

    def counting(conn):
        calls["n"] += 1
        return orig(conn)

    monkeypatch.setattr(shadow, "apply_shadow_migration", counting)
    MemoryClient.local(path, tenant_id="t1")
    assert calls["n"] == 0


# --------------------------------------------------------------------------
# Cross-tenant isolation (CORE-3) on both shadow query paths
# --------------------------------------------------------------------------

def test_cross_tenant_isolation_match_and_like(tmp_path):
    path = tmp_path / "iso.db"
    a = MemoryClient.local(path, tenant_id="tenant-A")
    a.set_entity("p", "a-city", {"t": "北京烤鸭 Łódź"})   # CJK (LIKE) + accented (MATCH)
    b = MemoryClient.local(path, tenant_id="tenant-B")
    b.set_entity("p", "b-city", {"t": "上海 Kraków"})

    with a.storage.connection() as conn:
        # MATCH path (>=3-char folded token 'lodz'): A sees a-city, B sees nothing
        assert [h["key"] for h in shadow_search(conn, "tenant-A", "lodz", limit=10)] == ["a-city"]
        assert shadow_search(conn, "tenant-B", "lodz", limit=10) == []
        # LIKE path (2-char CJK '北京'): A sees a-city, B sees nothing
        assert [h["key"] for h in shadow_search(conn, "tenant-A", "北京", limit=10)] == ["a-city"]
        assert shadow_search(conn, "tenant-B", "北京", limit=10) == []
        # and B's own content is isolated from A
        assert [h["key"] for h in shadow_search(conn, "tenant-B", "上海", limit=10)] == ["b-city"]
        assert shadow_search(conn, "tenant-A", "上海", limit=10) == []

    # end-to-end via the client funnel: B must never surface A's row
    assert b.search("Lodz", limit=10) == []
    assert b.search("北京", limit=10) == []


# --------------------------------------------------------------------------
# LIKE metacharacter escaping (%, _, \)
# --------------------------------------------------------------------------

def test_like_escape_regex_handles_all_metachars():
    esc = shadow._LIKE_ESC.sub(r"\\\1", "a%b_c\\d")
    assert esc == "a\\%b\\_c\\\\d"


def test_like_underscore_is_literal_not_wildcard(tmp_path):
    """A '_' inside a short non-ASCII (LIKE-path) token must match literally, not
    act as the single-char wildcard."""
    c = MemoryClient.local(tmp_path / "esc.db", tenant_id="t1")
    c.set_entity("p", "has-underscore", {"t": "北_"})   # literal underscore
    c.set_entity("p", "has-letter", {"t": "北x"})       # would match if '_' wildcarded
    hits = {h["key"] for h in c.search("北_", limit=10)}
    assert "has-underscore" in hits
    assert "has-letter" not in hits


# --------------------------------------------------------------------------
# Runtime tokenizer-clause selection across the SQLite 3.45 boundary
# --------------------------------------------------------------------------

def test_tokenizer_clause_pre_345(monkeypatch):
    monkeypatch.setattr(shadow.sqlite3, "sqlite_version_info", (3, 44, 0))
    assert trigram_tokenizer_clause() == "tokenize = 'trigram'"
    assert "remove_diacritics" not in create_table_sql()


def test_tokenizer_clause_345_and_up(monkeypatch):
    monkeypatch.setattr(shadow.sqlite3, "sqlite_version_info", (3, 45, 0))
    assert trigram_tokenizer_clause() == "tokenize = 'trigram remove_diacritics 1'"
    assert "trigram remove_diacritics 1" in create_table_sql()
