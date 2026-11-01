"""output_budget.shape_hits: the degrade order behind the MCP / Hermes output
budget (2026-10-22 output-size regression)."""
from __future__ import annotations

import json

from sibyl_memory_client import output_budget as ob


def _hit(i: int, snippet: bool = True) -> dict:
    h = {"tier": "entity", "category": "c", "key": f"k{i}", "match": "text",
         "body": "b" * 1500 + "…", "truncated": True, "chars": 9000, "title": None,
         "links": {"degree": {"out": 3, "in": 0}, "by_relation": {"r1": 1, "r2": 2},
                   "sample": [{"relation": "r1", "node": {"key": "x" * 200}},
                              {"relation": "r2", "node": {"key": "y" * 200}}]}}
    if snippet:
        h["snippet"] = "s" * 300
    return h


def _fits(limit: int):
    return lambda rows: len(json.dumps(rows)) <= limit


def test_fitting_result_is_returned_untouched():
    hits = [_hit(i) for i in range(3)]
    out, info = ob.shape_hits(hits, _fits(10**9))
    assert out is hits and info == {"degraded": [], "truncated": 0}


def test_body_shrinks_before_links_before_drop():
    hits = [_hit(i) for i in range(10)]
    full = len(json.dumps(hits))
    # Room for 500-char bodies: only the first stage runs.
    out, info = ob.shape_hits(hits, _fits(full - 10 * 900))
    assert info["degraded"] == ["body_500"] and info["truncated"] == 0
    assert all(len(h["body"]) == 501 and "sample" in h["links"] for h in out)
    # Bodies omitted, samples still there.
    no_body = [{k: v for k, v in h.items() if k != "body"} for h in hits]
    out, info = ob.shape_hits(hits, _fits(len(json.dumps(no_body)) + 10 * 40))
    assert info["degraded"] == ["body_500", "body_150", "body_omitted"]
    assert all("body" not in h and h["body_omitted"] and h["links"]["sample"] for h in out)
    assert [h["key"] for h in out] == [h["key"] for h in hits]


def test_last_resort_drops_trailing_hits_only():
    hits = [_hit(i) for i in range(30)]
    out, info = ob.shape_hits(hits, _fits(4_000))
    assert info["degraded"][-1] == "links_counts_only" and info["truncated"] > 0
    assert [h["key"] for h in out] == [f"k{i}" for i in range(len(out))]
    assert len(out) + info["truncated"] == 30 and len(json.dumps(out)) <= 4_000
    assert all("sample" not in h["links"] and h["links"]["sample_omitted"] and
               h["links"]["by_relation"] for h in out)


def test_snippetless_hits_keep_their_body_on_search():
    hits = [_hit(0, snippet=False), _hit(1)]
    out, _ = ob.shape_hits(hits, _fits(len(json.dumps(hits)) - 1200))
    assert len(out[0]["body"]) == 1501
    assert len(out[1].get("body", "")) < 1501


def test_list_mode_shortens_but_never_omits():
    hits = [_hit(i, snippet=False) for i in range(40)]
    out, info = ob.shape_hits(hits, _fits(8_000), snippet_only=False)
    assert "body_omitted" not in info["degraded"] and info["truncated"] > 0
    assert all(len(h["body"]) == 151 for h in out)


def test_top_hit_is_always_kept():
    out, info = ob.shape_hits([_hit(0), _hit(1)], _fits(10))
    assert len(out) == 1 and info["truncated"] == 1


def test_budget_from_env(monkeypatch):
    monkeypatch.delenv("X_BUDGET", raising=False)
    assert ob.budget_from_env("X_BUDGET") == 45_000
    assert ob.token_budget_from_env("X_BUDGET") == 22_000
    for raw, want in (("30000", 30_000), ("30_000", 30_000), ("1", 4_000),
                      ("10000000", 500_000), ("nope", 45_000), ("  ", 45_000)):
        monkeypatch.setenv("X_BUDGET", raw)
        assert ob.budget_from_env("X_BUDGET") == want

    monkeypatch.setenv("X_BUDGET", "5")
    assert ob.token_budget_from_env("X_BUDGET") == 1_000
    monkeypatch.setenv("X_BUDGET", "10000000")
    assert ob.token_budget_from_env("X_BUDGET") == 125_000


def test_estimate_tokens_is_conservative_for_cjk():
    assert ob.estimate_tokens("") == 0
    assert ob.estimate_tokens("abcd") == 1 and ob.estimate_tokens("abcde") == 2
    assert ob.estimate_tokens("路线图") == 3 and ob.estimate_tokens("ab路") == 2
    assert ob.within("abcd" * 10, tokens=10, chars=40)
    assert not ob.within("abcd" * 10, tokens=9, chars=40)
    assert not ob.within("abcd" * 10, tokens=10, chars=39)


def test_snippet_stage_runs_after_bodies_and_stays_match_centred():
    hits = [{**_hit(i), "snippet": "x" * 300 + "[roadmap]" + "y" * 290} for i in range(10)]
    no_body = [{k: v for k, v in h.items() if k != "body"} for h in hits]
    out, info = ob.shape_hits(hits, _fits(len(json.dumps(no_body)) - 10 * 300))
    assert info["degraded"] == ["body_500", "body_150", "body_omitted", "snippet_250"]
    for h in out:
        assert "[roadmap]" in h["snippet"] and len(h["snippet"]) <= 252
        assert h["links"]["sample"]          # links untouched: snippets were enough
