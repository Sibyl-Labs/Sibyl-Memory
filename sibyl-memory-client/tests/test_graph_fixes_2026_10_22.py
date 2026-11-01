"""V1 panel fixes, graph read side (PANEL-SYNTHESIS themes C, F; should-fix items).

C  neighbors() is pinned to the endpoint indexes (was a tenant scan through
   memory_links_created: 83.8 ms vs 0.24 ms per node); links_of() is an
   index-only O(degree) group summary with one sample edge per relation
   (operator decision 3) instead of a full-partition window sort (68 ms per
   hit at a 5,000-edge hub); self-loops count once; ``hub: true``.
F  search decoration never fails a search (a missing memory_links index used
   to turn every search into StorageError).
   neighbors() reports the queried node's own exists / archived.
   lint hub-degree counts in-degree too; lint flags a missing endpoint index.
   writes report edges {created, updated, unchanged}.
   ``stale`` on entity endpoints whose identity stamp no longer matches.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from sibyl_memory_client import (
    DEFAULT_TENANT,
    MemoryClient,
    entity_ref,
    reference_ref,
)
from sibyl_memory_client import _links
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.exceptions import StorageError
from sibyl_memory_client.lint import Linter
from sibyl_memory_client.multi_record import multi_record_search
from sibyl_memory_client.storage import new_id
from sibyl_memory_client.testing import NullEntitlementGate

MAYA = entity_ref("person", "Maya Patel")
REDWOOD = entity_ref("company", "Redwood")
DOC = reference_ref("doc/fireflies/kickoff")
MAYA_K = ("entity", "person", "Maya Patel")


def _client(tmp_path, name="m.db") -> MemoryClient:
    return MemoryClient.local(str(tmp_path / name), tenant_id=DEFAULT_TENANT,
                              entitlement_gate=NullEntitlementGate())


@pytest.fixture
def c(tmp_path) -> MemoryClient:
    cl = _client(tmp_path)
    cl.set_entity("person", "Maya Patel", {"role": "lead", "summary": "runs onboarding"})
    cl.set_entity("company", "Redwood", {"kind": "customer", "summary": "onboarding customer"})
    cl.set_reference(DOC["key"], "Kickoff transcript. Redwood onboarding.",
                     metadata={"title": "Kickoff"})
    return cl


class _Spy:
    """Collects EXPLAIN QUERY PLAN for every statement run through it."""

    def __init__(self, conn):
        self.conn = conn
        self.plans: list[str] = []

    def execute(self, sql, args=()):
        if not sql.lstrip().upper().startswith("PRAGMA"):
            self.plans.extend(str(tuple(r)) for r in self.conn.execute(
                "EXPLAIN QUERY PLAN " + sql, args))
        return self.conn.execute(sql, args)


def _bulk_edges(cl: MemoryClient, rows: list[tuple]) -> None:
    with cl.storage.transaction() as conn:
        conn.executemany(
            "INSERT INTO memory_links (id, tenant_id, from_tier, from_category, from_key, "
            "to_tier, to_category, to_key, relation_type, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _graph(cl: MemoryClient, people: int = 2000, edges: int = 40000, hub_in: int = 5000):
    """nocturne exp2 shape: degree ~20 everywhere plus one 5,000-in-edge hub."""
    with cl.storage.transaction() as conn:
        conn.executemany(
            "INSERT INTO entities (id, tenant_id, category, name, body) VALUES (?,?,?,?,?)",
            [(new_id(), DEFAULT_TENANT, "person", f"P{i}", "{}") for i in range(people)]
            + [(new_id(), DEFAULT_TENANT, "company", "Hub", "{}")])
    rows = [(new_id(), DEFAULT_TENANT, "entity", "person", f"P{i % people}", "entity",
             "person", f"P{(i * 7 + 1) % people}", f"r{i // people}",
             f"2026-01-01T00:00:{i % 60:02d}.{i % 1000:03d}Z") for i in range(edges)]
    rows += [(new_id(), DEFAULT_TENANT, "entity", "person", f"P{i % people}", "entity",
              "company", "Hub", f"member_of{i // people}",
              f"2026-02-01T00:00:00.{i % 1000:03d}Z") for i in range(hub_in)]
    _bulk_edges(cl, rows)


# ----------------------------------------------------------------------
# C1  neighbors pinned to the endpoint indexes
# ----------------------------------------------------------------------
@pytest.mark.parametrize("direction", ["both", "out", "in"])
@pytest.mark.parametrize("with_cursor", [False, True])
def test_neighbors_plan_uses_endpoint_indexes(c, direction, with_cursor):
    c.link(MAYA, REDWOOD, "works_at")
    c.link(REDWOOD, MAYA, "employs")
    cursor = ("2026-01-01T00:00:00.000Z", "x") if with_cursor else None
    with c.storage.connection() as conn:
        spy = _Spy(conn)
        _links.neighbors(spy, DEFAULT_TENANT, MAYA_K, relations=None, direction=direction,
                         tier=None, category=None, limit=50, cursor=cursor, preview=False)
    text = " ".join(spy.plans)
    assert "memory_links_created" not in text
    if direction in ("both", "out"):
        assert "memory_links_from (tenant_id=? AND from_tier=? AND from_category=? AND from_key=?)" in text
    if direction in ("both", "in"):
        assert "memory_links_to (tenant_id=? AND to_tier=? AND to_category=? AND to_key=?)" in text
    assert "SCAN memory_links" not in text


def test_neighbors_cursor_pages_are_complete_and_ordered(c):
    for i in range(30):
        c.set_entity("person", f"m{i}", {})
        c.link(REDWOOD, entity_ref("person", f"m{i}"), "has")
        c.link(entity_ref("person", f"m{i}"), REDWOOD, "member_of")
    seen, cur = [], None
    while True:
        page = c.neighbors(REDWOOD, limit=7, cursor=cur)
        seen += [(e["created_at"], e["id"]) for e in page["edges"]]
        cur = page["next_cursor"]
        if not cur:
            break
    assert len(seen) == len(set(seen)) == 60
    assert seen == sorted(seen)


def test_neighbors_is_per_node_not_per_tenant(tmp_path):
    cl = _client(tmp_path)
    _graph(cl)
    node = entity_ref("person", "P5")
    cl.neighbors(node)
    t = time.perf_counter()
    for _ in range(10):
        res = cl.neighbors(node)
    per = (time.perf_counter() - t) / 10 * 1000
    assert res["degree"] == {"out": 23, "in": 20} and len(res["edges"]) == 43
    # Was ~84 ms on this shape (tenant scan); per-node is well under 1 ms.
    # Loose bound so a slow CI box does not flake; the plan test is the guard.
    assert per < 20, per


# ----------------------------------------------------------------------
# C2  links_of: index-only, one sample edge per relation, self-loop parity
# ----------------------------------------------------------------------
def test_links_of_one_sample_per_relation_largest_first(c):
    c.set_entity("person", "Sam", {})
    c.link(MAYA, REDWOOD, "works_at")
    for i in range(3):
        c.set_reference(f"doc/x/{i}", f"note {i}")
        c.link(MAYA, reference_ref(f"doc/x/{i}"), "about")
    c.link(entity_ref("person", "Sam"), MAYA, "reports_to")
    s = c.links_of([MAYA])[MAYA_K]
    assert s["degree"] == {"out": 4, "in": 1}
    assert list(s["by_relation"].items()) == [("about", 3), ("works_at", 1), ("reports_to", 1)]
    assert [(e["direction"], e["relation"]) for e in s["sample"]] == [
        ("out", "about"), ("out", "works_at"), ("in", "reports_to")]
    assert s["sample"][0]["node"]["key"] == "doc/x/2"       # newest of its group
    assert "hub" not in s
    capped = c.links_of([MAYA], per_node=1)[MAYA_K]
    assert [e["relation"] for e in capped["sample"]] == ["about"]


def test_links_of_self_loop_counts_once_parity_with_neighbors(c):
    c.link(REDWOOD, MAYA, "knows")
    c.link(MAYA, MAYA, "knows")            # newest in-edge of "knows" is the self-loop
    c.link(MAYA, MAYA, "same_as")
    s = c.links_of([MAYA])[MAYA_K]
    assert s["degree"] == {"out": 2, "in": 1}
    assert s["by_relation"] == {"knows": 2, "same_as": 1}
    ins = [e for e in s["sample"] if e["direction"] == "in"]
    assert [(e["relation"], e["node"]["key"]) for e in ins] == [("knows", "Redwood")]
    n = c.neighbors(MAYA)
    assert n["degree"] == s["degree"]
    assert len(n["edges"]) == n["degree"]["out"] + n["degree"]["in"]
    # direction="in" still lists the self-loops: they do point at the node.
    assert len(c.neighbors(MAYA, direction="in")["edges"]) == 3


def test_links_of_only_self_loop_has_no_in_group(c):
    c.link(MAYA, MAYA, "same_as")
    s = c.links_of([MAYA])[MAYA_K]
    assert s["degree"] == {"out": 1, "in": 0}
    assert [(e["direction"], e["relation"]) for e in s["sample"]] == [("out", "same_as")]


def test_links_of_hub_flag(c, monkeypatch):
    for i in range(3):
        c.set_entity("person", f"m{i}", {})
        c.link(entity_ref("person", f"m{i}"), REDWOOD, "member_of")
    monkeypatch.setattr(_links, "HUB_DEGREE_WARN", 2)
    out = c.links_of([REDWOOD, MAYA])
    assert out[("entity", "company", "Redwood")]["hub"] is True
    assert "hub" not in out[MAYA_K]


def test_links_of_plan_is_index_only(c):
    c.link(MAYA, REDWOOD, "works_at")
    c.link(REDWOOD, MAYA, "employs")
    c.link(MAYA, MAYA, "same_as")
    with c.storage.connection() as conn:
        spy = _Spy(conn)
        _links.links_of(spy, DEFAULT_TENANT, [MAYA_K, ("entity", "company", "Redwood")], 5)
    text = " ".join(spy.plans)
    assert "memory_links_created" not in text
    searches = [p for p in spy.plans if "SEARCH" in p or "SCAN" in p]
    # the three group branches read only index b-trees
    assert any("COVERING INDEX memory_links_from" in p for p in searches)
    assert any("COVERING INDEX memory_links_to" in p for p in searches)
    assert any("COVERING INDEX sqlite_autoindex_memory_links" in p for p in searches)
    # the representative read is by rowid, one row per group (was a tenant
    # scan through the UNIQUE autoindex before the +tenant_id fix)
    assert any("INTEGER PRIMARY KEY (rowid=?)" in p for p in searches)
    assert not [p for p in searches if "(tenant_id=?)" in p]
    assert not [p for p in searches if "SCAN" in p
                and "CONSTANT ROWS" not in p and "SCAN w" not in p]


def test_links_of_hub_5000_edges_is_fast(tmp_path):
    cl = _client(tmp_path)
    _graph(cl)
    hub = [("entity", "company", "Hub")]
    with cl.storage.connection() as conn:
        _links.links_of(conn, DEFAULT_TENANT, hub, 5)
        t = time.perf_counter()
        for _ in range(10):
            out = _links.links_of(conn, DEFAULT_TENANT, hub, 5)
        per = (time.perf_counter() - t) / 10 * 1000
    s = out[hub[0]]
    assert s["degree"] == {"out": 0, "in": 5000} and s["hub"] is True
    assert sum(s["by_relation"].values()) == 5000 and len(s["sample"]) == 3
    # Was ~68 ms (window sort over every hub edge). Loose CI bound.
    assert per < 30, per


# ----------------------------------------------------------------------
# F  decoration never fails a search
# ----------------------------------------------------------------------
@pytest.mark.parametrize("index", ["memory_links_from", "memory_links_to"])
def test_search_survives_missing_endpoint_index(c, index):
    c.link(MAYA, REDWOOD, "works_at")
    with c.storage.connection() as conn:
        conn.execute(f"DROP INDEX {index}")
    res = multi_record_search(c, "redwood onboarding", limit=10, with_links=True)
    assert res, "search must still return hits"
    assert all(h["links"] is None for h in res)
    v = res.verdict.as_dict()
    assert "links unavailable" in v["decoration_note"]
    assert v["links_ms"] is not None
    # the explicit walk surfaces the broken store (Fable consult Q1: no silent
    # hint-less re-run), and lint names it
    with pytest.raises(StorageError):
        c.neighbors(MAYA)
    rep = Linter(c.storage, tenant_id=DEFAULT_TENANT).run()
    miss = [f for f in rep.findings if f.check == "links-index-missing"]
    assert miss and miss[0].detail["missing"] == [index]


def test_search_unaffected_by_missing_created_index(c):
    c.link(MAYA, REDWOOD, "works_at")
    with c.storage.connection() as conn:
        conn.execute("DROP INDEX memory_links_created")
    res = multi_record_search(c, "redwood onboarding", limit=10, with_links=True)
    red = next(h for h in res if h["key"] == "Redwood")
    assert red["links"]["degree"] == {"out": 0, "in": 1}
    assert res.verdict.decoration_note is None
    assert c.neighbors(MAYA)["degree"] == {"out": 1, "in": 0}


def test_search_survives_missing_links_table(c):
    with c.storage.connection() as conn:
        conn.execute("DROP TABLE memory_links")
    res = multi_record_search(c, "redwood onboarding", limit=10, with_links=True)
    assert res and all(h["links"] is None for h in res)
    assert res.verdict.decoration_note


def test_title_failure_does_not_fail_search(c, monkeypatch):
    def boom(_raw):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(client_mod, "_meta_title", boom)
    res = multi_record_search(c, "kickoff transcript", limit=10, with_links=True)
    assert res and all(h["title"] is None for h in res)
    assert "titles unavailable" in res.verdict.decoration_note
    assert res.verdict.decoration_note.count("links") == 0


def test_links_ms_recorded_only_with_links(c):
    c.link(MAYA, REDWOOD, "works_at")
    assert multi_record_search(c, "redwood", limit=5).verdict.links_ms is None
    v = multi_record_search(c, "redwood", limit=5, with_links=True).verdict
    assert isinstance(v.links_ms, float) and v.decoration_note is None


# ----------------------------------------------------------------------
# neighbors reports the queried node itself
# ----------------------------------------------------------------------
def test_neighbors_reports_queried_node_existence(c):
    c.link(MAYA, REDWOOD, "works_at")
    ok = c.neighbors(MAYA)["node"]
    assert ok["exists"] is True and ok["archived"] is False and ok["key"] == "Maya Patel"
    for typo in ("maya patel", "Maya Patl"):
        res = c.neighbors(entity_ref("person", typo))
        assert res["edges"] == [] and res["node"]["exists"] is False
        assert res["node"]["archived"] is False
    c.set_entity("person", "Loner", {})
    lone = c.neighbors(entity_ref("person", "Loner"))
    assert lone["edges"] == [] and lone["node"]["exists"] is True
    c.archive_entity("person", "Maya Patel")
    gone = c.neighbors(MAYA)
    assert gone["node"]["exists"] is False and gone["node"]["archived"] is True
    assert len(gone["edges"]) == 1                      # edges survive archive
    assert c.neighbors(DOC)["node"]["exists"] is True
    assert c.neighbors(reference_ref("doc/none"))["node"]["exists"] is False
    assert "_id" not in ok and all("_id" not in e["node"] for e in gone["edges"])


# ----------------------------------------------------------------------
# lint hub-degree: both directions
# ----------------------------------------------------------------------
def test_lint_hub_degree_counts_in_edges(c, monkeypatch):
    for i in range(3):
        c.set_entity("person", f"m{i}", {})
        c.link(entity_ref("person", f"m{i}"), REDWOOD, "member_of")
    monkeypatch.setattr(_links, "HUB_DEGREE_WARN", 2)
    rep = Linter(c.storage, tenant_id=DEFAULT_TENANT).run()
    hubs = [f for f in rep.findings if f.check == "hub-degree"]
    assert [(f.detail["key"], f.detail["direction"], f.detail["in_degree"]) for f in hubs] == [
        ("Redwood", "in", 3)]
    assert "3 in-edges" in hubs[0].message
    assert not [f for f in rep.findings if f.check == "links-index-missing"]


# ----------------------------------------------------------------------
# writes report {created, updated, unchanged}
# ----------------------------------------------------------------------
def test_write_results_report_created_and_updated(c):
    ent = c.set_entity("person", "Sam", {}, links=[{"to": REDWOOD, "relation": "works_at"},
                                                  {"to": DOC, "relation": "about"}])
    assert ent["links"] == {"created": 2, "updated": 0, "unchanged": 0}
    ent = c.set_entity("person", "Sam", {}, links=[{"to": REDWOOD, "relation": "works_at"},
                                                  {"to": MAYA, "relation": "knows"}])
    assert ent["links"] == {"created": 1, "updated": 0, "unchanged": 1}
    ent = c.set_entity("person", "Sam", {}, links=[{"to": MAYA, "relation": "knows",
                                                   "metadata": {"since": 2020}}])
    assert ent["links"] == {"created": 0, "updated": 1, "unchanged": 0}
    assert "links" not in c.set_entity("person", "Sam", {})
    ref = c.set_reference("doc/n", "n", links=[{"to": MAYA, "relation": "about"}])
    assert ref == {"key": "doc/n", "links": {"created": 1, "updated": 0, "unchanged": 0}}
    assert c.set_reference("doc/n", "n") == {"key": "doc/n", "links": None}


# ----------------------------------------------------------------------
# stale: endpoint identity stamps. ONE implementation (_links.STALE_SIDE_SQL,
# identity contract): stamp set AND a live entity at that (category, key) under
# a different id. Dangling (no live row) and NULL stamps are never stale.
# ----------------------------------------------------------------------
def _stamped_store(tmp_path):
    cl = _client(tmp_path, "s.db")
    cl.set_entity("person", "Sam Lee", {"role": "CFO"})
    cl.set_entity("company", "Acme", {})
    ids = {r["name"]: r["id"] for r in cl.list_entities()}
    return cl, ids


def _stamped_edge(cl, src, dst, rel, from_id, to_id, ts):
    with cl.storage.transaction() as conn:
        conn.execute(
            "INSERT INTO memory_links (id, tenant_id, from_tier, from_category, from_key, "
            "to_tier, to_category, to_key, relation_type, created_at, from_entity_id, "
            "to_entity_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), DEFAULT_TENANT, *src, *dst, rel, ts, from_id, to_id))


def test_stale_when_stamp_differs_from_live_entity(tmp_path):
    cl, ids = _stamped_store(tmp_path)
    sam = ("entity", "person", "Sam Lee")
    acme = ("entity", "company", "Acme")
    _stamped_edge(cl, sam, acme, "cfo_of", "old-sam-id", ids["Acme"], "2026-01-01T00:00:01.000Z")
    _stamped_edge(cl, sam, acme, "works_at", ids["Sam Lee"], ids["Acme"], "2026-01-01T00:00:02.000Z")
    _stamped_edge(cl, sam, acme, "legacy", None, None, "2026-01-01T00:00:03.000Z")
    _stamped_edge(cl, acme, sam, "employs", "old-acme-id", ids["Sam Lee"], "2026-01-01T00:00:04.000Z")
    res = cl.neighbors(entity_ref("person", "Sam Lee"))
    stale = {e["relation"]: e["stale"] for e in res["edges"]}
    assert stale == {"cfo_of": True, "works_at": False, "legacy": False, "employs": True}
    acme_view = {e["relation"]: e["stale"] for e in cl.neighbors(entity_ref("company", "Acme"))["edges"]}
    assert acme_view == stale
    s = cl.links_of([entity_ref("person", "Sam Lee")], per_node=10)[sam]
    by = {(e["direction"], e["relation"]): e["stale"] for e in s["sample"]}
    assert by == {("out", "cfo_of"): True, ("out", "works_at"): False,
                  ("out", "legacy"): False, ("in", "employs"): True}


def test_stale_absent_endpoint_is_not_stale(tmp_path):
    """An archived endpoint has no live id: reported by exists/archived, not stale."""
    cl, ids = _stamped_store(tmp_path)
    sam = ("entity", "person", "Sam Lee")
    _stamped_edge(cl, sam, ("entity", "company", "Acme"), "works_at", ids["Sam Lee"],
                  ids["Acme"], "2026-01-01T00:00:01.000Z")
    cl.archive_entity("company", "Acme")
    e = cl.neighbors(entity_ref("person", "Sam Lee"))["edges"][0]
    assert e["stale"] is False and e["node"]["archived"] is True


def test_stale_is_always_a_bool_and_false_for_fresh_edges(c):
    c.link(MAYA, REDWOOD, "works_at")
    assert c.neighbors(MAYA)["edges"][0]["stale"] is False
    assert all(e["stale"] is False for e in c.links_of([MAYA])[MAYA_K]["sample"])


def _hit_links(cl, query, node):
    for h in multi_record_search(cl, query, limit=20, with_links=True):
        if h.get("links") is not None and h.get("key") == node[2]:
            return {(e["direction"], e["relation"], e["node"].get("key") or e["node"].get("name")):
                    e["stale"] for e in h["links"]["sample"]}
    raise AssertionError(f"no hit for {node}")


def test_neighbors_and_search_hit_links_agree_on_stale(tmp_path):
    """Re-created (stale), archived (not stale), dangling (not stale),
    never-stamped (not stale): neighbors edges and the search-hit sample
    render the same answer for every case (reconciliation, V1 merge)."""
    cl = _client(tmp_path, "agree.db")
    hub = entity_ref("company", "Zephyrine Holdings")
    hub_k = ("entity", "company", "Zephyrine Holdings")
    cl.set_entity("company", "Zephyrine Holdings", {"summary": "zephyrine holding company"})
    for n in ("Recreated Rae", "Archived Arlo", "Dangling Dee", "Unstamped Uma"):
        cl.set_entity("person", n, {"summary": "person"})
    # one relation per case so each is the sample of its own relation group
    cl.link(hub, entity_ref("person", "Recreated Rae"), "rel_recreated")
    cl.link(hub, entity_ref("person", "Archived Arlo"), "rel_archived")
    cl.link(hub, entity_ref("person", "Dangling Dee"), "rel_dangling")
    cl.link(hub, entity_ref("person", "Unstamped Uma"), "rel_unstamped")
    with cl.storage.transaction() as conn:
        conn.execute("UPDATE memory_links SET to_entity_id = NULL WHERE relation_type = 'rel_unstamped'")
        conn.execute("DELETE FROM entities WHERE name = 'Dangling Dee'")   # 0.9.0-style hard delete
    cl.archive_entity("person", "Recreated Rae")
    cl.set_entity("person", "Recreated Rae", {"summary": "a different Rae"})
    cl.archive_entity("person", "Archived Arlo")
    # the unstamped side's name is re-created too: a NULL stamp is never stale
    cl.archive_entity("person", "Unstamped Uma")
    cl.set_entity("person", "Unstamped Uma", {"summary": "new Uma"})
    want = {"rel_recreated": True, "rel_archived": False, "rel_dangling": False,
            "rel_unstamped": False}
    walk = {e["relation"]: e["stale"] for e in cl.neighbors(hub)["edges"]}
    assert walk == want
    direct = {e["relation"]: e["stale"] for e in cl.links_of([hub], per_node=10)[hub_k]["sample"]}
    assert direct == want
    hit = _hit_links(cl, "zephyrine", hub_k)
    assert {rel: v for (_d, rel, _n), v in hit.items()} == want
    # the reverse view (from each person) agrees with the walk
    rae = cl.neighbors(entity_ref("person", "Recreated Rae"))["edges"]
    assert [e["stale"] for e in rae] == [True]
    assert _links.stale_ids is not None


def test_stamp_columns_added_after_a_read_are_seen_on_the_same_connection(tmp_path):
    """No per-connection cache: a connection that read a pre-identity v6 store
    (no stamp columns: stale False) renders stale as soon as the columns are
    added, on the same connection object."""
    db = tmp_path / "late.db"
    cl = _client(tmp_path, "late.db")
    cl.set_entity("person", "Sam Lee", {})
    cl.set_entity("company", "Acme", {})
    cl.link(entity_ref("person", "Sam Lee"), entity_ref("company", "Acme"), "works_at")
    cl.storage.close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE memory_links DROP COLUMN from_entity_id")
        conn.execute("ALTER TABLE memory_links DROP COLUMN to_entity_id")
        conn.commit()
        sam = ("entity", "person", "Sam Lee")

        def walk():
            return _links.neighbors(conn, DEFAULT_TENANT, sam, relations=None,
                                    direction="both", tier=None, category=None,
                                    limit=10, cursor=None, preview=False)["edges"][0]["stale"]

        def sample():
            return _links.links_of(conn, DEFAULT_TENANT, [sam], 5)[sam]["sample"][0]["stale"]

        assert walk() is False and sample() is False
        conn.execute("ALTER TABLE memory_links ADD COLUMN from_entity_id TEXT")
        conn.execute("ALTER TABLE memory_links ADD COLUMN to_entity_id TEXT")
        conn.execute("UPDATE memory_links SET from_entity_id = 'an-earlier-sam'")
        assert walk() is True and sample() is True
    finally:
        conn.close()


def test_stale_ids_looks_edges_up_by_primary_key_not_a_tenant_scan(tmp_path):
    """neighbors applies stale_ids to its page; it must probe by id (V1 merge:
    the tenant clause made SQLite scan memory_links_created, 14 ms per call at
    42k edges)."""
    cl = _client(tmp_path, "pk.db")
    cl.set_entity("person", "A", {})
    cl.set_entity("person", "B", {})
    cl.link(entity_ref("person", "A"), entity_ref("person", "B"), "knows")
    with cl.storage.connection() as conn:
        spy = _Spy(conn)
        ids = [r[0] for r in conn.execute("SELECT id FROM memory_links")]
        assert _links.stale_ids(spy, DEFAULT_TENANT, ids) == set()
    plan = " ".join(spy.plans)
    assert "memory_links_created" not in plan
    assert "SEARCH l USING INDEX sqlite_autoindex_memory_links_1 (id=?)" in plan
