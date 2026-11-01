"""Edge identity (V1 panel theme A + migration should-fix items).

Business-keyed edges attach to whatever later holds the key. These are the
regression tests for the panel's reproductions (LARCH exp1-3, YARROW skew.py /
copyexp.py, NOCTURNE 10):

* endpoint identity stamp (from_entity_id / to_entity_id) + the stale contract;
  set_entity INSERT reports inherited_links; link() re-stamps;
* edges carry updated_at, link() returns changed, a no-op re-link is not a
  lifecycle write;
* the additive, crash-atomic identity-column ALTER for stores already at v6;
* the v5 -> v6 copy goes through norm_relation, skips and counts bad rows,
  merges metadata on fold, and leaves a receipt;
* adoption: archived / dangling namesakes are collisions, audited renames are
  replayed, ambiguous edges are held, rename collisions drop instead of
  rolling back; the audit is homogeneous;
* every business-key renamer in the package calls rename_endpoint.
"""
from __future__ import annotations

import ast
import json
import logging
import shutil
import sqlite3
import types
from pathlib import Path

import pytest

import sibyl_memory_client
from sibyl_memory_client import (
    DEFAULT_TENANT,
    MemoryClient,
    entity_ref,
    reference_ref,
)
from sibyl_memory_client import _adopt, _links
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.testing import TEST_ACCOUNT_ID, NullEntitlementGate

FIXTURE_V5 = Path(__file__).resolve().parent / "fixtures" / "store_v5_0_9_0.db"
ENT = types.SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID)

SAM = entity_ref("person", "Sam Lee")
REDWOOD = entity_ref("company", "Redwood")
CONTRACT = reference_ref("doc/cfo-contract")


def _cl(db, tenant=DEFAULT_TENANT) -> MemoryClient:
    return MemoryClient.local(str(db), tenant_id=tenant, entitlement_gate=NullEntitlementGate())


def _q(db, sql, *args):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _stale(cl: MemoryClient) -> set[str]:
    with cl.storage.connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM memory_links")]
        return _links.stale_ids(conn, cl._tenant_id, ids)


def _edge_id(cl, src, dst, rel) -> str:
    with cl.storage.connection() as conn:
        s, d = _links.norm_node(src), _links.norm_node(dst)
        return conn.execute(
            "SELECT id FROM memory_links WHERE from_tier=? AND from_category=? AND from_key=? "
            "AND to_tier=? AND to_category=? AND to_key=? AND relation_type=?",
            (*s, *d, rel)).fetchone()[0]


# ----------------------------------------------------------------------
# 1. Identity stamp, stale, inherited_links (LARCH exp1 E1)
# ----------------------------------------------------------------------

def test_archive_then_recreate_marks_inherited_edges_stale(tmp_path) -> None:
    c = _cl(tmp_path / "m.db")
    sam = c.set_entity("person", "Sam Lee", {"role": "CFO"})
    assert sam["inherited_links"] == 0
    c.set_entity("company", "Redwood", {})
    c.set_reference("doc/cfo-contract", "Sam Lee's employment contract")
    c.link(SAM, REDWOOD, "works_at")
    c.link(REDWOOD, CONTRACT, "source_of")
    c.link(CONTRACT, SAM, "about")
    with c.storage.connection() as conn:
        row = conn.execute("SELECT from_entity_id, to_entity_id FROM memory_links "
                           "WHERE relation_type='works_at'").fetchone()
        assert tuple(row) == (sam["id"], c.get_entity("company", "Redwood")["id"])
        ref = conn.execute("SELECT from_entity_id, to_entity_id FROM memory_links "
                           "WHERE relation_type='source_of'").fetchone()
        assert ref[1] is None                         # reference endpoints: no stamp
    assert _stale(c) == set()

    c.archive_entity("person", "Sam Lee", reason="left")
    assert _stale(c) == set()                         # dangling is not stale
    intern = c.set_entity("person", "Sam Lee", {"role": "intern"})   # a different person
    assert intern["inherited_links"] == 2             # works_at + about
    works, about = _edge_id(c, SAM, REDWOOD, "works_at"), _edge_id(c, CONTRACT, SAM, "about")
    assert _stale(c) == {works, about}
    # The edges survive (operator decision 1); re-asserting one re-stamps it.
    res = c.link(SAM, REDWOOD, "works_at")
    assert res == {"id": works, "created": False, "changed": True}
    assert _stale(c) == {about}
    # Update path does not report inherited links.
    assert "inherited_links" not in c.set_entity("person", "Sam Lee", {"role": "intern", "v": 2})


def test_links_written_on_recreate_are_restamped(tmp_path) -> None:
    c = _cl(tmp_path / "m.db")
    c.set_entity("company", "Redwood", {})
    c.set_entity("person", "Sam Lee", {}, links=[{"to": REDWOOD, "relation": "works_at"}])
    c.archive_entity("person", "Sam Lee")
    out = c.set_entity("person", "Sam Lee", {}, links=[{"to": REDWOOD, "relation": "works_at"}])
    assert out["inherited_links"] == 1                # counted before this call's links=
    assert _stale(c) == set()


def test_stale_sql_fragment_is_the_documented_contract(tmp_path) -> None:
    """Read paths owned elsewhere (neighbors/links_of) render stale with this
    fragment; it must agree with stale_ids."""
    c = _cl(tmp_path / "m.db")
    c.set_entity("company", "Redwood", {})
    c.set_entity("person", "Sam Lee", {}, links=[{"to": REDWOOD, "relation": "works_at"}])
    c.archive_entity("company", "Redwood")
    c.set_entity("company", "Redwood", {"new": True})
    with c.storage.connection() as conn:
        rows = conn.execute(f"SELECT l.id, {_links.stale_sql('l')} FROM memory_links l").fetchall()
    assert [bool(r[1]) for r in rows] == [True]
    assert _stale(c) == {rows[0][0]}


# ----------------------------------------------------------------------
# 2. updated_at + changed; no-op re-link is not a lifecycle write (LARCH E20)
# ----------------------------------------------------------------------

def test_edge_updated_at_and_changed(tmp_path, monkeypatch) -> None:
    c = _cl(tmp_path / "m.db")
    c.set_entity("person", "Sam Lee", {})
    c.set_entity("company", "Redwood", {})
    noted: list[str] = []
    monkeypatch.setattr(client_mod, "_note_lifecycle_write",
                        lambda cl, kind, a, k: noted.append(kind))
    a = c.link(SAM, REDWOOD, "works_at", metadata={"since": "1999"})
    assert a["created"] is True and a["changed"] is False
    ts = lambda: _q(tmp_path / "m.db", "SELECT created_at, updated_at FROM memory_links")[0]
    created, updated = ts()
    assert created == updated
    assert c.link(SAM, REDWOOD, "works_at", metadata={"since": "1999"})["changed"] is False
    assert c.link(SAM, REDWOOD, "works_at")["changed"] is False
    assert ts()[1] == updated
    assert noted == ["link"]                          # the two no-ops were not counted
    assert c.link(SAM, REDWOOD, "works_at", metadata={"since": "2001"})["changed"] is True
    assert ts()[1] >= updated and noted == ["link", "link"]
    assert c.unlink(SAM, REDWOOD, "nope") == 0
    assert noted == ["link", "link"]


# ----------------------------------------------------------------------
# Identity columns on a store already at v6 (earlier V1 build)
# ----------------------------------------------------------------------

_OLD_V6 = """CREATE TABLE memory_links (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
  from_tier TEXT NOT NULL, from_category TEXT NOT NULL, from_key TEXT NOT NULL,
  to_tier TEXT NOT NULL, to_category TEXT NOT NULL, to_key TEXT NOT NULL,
  relation_type TEXT NOT NULL, metadata TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (tenant_id, from_tier, from_category, from_key, to_tier, to_category, to_key, relation_type))"""


def _old_v1_store(tmp_path) -> tuple[Path, dict]:
    """A v6 store as the earlier V1 build left it: memory_links without the
    stamp/updated_at columns, tenant_adoptions without links_audit. Sam Lee
    was archived and re-created AFTER the edge was written; Redwood is the
    original row."""
    db = tmp_path / "old.db"
    c = _cl(db)
    red = c.set_entity("company", "Redwood", {})
    sam_old = c.set_entity("person", "Sam Lee", {"role": "CFO"})
    c.storage.close()
    con = sqlite3.connect(str(db))
    con.executescript(
        "DROP TABLE memory_links;" + _OLD_V6 + ";"
        "CREATE INDEX memory_links_from ON memory_links (tenant_id, from_tier, from_category, from_key, relation_type);"
        "CREATE INDEX memory_links_to ON memory_links (tenant_id, to_tier, to_category, to_key, relation_type);"
        "ALTER TABLE tenant_adoptions DROP COLUMN links_audit;")
    con.execute("INSERT INTO memory_links (id, tenant_id, from_tier, from_category, from_key, to_tier, "
                "to_category, to_key, relation_type, created_at) VALUES "
                "('e1', ?, 'entity', 'person', 'Sam Lee', 'entity', 'company', 'Redwood', 'works_at', "
                "'2099-01-01T00:00:00.000Z')", (DEFAULT_TENANT,))
    # Sam Lee archived after the edge (2099-01-02), re-created as someone else.
    con.execute("INSERT INTO archived_entities (id, tenant_id, original_entity_id, category, name, "
                "body, archived_at) VALUES ('a1', ?, ?, 'person', 'Sam Lee', '{}', "
                "'2099-01-02T00:00:00.000Z')", (DEFAULT_TENANT, sam_old["id"]))
    con.execute("UPDATE entities SET id = 'sam-new', created_at = '2099-01-03T00:00:00.000Z' "
                "WHERE name = 'Sam Lee'")
    con.execute("UPDATE entities SET created_at = '2000-01-01T00:00:00.000Z' WHERE name = 'Redwood'")
    con.commit()
    con.close()
    return db, {"red": red["id"], "sam_old": sam_old["id"]}


def test_identity_columns_added_and_backfilled_on_open(tmp_path) -> None:
    db, ids = _old_v1_store(tmp_path)
    c = _cl(db)
    cols = {r[1] for r in _q(db, "PRAGMA table_info(memory_links)")}
    assert {"from_entity_id", "to_entity_id", "updated_at"} <= cols
    assert "links_audit" in {r[1] for r in _q(db, "PRAGMA table_info(tenant_adoptions)")}
    row = _q(db, "SELECT from_entity_id, to_entity_id, updated_at, created_at FROM memory_links")[0]
    # Sam Lee: the live row is newer than the edge -> stamped with the archived
    # original (the backfill does not launder the inherited edge); Redwood: live.
    assert row[:2] == (ids["sam_old"], ids["red"])
    assert row[2] == row[3]
    assert _stale(c) == {"e1"}
    c.storage.close()
    _cl(db).storage.close()                              # idempotent reopen


def test_identity_columns_migration_is_atomic(tmp_path, monkeypatch) -> None:
    db, _ = _old_v1_store(tmp_path)

    def boom(conn):
        conn.execute("ALTER TABLE memory_links ADD COLUMN from_entity_id TEXT")
        raise sqlite3.OperationalError("simulated crash")

    monkeypatch.setattr(_links, "apply_identity_columns", boom)
    with pytest.raises(Exception):
        _cl(db)
    assert "from_entity_id" not in {r[1] for r in _q(db, "PRAGMA table_info(memory_links)")}
    monkeypatch.undo()
    _cl(db).storage.close()
    assert "from_entity_id" in {r[1] for r in _q(db, "PRAGMA table_info(memory_links)")}


# ----------------------------------------------------------------------
# 5. v5 -> v6 copy: norm_relation, skip-and-count, metadata merge, receipt
#    (YARROW copyexp.py, LARCH exp1 E4)
# ----------------------------------------------------------------------

def test_v5_copy_normalises_skips_merges_and_writes_receipt(tmp_path, caplog) -> None:
    db = tmp_path / "memory.db"
    shutil.copyfile(FIXTURE_V5, db)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA foreign_keys=OFF")
    ten = DEFAULT_TENANT
    ids = {n: i for i, n in con.execute(
        "SELECT id, name FROM entities WHERE tenant_id = ?", (ten,))}
    maya, red, atlas = ids["Maya Patel"], ids["Redwood"], ids["atlas"]
    con.execute("INSERT INTO entities (id, tenant_id, category, name, body) "
                "VALUES ('nocat', ?, '', 'NoCat', '{}')", (ten,))
    rows = [
        ("x-ws", maya, red, "  Reports_To  ", '{"a": 1}', "2026-02-01T00:00:00.000Z"),
        ("x-ws2", maya, red, "reports_to", '{"b": 2, "a": 9}', "2026-02-02T00:00:00.000Z"),
        ("x-uml", maya, red, "ÜBER", None, "2026-02-03T00:00:00.000Z"),
        ("x-long", maya, red, "a" * 70, None, "2026-02-04T00:00:00.000Z"),
        ("x-blank", maya, red, "   ", None, "2026-02-05T00:00:00.000Z"),
        ("x-orphan", "missing-id", red, "r", None, "2026-02-06T00:00:00.000Z"),
        ("x-badcat", maya, "nocat", "r", None, "2026-02-07T00:00:00.000Z"),
        ("x-list", red, atlas, "has", "[1, 2]", "2026-02-08T00:00:00.000Z"),
    ]
    for rid, f, t, rel, meta, ts in rows:
        con.execute("INSERT INTO entity_relations (id, tenant_id, from_id, to_id, relation_type, "
                    "metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (rid, ten, f, t, rel, meta, ts))
    con.commit()
    con.close()

    with caplog.at_level(logging.INFO, logger="sibyl_memory_client.storage"):
        c = _cl(db)
    rels = sorted(e["relation"] for e in c.neighbors(entity_ref("person", "Maya Patel"),
                                                       direction="out")["edges"])
    # Never truncated: the 70-char relation is skipped, not turned into another one.
    assert rels == ["member_of", "reports_to", "works_at", "über"]
    rt = c.neighbors(entity_ref("person", "Maya Patel"), relation="reports_to")["edges"][0]
    assert rt["metadata"] == {"a": 9, "b": 2}            # merged, later key wins
    assert rt["created_at"] == "2026-02-01T00:00:00.000Z"   # earliest row kept
    assert c.link(entity_ref("person", "Maya Patel"), REDWOOD, "ÜBER")["created"] is False
    has = c.neighbors(REDWOOD, relation="has")["edges"][0]
    assert has["metadata"] is None                       # non-object metadata dropped, edge kept
    # Stamps are the ids the relation named.
    stamps = _q(db, "SELECT from_entity_id, to_entity_id FROM memory_links WHERE id = 'x-ws'")
    assert stamps == [(maya, red)]
    desc = _q(db, "SELECT description FROM sibyl_memory_schema_version WHERE version = 6")[0][0]
    receipt = json.loads(desc.split("Migration receipt: ", 1)[1])
    assert receipt == {"source_rows": 13, "copied": 6, "folded": 2, "skipped_orphan": 1,
                       "skipped_cross_tenant": 1, "skipped_bad_endpoint": 1,
                       "skipped_bad_relation": 2, "metadata_dropped": 1}
    assert any("migration receipt" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------
# 3. Adoption
# ----------------------------------------------------------------------

def _pair(db):
    return _cl(db), _cl(db, TEST_ACCOUNT_ID)


def _adopt_cmd(cl):
    return _adopt.adopt_default_tenant(cl.storage, TEST_ACCOUNT_ID, ENT, trigger="command")


def _keys(cl, node):
    return {(e["direction"], e["relation"], e["node"]["key"]) for e in cl.neighbors(node)["edges"]}


def test_adoption_archived_namesake_is_a_collision(tmp_path) -> None:
    """LARCH exp2: a forgotten pre-sign-in "Acme" must not hand its edges to the
    account's live, different "Acme"."""
    db = tmp_path / "adopt.db"
    pre, acct = _pair(db)
    acme = entity_ref("company", "Acme")
    acct.set_entity("company", "Acme", {"kind": "customer"})
    acct.set_reference("doc/acme-msa", "msa")
    acct.link(acme, reference_ref("doc/acme-msa"), "source_of")
    pre.set_entity("company", "Acme", {"kind": "test vendor"})
    pre.set_entity("person", "Bob", {})
    pre.set_reference("doc/old-pitch", "old pitch")
    pre.link(entity_ref("person", "Bob"), acme, "works_at")
    pre.link(acme, reference_ref("doc/old-pitch"), "source_of")
    pre.archive_entity("company", "Acme")
    res = _adopt_cmd(pre)
    assert res.status == "moved"
    assert _keys(acct, acme) == {("out", "source_of", "doc/acme-msa")}
    twin = entity_ref("company", "Acme (before sign-in)")
    assert _keys(acct, twin) == {("in", "works_at", "Bob"), ("out", "source_of", "doc/old-pitch")}
    assert _q(db, "SELECT name FROM archived_entities WHERE tenant_id = ?", TEST_ACCOUNT_ID) == [
        ("Acme (before sign-in)",)]
    assert res.renamed == [{"table": "archived_entities", "category": "company", "from": "Acme",
                            "to": "Acme (before sign-in)", "reason": "archived"}]
    assert {r["how"] for r in res.resolved_links} == {"collision"}


def test_adoption_dangling_twin_edge_drops_instead_of_rolling_back(tmp_path) -> None:
    """NOCTURNE 10: a dangling edge to "X (before sign-in)" made rename_endpoint
    hit the UNIQUE and the adoption roll back on every sign-in."""
    db = tmp_path / "adopt.db"
    pre, acct = _pair(db)
    acct.set_entity("p", "X", {})
    pre.set_entity("p", "X", {})
    pre.set_entity("p", "Y", {})
    pre.link(entity_ref("p", "Y"), entity_ref("p", "X"), "r", metadata={"m": 1})
    with pre.storage.transaction() as conn:   # an edge left by an earlier rename, target gone
        conn.execute("INSERT INTO memory_links (id, tenant_id, from_tier, from_category, from_key, "
                     "to_tier, to_category, to_key, relation_type) VALUES ('dz', ?, 'entity', 'p', "
                     "'Y', 'entity', 'p', 'X (before sign-in)', 'r')", (DEFAULT_TENANT,))
    res = _adopt_cmd(pre)
    assert res.status == "moved"
    assert [(d["reason"], d["metadata"]) for d in res.dropped_links] == [("rename_collision", {"m": 1})]
    assert _keys(acct, entity_ref("p", "X (before sign-in)")) == {("in", "r", "Y")}
    assert _keys(acct, entity_ref("p", "X")) == set()
    la = json.loads(_q(db, "SELECT links_audit FROM tenant_adoptions")[0][0])
    assert la["dropped"][0]["reason"] == "rename_collision"


def _seed_after_090_adoption(db) -> None:
    """The state a 0.9.0 adoption leaves (YARROW skew): entities moved and
    renamed, edges left under DEFAULT keyed by the OLD name. ``stamp`` False
    simulates edges whose provenance is unknown (NULL stamp)."""
    pre, acct = _pair(db)
    acct.set_entity("person", "Maya Patel", {"who": "acct"})
    pre.set_entity("person", "Maya Patel", {"who": "pre"})
    pre.set_entity("company", "Redwood", {})
    pre.link(entity_ref("person", "Maya Patel"), REDWOOD, "works_at")
    with pre.storage.transaction() as conn:   # what 0.9.0's adoption does
        conn.execute("UPDATE entities SET name = 'Maya Patel (before sign-in)' "
                     "WHERE tenant_id = ? AND name = 'Maya Patel'", (DEFAULT_TENANT,))
        conn.execute("UPDATE entities SET tenant_id = ? WHERE tenant_id = ?",
                     (TEST_ACCOUNT_ID, DEFAULT_TENANT))
        conn.execute("INSERT INTO tenant_adoptions (id, from_tenant, to_tenant, account_id, kind, "
                     "counts, renamed) VALUES ('old', ?, ?, ?, 'command', '{}', ?)",
                     (DEFAULT_TENANT, TEST_ACCOUNT_ID, TEST_ACCOUNT_ID, json.dumps([
                         {"table": "entities", "category": "person", "from": "Maya Patel",
                          "to": "Maya Patel (before sign-in)"}])))


@pytest.mark.parametrize("stamped", [True, False])
def test_adoption_replays_earlier_renames_onto_stranded_edges(tmp_path, stamped) -> None:
    db = tmp_path / "adopt.db"
    _seed_after_090_adoption(db)
    if not stamped:
        con = sqlite3.connect(str(db))
        con.execute("UPDATE memory_links SET from_entity_id = NULL, to_entity_id = NULL")
        con.commit()
        con.close()
    acct = _cl(db, TEST_ACCOUNT_ID)
    res = _adopt_cmd(acct)
    assert res.status == "moved"
    assert [r["how"] for r in res.resolved_links] == ["stamp" if stamped else "replay"]
    assert _keys(acct, entity_ref("person", "Maya Patel")) == set()
    assert _keys(acct, entity_ref("person", "Maya Patel (before sign-in)")) == {
        ("out", "works_at", "Redwood")}
    assert _stale(acct) == set()


def test_adoption_holds_ambiguous_edges_and_does_not_re_audit(tmp_path) -> None:
    db = tmp_path / "adopt.db"
    _seed_after_090_adoption(db)
    con = sqlite3.connect(str(db))   # unknown provenance and no audit to replay
    con.execute("UPDATE memory_links SET from_entity_id = NULL, to_entity_id = NULL")
    con.execute("DELETE FROM tenant_adoptions")
    con.execute("INSERT INTO tenant_adoptions (id, from_tenant, to_tenant, account_id, kind, counts) "
                "VALUES ('s', ?, ?, ?, 'seen', '{}')", (DEFAULT_TENANT, TEST_ACCOUNT_ID, TEST_ACCOUNT_ID))
    con.commit()
    con.close()
    acct = _cl(db, TEST_ACCOUNT_ID)
    res = _adopt_cmd(acct)
    assert res.status == "nothing_to_move"
    assert [(h["reason"], h["from"]["key"]) for h in res.held_links] == [("twin", "Maya Patel")]
    assert _q(db, "SELECT tenant_id FROM memory_links") == [(DEFAULT_TENANT,)]   # not moved
    assert _keys(acct, entity_ref("person", "Maya Patel")) == set()
    n_audit = len(_q(db, "SELECT 1 FROM tenant_adoptions"))
    assert _adopt_cmd(acct).status == "nothing_to_move"
    assert len(_q(db, "SELECT 1 FROM tenant_adoptions")) == n_audit


def test_adoption_entity_rename_moves_only_its_own_edges(tmp_path) -> None:
    """Fable Q3a: an edge stamped with an ARCHIVED pre-sign-in namesake does
    not follow the live pre-sign-in row's rename."""
    db = tmp_path / "adopt.db"
    pre, acct = _pair(db)
    acme = entity_ref("company", "Acme")
    acct.set_entity("company", "Acme", {})
    pre.set_entity("company", "Acme", {"v": "old"})
    pre.set_entity("person", "Bob", {})
    pre.link(entity_ref("person", "Bob"), acme, "worked_at")
    pre.archive_entity("company", "Acme")
    pre.set_entity("company", "Acme", {"v": "new"})
    pre.link(entity_ref("person", "Bob"), acme, "works_at")
    res = _adopt_cmd(pre)
    assert res.status == "moved"
    assert _keys(acct, entity_ref("company", "Acme (before sign-in)")) == {("in", "works_at", "Bob")}
    assert _keys(acct, entity_ref("company", "Acme (before sign-in 2)")) == {("in", "worked_at", "Bob")}
    assert _keys(acct, acme) == set()
    assert _stale(acct) == set()


# ----------------------------------------------------------------------
# 4. Invariant: every business-key renamer calls rename_endpoint
# ----------------------------------------------------------------------

_RENAMES = (("entities", "name"), ("reference_documents", "doc_key"),
            ("state_documents", "document_key"))


def _sql_strings(node: ast.AST) -> list[str]:
    """String literals passed as call arguments (SQL handed to execute & co.);
    docstrings and comments are not SQL."""
    out = []
    args = [a for c in ast.walk(node) if isinstance(c, ast.Call) for a in c.args]
    for n in (x for a in args for x in ast.walk(a)):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values))
    return out


def _is_renamer(text: str) -> bool:
    t = " ".join(text.split())
    if any(f"UPDATE {tbl} SET {col} " in t or f"UPDATE {tbl} SET {col}=" in t
           for tbl, col in _RENAMES):
        return True
    return "UPDATE {} SET {} " in t   # f"UPDATE {table} SET {col} = ..." (adoption doc renames)


def test_every_business_key_renamer_calls_rename_endpoint() -> None:
    pkg = Path(sibyl_memory_client.__file__).parent
    renamers = []
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_renamer(s) for s in _sql_strings(fn)):
                continue
            calls = {getattr(c.func, "attr", getattr(c.func, "id", None))
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            renamers.append(f"{path.name}:{fn.name}")
            assert "rename_endpoint" in calls, (
                f"{path.name}:{fn.name} renames a business key without rewriting edges")
    assert "_adopt.py:_resolve_conflicts" in renamers   # the scan sees the known renamer


def test_invariant_scanner_flags_an_unpaired_renamer() -> None:
    src = 'def f(conn):\n    conn.execute("UPDATE entities SET name = ? WHERE id = ?", (1, 2))\n'
    fn = ast.parse(src).body[0]
    assert any(_is_renamer(s) for s in _sql_strings(fn))
