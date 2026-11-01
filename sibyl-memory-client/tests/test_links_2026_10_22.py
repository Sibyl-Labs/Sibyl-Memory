"""v6 relations: a node graph across tiers (spec tiered-retrieval 2.B).

memory_links edges keyed by business keys across entity / reference / state;
link / unlink / neighbors / links_of; links= on set_entity and set_reference;
cascade on hard delete, survival on archive; adoption rename rewrite; tenant
isolation; cap accounting; the v5 -> v6 migration on an existing 0.9.0 store.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from sibyl_memory_client import (
    DEFAULT_TENANT,
    CapExceededError,
    CapGate,
    MemoryClient,
    Storage,
    TierCache,
    TierVerificationError,
    entity_ref,
    reference_ref,
    state_ref,
)
from sibyl_memory_client import _links
from sibyl_memory_client.exceptions import NotFoundError, ValidationError
from sibyl_memory_client.storage import db_size_bytes
from sibyl_memory_client.testing import TEST_ACCOUNT_ID, NullEntitlementGate

FIXTURE_V5 = Path(__file__).resolve().parent / "fixtures" / "store_v5_0_9_0.db"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"   # second tenant inside the fixture

MAYA = entity_ref("person", "Maya Patel")
REDWOOD = entity_ref("company", "Redwood")
DOC = reference_ref("doc/fireflies/kickoff")
DIR = state_ref("directory:customers")


def _client(tmp_path, tenant=DEFAULT_TENANT, name="m.db") -> MemoryClient:
    return MemoryClient.local(str(tmp_path / name), tenant_id=tenant,
                              entitlement_gate=NullEntitlementGate())


@pytest.fixture
def c(tmp_path) -> MemoryClient:
    cl = _client(tmp_path)
    cl.set_entity("person", "Maya Patel", {"role": "lead"})
    cl.set_entity("company", "Redwood", {"kind": "customer"})
    cl.set_reference("doc/fireflies/kickoff", "Kickoff transcript. Redwood onboarding.",
                     metadata={"title": "Kickoff"})
    cl.set_state("directory:customers", {"items": ["Redwood"]})
    return cl


def _count(cl: MemoryClient) -> int:
    with cl.storage.connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]


# ----------------------------------------------------------------------
# Round trips across tiers
# ----------------------------------------------------------------------

def test_link_round_trip_across_all_tiers(c) -> None:
    r1 = c.link(MAYA, REDWOOD, "Works_At", metadata={"since": "2024"})
    assert r1["created"] is True
    c.link(REDWOOD, DOC, "source_of", metadata={"date": "2026-03-10"})
    c.link(DOC, MAYA, "mentions")
    c.link(DIR, REDWOOD, "lists")
    out = c.neighbors(REDWOOD)
    got = {(e["direction"], e["relation"], e["node"]["tier"], e["node"]["key"]) for e in out["edges"]}
    assert got == {("in", "works_at", "entity", "Maya Patel"),
                   ("out", "source_of", "reference", "doc/fireflies/kickoff"),
                   ("in", "lists", "state", "directory:customers")}
    assert out["degree"] == {"out": 1, "in": 2}
    assert all(e["node"]["exists"] for e in out["edges"])
    doc_edge = next(e for e in out["edges"] if e["relation"] == "source_of")
    assert doc_edge["metadata"] == {"date": "2026-03-10"}
    assert doc_edge["node"]["category"] is None
    assert c.storage.count_rows("memory_links", DEFAULT_TENANT) == 4


def test_link_is_idempotent_and_upserts_metadata(c) -> None:
    a = c.link(MAYA, REDWOOD, "works_at", metadata={"since": "2024"})
    b = c.link(MAYA, REDWOOD, "WORKS_AT")                 # None keeps metadata
    assert b == {"id": a["id"], "created": False, "changed": False}
    assert c.neighbors(MAYA)["edges"][0]["metadata"] == {"since": "2024"}
    assert c.link(MAYA, REDWOOD, "works_at", metadata={"since": "2025"})["changed"] is True
    assert c.neighbors(MAYA)["edges"][0]["metadata"] == {"since": "2025"}
    assert _count(c) == 1


def test_unlink_with_and_without_relation(c) -> None:
    c.link(MAYA, REDWOOD, "works_at")
    c.link(MAYA, REDWOOD, "advises")
    c.link(MAYA, DOC, "about")
    assert c.unlink(MAYA, REDWOOD, "advises") == 1
    assert c.unlink(MAYA, REDWOOD, "advises") == 0
    c.link(MAYA, REDWOOD, "advises")
    assert c.unlink(MAYA, REDWOOD) == 2
    assert [e["relation"] for e in c.neighbors(MAYA)["edges"]] == ["about"]


def test_direction_relation_tier_category_filters(c) -> None:
    c.set_entity("project", "atlas", {"x": 1})
    atlas = entity_ref("project", "atlas")
    c.link(MAYA, REDWOOD, "works_at")
    c.link(MAYA, atlas, "member_of")
    c.link(MAYA, DOC, "about")
    c.link(REDWOOD, MAYA, "employs")
    assert {e["relation"] for e in c.neighbors(MAYA, direction="out")["edges"]} == {
        "works_at", "member_of", "about"}
    assert [e["relation"] for e in c.neighbors(MAYA, direction="in")["edges"]] == ["employs"]
    assert {e["relation"] for e in c.neighbors(MAYA, relation=["works_at", "EMPLOYS"])["edges"]} == {
        "works_at", "employs"}
    assert [e["node"]["key"] for e in c.neighbors(MAYA, tier="reference")["edges"]] == [
        "doc/fireflies/kickoff"]
    assert [e["node"]["key"] for e in c.neighbors(MAYA, direction="out", category="project")["edges"]] == [
        "atlas"]


def test_self_loop_listed_once_under_both(c) -> None:
    c.link(MAYA, MAYA, "same_as")
    both = c.neighbors(MAYA)
    assert [(e["direction"], e["relation"]) for e in both["edges"]] == [("out", "same_as")]
    assert len(c.neighbors(MAYA, direction="in")["edges"]) == 1


def test_bare_category_name_is_an_entity_noderef(c) -> None:
    c.link({"category": "person", "name": "Maya Patel"}, {"tier": "reference", "key": DOC["key"]},
           "about")
    assert c.neighbors({"category": "company", "name": "Redwood"})["edges"] == []
    assert len(c.neighbors(DOC)["edges"]) == 1


@pytest.mark.parametrize("bad", [
    {"tier": "journal", "key": "x"},
    {"tier": "entity", "key": "x"},                         # entity needs a category
    {"tier": "reference", "category": "c", "key": "x"},     # reference has no category
    "person/Maya",
    {"tier": "state", "key": ""},
])
def test_bad_noderefs_are_validation_errors(c, bad) -> None:
    with pytest.raises(ValidationError):
        c.link(bad, MAYA, "x")


def test_relation_and_metadata_bounds(c) -> None:
    with pytest.raises(ValidationError):
        c.link(MAYA, REDWOOD, "r" * 65)
    with pytest.raises(ValidationError):
        c.link(MAYA, REDWOOD, "")
    with pytest.raises(ValidationError):
        c.link(MAYA, REDWOOD, "x", metadata={"big": "y" * 5000})
    with pytest.raises(ValidationError):
        c.link(MAYA, REDWOOD, "x", metadata=["not", "an", "object"])
    with pytest.raises(ValidationError):
        c.neighbors(MAYA, direction="sideways")
    with pytest.raises(ValidationError):
        c.neighbors(MAYA, cursor="not-a-cursor!!")


# ----------------------------------------------------------------------
# Pagination
# ----------------------------------------------------------------------

def test_neighbors_pagination_is_complete_and_stable_under_inserts(c) -> None:
    for i in range(23):
        c.set_entity("ticket", f"T-{i:02d}", {"i": i})
        c.link(REDWOOD, entity_ref("ticket", f"T-{i:02d}"), "owns")
    seen: list[str] = []
    page = c.neighbors(REDWOOD, limit=10)
    seen += [e["id"] for e in page["edges"]]
    # A concurrent insert between pages lands after the cursor, never in a
    # page already returned and never displacing an unseen row.
    c.set_entity("ticket", "T-new", {})
    c.link(REDWOOD, entity_ref("ticket", "T-new"), "owns")
    while page["next_cursor"]:
        page = c.neighbors(REDWOOD, limit=10, cursor=page["next_cursor"])
        seen += [e["id"] for e in page["edges"]]
    assert len(seen) == len(set(seen)) == 24
    assert c.neighbors(REDWOOD, limit=1000)["edges"].__len__() == 24   # clamped to 200
    assert len(c.neighbors(REDWOOD, limit=0)["edges"]) == 1            # clamped to 1


def test_links_of_batch_summary(c) -> None:
    c.link(MAYA, REDWOOD, "works_at")
    c.link(MAYA, DOC, "about")
    c.link(REDWOOD, DOC, "source_of")
    out = c.links_of([MAYA, REDWOOD, DIR], per_node=1)
    m = out[("entity", "person", "Maya Patel")]
    assert m["degree"] == {"out": 2, "in": 0}
    assert m["by_relation"] == {"works_at": 1, "about": 1}
    assert len(m["sample"]) == 1
    assert out[("entity", "company", "Redwood")]["degree"] == {"out": 1, "in": 1}
    assert out[("state", None, "directory:customers")]["degree"] == {"out": 0, "in": 0}


# ----------------------------------------------------------------------
# Strict endpoints and links= atomicity
# ----------------------------------------------------------------------

def test_link_requires_both_endpoints_and_names_the_side(c) -> None:
    with pytest.raises(NotFoundError, match="source"):
        c.link(entity_ref("person", "Ghost"), REDWOOD, "x")
    with pytest.raises(NotFoundError, match="target"):
        c.link(MAYA, reference_ref("doc/missing"), "x")
    assert _count(c) == 0


def test_links_on_set_entity_writes_node_and_edges_together(c) -> None:
    c.set_entity("project", "atlas", {"x": 1},
                 links=[{"to": REDWOOD, "relation": "part_of"},
                        {"to": DOC, "relation": "source_of", "metadata": {"date": "2026"}},
                        {"to": entity_ref("project", "atlas"), "relation": "same_as"}])
    out = c.neighbors(entity_ref("project", "atlas"), direction="out")
    assert {e["relation"] for e in out["edges"]} == {"part_of", "source_of", "same_as"}


def test_links_on_set_entity_rolls_back_node_on_bad_edge(c) -> None:
    with pytest.raises(NotFoundError):
        c.set_entity("project", "atlas", {"x": 1},
                     links=[{"to": REDWOOD, "relation": "part_of"},
                            {"to": entity_ref("company", "Typo Corp"), "relation": "part_of"}])
    with pytest.raises(NotFoundError):
        c.get_entity("project", "atlas")
    assert _count(c) == 0
    # An UPDATE of an existing entity rolls back too (body unchanged).
    with pytest.raises(NotFoundError):
        c.set_entity("person", "Maya Patel", {"role": "changed"},
                     links=[{"to": reference_ref("doc/nope"), "relation": "about"}])
    assert c.get_entity("person", "Maya Patel")["body"] == {"role": "lead"}


def test_links_on_set_reference_strict_and_atomic(c) -> None:
    c.set_reference("doc/jira/SUP-1", "ticket text", links=[{"to": REDWOOD, "relation": "about"}])
    assert c.neighbors(REDWOOD, direction="in")["edges"][0]["node"]["key"] == "doc/jira/SUP-1"
    with pytest.raises(NotFoundError):
        c.set_reference("doc/jira/SUP-2", "ticket text",
                        links=[{"to": entity_ref("company", "Nope"), "relation": "about"}])
    assert c.get_reference("doc/jira/SUP-2") is None


def test_links_argument_validation(c) -> None:
    with pytest.raises(ValidationError):
        c.set_entity("project", "p", {}, links={"to": REDWOOD, "relation": "x"})
    with pytest.raises(ValidationError):
        c.set_entity("project", "p", {}, links=[{"to": REDWOOD}])
    too_many = [{"to": REDWOOD, "relation": "x"}] * (_links.MAX_LINKS_PER_WRITE + 1)
    with pytest.raises(ValidationError):
        c.set_entity("project", "p", {}, links=too_many)
    with pytest.raises(NotFoundError):
        c.get_entity("project", "p")


# ----------------------------------------------------------------------
# Cascade semantics
# ----------------------------------------------------------------------

def test_delete_entity_cascades_every_touching_edge(c) -> None:
    c.link(MAYA, REDWOOD, "works_at")
    c.link(DOC, MAYA, "mentions")
    c.link(REDWOOD, DOC, "source_of")
    assert c.delete_entity("person", "Maya Patel") is True
    assert _count(c) == 1
    assert c.neighbors(REDWOOD)["edges"][0]["relation"] == "source_of"


def test_archive_keeps_edges_reports_archived_and_recreate_restores(c) -> None:
    c.link(MAYA, REDWOOD, "works_at")
    c.link(DOC, MAYA, "mentions")
    c.archive_entity("person", "Maya Patel", reason="left")
    assert _count(c) == 2                                   # no silent loss on archive
    edges = c.neighbors(REDWOOD)["edges"]
    node = edges[0]["node"]
    assert node["tier"] == "entity" and node["exists"] is False
    assert node["archived"] is True and node["status"] == "archived" and node["archived_at"]
    # The archived side can still be walked (business keys, no row needed).
    assert len(c.neighbors(MAYA)["edges"]) == 2
    c.set_entity("person", "Maya Patel", {"role": "back"})
    assert all(e["node"]["exists"] for e in c.neighbors(REDWOOD)["edges"])


def test_missing_endpoint_without_archive_reports_exists_false(c) -> None:
    c.link(MAYA, DIR, "watches")
    with c.storage.transaction() as conn:   # out-of-band removal of the state doc
        conn.execute("DELETE FROM state_documents WHERE document_key = 'directory:customers'")
    node = c.neighbors(MAYA)["edges"][0]["node"]
    assert node["exists"] is False and "archived" not in node


def test_cascade_helper_is_the_delete_reference_merge_point(c) -> None:
    c.link(MAYA, DOC, "about")
    c.link(DOC, REDWOOD, "about")
    with c.storage.transaction() as conn:
        n = c._cascade_node_links(conn, "reference", None, DOC["key"])
    assert n == 2 and _count(c) == 0


def test_delete_reference_cascades_edges(c) -> None:
    c.link(MAYA, DOC, "about")
    c.link(DOC, REDWOOD, "about")
    c.link(MAYA, REDWOOD, "works_at")
    assert c.delete_reference(DOC["key"])
    assert _count(c) == 1


# ----------------------------------------------------------------------
# Tenant isolation
# ----------------------------------------------------------------------

def test_cross_tenant_link_refused_and_invisible(c, tmp_path) -> None:
    other = _client(tmp_path, tenant=OTHER)
    other.set_entity("person", "Eve", {})
    c.link(MAYA, REDWOOD, "works_at")
    # Eve's tenant cannot point at Maya (exists only in DEFAULT_TENANT) ...
    with pytest.raises(NotFoundError):
        other.link(entity_ref("person", "Eve"), MAYA, "spies_on")
    # ... and sees none of DEFAULT_TENANT's edges, by any route.
    assert other.neighbors(MAYA)["edges"] == []
    assert other.neighbors(MAYA)["degree"] == {"out": 0, "in": 0}
    assert other.links_of([MAYA])[("entity", "person", "Maya Patel")]["degree"] == {"out": 0, "in": 0}
    assert other.unlink(MAYA, REDWOOD) == 0
    assert _count(c) == 1
    # Same key in both tenants: each tenant's edges stay its own.
    other.set_entity("person", "Maya Patel", {})
    other.set_entity("company", "Redwood", {})
    other.link(MAYA, REDWOOD, "works_at")
    assert _count(c) == 2
    assert c.neighbors(MAYA)["degree"]["out"] == 1


# ----------------------------------------------------------------------
# Cap accounting: edges count toward the free-tier cap by bytes
# ----------------------------------------------------------------------

def test_edges_alone_can_hit_the_free_cap(tmp_path) -> None:
    db = tmp_path / "memory.db"
    storage = Storage(str(db))
    cap = db_size_bytes(db) + 40 * 1024

    def offline_fn(url, payload, timeout=4.0):
        raise TierVerificationError("blackholed")

    gate = CapGate(account_id=None, session_token=None, db_size_fn=lambda: 0,
                   local_tier_hint="free", cache=TierCache(tmp_path / "tc.json"),
                   check_fn=offline_fn, cap_bytes=cap)
    cl = MemoryClient(storage=storage, tenant_id="qa", tier="free", cap_gate=gate)
    cl.set_entity("hub", "h", {"x": 1})
    cl.set_entity("leaf", "l", {"x": 1})
    with pytest.raises(CapExceededError):
        for i in range(2000):
            cl.link(entity_ref("hub", "h"), entity_ref("leaf", "l"), f"r{i}",
                    metadata={"pad": "p" * 900})
    assert db_size_bytes(db) <= cap + 8192


def test_cap_gate_estimate_includes_edge_bytes(c) -> None:
    seen: list[int] = []

    class Rec:
        def check(self, *, proposed_delta_bytes: int) -> None:
            seen.append(proposed_delta_bytes)

        def check_total_local(self, total: int) -> None:
            pass

    c._cap_gate = Rec()
    c.set_entity("project", "p", {"x": 1})
    c.set_entity("project", "p", {"x": 1},
                 links=[{"to": REDWOOD, "relation": "part_of", "metadata": {"pad": "z" * 500}}])
    assert seen[1] - seen[0] >= 500 + _links.EDGE_OVERHEAD_BYTES
    c.link(MAYA, REDWOOD, "works_at")
    assert seen[2] >= _links.EDGE_OVERHEAD_BYTES


# ----------------------------------------------------------------------
# Gate + lint
# ----------------------------------------------------------------------

def test_link_ops_are_write_ops_and_reads_are_not() -> None:
    from sibyl_memory_client._entitlement import WRITE_OPS
    assert {"link", "unlink"} <= WRITE_OPS
    assert not ({"neighbors", "links_of"} & WRITE_OPS)


def test_lint_link_rules(c, monkeypatch) -> None:
    from sibyl_memory_client.lint import Linter
    c.set_reference("doc/orphan", "nobody links here")
    c.link(REDWOOD, DOC, "source_of")
    c.link(MAYA, DIR, "watches")
    c.archive_entity("person", "Maya Patel")
    c.set_entity("x", "temp", {})
    c.link(entity_ref("x", "temp"), REDWOOD, "r")
    with c.storage.transaction() as conn:   # out-of-band hard delete, no cascade
        conn.execute("DELETE FROM entities WHERE category = 'x'")
    monkeypatch.setattr(_links, "HUB_DEGREE_WARN", 0)
    rep = Linter(c.storage, tenant_id=DEFAULT_TENANT).run()
    by = {}
    for f in rep.findings:
        by.setdefault(f.check, []).append(f)
    assert {f.detail["archived"] for f in by["dangling-link"]} == {True, False}
    assert {f.severity for f in by["dangling-link"]} == {"info", "warning"}
    assert by["reference-orphan"][0].detail["sample"] == ["doc/orphan"]
    assert "hub-degree" in by
    assert rep.counts["memory_links"] == 3


def test_lint_quiet_when_no_edges(c) -> None:
    from sibyl_memory_client.lint import Linter
    rep = Linter(c.storage, tenant_id=DEFAULT_TENANT).run()
    assert not {"reference-orphan", "dangling-link", "hub-degree"} & {f.check for f in rep.findings}


# ----------------------------------------------------------------------
# Adoption rename rewrites both endpoint columns
# ----------------------------------------------------------------------

def test_adoption_rename_rewrites_edges_and_dedupes(tmp_path) -> None:
    db = tmp_path / "memory.db"
    pre = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    acct = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID,
                              entitlement_gate=NullEntitlementGate())
    # Account side: Maya exists (forces a rename of the pre-sign-in Maya).
    # Account side also holds the edge Redwood --lists--> Atlas with BOTH ends
    # archived, so no rename happens and the pre-sign-in copy of that edge is
    # identical on the UNIQUE.
    hub, atlas = REDWOOD, entity_ref("project", "Atlas")
    for cl in (pre, acct):
        cl.set_entity("person", "Maya Patel", {"who": "pre" if cl is pre else "acct"})
        cl.set_entity("company", "Redwood", {})
        cl.set_entity("project", "Atlas", {})
        cl.link(hub, atlas, "lists")
    acct.archive_entity("company", "Redwood")
    acct.archive_entity("project", "Atlas")
    pre.set_reference("doc/k", "pre doc")
    pre.link(MAYA, REDWOOD, "works_at")
    pre.link(entity_ref("person", "Maya Patel"), reference_ref("doc/k"), "about")
    pre.link(reference_ref("doc/k"), MAYA, "mentions")
    pre.storage.close()
    acct.storage.close()

    signed_in = MemoryClient.local(str(db), tenant_id=TEST_ACCOUNT_ID)   # auto adoption
    signed_in.list_entities()
    res = signed_in.adoption
    assert res is not None and res.status == "moved"
    new_maya = entity_ref("person", "Maya Patel (before sign-in)")
    rel = {(e["direction"], e["relation"], e["node"]["key"]) for e in signed_in.neighbors(new_maya)["edges"]}
    assert rel == {("out", "works_at", "Redwood"), ("out", "about", "doc/k"),
                   ("in", "mentions", "doc/k")}
    # The account's own Maya is untouched and has no edges.
    assert signed_in.neighbors(MAYA)["edges"] == []
    # The identical Redwood -> Atlas edge is kept once (the account's row).
    assert [e["relation"] for e in signed_in.neighbors(hub, relation="lists")["edges"]] == ["lists"]
    assert len(res.dropped_duplicate_links) == 1
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM memory_links WHERE tenant_id = ?",
                           (DEFAULT_TENANT,)).fetchone()[0] == 0
        renamed, la = con.execute(
            "SELECT renamed, links_audit FROM tenant_adoptions WHERE kind='auto'").fetchone()
    finally:
        con.close()
    # renamed is a homogeneous list of renames; edge outcomes live in links_audit.
    assert all({"table", "from", "to"} <= set(e) for e in json.loads(renamed))
    dropped = json.loads(la)["dropped"]
    assert [d["reason"] for d in dropped] == ["duplicate"]
    assert {"id", "from", "to", "relation", "metadata", "reason"} == set(dropped[0])


def test_rename_endpoint_collision_drops_and_audits(tmp_path) -> None:
    """NOC 10b: a rename onto a key whose edge already exists used to raise on
    the UNIQUE and roll the caller (adoption) back on every sign-in. The moving
    copy is the same fact: it is dropped and reported, the rest proceeds."""
    cl = _client(tmp_path)
    cl.set_entity("p", "a", {})
    cl.set_entity("p", "b", {})
    cl.set_entity("p", "c", {})
    cl.link(entity_ref("p", "a"), entity_ref("p", "b"), "r", metadata={"m": 1})
    cl.link(entity_ref("p", "b"), entity_ref("p", "b"), "r")
    cl.link(entity_ref("p", "a"), entity_ref("p", "c"), "r")
    dropped: list = []
    with cl.storage.transaction() as conn:
        n = _links.rename_endpoint(conn, DEFAULT_TENANT, "entity", "p", "a", "b", dropped=dropped)
    assert n == 1                                   # a->c became b->c
    assert [(d["from"]["key"], d["to"]["key"], d["reason"], d["metadata"]) for d in dropped] == [
        ("a", "b", "rename_collision", {"m": 1})]
    assert _count(cl) == 2


# ----------------------------------------------------------------------
# Schema v6 and the v5 -> v6 migration on an existing 0.9.0 store
# ----------------------------------------------------------------------

def _norm_sql(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_schema_sql_carries_the_same_v6_ddl() -> None:
    import sibyl_memory_client
    sql = _norm_sql((Path(sibyl_memory_client.__file__).parent / "schema.sql").read_text())
    for stmt in _links.V6_DDL:
        assert _norm_sql(stmt) + ";" in sql, stmt[:60]


def test_fresh_store_is_v6(tmp_path) -> None:
    cl = _client(tmp_path)
    assert cl.schema_version() == 6
    assert cl.storage.count_rows("memory_links", DEFAULT_TENANT) == 0


def test_category_check_constraint(c) -> None:
    with c.storage.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO memory_links (id, tenant_id, from_tier, from_category, "
                         "from_key, to_tier, to_category, to_key, relation_type) VALUES "
                         "('x', 't', 'reference', 'oops', 'k', 'state', '', 'k', 'r')")


def _v5_copy(tmp_path) -> Path:
    db = tmp_path / "memory.db"
    shutil.copyfile(FIXTURE_V5, db)
    return db


def test_fixture_is_a_real_0_9_0_v5_store(tmp_path) -> None:
    db = _v5_copy(tmp_path)
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT MAX(version) FROM sibyl_memory_schema_version").fetchone()[0] == 5
        assert con.execute("SELECT name FROM sqlite_master WHERE name='memory_links'").fetchone() is None
        assert con.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 5
        assert con.execute("PRAGMA user_version").fetchone()[0] > 0   # opened by 0.9.0
    finally:
        con.close()


def test_v5_to_v6_migration_copies_entity_relations(tmp_path) -> None:
    db = _v5_copy(tmp_path)
    cl = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    assert cl.schema_version() == 6
    out = cl.neighbors(MAYA, direction="out")
    got = sorted((e["relation"], e["node"]["key"], e["created_at"]) for e in out["edges"])
    # rel-1 'Works_At' and rel-3 'works_at' fold to one edge (the earlier row
    # wins, metadata kept); rel-4 points at another tenant's id: not copied.
    assert got == [("member_of", "atlas", "2026-01-02T00:00:00.000Z"),
                   ("works_at", "Redwood", "2026-01-01T00:00:00.000Z")]
    assert next(e for e in out["edges"] if e["relation"] == "works_at")["metadata"] == {"since": "2024"}
    other = MemoryClient.local(str(db), tenant_id=OTHER, entitlement_gate=NullEntitlementGate())
    assert [e["relation"] for e in other.neighbors(MAYA)["edges"]] == ["self"]
    # Existing data is intact and still searchable.
    assert cl.get_entity("company", "Redwood")["body"] == {"kind": "customer"}
    assert cl.get_reference("doc/kickoff")["body"].startswith("Kickoff")
    assert any(h.get("key") == "doc/kickoff" for h in cl.search("kickoff", tiers=("reference",)))
    # entity_relations is left in place, unwritten.
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 5
    finally:
        con.close()


def test_v6_copy_runs_once_unlinked_edge_not_resurrected(tmp_path) -> None:
    db = _v5_copy(tmp_path)
    cl = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    assert cl.unlink(MAYA, REDWOOD, "works_at") == 1
    cl.storage.close()
    again = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    assert [e["relation"] for e in again.neighbors(MAYA)["edges"]] == ["member_of"]


def test_v6_migration_is_atomic_on_failure(tmp_path, monkeypatch) -> None:
    db = _v5_copy(tmp_path)

    def boom(conn):
        for stmt in _links.V6_DDL:
            conn.execute(stmt)
        raise sqlite3.OperationalError("simulated crash mid-copy")

    monkeypatch.setattr(_links, "apply_v6_migration", boom)
    with pytest.raises(Exception):
        MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    con = sqlite3.connect(str(db))
    try:   # table creation rolled back with the copy: the next open retries
        assert con.execute("SELECT name FROM sqlite_master WHERE name='memory_links'").fetchone() is None
    finally:
        con.close()
    monkeypatch.undo()
    cl = MemoryClient.local(str(db), entitlement_gate=NullEntitlementGate())
    assert cl.neighbors(MAYA)["degree"]["out"] == 2
