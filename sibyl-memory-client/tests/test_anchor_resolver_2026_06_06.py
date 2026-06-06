"""Anchor-first resolver + search refinements (combined patch, 2026-06-06).

Validates the fix for the multi-record recall/precision regression that tester
Sylvain surfaced (Runs 16/17 ~0.36 recall at 50-100 companies) and validated the
anchor-first remedy for (Runs 24-29: full recall, zero pollution at 100
companies). Source memo: memory/research/sylvain-anchor-first-resolver-runs24-29-2026-05-31.md.

Three changes under test:
  1. multi_record_search anchor-first strict-filter (scale-invariant precision).
  2. MemoryClient.search_entities(category=...) anchor filter.
  3. MemoryClient.search() cross-tier rank tiebreaker (content before journal).
"""
from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search

_TYPES = {
    "report":  "report revenue forecast quarterly",
    "email":   "email thread followup correspondence",
    "journal": "journal meeting notes minutes",
    "bug":     "bug ticket error defect",
}


def _build_corpus(c, n):
    """n companies, each with 4 linked records sharing THREE per-group topic
    terms — the cross-cluster contamination vector that defeated the old
    corpus-fraction selectivity cutoff."""
    for i in range(n):
        anchor = f"co{i:04d}"
        g = i % max(1, n // 12)
        topics = f"topic{g}alpha topic{g}beta topic{g}gamma"
        for t, tt in _TYPES.items():
            c.set_entity(t, f"{t}-{i}", {"text": f"{anchor} {topics} {t} {tt} project status update"})


def test_anchor_first_full_recall_zero_pollution_at_scale(tmp_path):
    n = 60
    c = MemoryClient.local(tmp_path / "scale.db", tenant_id="scale")
    _build_corpus(c, n)

    exp_total = rec_total = pollution = 0
    for i in range(n):
        anchor = f"co{i:04d}"
        g = i % max(1, n // 12)
        res = multi_record_search(c, f"{anchor} topic{g}alpha topic{g}beta topic{g}gamma", limit=20)
        expected = {f"{t}-{i}" for t in _TYPES}
        got = {h.get("key") for h in res}
        exp_total += len(expected)
        rec_total += len(expected & got)
        for h in res:
            txt = (h.get("body") or {}).get("text", "")
            if anchor not in txt:
                pollution += 1

    assert rec_total == exp_total, f"recall regressed: {rec_total}/{exp_total}"
    assert pollution == 0, f"cross-cluster pollution leaked: {pollution} hits"


def test_abstention_preserved(tmp_path):
    c = MemoryClient.local(tmp_path / "ab.db", tenant_id="scale")
    _build_corpus(c, 20)
    # a term with zero corpus support must collapse the whole query to []
    assert multi_record_search(c, "co0001 nonexistenttokenzzzq report", limit=10) == []


def test_single_cluster_query_returns_only_that_cluster(tmp_path):
    n = 40
    c = MemoryClient.local(tmp_path / "sc.db", tenant_id="scale")
    _build_corpus(c, n)
    g = 7 % max(1, n // 12)   # same group formula the corpus uses
    res = multi_record_search(c, f"co0007 topic{g}alpha topic{g}beta topic{g}gamma", limit=20)
    assert res, "expected the anchor cluster to be returned"
    for h in res:
        assert "co0007" in (h.get("body") or {}).get("text", ""), "leaked a non-anchor record"


def test_search_entities_category_filter(tmp_path):
    c = MemoryClient.local(tmp_path / "cat.db", tenant_id="scale")
    c.set_entity("report", "r1", {"text": "synergy roadmap alpha"})
    c.set_entity("report", "r2", {"text": "synergy roadmap beta"})
    c.set_entity("memo", "m1", {"text": "synergy roadmap gamma"})

    all_hits = c.search_entities("synergy")
    assert {h["name"] for h in all_hits} == {"r1", "r2", "m1"}

    report_only = c.search_entities("synergy", category="report")
    assert {h["name"] for h in report_only} == {"r1", "r2"}
    assert all(h["category"] == "report" for h in report_only)

    memo_only = c.search_entities("synergy", category="memo")
    assert {h["name"] for h in memo_only} == {"m1"}
