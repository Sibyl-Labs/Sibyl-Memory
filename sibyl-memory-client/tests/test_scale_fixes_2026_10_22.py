"""2.D scale fixes (spec tiered-retrieval, 2026-10-22).

D1  recall depth K = clamp(200, ceil(0.05 * corpus_n), 5000); df is the true
    per-tier COUNT; stage 1 reads ids only, bodies are decoded for the final rows.
D2  NAME_WEIGHT = 8 on the entity name column (KEY_WEIGHT = 4 on doc/state keys).
D3  40-token match-centred snippet.
D4  prep filter reads reference metadata.status or the first 2,000 chars.

The small-store golden (3,000 rows) proves D1 changes nothing where no per-token
cap binds: with the D2 weights set back to 1.0 every result, rank, body and
verdict is identical to the frozen 0.9.0 outputs. The large store (>= 20k
entities) shows depth, true df and name weighting doing their job, and the
abstention gate still firing.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import client as client_mod
from sibyl_memory_client import multi_record as mr
from sibyl_memory_client.multi_record import multi_record_search, recall_depth
from sibyl_memory_client.verdicts import VerdictCode

import _scale_corpus as sc

GOLDEN = Path(__file__).parent / "fixtures" / "scale_golden_0_9_0.json"

# 2.C (R3) changed three things on purpose, and the frozen 0.9.0 golden is
# compared with exactly those three put back (procedure agreed with Fable,
# 2026-10-22; the fixture itself stays frozen):
#   * the cross-tier tie-break (reference 0 -> 1, journal 1 -> 2) moves only
#     rows of EQUAL coverage, so the 0.9.0 tie-break is patched back in;
#   * hits gain `match`, `chars`, `title` and the verdict gains `tiers_hit`,
#     `name_matches`, `recall_depth`, `scope`: stripped before digesting;
#   * a gated zero within NEAR_FLOOR_BAND of the floor names its recall depth
#     in explain(): the band is patched off.
# test_tie_break_only_reorders_equal_coverage_rows below proves the first
# point holds on this corpus (same row sets at full depth).
_OLD_TIER = {"entity": 0, "state": 0, "reference": 0, "journal": 1}
_NEW_HIT_KEYS = ("match", "chars", "title", "links")
_NEW_VERDICT_KEYS = ("tiers_hit", "name_matches", "recall_depth", "scope",
                     "links_ms", "decoration_note")   # additive, V1 graph fixes


def _as_0_9_0(monkeypatch):
    from sibyl_memory_client import verdicts as verdicts_mod
    monkeypatch.setattr(mr, "_TIER_PRIORITY", dict(_OLD_TIER))
    monkeypatch.setattr(client_mod, "_TIER_RANK", dict(_OLD_TIER))
    monkeypatch.setattr(verdicts_mod, "NEAR_FLOOR_BAND", -1.0)


def _strip_hits(rows):
    for h in rows:
        for k in _NEW_HIT_KEYS:
            h.pop(k, None)
    return rows


def _strip_verdict(v: dict) -> dict:
    for k in _NEW_VERDICT_KEYS:
        v.pop(k)
    return v


# ----------------------------------------------------------------------
# D1: the depth formula
# ----------------------------------------------------------------------
@pytest.mark.parametrize("n,k", [
    (0, 200), (1, 200), (3_000, 200), (4_000, 200), (4_001, 201),
    (20_000, 1_000), (42_000, 2_100), (100_000, 5_000), (1_000_000, 5_000),
])
def test_recall_depth_formula(n, k):
    assert recall_depth(n) == k


def test_recall_depth_constants():
    assert mr._PER_TOKEN_LIMIT == 200
    assert mr.RECALL_FRACTION == 0.05
    assert mr.RECALL_DEPTH_MAX == 5_000
    assert client_mod.NAME_WEIGHT == 8.0
    assert client_mod.KEY_WEIGHT == 4.0
    assert client_mod.SNIPPET_TOKENS == 40


# ----------------------------------------------------------------------
# D1: byte identity at <= 4,000 rows against the frozen 0.9.0 outputs
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def small_store(tmp_path_factory):
    c = MemoryClient.local(tmp_path_factory.mktemp("small") / "small.db",
                           tenant_id="golden")
    sc.lift_cap(c)
    sc.build_small(c)
    return c


def test_small_store_is_byte_identical_to_0_9_0(small_store, monkeypatch):
    """With the D2 weights neutralised (1.0 = 0.9.0's `ORDER BY rank`), every
    multi_record result (rows, ranks, bodies), every verdict and every raw
    search result on the 3,000-row store is identical to 0.9.0. Snippets are
    excluded: D3 changes them on purpose (asserted below)."""
    monkeypatch.setattr(client_mod, "NAME_WEIGHT", 1.0)
    monkeypatch.setattr(client_mod, "KEY_WEIGHT", 1.0)
    _as_0_9_0(monkeypatch)
    c = small_store
    golden = json.loads(GOLDEN.read_text())["queries"]
    assert sorted(golden) == sorted(sc.golden_queries())
    assert recall_depth(mr._corpus_count(c)) == 200
    # Precondition, the definition of "byte-identical at <= 4,000 rows": no
    # golden token saturates the 0.9.0 per-token cap (200 overall, 50 journal),
    # so true df == the old capped df for every one of them.
    for q in golden:
        for t in mr._significant_tokens(q):
            rows, df = c._recall_token(t, limit=200)
            assert df < 200 and len(rows) == df, (q, t, df)
            assert sum(1 for r in rows if r["tier"] == "journal") < 50, (q, t)
    mismatches = []
    for q, want in golden.items():
        res = multi_record_search(c, q, limit=10)
        rows = _strip_hits(sc.normalize(res))
        for h in rows:
            snip = h.pop("snippet", None)
            # a body match is bracketed; a name/key-only match has no body
            # window to centre on (FTS5 returns the column head, as in 0.9.0)
            assert snip is None or isinstance(snip, str), (q, snip)
        raw = sc.normalize(c.search(q, limit=10))
        for h in raw:
            h.pop("snippet", None)
        got = {
            "mrs_keys": [[h["tier"], h["key"]] for h in rows],
            "mrs_digest": sc.digest(rows),
            "verdict_digest": sc.digest(_strip_verdict(res.verdict.as_dict())),
            "raw_digest": sc.digest(raw),
        }
        if got != want:
            mismatches.append((q, want["mrs_keys"], got["mrs_keys"]))
    assert not mismatches, mismatches
    # the golden is not vacuous: most queries return rows, one abstains
    assert sum(1 for v in golden.values() if v["mrs_keys"]) >= 30


def test_small_store_with_weights_on_still_matches_row_sets(small_store, monkeypatch):
    """With the real weights only ordering may move (name/key columns); the row
    SETS returned by multi_record are unchanged on the golden corpus (0.9.0
    tie-break, see _as_0_9_0: the 2.C tie-break can move rows across the
    limit cut, which test_tie_break_only_reorders_equal_coverage_rows covers)."""
    _as_0_9_0(monkeypatch)
    c = small_store
    golden = json.loads(GOLDEN.read_text())["queries"]
    for q, want in golden.items():
        got = sorted(tuple(x) for x in
                     [[h["tier"], h["key"]] for h in sc.normalize(
                         multi_record_search(c, q, limit=10))])
        assert got == sorted(tuple(x) for x in want["mrs_keys"]), q


def test_tie_break_only_reorders_equal_coverage_rows(small_store, monkeypatch):
    """2.C tie-break on the frozen corpus: at full depth every query returns
    the SAME row set as under the 0.9.0 tie-break, and every pair whose
    relative order changed is a higher-priority tier moving ahead of a lower
    one (entity/state ahead of reference ahead of journal)."""
    c = small_store
    golden = json.loads(GOLDEN.read_text())["queries"]

    def keys(q):
        return [(h["tier"], h["key"]) for h in sc.normalize(
            multi_record_search(c, q, limit=5_000))]

    with monkeypatch.context() as m:
        m.setattr(mr, "_TIER_PRIORITY", dict(_OLD_TIER))
        m.setattr(client_mod, "_TIER_RANK", dict(_OLD_TIER))
        old = {q: keys(q) for q in golden}
    moved = 0
    for q in golden:
        new = keys(q)
        assert sorted(new) == sorted(old[q]), q
        pos = {k: i for i, k in enumerate(old[q])}
        for i, a in enumerate(new):
            for b in new[i + 1:]:
                if pos[a] > pos[b]:
                    moved += 1
                    assert mr._TIER_PRIORITY[a[0]] < mr._TIER_PRIORITY[b[0]], (q, a, b)
    assert moved > 0   # the corpus does exercise the tie-break


# ----------------------------------------------------------------------
# D1: true df (journal cap and the 200 cap no longer clip df)
# ----------------------------------------------------------------------
def test_true_df_counts_past_the_journal_cap(tmp_path):
    c = MemoryClient.local(tmp_path / "j.db", tenant_id="j")
    sc.lift_cap(c)
    for i in range(80):
        c.write_event(evaluated=f"quarzite review number {i}")
    c.set_entity("note", "n1", {"text": "quarzite"})
    rows, df = c._recall_token("quarzite", limit=200)
    # 0.9.0 df was len(search(...)) = 1 entity + journal capped at 200//4 = 50
    assert len(c.search("quarzite", limit=200)) == 51
    assert len(rows) == 51          # the candidate SET is unchanged
    assert df == 81                 # df is the truth


def test_recall_token_rows_match_search_minus_bodies(small_store):
    c = small_store
    vocab = sc.vocabulary()
    for tok in vocab[:25] + ["company", "doc", "draft"]:
        rows, df = c._recall_token(tok, limit=200)
        hits = c.search(tok, limit=200)
        assert [(r["tier"], r["key"], r["category"], r["rank"]) for r in rows] == \
               [(h["tier"], h["key"], h["category"], h["rank"]) for h in hits], tok
        assert all("body" not in r for r in rows if "_rid" in r)
        assert df >= len(rows)


# ----------------------------------------------------------------------
# D3: match-centred snippet
# ----------------------------------------------------------------------
def test_snippet_is_centred_on_the_match_deep_in_a_document(tmp_path):
    c = MemoryClient.local(tmp_path / "s.db", tenant_id="s")
    filler = " ".join(f"filler{i % 50}" for i in range(2000))   # ~16 KB
    c.set_reference("doc/deep", filler + " the zephyrine clause applies " + filler)
    res = multi_record_search(c, "zephyrine", limit=5)
    assert [h["key"] for h in res] == ["doc/deep"]
    snip = res[0]["snippet"]
    assert "[zephyrine]" in snip
    assert "zephyrine" not in res[0]["body"][:1500]   # the prefix never shows it
    # 40-token window: far longer than the 12-token one, still bounded
    assert 30 <= len(snip.split()) <= 45


def test_snippet_prefers_the_window_with_most_matched_terms(tmp_path):
    c = MemoryClient.local(tmp_path / "s2.db", tenant_id="s")
    pad = " ".join(f"pad{i}" for i in range(300))
    c.set_reference("doc/x", f"orvalith {pad} orvalith quenmarch tessaline {pad}")
    res = multi_record_search(c, "orvalith quenmarch tessaline", limit=5)
    snip = res[0]["snippet"]
    assert "[quenmarch]" in snip and "[tessaline]" in snip


# ----------------------------------------------------------------------
# D4: prep filter scope on reference documents
# ----------------------------------------------------------------------
def test_prep_filter_reads_only_the_head_of_long_reference_docs(tmp_path):
    c = MemoryClient.local(tmp_path / "p.db", tenant_id="p")
    head = " ".join(f"word{i}" for i in range(1500))            # > 2,000 chars
    late = head + " draft " + head                                 # 'draft' at ~10 KB
    # "not approved" carries the query token without being a terminal marker
    # (_TERM_RE's negative lookbehind), so only the prep markers decide.
    lead = "brontivax contract not approved "
    c.set_reference("doc/late-draft", lead + late)
    c.set_reference("doc/early-draft", "draft " + lead + head)
    c.set_reference("doc/status-draft", lead + head, metadata={"status": "draft"})
    c.set_reference("doc/status-final", "draft " + lead, metadata={"status": "approved"})
    res = multi_record_search(c, "approved brontivax", limit=10)
    keys = {h["key"] for h in res}
    assert "doc/late-draft" in keys            # 0.9.0 dropped it on 'draft' at 10 KB
    assert "doc/early-draft" not in keys       # a real prep marker at the head
    assert "doc/status-draft" not in keys      # metadata.status wins
    assert "doc/status-final" in keys          # ... in both directions


def test_prep_filter_unchanged_for_entities(tmp_path):
    c = MemoryClient.local(tmp_path / "pe.db", tenant_id="p")
    c.set_entity("plan", "a", {"text": "brontivax roadmap draft not approved"})
    c.set_entity("plan", "b", {"text": "brontivax roadmap shipped not approved"})
    res = multi_record_search(c, "approved brontivax roadmap", limit=10)
    assert [h["key"] for h in res] == ["b"]


# ----------------------------------------------------------------------
# D2: name weighting (small)
# ----------------------------------------------------------------------
def test_named_entity_ranks_first_against_bodies_that_mention_it(tmp_path):
    c = MemoryClient.local(tmp_path / "n.db", tenant_id="n")
    sc.lift_cap(c)
    for i in range(100):
        c.set_entity("note", f"note-{i:03d}", {
            "text": "kestrel varga " * 3 + " ".join(f"w{i}x{j}" for j in range(40))})
    # a ~1 KB card: the shape the spec tells agents to write
    c.set_entity("person", "Kestrel Varga",
                 {"role": "lead", "notes": " ".join(f"c{j}" for j in range(150))})
    assert c.search_entities("kestrel varga", limit=5)[0]["name"] == "Kestrel Varga"
    assert c.search("kestrel varga", limit=5)[0]["key"] == "Kestrel Varga"
    names = [e["name"] for e in c.search_entities("kestrel varga", limit=200)]
    assert names.index("Kestrel Varga") == 0


# ----------------------------------------------------------------------
# The large store (>= 20k entities)
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def large_store(tmp_path_factory):
    db = tmp_path_factory.mktemp("large") / "large.db"
    facts = sc.build_large(db, "big", n_entities=20_000)
    c = MemoryClient.local(db, tenant_id="big")
    return c, facts, db


def test_large_store_depth_scales(large_store):
    c, _facts, _db = large_store
    n = mr._corpus_count(c)
    assert n >= 22_000
    assert recall_depth(n) == -(-n * 5 // 100) > 1_000


def test_large_store_df_is_true_count(large_store):
    c, facts, db = large_store
    common = facts["common"]
    rows, df = c._recall_token(common, limit=recall_depth(mr._corpus_count(c)))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    want = sum(con.execute(f"SELECT count(*) FROM {t} WHERE {t} MATCH ?",
                           (f'"{common}"',)).fetchone()[0]
               for t in ("entities_fts", "reference_documents_fts"))
    con.close()
    assert df == want > 5_000          # 0.9.0 reported min(df, 200) = 200
    assert len(rows) == recall_depth(mr._corpus_count(c))


def test_large_store_planted_document_clears_the_floor_only_with_depth(
        large_store, monkeypatch):
    c, facts, _db = large_store
    q = " ".join(facts["target_tokens"])
    res = multi_record_search(c, q, limit=10)
    assert "doc/target" in [h["key"] for h in res], res.verdict.as_dict()
    # at the old fixed depth the target falls outside most tokens' top-200
    monkeypatch.setattr(mr, "RECALL_DEPTH_MAX", 200)
    old = multi_record_search(c, q, limit=10)
    assert "doc/target" not in [h["key"] for h in old]


def test_large_store_still_abstains_on_unsupported_token(large_store):
    c, facts, _db = large_store
    q = " ".join(facts["target_tokens"][:8]) + " " + facts["unsupported"]
    res = multi_record_search(c, q, limit=10)
    assert list(res) == []
    assert res.verdict.code is VerdictCode.ABSTAINED_ON
    assert res.verdict.tokens == [facts["unsupported"]]


def test_large_store_name_weight_lifts_the_named_hub(large_store, monkeypatch):
    c, facts, _db = large_store
    name = facts["hub_name"]
    assert c.search_entities(name, limit=10)[0]["name"] == name
    assert c.search(name, limit=10)[0]["key"] == name
    monkeypatch.setattr(client_mod, "NAME_WEIGHT", 1.0)
    unweighted = [e["name"] for e in c.search_entities(name, limit=200)]
    assert unweighted[0] != name       # 0.9.0 buried it under the 150 mentions


def test_large_store_latency(large_store):
    c, facts, _db = large_store
    q15 = " ".join(facts["target_tokens"])
    vocab = sc.vocabulary()
    q24 = " ".join(vocab[300:324])
    for q in (q15, q24, "approved " + q15):
        multi_record_search(c, q, limit=10)          # warm
        t0 = time.perf_counter()
        for _ in range(3):
            multi_record_search(c, q, limit=10)
        dt = (time.perf_counter() - t0) / 3
        print(f"\nlatency {len(q.split())} tokens: {dt * 1000:.0f} ms")
        # generous CI bound; the measured numbers go in the CHANGELOG
        assert dt < 5.0
