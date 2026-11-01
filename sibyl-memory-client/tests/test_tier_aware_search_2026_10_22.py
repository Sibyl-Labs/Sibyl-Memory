"""2.C tier-aware search (spec tiered-retrieval, R3, 2026-10-22).

Stage 0 exact-name resolution, the cross-tier tie-break at equal coverage,
scope="graph", the additive hit fields (match, chars, title, links) and the
additive verdict fields (tiers_hit, name_matches, recall_depth, scope). The
gates are unchanged: abstention still wins over a name match, and a name match
never qualifies a row by body text.
"""
from __future__ import annotations

import sqlite3

import pytest

from sibyl_memory_client import MemoryClient, entity_ref, reference_ref, state_ref
from sibyl_memory_client import _links
from sibyl_memory_client import client as client_mod
from sibyl_memory_client import multi_record as mr
from sibyl_memory_client.exceptions import ValidationError
from sibyl_memory_client.multi_record import multi_record_search
from sibyl_memory_client.testing import NullEntitlementGate
from sibyl_memory_client.verdicts import (
    GateCounters, Verdict, VerdictCode, gated_verdict, GateCause)

MAYA = entity_ref("person", "Maya Patel")
REDWOOD = entity_ref("company", "Redwood")


def _client(tmp_path, tenant="t1", name="m.db") -> MemoryClient:
    return MemoryClient.local(str(tmp_path / name), tenant_id=tenant,
                              entitlement_gate=NullEntitlementGate())


@pytest.fixture
def c(tmp_path) -> MemoryClient:
    cl = _client(tmp_path)
    cl.set_entity("person", "Maya Patel", {"role": "lead", "summary": "runs onboarding"})
    cl.set_entity("company", "Redwood", {"kind": "customer", "summary": "onboarding in Q3"})
    # rows that mention the name in their BODY, many times, so text ranking
    # alone would put them first
    for i in range(6):
        cl.set_entity("note", f"note-{i}", {"text": "Maya Patel " * 5 + "met Maya Patel"})
    cl.set_reference("doc/fireflies/kickoff",
                     "Kickoff transcript. Maya Patel led the Redwood onboarding call.",
                     metadata={"title": "Redwood kickoff", "date": "2026-03-10"})
    cl.set_state("directory:customers", {"items": ["Redwood"]})
    return cl


def _keys(res):
    return [(h["tier"], h["key"], h["match"]) for h in res]


# ----------------------------------------------------------------------
# Stage 0
# ----------------------------------------------------------------------
def test_exact_name_comes_first_with_match_name(c):
    res = multi_record_search(c, "maya patel", limit=10)
    assert _keys(res)[0] == ("entity", "Maya Patel", "name")
    assert all(h["match"] == "text" for h in res[1:] if h["key"] != "Maya Patel")
    assert sum(1 for h in res if h["key"] == "Maya Patel") == 1      # deduped
    assert res.verdict.name_matches == 1
    assert res.verdict.code is VerdictCode.OK


def test_exact_name_is_case_insensitive_ascii(c):
    assert _keys(multi_record_search(c, "  MAYA PATEL ", limit=3))[0] == \
        ("entity", "Maya Patel", "name")


def test_category_prefix_form(c):
    res = multi_record_search(c, "person:Maya Patel", limit=5)
    assert _keys(res)[0] == ("entity", "Maya Patel", "name")
    # a prefix naming a different category does not resolve the name
    res = multi_record_search(c, "company:Maya Patel", limit=5)
    assert all(h["match"] != "name" for h in res)


def test_single_short_token_runs_exact_but_not_partial(tmp_path):
    """V1 panel fix: the exact probe runs for short names ("Sam", "IBM");
    the phrase-on-name partial keeps the 4-char guard."""
    cl = _client(tmp_path)
    cl.set_entity("person", "Sam", {"role": "ops"})
    cl.set_entity("person", "Sam Lee", {"role": "cfo"})
    res = multi_record_search(cl, "sam", limit=5)
    assert res[0]["key"] == "Sam" and res[0]["match"] == "name"
    assert all(h["match"] == "text" for h in res[1:])
    assert res.verdict.name_matches == 1


def test_stage_0_reaches_a_name_with_no_significant_token(tmp_path):
    """'Jo Li' has no token the engine scores (all <= 2 chars); stage 0 runs
    before tokenising, so the named entity is still found."""
    cl = _client(tmp_path)
    cl.set_entity("person", "Jo Li", {"role": "ops"})
    res = multi_record_search(cl, "Jo Li", limit=5)
    assert _keys(res) == [("entity", "Jo Li", "name")]


def test_hub_shard_collision_parent_wins(tmp_path):
    cl = _client(tmp_path)
    cl.set_entity("person", "Marissa Cole",
                  {"summary": "account lead, Tethys; " + "notes " * 200})
    for i in range(40):
        cl.set_entity("person", f"Marissa Cole docs {i:03d}", {"docs": [f"d{i}"]})
    res = multi_record_search(cl, "marissa cole", limit=10)
    k = _keys(res)
    assert k[0] == ("entity", "Marissa Cole", "name")
    assert [m for _t, _k, m in k[1:4]] == ["name_partial"] * 3
    assert all(m == "text" for _t, _k, m in k[4:])
    assert res.verdict.name_matches == 4


def test_name_partial_is_name_column_only(tmp_path):
    """A body that holds the phrase is never a name match."""
    cl = _client(tmp_path)
    cl.set_entity("note", "standup", {"text": "orbit planner review"})
    res = multi_record_search(cl, "orbit planner", limit=5)
    assert res and all(h["match"] == "text" for h in res)


def test_stage_0_is_tenant_scoped(tmp_path):
    a = _client(tmp_path, tenant="ta", name="x.db")
    b = _client(tmp_path, tenant="tb", name="x.db")
    b.set_entity("person", "Orla Vantreeze", {"role": "secret"})
    a.set_entity("note", "n", {"text": "unrelated words here"})
    assert a._name_matches("Orla Vantreeze") == []
    assert multi_record_search(a, "orla vantreeze", limit=5) == []


def test_exact_name_probe_uses_the_lower_name_index(c):
    """The real statement (and its category form) is a one-key index lookup,
    never a tenant scan through entities_updated_at (measured 57 ms at 100k
    rows when the SQL carried ORDER BY updated_at)."""
    with c.storage.connection() as conn:
        for sql, args in (
                (client_mod._NAME_EXACT_SQL + " LIMIT ?", ("t1", "maya patel", 200)),
                (client_mod._NAME_EXACT_SQL + " AND lower(category) = ? LIMIT ?",
                 ("t1", "maya patel", "person", 200))):
            plan = " ".join(str(tuple(r)) for r in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, args))
            assert "entities_tenant_lname" in plan, plan
            assert "entities_updated_at" not in plan, plan


def test_exact_name_orders_newest_first_then_category(tmp_path):
    cl = _client(tmp_path)
    for cat in ("zeta", "alpha", "mid"):
        cl.set_entity(cat, "Common Name", {"k": cat})
    cl.set_entity("alpha", "Common Name", {"k": "touched last"})
    got = [h["category"] for h in cl._name_matches("common name")]
    assert got[0] == "alpha" and sorted(got) == ["alpha", "mid", "zeta"]


def test_abstention_wins_over_a_name_match(tmp_path):
    cl = _client(tmp_path)
    cl.set_entity("deal", "Not Approved Deal", {"status": "open"})
    # operator decision 2 (V1 panel): the exact whole-query name survives the
    # negation gate, alone; a partial of the name still abstains
    res = multi_record_search(cl, "not approved deal", limit=5)
    assert [(h["key"], h["match"]) for h in res] == [("Not Approved Deal", "name")]
    res = multi_record_search(cl, "not approved", limit=5)
    assert res == []
    assert res.verdict.code is VerdictCode.NEGATION_ABSTAIN
    # and an unsupported content token still abstains with a name present
    cl.set_entity("person", "Maya Patel", {"role": "lead"})
    res = multi_record_search(cl, "maya patel zqxvwyk", limit=5)
    assert res == [] and res.verdict.code is VerdictCode.ABSTAINED_ON


# ----------------------------------------------------------------------
# Tie-break at equal coverage
# ----------------------------------------------------------------------
def test_tier_priority_tables_agree():
    assert mr._TIER_PRIORITY == client_mod._TIER_RANK == \
        {"entity": 0, "state": 0, "reference": 1, "journal": 2}


def test_equal_coverage_orders_entity_state_reference_journal(tmp_path):
    cl = _client(tmp_path)
    text = "zorblat quintex"
    cl.write_event(evaluated=text)
    cl.set_reference("doc/z", text)
    cl.set_entity("note", "z-entity", {"t": text})
    cl.set_state("z-state", {"t": text})
    res = multi_record_search(cl, text, limit=10)
    tiers = [h["tier"] for h in res]
    assert set(tiers[:2]) == {"entity", "state"}
    assert tiers[2:] == ["reference", "journal"]


def test_coverage_still_decides_first(tmp_path):
    cl = _client(tmp_path)
    cl.set_entity("note", "partial", {"t": "zorblat"})
    cl.set_reference("doc/full", "zorblat quintex vandrel")
    for i in range(5):     # make 'zorblat' common so the doc's extra terms dominate
        cl.set_entity("filler", f"f{i}", {"t": "zorblat"})
    res = multi_record_search(cl, "zorblat quintex vandrel", limit=10)
    assert res[0]["tier"] == "reference" and res[0]["key"] == "doc/full"


# ----------------------------------------------------------------------
# scope
# ----------------------------------------------------------------------
def test_scope_graph_excludes_reference_and_journal(c):
    c.write_event(evaluated="Redwood onboarding call notes")
    all_ = multi_record_search(c, "redwood onboarding", limit=20)
    assert {"reference", "journal"} & {h["tier"] for h in all_}
    graph = multi_record_search(c, "redwood onboarding", limit=20, scope="graph")
    assert graph and {h["tier"] for h in graph} <= {"entity", "state"}
    assert graph.verdict.scope == "graph"
    assert graph.verdict.tiers_hit["reference"] == graph.verdict.tiers_hit["journal"] == 0


def test_scope_graph_still_abstains(c):
    res = multi_record_search(c, "redwood zqxvwyk", limit=5, scope="graph")
    assert res == [] and res.verdict.code is VerdictCode.ABSTAINED_ON
    # a token that lives only in a document is unsupported in the graph scope,
    # and the explanation says so instead of "nowhere in this store"
    res = multi_record_search(c, "kickoff transcript", limit=5, scope="graph")
    assert res == [] and res.verdict.code is VerdictCode.ABSTAINED_ON
    assert "scope='graph'" in res.verdict.explain()
    assert "nowhere in this store" not in res.verdict.explain()
    # scope all: unchanged wording
    res = multi_record_search(c, "redwood zqxvwyk", limit=5)
    assert "appears nowhere in this store" in res.verdict.explain()


def test_scope_graph_name_lookup(c):
    res = multi_record_search(c, "Maya Patel", limit=3, scope="graph")
    assert _keys(res)[0] == ("entity", "Maya Patel", "name")


def test_bad_scope_is_a_validation_error(c):
    with pytest.raises(ValidationError):
        multi_record_search(c, "maya", scope="docs")


# ----------------------------------------------------------------------
# Hit fields: chars, title, links
# ----------------------------------------------------------------------
def test_reference_hit_title_and_chars(c):
    res = multi_record_search(c, "kickoff transcript", limit=5)
    doc = next(h for h in res if h["tier"] == "reference")
    assert doc["title"] == "Redwood kickoff"
    assert doc["chars"] == len(c.get_reference("doc/fireflies/kickoff")["body"])
    ent = multi_record_search(c, "maya patel", limit=1)[0]
    assert ent["title"] is None
    import json
    assert ent["chars"] == len(json.dumps(ent["body"], ensure_ascii=False))


def test_links_summary_is_correct(c):
    c.link(MAYA, REDWOOD, "works_at")
    c.link(REDWOOD, reference_ref("doc/fireflies/kickoff"), "source_of")
    c.link(reference_ref("doc/fireflies/kickoff"), MAYA, "mentions")
    for i in range(7):
        c.set_reference(f"doc/x/{i}", f"note {i}")
        c.link(REDWOOD, reference_ref(f"doc/x/{i}"), "source_of")
    c.write_event(evaluated="Maya Patel onboarding")
    res = multi_record_search(c, "maya patel", limit=20, with_links=True)
    maya = next(h for h in res if h["key"] == "Maya Patel")
    assert maya["links"]["degree"] == {"out": 1, "in": 1}
    assert maya["links"]["by_relation"] == {"works_at": 1, "mentions": 1}
    red = multi_record_search(c, "redwood", limit=5, with_links=True)
    red = next(h for h in red if h["key"] == "Redwood")
    assert red["links"]["degree"] == {"out": 8, "in": 1}
    assert red["links"]["by_relation"] == {"source_of": 8, "works_at": 1}
    sample = red["links"]["sample"]
    # Operator decision 3: one sample edge per (direction, relation), largest
    # group first (was: the five newest edges).
    assert [(e["direction"], e["relation"]) for e in sample] == [
        ("out", "source_of"), ("in", "works_at")]
    assert sample[0]["node"]["key"] == "doc/x/6"          # newest of its group
    assert all(set(e) == {"relation", "direction", "node", "stale"} for e in sample)
    assert not any(e["stale"] for e in sample)
    for h in res:
        if h["tier"] == "journal":
            assert h["links"] is None


def test_links_summary_is_one_statement_for_all_hits(tmp_path):
    cl = _client(tmp_path)
    nodes = []
    for i in range(50):
        cl.set_entity("person", f"p{i}", {"t": "x"})
        nodes.append(("entity", "person", f"p{i}"))
    for i in range(49):
        cl.link(entity_ref("person", f"p{i}"), entity_ref("person", f"p{i + 1}"), "knows")
    statements = []
    with cl.storage.connection() as conn:
        conn.set_trace_callback(statements.append)
        try:
            out = _links.links_of(conn, "t1", nodes, 5)
        finally:
            conn.set_trace_callback(None)
    # A constant number of statements (group counts + one representative row
    # per group), never one per node: 50 nodes, 2 statements.
    # (The once-per-connection PRAGMA table_info stamp probe is not a query.)
    assert len([s for s in statements
                if "memory_links" in s and not s.startswith("PRAGMA")]) == 2
    assert out[("entity", "person", "p0")]["degree"] == {"out": 1, "in": 0}
    assert out[("entity", "person", "p10")]["degree"] == {"out": 1, "in": 1}
    # per_node = 0 still returns the counts
    with cl.storage.connection() as conn:
        z = _links.links_of(conn, "t1", nodes[:2], 0)
    assert z[("entity", "person", "p1")]["by_relation"] == {"knows": 2}
    assert z[("entity", "person", "p1")]["sample"] == []


def test_links_summary_plan_is_per_node_index_lookups(tmp_path):
    """Both halves of the one statement look nodes up by the full key of
    memory_links_from / memory_links_to; neither scans the tenant (the "in"
    half used to drive through memory_links_created: 54 ms for 10 nodes at
    26k edges, 0.7 ms after)."""
    cl = _client(tmp_path)
    cl.set_entity("person", "a", {"t": 1})
    plans = []

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=()):
            plans.extend(str(tuple(r)) for r in self.conn.execute(
                "EXPLAIN QUERY PLAN " + sql, args))
            return self.conn.execute(sql, args)

    with cl.storage.connection() as conn:
        _links.links_of(Spy(conn), "t1", [("entity", "person", "a"),
                                          ("reference", "", "doc/x")], 5)
    text = " ".join(plans)
    assert ("memory_links_from (tenant_id=? AND from_tier=? AND from_category=? "
            "AND from_key=?)") in text
    assert ("memory_links_to (tenant_id=? AND to_tier=? AND to_category=? "
            "AND to_key=?)") in text
    assert "memory_links_created" not in text


def test_search_calls_links_once(c, monkeypatch):
    calls = []
    real = _links.links_of
    monkeypatch.setattr(_links, "links_of",
                        lambda *a, **k: calls.append(len(a[2])) or real(*a, **k))
    res = multi_record_search(c, "redwood onboarding", limit=10, with_links=True)
    assert res and len(calls) == 1
    multi_record_search(c, "redwood onboarding", limit=10)     # default: no links
    assert len(calls) == 1


def test_results_unchanged_when_no_links_exist(c):
    for q in ("maya patel", "redwood onboarding", "kickoff transcript", "onboarding"):
        plain = multi_record_search(c, q, limit=10)
        linked = multi_record_search(c, q, limit=10, with_links=True)
        assert all("links" not in h for h in plain)
        stripped = [{k: v for k, v in h.items() if k != "links"} for h in linked]
        assert stripped == list(plain), q
        for h in linked:
            if h["tier"] != "journal":
                assert h["links"] == {"degree": {"out": 0, "in": 0},
                                      "by_relation": {}, "sample": []}
        lv, pv = linked.verdict.as_dict(), plain.verdict.as_dict()
        assert pv["links_ms"] is None and isinstance(lv["links_ms"], float)
        lv.pop("links_ms"), pv.pop("links_ms")
        assert lv == pv


def test_raw_search_stays_link_free(c):
    c.link(MAYA, REDWOOD, "works_at")
    for h in c.search("maya patel", limit=5):
        assert "links" not in h and "match" not in h


# ----------------------------------------------------------------------
# Verdict fields
# ----------------------------------------------------------------------
def test_verdict_additive_fields(c):
    res = multi_record_search(c, "maya patel", limit=10)
    v = res.verdict.as_dict()
    assert v["tiers_hit"] == {
        t: sum(1 for h in res if h["tier"] == t)
        for t in ("entity", "state", "reference", "journal")}
    assert v["name_matches"] == 1
    assert v["recall_depth"] == 200
    assert v["scope"] == "all"
    z = multi_record_search(c, "redwood zqxvwyk", limit=5).verdict.as_dict()
    assert z["tiers_hit"] == {"entity": 0, "state": 0, "reference": 0, "journal": 0}
    assert z["name_matches"] == 0 and z["recall_depth"] == 200


def test_raw_primitives_report_tiers_hit_but_not_stage_0(c):
    v = c.search("redwood", limit=10).verdict
    assert sum(v.tiers_hit.values()) == v.returned
    assert v.name_matches is None and v.recall_depth is None and v.scope is None
    v = c.search_entities("redwood", limit=10).verdict
    assert v.tiers_hit["entity"] == v.returned


def test_explain_names_the_depth_on_a_near_miss():
    g = GateCounters(coverage_floor=12, best_pre_gate_coverage=0.41)
    v = gated_verdict(GateCause.COVERAGE_FLOOR, g, candidates=12)
    v.recall_depth = 2100
    assert "0.41 against the 0.45 floor at depth 2,100" in v.explain()
    far = gated_verdict(GateCause.COVERAGE_FLOOR,
                        GateCounters(coverage_floor=3, best_pre_gate_coverage=0.2),
                        candidates=3)
    far.recall_depth = 2100
    assert "depth" not in far.explain()
