"""multi_record_search — multi-record (linked-record) retrieval.

Two-stage retrieve-then-verify search. A drop-in for a single client.search()
call on workflow / linked-record queries: queries whose answer spans several
related records (e.g. feedback + bug + journal, report + email, sheet + report).

Why it exists (tester Run15): flat single-pass FTS5 AND-of-tokens requires one
record to contain the whole query vocabulary, so a query that needs several
linked records returns only the single strongest match and misses the rest.

  Stage 1 RECALL   per-significant-token search, union the candidates, track
                   which query tokens each record matched.
  Stage 2 VERIFY   - abstain if any significant term has zero corpus support
                     (so "rejected" / "denied" / injection queries return []);
                   - on a terminal-state query, drop purely-preparatory records
                     (draft / triage / forecast), negation-aware;
                   - a candidate must match >= 1 rare/selective term, not just
                     common ones (kills cross-talk from neighbouring clusters);
                   - rank by IDF-weighted coverage, keep >= COVERAGE_THRESHOLD.

Bench (reconstructed Run15 oracle, client 0.4.x): baseline single-pass 4/10;
recall-only multipass 3/10 (REGRESSES — breaks abstention + pulls distractors);
this 10/10. The verify gates are load-bearing — recall alone regresses.

CAVEAT — the constants below (SELECTIVE_CUTOFF_FRAC, COVERAGE_THRESHOLD, the
prep/terminal lexicon, the strict zero-support abstention) are tuned on a
24-record reconstruction, NOT generalized to production-scale corpora. Validate
against real-scale data or gate behind a flag before relying on it at scale.
Generalization candidates: corpus-relative IDF percentile, normalized coverage
threshold, anchor-term / min-coverage abstention, learned state classification.

Uses only the public MemoryClient surface (search / list_entities), so it adds
no coupling to client internals.
"""
from __future__ import annotations
import json
import math
import re

_STOP = {"the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
         "to", "of", "in", "on", "at", "for", "with", "this", "that",
         "final", "current", "by"}

_TERMINAL_Q = {"final", "resolved", "approved", "published", "closed", "sent",
               "emailed", "decision", "finalized"}

_TERM_RE = re.compile(
    r'(?<!not )\b(final|finaliz\w*|resolved|approved|published|closed|sent|'
    r'emailed|decision|signed|bound)\b')
_PREP_RE = re.compile(
    r'\b(draft|triage|forecast|planning|proposed|tentative|pending|agenda|'
    r'scheduled|rehearsal|sample|option|wip|follow-?up)\b|work in progress')

# --- bench-tuned constants (see CAVEAT in the module docstring) -------------
SELECTIVE_CUTOFF_FRAC = 0.15   # term is "selective"/rare if df <= frac * corpus_size
COVERAGE_THRESHOLD = 0.45      # keep candidates whose IDF-weighted coverage >= this
_PER_TOKEN_LIMIT = 200         # recall depth per token


def _significant_tokens(query: str):
    return [t for t in re.findall(r"[A-Za-z0-9]+", query.lower())
            if len(t) > 2 and t not in _STOP]


def _pure_prep(body_lower: str) -> bool:
    """True if the body is purely preparatory (a prep marker, no terminal marker)."""
    return bool(_PREP_RE.search(body_lower)) and not bool(_TERM_RE.search(body_lower))


def multi_record_search(client, query: str, *, limit: int = 10, corpus_n: int | None = None):
    """Two-stage retrieve-then-verify search over a MemoryClient.

    Returns a ranked list of hit dicts in the SAME shape client.search() returns
    ({tier, key, category, body, snippet, rank, ts}), best-first. Returns [] when
    the query is unsatisfiable (abstention) or nothing clears the verify gates.

    For exact single-entity lookups, prefer client.recall() / get_entity().
    """
    toks = _significant_tokens(query)
    if not toks:
        return []
    if corpus_n is None:
        corpus_n = len(client.list_entities(limit=100000))

    cand: dict = {}
    df: dict = {}
    for t in toks:
        hits = client.search(t, limit=_PER_TOKEN_LIMIT)
        df[t] = len(hits)
        if df[t] == 0:
            return []  # abstention: a discriminating term that nothing satisfies
        for h in hits:
            key = (h.get("tier"), h.get("key"), h.get("category"))
            e = cand.setdefault(key, {"m": set(), "best": 0.0, "hit": h,
                                      "body": json.dumps(h.get("body")).lower()})
            e["m"].add(t)
            rank = h.get("rank", 0.0) or 0.0
            if rank < e["best"]:
                e["best"] = rank

    idf = {t: math.log((corpus_n + 1) / (df[t] + 1)) + 1.0 for t in toks}
    total = sum(idf.values()) or 1.0
    terminal_q = bool(set(toks) & _TERMINAL_Q)
    sel_cut = max(1, round(SELECTIVE_CUTOFF_FRAC * corpus_n))
    selective = {t for t in toks if df[t] <= sel_cut}

    scored = []
    for e in cand.values():
        if terminal_q and _pure_prep(e["body"]):
            continue                                   # drop purely-preparatory on a final-state query
        if selective and not (e["m"] & selective):
            continue                                   # drop cross-talk (only common terms matched)
        cov = sum(idf[t] for t in e["m"]) / total
        if cov >= COVERAGE_THRESHOLD:
            scored.append((e["hit"], cov, e["best"]))
    scored.sort(key=lambda x: (-x[1], x[2]))
    return [h for h, _cov, _best in scored[:limit]]
