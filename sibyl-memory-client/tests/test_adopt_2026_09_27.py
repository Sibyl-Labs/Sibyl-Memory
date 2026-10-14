"""Pre-sign-in adoption (0.9.0 gate fix, defect 1).

Promise: memory written on this computer before sign-in (DEFAULT_TENANT) is
never deleted and is readable again the moment you sign in. Before this fix the
client resolved the account tenant after sign-in and the DEFAULT_TENANT rows
were invisible; nothing re-tenanted them.

The pre-activation store is written the way a 0.8.x install wrote it: a
DEFAULT_TENANT client with no gate (NullEntitlementGate stands in for the
pre-gate client). "Sign in" is the conftest's entitled home (TEST_ACCOUNT_ID,
test-signed entitlement, real EntitlementGate).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from sibyl_memory_client import DEFAULT_TENANT, EntitlementError, MemoryClient
from sibyl_memory_client.shadow import shadow_search
from sibyl_memory_client.testing import (
    TEST_ACCOUNT_ID,
    NullEntitlementGate,
    write_credentials,
    write_entitlement,
)

OTHER = "99999999-8888-4777-8666-555555555555"
SRC = str(Path(__file__).resolve().parent.parent / "src")


def _pre_signin_client(db: Path, tenant: str = DEFAULT_TENANT) -> MemoryClient:
    return MemoryClient.local(str(db), tenant_id=tenant, entitlement_gate=NullEntitlementGate())


def _seed_pre_signin(db: Path) -> None:
    c = _pre_signin_client(db)
    c.set_entity("project", "atlas", {"note": "zeppelin migration plan"}, status="active")
    c.set_entity("person", "ada", {"role": "reviewer"})
    c.set_state("session", {"focus": "quokka rollout"})
    c.set_reference("guide/deploy", "Deploy with the marmoset checklist")
    c.write_event(acted=["shipped the narwhal release"])
    c.storage.close()


def _rows(db: Path, table: str, tenant: str) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?",
                           (tenant,)).fetchone()[0]
    finally:
        con.close()


def _audit(db: Path) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT account_id, to_tenant, kind, counts, renamed "
                           "FROM tenant_adoptions ORDER BY rowid").fetchall()
    finally:
        con.close()


# ----------------------------------------------------------------------
# The promise
# ----------------------------------------------------------------------

def test_pre_signin_rows_are_readable_after_sign_in(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)   # signed in
    assert c.get_entity("project", "atlas")["body"] == {"note": "zeppelin migration plan"}
    assert {e["name"] for e in c.list_entities()} == {"atlas", "ada"}
    assert c.get_state("session")["body"] == {"focus": "quokka rollout"}
    assert c.get_reference("guide/deploy")["body"] == "Deploy with the marmoset checklist"
    assert len(c.read_events()) == 1
    # Nothing left behind, nothing deleted.
    for t in ("entities", "state_documents", "reference_documents", "journal_events"):
        assert _rows(db, t, DEFAULT_TENANT) == 0
    assert _rows(db, "entities", TEST_ACCOUNT_ID) == 2


def test_fts_and_shadow_find_moved_rows(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.list_entities()   # first gated op adopts
    tiers = {h["tier"] for q in ("zeppelin", "quokka", "marmoset", "narwhal")
             for h in c.search(q)}
    assert tiers == {"entity", "state", "reference", "journal"}
    # Search shadow (substring fallback) follows too, journal rows included.
    with c.storage.connection() as conn:
        assert shadow_search(conn, TEST_ACCOUNT_ID, "arwha")
        assert shadow_search(conn, TEST_ACCOUNT_ID, "eppeli")
        assert shadow_search(conn, DEFAULT_TENANT, "arwha") == []
        assert shadow_search(conn, DEFAULT_TENANT, "eppeli") == []
    # The old tenant sees nothing any more through any index.
    old = _pre_signin_client(db)
    for q in ("zeppelin", "quokka", "marmoset", "narwhal"):
        assert list(old.search(q)) == []
    # Every FTS index still agrees with its base table.
    with c.storage.connection() as conn:
        for fts in ("entities_fts", "state_documents_fts", "reference_documents_fts"):
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
        n_fts = conn.execute("SELECT COUNT(*) FROM journal_events_fts WHERE tenant_id = ?",
                             (TEST_ACCOUNT_ID,)).fetchone()[0]
        assert n_fts == 1


def test_audit_marker_records_counts_not_content(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.get_state("session")
    (acct, to, kind, counts, renamed), = _audit(db)
    assert (acct, to, kind) == (TEST_ACCOUNT_ID, TEST_ACCOUNT_ID, "auto")
    assert json.loads(counts) == {"entities": 2, "state_documents": 1,
                                  "reference_documents": 1, "journal_events": 1}
    assert renamed is None
    assert "zeppelin" not in counts
    assert c.adoption.status == "moved" and c.adoption.moved == 5


def test_idempotent_rerun(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID).list_entities()
    before = _audit(db)
    c2 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert len(c2.list_entities()) == 2
    assert c2.adoption.status == "skipped" and c2.adoption.reason == "already_seen"
    r = c2.adopt_pre_signin_memory()
    assert r.status == "nothing_to_move"
    assert _audit(db) == before
    assert _rows(db, "entities", TEST_ACCOUNT_ID) == 2


def test_first_sign_in_with_nothing_to_move_leaves_a_seen_marker(tmp_path) -> None:
    db = tmp_path / "memory.db"
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    c.set_entity("project", "p", {"a": 1})
    assert [row[2] for row in _audit(db)] == ["seen"]
    # Rows written under DEFAULT_TENANT AFTER the account's first sign-in (an
    # explicit DEFAULT_TENANT client) are NOT swept by a later automatic pass.
    _pre_signin_client(db).set_entity("project", "later", {"b": 2})
    c2 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert [e["name"] for e in c2.list_entities()] == ["p"]
    assert _rows(db, "entities", DEFAULT_TENANT) == 1
    # ...but the explicit command moves them.
    assert c2.adopt_pre_signin_memory().status == "moved"
    assert {e["name"] for e in c2.list_entities()} == {"p", "later"}


# ----------------------------------------------------------------------
# Conflicts: keep both, overwrite neither
# ----------------------------------------------------------------------

def test_conflict_keeps_both(tmp_path) -> None:
    db = tmp_path / "memory.db"
    acct = _pre_signin_client(db, TEST_ACCOUNT_ID)   # account rows from an earlier sign-in
    acct.set_entity("project", "atlas", {"note": "account copy"})
    acct.set_state("session", {"focus": "account state"})
    acct.set_reference("guide/deploy", "account reference")
    _seed_pre_signin(db)
    # The account has no audit row (the rows predate 0.9.0), so the explicit
    # command handles it; the automatic pass handles it identically.
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    names = {e["name"]: e["body"] for e in c.list_entities()}
    assert names == {
        "atlas": {"note": "account copy"},
        "atlas (before sign-in)": {"note": "zeppelin migration plan"},
        "ada": {"role": "reviewer"},
    }
    assert c.get_state("session")["body"] == {"focus": "account state"}
    assert c.get_state("session (before sign-in)")["body"] == {"focus": "quokka rollout"}
    assert c.get_reference("guide/deploy")["body"] == "account reference"
    assert c.get_reference("guide/deploy (before sign-in)")["body"] == \
        "Deploy with the marmoset checklist"
    renamed = json.loads(_audit(db)[-1][4])
    assert {(r["table"], r["from"], r["to"]) for r in renamed} == {
        ("entities", "atlas", "atlas (before sign-in)"),
        ("state_documents", "session", "session (before sign-in)"),
        ("reference_documents", "guide/deploy", "guide/deploy (before sign-in)"),
    }
    # Renamed rows are searchable under their new key.
    assert any(h["key"] == "project/atlas (before sign-in)" or
               "atlas (before sign-in)" in str(h.get("key")) for h in c.search("zeppelin"))


def test_conflict_suffix_is_unique(tmp_path) -> None:
    db = tmp_path / "memory.db"
    acct = _pre_signin_client(db, TEST_ACCOUNT_ID)
    acct.set_entity("project", "atlas", {"v": "account"})
    acct.set_entity("project", "atlas (before sign-in)", {"v": "account, earlier move"})
    _pre_signin_client(db).set_entity("project", "atlas", {"v": "default"})
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert c.get_entity("project", "atlas (before sign-in 2)")["body"] == {"v": "default"}
    assert c.get_entity("project", "atlas")["body"] == {"v": "account"}
    assert c.get_entity("project", "atlas (before sign-in)")["body"] == {"v": "account, earlier move"}


# ----------------------------------------------------------------------
# Different account on the same store
# ----------------------------------------------------------------------

def test_store_that_held_another_account_is_not_merged_automatically(tmp_path) -> None:
    from sibyl_memory_client._adopt import AdoptionRefused
    db = tmp_path / "memory.db"
    _pre_signin_client(db, OTHER).set_entity("project", "theirs", {"x": 1})
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert c.list_entities() == []
    assert c.adoption.status == "skipped" and c.adoption.reason == "other_account"
    assert _rows(db, "entities", DEFAULT_TENANT) == 2
    assert _audit(db) == []
    with pytest.raises(AdoptionRefused) as ei:
        c.adopt_pre_signin_memory()
    assert ei.value.reason == "other_account"
    assert _rows(db, "entities", DEFAULT_TENANT) == 2
    r = c.adopt_pre_signin_memory(confirm_other_accounts=True)
    assert r.status == "moved"
    assert {e["name"] for e in c.list_entities()} == {"atlas", "ada"}
    assert _rows(db, "entities", OTHER) == 1            # never touched
    assert _audit(db)[-1][2] == "command"


def test_other_account_sign_in_without_data_also_blocks_auto(tmp_path) -> None:
    """Signed in as OTHER (audit 'seen' row, no data), logged out, DEFAULT rows
    written, then signed in as a DIFFERENT account: no automatic merge."""
    from sibyl_memory_client import _adopt
    db = tmp_path / "memory.db"
    s = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate()).storage
    r = _adopt.adopt_default_tenant(s, OTHER, SimpleNamespace(ten=OTHER, sub=OTHER),
                                    trigger="auto")
    assert r.status == "nothing_to_move"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert c.list_entities() == []
    assert c.adoption.reason == "other_account"


def test_target_must_belong_to_the_verified_entitlement(tmp_path) -> None:
    from sibyl_memory_client import _adopt
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    # A client whose tenant is not the entitlement's ten/sub never adopts.
    c = MemoryClient.local(str(db), tenant_id="some-override-tenant")
    c.list_entities()
    assert c.adoption is None
    assert _rows(db, "entities", DEFAULT_TENANT) == 2
    s = c.storage
    with pytest.raises(_adopt.AdoptionRefused) as ei:
        _adopt.adopt_default_tenant(s, "some-override-tenant",
                                    SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID))
    assert ei.value.reason == "tenant_mismatch"
    with pytest.raises(_adopt.AdoptionRefused) as ei:
        _adopt.adopt_default_tenant(s, TEST_ACCOUNT_ID, None)
    assert ei.value.reason == "not_verified"
    # The explicit command on such a client is refused too.
    with pytest.raises(_adopt.AdoptionRefused):
        c.adopt_pre_signin_memory()
    assert _rows(db, "entities", DEFAULT_TENANT) == 2


def test_no_gate_no_adoption(tmp_path) -> None:
    """The CLI's no-account reader (NullEntitlementGate) returns no verified
    entitlement, so it never moves anything."""
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                           entitlement_gate=NullEntitlementGate())
    assert c.list_entities() == []
    assert _rows(db, "entities", DEFAULT_TENANT) == 2


def test_signed_out_cannot_adopt(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    with pytest.raises(EntitlementError):
        c.list_entities()
    with pytest.raises(EntitlementError):
        c.adopt_pre_signin_memory()
    assert _rows(db, "entities", DEFAULT_TENANT) == 2


def test_second_account_in_another_home_signs_in(tmp_path, test_signer) -> None:
    """Credentials for OTHER beside the DB: the tenant resolves to OTHER and the
    move follows OTHER's verified entitlement (first account on this store)."""
    home = tmp_path / "h"
    write_credentials(home, account_id=OTHER, session_token="ffffffff-0000-4000-8000-000000000000")
    write_entitlement(home, test_signer.mint(account_id=OTHER,
                                             session_token="ffffffff-0000-4000-8000-000000000000"))
    db = home / "memory.db"
    _seed_pre_signin(db)
    c = MemoryClient.local(str(db), tenant_id=OTHER, credentials_path=home / "credentials.json")
    assert {e["name"] for e in c.list_entities()} == {"atlas", "ada"}


# ----------------------------------------------------------------------
# Atomicity
# ----------------------------------------------------------------------

def test_error_mid_move_rolls_everything_back(tmp_path, monkeypatch) -> None:
    from sibyl_memory_client import _adopt
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    real = _adopt._move_table
    calls = []

    def boom(conn, table, target):
        calls.append(table)
        if table == "journal_events":
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, table, target)
    monkeypatch.setattr(_adopt, "_move_table", boom)
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert c.list_entities() == []          # the op itself still works
    assert "entities" in calls              # entities WERE updated, then rolled back
    assert _rows(db, "entities", DEFAULT_TENANT) == 2
    assert _rows(db, "state_documents", DEFAULT_TENANT) == 1
    assert _rows(db, "entities", TEST_ACCOUNT_ID) == 0
    assert _audit(db) == []
    monkeypatch.setattr(_adopt, "_move_table", real)
    c2 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert {e["name"] for e in c2.list_entities()} == {"atlas", "ada"}


def test_process_crash_mid_move_leaves_no_partial_state(tmp_path) -> None:
    db = tmp_path / "memory.db"
    _seed_pre_signin(db)
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {SRC!r})
        from types import SimpleNamespace
        from sibyl_memory_client import _adopt
        from sibyl_memory_client.storage import Storage
        real = _adopt._move_table
        def crash(conn, table, target):
            n = real(conn, table, target)
            if table == "state_documents":
                os._exit(9)          # hard crash: no rollback, no commit
            return n
        _adopt._move_table = crash
        s = Storage({str(db)!r})
        ent = SimpleNamespace(ten={TEST_ACCOUNT_ID!r}, sub={TEST_ACCOUNT_ID!r})
        _adopt.adopt_default_tenant(s, {TEST_ACCOUNT_ID!r}, ent, trigger="auto")
        sys.exit(0)
    """)
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                          timeout=60)
    assert proc.returncode == 9, proc.stderr.decode()[-500:]
    for t in ("entities", "state_documents", "reference_documents", "journal_events"):
        assert _rows(db, t, TEST_ACCOUNT_ID) == 0, t
    assert _rows(db, "entities", DEFAULT_TENANT) == 2
    assert _audit(db) == []
    c = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)
    assert {e["name"] for e in c.list_entities()} == {"atlas", "ada"}
    assert {h["tier"] for h in c.search("zeppelin")} == {"entity"}


# ----------------------------------------------------------------------
# Coverage guards
# ----------------------------------------------------------------------

def test_every_tenant_table_is_moved(tmp_path) -> None:
    from sibyl_memory_client import _adopt
    from sibyl_memory_client.client import DEFAULT_TENANT as CLIENT_DEFAULT
    assert _adopt.DEFAULT_TENANT == CLIENT_DEFAULT
    c = MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=NullEntitlementGate())
    with c.storage.connection() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        scoped = set()
        for n in names:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{n}')")}
            if "tenant_id" in cols:
                scoped.add(n)
    derived = {"entities_fts", "state_documents_fts", "reference_documents_fts",
               "journal_events_fts", "search_shadow"}
    assert scoped - derived == set(_adopt.TENANT_TABLES)


def test_all_tables_move_including_minor_ones(tmp_path) -> None:
    db = tmp_path / "memory.db"
    c0 = _pre_signin_client(db)
    c0.set_entity("project", "a", {"x": 1})
    c0.set_entity("project", "b", {"x": 2})
    c0.archive_entity("project", "b", reason="done")
    c0.set_state("s", {"k": 1})
    c0.set_reference("r", "ref")
    c0.write_event(acted=["e"])
    with c0.storage.transaction() as conn:
        a_id = conn.execute("SELECT id FROM entities WHERE name='a'").fetchone()[0]
        for sql, args in (
            ("INSERT INTO entity_relations (id, tenant_id, from_id, to_id, relation_type) "
             "VALUES ('r1', ?, ?, ?, 'self')", (DEFAULT_TENANT, a_id, a_id)),
            ("INSERT INTO revenue_events (id, tenant_id, ts) VALUES ('v1', ?, 't')", (DEFAULT_TENANT,)),
            ("INSERT INTO error_events (id, tenant_id, ts) VALUES ('e1', ?, 't')", (DEFAULT_TENANT,)),
            ("INSERT INTO flagged_actors (id, tenant_id) VALUES ('f1', ?)", (DEFAULT_TENANT,)),
            ("INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, "
             "proposed_body, evidence, confidence, summarizer) VALUES "
             "('s1', ?, 'k', 'slug', 'b', '[]', 0.5, 'x')", (DEFAULT_TENANT,)),
            ("INSERT INTO learning_runs (id, tenant_id, summarizer) VALUES ('l1', ?, 'x')",
             (DEFAULT_TENANT,)),
        ):
            conn.execute(sql, args)
    c0.storage.close()
    MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID).list_entities()
    from sibyl_memory_client._adopt import TENANT_TABLES
    for t in TENANT_TABLES:
        assert _rows(db, t, DEFAULT_TENANT) == 0, t
        assert _rows(db, t, TEST_ACCOUNT_ID) >= 1, t


def test_adoption_module_has_no_network_code() -> None:
    src = (Path(SRC) / "sibyl_memory_client" / "_adopt.py").read_text(encoding="utf-8")
    for bad in ("urllib", "socket", "http", "requests", "_heartbeat", "_capcheck"):
        assert f"import {bad}" not in src and f"from .{bad}" not in src, bad
