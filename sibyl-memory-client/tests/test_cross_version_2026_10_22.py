"""Cross-version harness: the 0.9.0 client and this client against ONE store.

The 0.9.0 client is the released source at commit 708bd89, extracted with
``git archive`` into a temp dir and driven in a subprocess (so its modules never
mix with this package). Sequence (YARROW skew.py, panel theme A):

  V1   open, write entities + edges (pre-sign-in and account)
  0.9.0 open the v6 store, hard-delete an edge endpoint (no cascade: 0.9.0 does
       not know memory_links), run 0.9.0 adoption (renames entities, never
       rewrites edges, leaves memory_links under the default tenant)
  V1   reopen, adopt again

Asserted with the identity stamp: no edge ends up attached to a row other than
the one it was written for, and the dangling edge is reported (adoption result
and the dangling-link lint), not silently re-attached.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import types
from pathlib import Path

import pytest

from sibyl_memory_client import DEFAULT_TENANT, MemoryClient, entity_ref, reference_ref
from sibyl_memory_client import _adopt, _links
from sibyl_memory_client.testing import TEST_ACCOUNT_ID, NullEntitlementGate

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
    dest = tmp_path_factory.mktemp("client090")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        tf.extractall(dest, filter="data")
    src = dest / "sibyl-memory-client" / "src"
    assert (src / "sibyl_memory_client" / "_adopt.py").exists()
    return src


def _run_090(src: Path, db: Path, body: str) -> dict:
    script = textwrap.dedent(f"""
        import json, sys, types
        sys.path.insert(0, {str(src)!r})
        import sibyl_memory_client as smc
        assert smc.__file__.startswith({str(src)!r}), smc.__file__
        from sibyl_memory_client import MemoryClient, _adopt
        from sibyl_memory_client.testing import TEST_ACCOUNT_ID, NullEntitlementGate
        out = {{}}
        db = {str(db)!r}
    """) + textwrap.dedent(body) + "\nprint(json.dumps(out))\n"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    return json.loads(proc.stdout.decode().strip().splitlines()[-1])


def _edges(db: Path) -> dict[str, tuple]:
    con = sqlite3.connect(str(db))
    try:
        return {r[0]: tuple(r[1:]) for r in con.execute(
            "SELECT id, tenant_id, from_key, to_key, relation_type, from_entity_id, to_entity_id "
            "FROM memory_links")}
    finally:
        con.close()


def _entity_by_id(db: Path) -> dict[str, tuple]:
    con = sqlite3.connect(str(db))
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            "SELECT id, tenant_id, category, name FROM entities")}
    finally:
        con.close()


def test_090_then_v1_never_reattaches_an_edge(tmp_path, client_090) -> None:
    db = tmp_path / "memory.db"
    maya, redwood, doc = (entity_ref("person", "Maya Patel"), entity_ref("company", "Redwood"),
                          reference_ref("doc/k"))
    # -- V1: write ------------------------------------------------------
    pre = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    acct = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                              entitlement_gate=NullEntitlementGate())
    acct.set_entity("person", "Maya Patel", {"who": "acct"})
    pre_maya = pre.set_entity("person", "Maya Patel", {"who": "pre"})["id"]
    pre.set_entity("company", "Redwood", {})
    pre.set_reference("doc/k", "pre doc")
    pre.link(maya, redwood, "works_at")
    pre.link(maya, doc, "about")
    pre.link(doc, maya, "mentions")
    pre.storage.close()
    acct.storage.close()
    written = _edges(db)
    assert len(written) == 3

    # -- 0.9.0: delete + adopt -------------------------------------------
    out = _run_090(client_090, db, """
        cl = MemoryClient.local(db, entitlement_gate=NullEntitlementGate())
        out["schema"] = cl.schema_version()
        out["deleted"] = cl.delete_entity("company", "Redwood")
        ent = types.SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID)
        res = _adopt.adopt_default_tenant(cl.storage, TEST_ACCOUNT_ID, ent, trigger="command")
        out["status"] = res.status
        out["renamed"] = res.renamed
        cl.storage.close()
    """)
    assert out["deleted"] is True and out["status"] == "moved"
    assert out["renamed"] == [{"table": "entities", "category": "person", "from": "Maya Patel",
                               "to": "Maya Patel (before sign-in)"}]
    # 0.9.0 left every edge under the default tenant, keyed by the OLD name.
    assert {v[0] for v in _edges(db).values()} == {DEFAULT_TENANT}

    # -- V1: reopen + adopt ------------------------------------------------
    v1 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                            entitlement_gate=NullEntitlementGate())
    ent = types.SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID)
    res = _adopt.adopt_default_tenant(v1.storage, TEST_ACCOUNT_ID, ent, trigger="command")
    assert res.status == "moved"
    assert res.held_links == [] and res.dropped_links == []
    assert res.dangling_links == 1                         # works_at -> deleted Redwood

    final = _edges(db)
    by_id = _entity_by_id(db)
    assert set(final) == set(written)                      # no edge lost or duplicated
    for eid, (tenant, fkey, tkey, rel, fstamp, tstamp) in final.items():
        assert tenant == TEST_ACCOUNT_ID
        assert (fstamp, tstamp) == written[eid][4:]       # identity never rewritten
        for stamp, key in ((fstamp, fkey), (tstamp, tkey)):
            if stamp is None:
                continue
            if stamp in by_id:   # the row it was written for: the edge names it
                assert by_id[stamp][0] == tenant and by_id[stamp][2] == key, (eid, rel)
            else:                # gone: the key must not be a live namesake
                assert not any(v[2] == key and v[0] == tenant for v in by_id.values()), (eid, rel)
    assert by_id[pre_maya][2] == "Maya Patel (before sign-in)"
    with v1.storage.connection() as conn:
        assert _links.stale_ids(conn, TEST_ACCOUNT_ID, list(final)) == set()
    # The account's own Maya inherits nothing.
    assert v1.neighbors(maya)["edges"] == []
    # The dangling edge is reported by lint.
    from sibyl_memory_client.lint import Linter
    rep = Linter(v1.storage, tenant_id=TEST_ACCOUNT_ID).run()
    dangling = [f for f in rep.findings if f.check == "dangling-link"]
    assert dangling and "Redwood" in json.dumps([f.detail for f in dangling])

    # -- 0.9.0 can still open the store V1 last wrote ---------------------
    v1.storage.close()
    out = _run_090(client_090, db, """
        cl = MemoryClient.local(db, tenant_id=TEST_ACCOUNT_ID,
                                entitlement_gate=NullEntitlementGate())
        out["names"] = sorted(e["name"] for e in cl.list_entities())
        cl.storage.close()
    """)
    assert out["names"] == ["Maya Patel", "Maya Patel (before sign-in)"]


def test_090_adoption_of_archived_namesake_then_v1(tmp_path, client_090) -> None:
    """LARCH exp2 across versions: the pre-sign-in "Acme" was archived (V1),
    then 0.9.0 adopts (moves the archived row, not the edges); V1's adoption
    must not hand the old Acme's edges to the account's live Acme."""
    db = tmp_path / "memory.db"
    acme = entity_ref("company", "Acme")
    pre = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    acct = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                              entitlement_gate=NullEntitlementGate())
    acct.set_entity("company", "Acme", {"kind": "customer"})
    pre.set_entity("company", "Acme", {"kind": "test vendor"})
    pre.set_entity("person", "Bob", {})
    pre.link(entity_ref("person", "Bob"), acme, "works_at")
    pre.archive_entity("company", "Acme")
    pre.storage.close()
    acct.storage.close()
    out = _run_090(client_090, db, """
        cl = MemoryClient.local(db, entitlement_gate=NullEntitlementGate())
        ent = types.SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID)
        out["status"] = _adopt.adopt_default_tenant(cl.storage, TEST_ACCOUNT_ID, ent,
                                                     trigger="command").status
        cl.storage.close()
    """)
    assert out["status"] == "moved"
    v1 = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                            entitlement_gate=NullEntitlementGate())
    ent = types.SimpleNamespace(ten=TEST_ACCOUNT_ID, sub=TEST_ACCOUNT_ID)
    res = _adopt.adopt_default_tenant(v1.storage, TEST_ACCOUNT_ID, ent, trigger="command")
    assert res.status == "moved"
    assert v1.neighbors(acme)["edges"] == []
    twin = v1.neighbors(entity_ref("company", "Acme (before sign-in)"))["edges"]
    assert [(e["relation"], e["node"]["key"]) for e in twin] == [("works_at", "Bob")]
    con = sqlite3.connect(str(db))
    try:   # the archived row moved by 0.9.0 is renamed with its edges
        assert sorted(r[0] for r in con.execute("SELECT name FROM archived_entities")) == [
            "Acme (before sign-in)"]
    finally:
        con.close()
