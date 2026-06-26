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
                   - ANCHOR-FIRST (hybrid): keep a candidate that is in the
                     anchor's cluster (matches >= 1 anchor term, the rarest most
                     discriminating tokens) OR clears the high-coverage bar
                     ANCHOR_HYBRID_HI. A non-anchor, mid-coverage candidate is
                     cross-cluster pollution and is dropped. The pure strict
                     filter killed pollution but over-dropped natural-language
                     evidence that lacks the rare anchor; the hybrid keeps both;
                   - rank by IDF-weighted coverage with a tier tiebreaker
                     (content tiers before contentless journal), keep
                     >= COVERAGE_THRESHOLD.

Bench: baseline single-pass 4/10; recall-only multipass 3/10 (REGRESSES). The
prior retrieve-then-verify scored 10/10 at 24 records but only ~0.36 recall at
50-100 companies (tester Runs 16/17) because its selectivity cutoff was a corpus
fraction (round(0.15 * corpus_n)) that lost meaning at scale: past ~150 records
almost every term read as "selective," so cross-cluster records cleared the gate.
The anchor-first rewrite (this version) defines the anchor RELATIVE to the rarest
query term, so the precision gate is scale-invariant (tester Runs 24-29:
100/100 recall, 0 pollution at 100 companies / 1621 writes). Abstention and the
terminal/prep gates are preserved unchanged.

ANCHOR_HYBRID_HI was tuned on a real-data retrieval diagnostic (LongMemEval text
combined into one store): the pure anchor-only filter regressed natural-language
recall (gold evidence that lacks the rare anchor); HI=0.65 restores it while
keeping synthetic-workflow pollution at 0. Per-question (oracle) retrieval is not
regressed by this change (NEW >= OLD).

CAVEAT — COVERAGE_THRESHOLD, ANCHOR_BAND, ANCHOR_HYBRID_HI, and the prep/terminal
lexicon are defaults validated against the synthetic multi-cluster scale test
(tests/test_anchor_resolver_2026_06_06.py) + the LongMemEval retrieval diagnostic;
re-validate if corpus structure changes.

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

# --- anchor-first resolver constants (see CAVEAT in the module docstring) ---
# Replaces the 24-record bench tuning (SELECTIVE_CUTOFF_FRAC = 0.15) that
# collapsed at scale. The anchor is defined RELATIVE to the rarest query term,
# so it is scale-invariant.
ANCHOR_BAND = 2.0              # a term is an "anchor" if df <= ANCHOR_BAND * rarest-term df
COVERAGE_THRESHOLD = 0.45      # hard coverage floor: drop candidates below this
ANCHOR_HYBRID_HI = 0.65        # a non-anchor candidate is kept only if coverage >= this
_PER_TOKEN_LIMIT = 200         # recall depth per token
# content tiers beat the contentless journal tier at equal coverage (cross-tier
# BM25 scores are not comparable; tester email 19e7eb3096b4dae5)
_TIER_PRIORITY = {"entity": 0, "state": 0, "reference": 0, "journal": 1}


def _significant_tokens(query: str):
    return [t for t in re.findall(r"[A-Za-z0-9]+", query.lower())
            if len(t) > 2 and t not in _STOP]


# CORE-6/MH-3 (2026-06-25 pre-launch audit): cap the per-token recall fan-out.
# An attacker (or a pathological query) with many significant tokens previously
# issued one 200-row FTS5 search PER token, an unbounded multiplier on a single
# untiered call. Bound the fan-out to the most-significant (longest, a cheap
# rarity proxy) tokens so the work per query is O(MAX_FANOUT_TOKENS), not
# O(len(query)).
_MAX_FANOUT_TOKENS = 24


def _corpus_count(client) -> int:
    """Cheap corpus size for IDF weighting (CORE-6/MH-3).

    Prefer the client's storage COUNT(*) over the old
    ``len(list_entities(limit=100000))``, which materialized + JSON-decoded every
    entity row just to count them. Falls back to the old path only if the cheap
    method is unavailable (older client without count_rows / storage access).
    """
    storage = getattr(client, "storage", None)
    tenant = None
    get_tenant = getattr(client, "get_tenant", None)
    if callable(get_tenant):
        try:
            tenant = get_tenant()
        except Exception:
            tenant = None
    if storage is not None and tenant is not None and hasattr(storage, "count_rows"):
        try:
            return storage.count_rows("entities", tenant)
        except Exception:
            pass
    # Fallback: bounded list (still cheaper than the old 100000 with the clamp).
    return len(client.list_entities(limit=10_000))


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
    # CORE-6/MH-3: bound token fan-out. De-dup, then keep the longest (rarest-
    # proxy) tokens up to the cap so an attacker can't force one FTS5 search per
    # token on an arbitrarily long query. Terminal-state keywords are always
    # retained so the terminal/prep gate still has its signal.
    uniq = list(dict.fromkeys(toks))
    if len(uniq) > _MAX_FANOUT_TOKENS:
        forced = [t for t in uniq if t in _TERMINAL_Q]
        rest = sorted((t for t in uniq if t not in _TERMINAL_Q), key=len, reverse=True)
        keep = list(dict.fromkeys(forced + rest))[:_MAX_FANOUT_TOKENS]
        toks = keep
    else:
        toks = uniq
    if corpus_n is None:
        corpus_n = _corpus_count(client)  # CORE-6/MH-3: cheap COUNT(*), not full scan

    terminal_q = bool(set(toks) & _TERMINAL_Q)

    cand: dict = {}
    df: dict = {}
    for t in toks:
        hits = client.search(t, limit=_PER_TOKEN_LIMIT)
        df[t] = len(hits)
        if df[t] == 0:
            return []  # abstention: a discriminating term that nothing satisfies
        for h in hits:
            key = (h.get("tier"), h.get("key"), h.get("category"))
            e = cand.get(key)
            if e is None:
                # CORE-6/MH-3: only serialize+lower the body when a terminal-state
                # query will actually consult it (the prep/terminal gate). For
                # non-terminal queries the body string is never read, so skip the
                # per-hit json.dumps entirely.
                body_lower = json.dumps(h.get("body")).lower() if terminal_q else ""
                e = cand[key] = {"m": set(), "best": 0.0, "hit": h, "body": body_lower}
            e["m"].add(t)
            rank = h.get("rank", 0.0) or 0.0
            if rank < e["best"]:
                e["best"] = rank

    idf = {t: math.log((corpus_n + 1) / (df[t] + 1)) + 1.0 for t in toks}
    total = sum(idf.values()) or 1.0

    # Anchor-first: anchor terms are the rarest (most discriminating) tokens,
    # defined relative to the rarest term so the band is scale-invariant. Every
    # candidate is strict-filtered to the anchor's cluster (must match >= 1 anchor
    # term), which removes the cross-cluster pollution the old corpus-fraction
    # cutoff let through at scale. Anchor-raw recalls fully but pollutes; the
    # strict filter is the load-bearing precision gate (tester Runs 24-29).
    min_df = min(df.values())
    anchor_cut = max(2, round(ANCHOR_BAND * min_df))
    anchor_terms = {t for t in toks if df[t] <= anchor_cut}

    scored = []
    for e in cand.values():
        if terminal_q and _pure_prep(e["body"]):
            continue                                   # drop purely-preparatory on a final-state query
        cov = sum(idf[t] for t in e["m"]) / total
        if cov < COVERAGE_THRESHOLD:
            continue                                   # below the hard coverage floor
        # Anchor-first HYBRID gate: keep a candidate that is in the anchor's
        # cluster (matches an anchor term) OR clears the high-coverage bar
        # (genuinely relevant despite lacking the rare anchor, e.g. natural-
        # language evidence). A non-anchor, mid-coverage candidate is pure
        # cross-cluster pollution and is dropped. Tuned on the LongMemEval
        # retrieval diagnostic: synthetic-workflow pollution -> 0 while natural-
        # language recall is preserved (anchor-only over-filtered real queries).
        if anchor_terms and not (e["m"] & anchor_terms) and cov < ANCHOR_HYBRID_HI:
            continue
        tier = e["hit"].get("tier")
        scored.append((e["hit"], cov, _TIER_PRIORITY.get(tier, 0), e["best"]))
    scored.sort(key=lambda x: (-x[1], x[2], x[3]))
    return [h for h, _cov, _tp, _best in scored[:limit]]
