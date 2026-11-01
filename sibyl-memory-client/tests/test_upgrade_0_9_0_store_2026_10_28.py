"""Upgrade safety: a 0.9.0 (and R2, same schema) store opened by 0.10.0.

Invariants (release merge spec section 5): no existing row is rewritten; the
only additions are memory_links (one row per eligible entity_relations row),
its indexes, version row 6 carrying the copy receipt and one nullable column
on tenant_adoptions; PRAGMA user_version is unchanged; a second open changes
nothing; the 0.9.0 client (released source 708bd89, run in a subprocess) still
reads and searches the upgraded file. A crash inside the copy leaves no table
and no row 6, and the next open succeeds.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import _links
from sibyl_memory_client.exceptions import SchemaError, ValidationError
from sibyl_memory_client.testing import NullEntitlementGate

FIXTURE_V5 = Path(__file__).resolve().parent / "fixtures" / "store_v5_0_9_0.db"
RELEASE_0_9_0 = "708bd89"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def client_090(tmp_path_factory) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    proc = subprocess.run([git, "-C", str(REPO), "archive", RELEASE_0_9_0,
                           "sibyl-memory-client/src"], capture_output=True)
    if proc.returncode != 0:
        pytest.skip(f"0.9.0 source ({RELEASE_0_9_0}) not reachable: "
                    f"{proc.stderr.decode()[-200:]}")
    dest = tmp_path_factory.mktemp("client090u")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        tf.extractall(dest, filter="data")
    return dest / "sibyl-memory-client" / "src"


def _run_090(src: Path, db: Path, body: str) -> dict:
    script = textwrap.dedent(f"""
        import json, sqlite3, sys
        sys.path.insert(0, {str(src)!r})
        import sibyl_memory_client as smc
        assert smc.__file__.startswith({str(src)!r}), smc.__file__
        from sibyl_memory_client import MemoryClient, DEFAULT_TENANT
        from sibyl_memory_client.testing import NullEntitlementGate
        out = {{}}
        db = {str(db)!r}
    """) + textwrap.dedent(body) + "\nprint(json.dumps(out))\n"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])


# A store the 0.9.0 client creates and fills (the R2 case: R2 changes no schema).
_FILL_090 = """
    cl = MemoryClient.local(db, entitlement_gate=NullEntitlementGate())
    for n in ("Maya Patel", "Liam Chen"):
        cl.set_entity("person", n, {"role": "engineer", "note": "kickoff budget"})
    cl.set_entity("company", "Redwood", {"kind": "customer"})
    cl.set_state("directory:customers", {"items": ["Redwood"]})
    cl.set_reference("doc/k", "Kickoff transcript. Redwood budget.")
    cl.storage.close()
    con = sqlite3.connect(db)
    ids = {n: i for i, n in con.execute("SELECT id, name FROM entities")}
    t = DEFAULT_TENANT
    rows = [("r1", t, ids["Maya Patel"], ids["Redwood"], "works_at", '{"since": "2024"}', "2026-01-01T00:00:00.000Z"),
            ("r2", t, ids["Maya Patel"], ids["Redwood"], "Works_At", None, "2026-01-02T00:00:00.000Z"),
            ("r3", t, ids["Liam Chen"], ids["Maya Patel"], "reports_to", None, "2026-01-03T00:00:00.000Z"),
            ("r4", t, ids["Liam Chen"], "no-such-id", "knows", None, "2026-01-04T00:00:00.000Z")]
    con.executemany("INSERT INTO entity_relations (id, tenant_id, from_id, to_id, relation_type, "
                    "metadata, created_at) VALUES (?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()
"""


@pytest.fixture(params=["fixture_0_9_0", "created_by_0_9_0"])
def store(request, tmp_path) -> Path:
    db = tmp_path / "memory.db"
    if request.param == "fixture_0_9_0":
        shutil.copyfile(FIXTURE_V5, db)
    else:
        src = request.getfixturevalue("client_090")
        _run_090(src, db, _FILL_090)
    return db


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _snapshot(db: Path, cols: dict[str, list[str]] | None = None):
    """{table: sha256 of every row, ordered} for every base table except the
    version table; with `cols`, hash those columns only (tables that gain a
    column on upgrade are compared on their original columns)."""
    con = _ro(db)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND (sql IS NULL OR sql NOT LIKE 'CREATE VIRTUAL%') "
            "AND name NOT IN ('sibyl_memory_schema_version', 'memory_links') ORDER BY name")]
        out, seen_cols = {}, {}
        for t in tables:
            cl = (cols or {}).get(t) or [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            seen_cols[t] = cl
            sel = ", ".join(f'"{c}"' for c in cl)
            h = hashlib.sha256()
            for row in con.execute(f'SELECT {sel} FROM "{t}" ORDER BY {sel}'):
                h.update(repr(row).encode())
            out[t] = h.hexdigest()
        versions = con.execute("SELECT version, description FROM sibyl_memory_schema_version "
                               "ORDER BY version").fetchall()
        uv = con.execute("PRAGMA user_version").fetchone()[0]
        has_links = con.execute("SELECT 1 FROM sqlite_master WHERE name='memory_links'").fetchone()
        return out, seen_cols, versions, uv, bool(has_links)
    finally:
        con.close()


def _eligible(db: Path) -> set[tuple]:
    con = _ro(db)
    try:
        rows = con.execute(
            "SELECT r.tenant_id, r.relation_type, f.tenant_id, f.category, f.name, "
            "t.tenant_id, t.category, t.name FROM entity_relations r "
            "JOIN entities f ON f.id = r.from_id JOIN entities t ON t.id = r.to_id").fetchall()
    finally:
        con.close()
    out = set()
    for ten, rel, ft, fc, fk, tt, tc, tk in rows:
        if ft != ten or tt != ten:
            continue
        try:
            out.add((ten, fc, fk, tc, tk, _links.norm_relation(rel)))
        except ValidationError:
            continue
    return out


def _open(db: Path) -> None:
    cl = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    cl.storage.close()


def test_upgrade_adds_links_and_rewrites_nothing(store, client_090) -> None:
    before, cols, v_before, uv_before, had_links = _snapshot(store)
    assert not had_links and [v for v, _ in v_before] == [1, 2, 3, 4, 5]
    eligible = _eligible(store)
    assert eligible

    _open(store)
    after, _, v_after, uv_after, has_links = _snapshot(store, cols)
    assert after == before                                     # no base-table row rewritten
    assert uv_after == uv_before
    assert has_links
    assert v_after[:5] == v_before                             # rows 1..5 untouched
    assert [v for v, _ in v_after] == [1, 2, 3, 4, 5, 6]
    receipt = json.loads(v_after[5][1].split("Migration receipt: ", 1)[1])
    con = _ro(store)
    try:
        links = {(r[0], r[1], r[2], r[3], r[4], r[5]) for r in con.execute(
            "SELECT tenant_id, from_category, from_key, to_category, to_key, relation_type "
            "FROM memory_links WHERE from_tier='entity' AND to_tier='entity'")}
        n_links = con.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        ta_cols = [r[1] for r in con.execute("PRAGMA table_info(tenant_adoptions)")]
    finally:
        con.close()
    assert links == eligible and n_links == len(eligible) == receipt["copied"]
    assert receipt["source_rows"] == sum(1 for _ in _ro(store).execute("SELECT 1 FROM entity_relations"))
    assert "links_audit" in ta_cols

    # a second open changes nothing at all
    snap2 = _snapshot(store, cols)
    _open(store)
    assert _snapshot(store, cols) == snap2

    # the 0.9.0 client still opens, reads and searches the upgraded store
    out = _run_090(client_090, store, """
        cl = MemoryClient.local(db, entitlement_gate=NullEntitlementGate())
        out["schema"] = cl.schema_version()
        with cl.storage.connection() as conn:
            row = conn.execute("SELECT category, name FROM entities WHERE tenant_id = ? LIMIT 1",
                               (DEFAULT_TENANT,)).fetchone()
        out["got"] = cl.get_entity(row[0], row[1])["name"] == row[1]
        out["hits"] = len(cl.search(row[1].split()[0], limit=5))
        cl.storage.close()
    """)
    assert out["got"] is True and out["hits"] >= 1
    assert _snapshot(store, cols)[0] == after                   # 0.9.0 read path wrote nothing


def test_crash_inside_the_copy_rolls_back(store, monkeypatch) -> None:
    before, cols, v_before, uv_before, _ = _snapshot(store)

    def boom(conn):
        raise sqlite3.OperationalError("simulated crash inside the v6 copy")

    real = _links._copy_entity_relations
    monkeypatch.setattr(_links, "_copy_entity_relations", boom)
    with pytest.raises(SchemaError):
        _open(store)
    snap = _snapshot(store, cols)
    assert snap[0] == before and snap[2] == v_before and snap[3] == uv_before
    assert snap[4] is False                                    # no memory_links table
    monkeypatch.setattr(_links, "_copy_entity_relations", real)
    _open(store)
    after, _, v_after, _, has_links = _snapshot(store, cols)
    assert has_links and [v for v, _ in v_after] == [1, 2, 3, 4, 5, 6] and after == before
