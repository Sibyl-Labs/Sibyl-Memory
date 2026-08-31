"""verdicts — the ONE canonical cause vocabulary for a search that returned rows,
and for a search that returned none.

WHY THIS MODULE EXISTS (stage 3, 2026-08-31)
--------------------------------------------
Four external evaluation cycles on the multilingual search stack reduced to a
single defect that outlived every fix: **a zero result that cannot explain
itself.** `multi_record_search` has, for months, been able to return `[]` for
five structurally different reasons — an unsupported discriminating term, a
negation policy, three separate scoring gates, an empty store, and an honest
miss — and every one of them reached the agent as the same four bytes. The MCP
server never even forwarded the optional `diagnostics=` channel that existed
(server.py called `multi_record_search(client, query, limit=...)` with no
`diagnostics` kwarg), so the channel was unreachable from the surface that
matters most.

The defect is not that the gates drop candidates. The gates are load-bearing:
they are what makes an injection-shaped query return nothing. The defect is that
the drop is SILENT, so an agent cannot tell "your store does not contain this"
from "one word in your question blocked the whole query" and therefore cannot
recover from the second case.

THE ONE-VOCABULARY RULE
-----------------------
Every package in this repo (client, mcp, hermes, cli, langgraph) IMPORTS the
names below. No package re-declares a cause string, a gate name, or an envelope
key of its own. A re-declaration is how the next divergence happens: the surface
drifts from the engine, the contract test passes against the local copy, and the
agent is told a cause the engine never emitted. If a new cause is genuinely
needed, it is added HERE and nowhere else, and `ZERO_CAUSES` grows with it.

WHAT A VERDICT MAY CARRY
------------------------
Numeric and enum data only, plus tokens **from the caller's own query**. Never
stored-record content: no body text, no snippet, no entity name, no category,
no key. This is not a style preference — the MCP envelope is fenced (MH-1
scrub + MH-2 size caps) precisely because stored bodies are attacker-controlled,
and a verdict that carried record text would be a second, unfenced channel out
of the store. `Verdict.as_dict()` emits a bounded, JSON-safe dict that composes
with that fence rather than routing around it. Query tokens are safe by
construction (the caller wrote them and the MCP response already echoes
`query`), and are length- and count-capped here anyway.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "VerdictCode",
    "GateCause",
    "ZERO_CAUSES",
    "GATE_ORDER",
    "Verdict",
    "SearchResults",
    "GateCounters",
    "MAX_TOKEN_CHARS",
    "MAX_TOKENS_REPORTED",
    "ok_verdict",
    "no_match_verdict",
    "empty_store_verdict",
    "abstained_on_verdict",
    "negation_abstain_verdict",
    "gated_verdict",
    "explain",
    "recovery_for",
    "stamp",
    "store_is_empty",
    "refine_zero",
]

# --- bounds -----------------------------------------------------------------
# A verdict is a control-plane object that rides beside the fenced payload. It
# is bounded on every axis so it can never become the context-flood vector the
# MH-2 output budget exists to prevent.
MAX_TOKEN_CHARS = 64        # per reported query token
MAX_TOKENS_REPORTED = 8     # per list field


class VerdictCode(str, Enum):
    """The closed set of search outcomes.

    ``OK`` is the only member that accompanies a non-empty result. The other
    five are the CAUSES: exactly one of them is stamped on every empty result,
    on every path, on every surface. There is no sixth reason to return nothing
    and no way to return nothing without naming one of these.
    """

    OK = "ok"
    #: A CONTENT-shaped query token has zero corpus support anywhere. The
    #: load-bearing precision gate (it is what collapses injection / "rejected"
    #: queries to nothing). Carries the blocking token in ``tokens``.
    ABSTAINED_ON = "abstained_on"
    #: A negation word was dropped from the query, and NEGATION_POLICY="abstain"
    #: refuses to answer the un-negated question with the record asserting the
    #: opposite. Carries the negation token(s) in ``tokens``.
    NEGATION_ABSTAIN = "negation_abstain"
    #: Candidates were retrieved and then dropped by a scoring gate. Carries
    #: ``gate`` (which gate dominated), ``gate_drops`` (per-gate counts) and
    #: ``best_pre_gate_coverage`` (how close the best candidate came).
    GATED = "gated"
    #: The store holds no searchable rows in any tier for this tenant. Verified
    #: by an explicit probe, never inferred from a zero result.
    EMPTY_STORE = "empty_store"
    #: The query ran with every gate armed against a non-empty store and
    #: genuinely matched nothing (or carried no significant tokens to match
    #: with). The honest miss.
    NO_MATCH = "no_match"


class GateCause(str, Enum):
    """Which verify-stage gate dropped the candidates.

    These name the three `continue` statements in ``multi_record`` that used to
    discard candidates with no record of having done so.
    """

    #: ``cov < COVERAGE_THRESHOLD`` — the hard IDF-weighted coverage floor.
    COVERAGE_FLOOR = "coverage_floor"
    #: The anchor-first HYBRID gate: not in the anchor cluster and below
    #: ``ANCHOR_HYBRID_HI``.
    ANCHOR_GATE = "anchor_gate"
    #: The terminal/prep filter: a purely-preparatory record on a
    #: terminal-state query.
    PREP_FILTER = "prep_filter"


#: The five causes that may be stamped on an empty result. ``OK`` is excluded by
#: construction: a contract test in every package asserts that no zero-row
#: response ships with ``OK`` and that every zero-row response ships with a
#: member of this set.
ZERO_CAUSES = frozenset({
    VerdictCode.ABSTAINED_ON,
    VerdictCode.NEGATION_ABSTAIN,
    VerdictCode.GATED,
    VerdictCode.EMPTY_STORE,
    VerdictCode.NO_MATCH,
})

#: Deterministic tie-break order when several gates dropped the same number of
#: candidates. Fixed here so the reported dominant gate is reproducible across
#: processes and hash seeds — the gate counters are a dict, and "whichever gate
#: iterated first" is not a contract.
GATE_ORDER = (GateCause.COVERAGE_FLOOR, GateCause.ANCHOR_GATE, GateCause.PREP_FILTER)


# --- recovery advice ---------------------------------------------------------
# The recovery string is an ENUM, not prose: the agent-side loop branches on it.
# The prose lives in `explain()`.
RECOVERY_NONE = "none"
RECOVERY_DROP_TOKEN_AND_RETRY = "drop_token_and_retry"
RECOVERY_REPHRASE_WITHOUT_NEGATION = "rephrase_without_negation"
RECOVERY_BROADEN_QUERY = "broaden_query"
RECOVERY_WRITE_FIRST = "write_first"

_RECOVERY = {
    VerdictCode.OK: RECOVERY_NONE,
    VerdictCode.ABSTAINED_ON: RECOVERY_DROP_TOKEN_AND_RETRY,
    VerdictCode.NEGATION_ABSTAIN: RECOVERY_REPHRASE_WITHOUT_NEGATION,
    VerdictCode.GATED: RECOVERY_BROADEN_QUERY,
    VerdictCode.EMPTY_STORE: RECOVERY_WRITE_FIRST,
    VerdictCode.NO_MATCH: RECOVERY_BROADEN_QUERY,
}


def recovery_for(code: VerdictCode) -> str:
    return _RECOVERY.get(code, RECOVERY_NONE)


def _clean_tokens(tokens) -> list[str]:
    """Bound the token list: caps count and per-token length, drops non-strings.

    Tokens come from the CALLER'S query, never from a stored record, so this is
    a size bound rather than a trust boundary — but the bound is enforced here,
    once, so no surface has to remember to do it.
    """
    if not tokens:
        return []
    out: list[str] = []
    for t in tokens:
        if not isinstance(t, str):
            continue
        out.append(t[:MAX_TOKEN_CHARS])
        if len(out) >= MAX_TOKENS_REPORTED:
            break
    return out


@dataclass
class GateCounters:
    """Per-gate drop counts + the best coverage any candidate reached.

    Numbers only. ``best_pre_gate_coverage`` is the highest IDF-weighted
    coverage computed for any candidate that reached the coverage comparison
    (candidates removed by the prep filter never have a coverage computed, by
    design — the filter runs first and computing a score for a row we already
    know is dropped would be work for nothing). ``None`` means no candidate ever
    reached the coverage comparison.
    """

    coverage_floor: int = 0
    anchor_gate: int = 0
    prep_filter: int = 0
    best_pre_gate_coverage: float | None = None

    def record(self, gate: GateCause) -> None:
        setattr(self, gate.value, getattr(self, gate.value) + 1)

    def observe_coverage(self, cov: float) -> None:
        if self.best_pre_gate_coverage is None or cov > self.best_pre_gate_coverage:
            self.best_pre_gate_coverage = cov

    @property
    def total(self) -> int:
        return self.coverage_floor + self.anchor_gate + self.prep_filter

    def dominant(self) -> GateCause | None:
        """The gate that dropped the most candidates, ties broken by GATE_ORDER."""
        best: GateCause | None = None
        best_n = 0
        for g in GATE_ORDER:
            n = getattr(self, g.value)
            if n > best_n:
                best, best_n = g, n
        return best

    def as_dict(self) -> dict:
        return {
            GateCause.COVERAGE_FLOOR.value: self.coverage_floor,
            GateCause.ANCHOR_GATE.value: self.anchor_gate,
            GateCause.PREP_FILTER.value: self.prep_filter,
        }


@dataclass
class Verdict:
    """Why a search returned what it returned.

    Every field is a number, an enum member, a bool, or a token the caller
    typed. Nothing here is derived from a stored record.
    """

    code: VerdictCode = VerdictCode.OK
    #: The blocking / dropped query token(s), for ABSTAINED_ON and
    #: NEGATION_ABSTAIN. Empty for every other code.
    tokens: list[str] = field(default_factory=list)
    #: Which gate dominated, for GATED. None otherwise.
    gate: GateCause | None = None
    #: Per-gate drop counts + best coverage reached. Always present so a caller
    #: can see that gates ran and dropped nothing, which is itself information.
    gates: GateCounters = field(default_factory=GateCounters)
    #: Significant tokens the query produced, before any drop. ``None`` means
    #: "not measured at this level" — ``client.search()`` is a policy-free
    #: primitive with no token pipeline, and reporting 0 there would let
    #: ``explain()`` claim the query had no searchable terms when it had plenty.
    #: Unknown and zero are different facts; conflating them is how a diagnostic
    #: starts lying.
    tokens_total: int | None = None
    #: Significant tokens that survived to scoring. ``None`` = not measured.
    tokens_scored: int | None = None
    #: Candidate rows gathered in stage 1, before the verify gates. ``None`` =
    #: not measured (there is no candidate stage in the raw primitive).
    candidates: int | None = None
    #: Rows actually returned.
    returned: int = 0
    #: Function-shaped tokens excluded from scoring (N1 at df==0, N4 at any df).
    dropped_function: list[str] = field(default_factory=list)

    # -- constructors are the module-level helpers below; use those. --

    @property
    def abstained(self) -> bool:
        return self.code in (VerdictCode.ABSTAINED_ON, VerdictCode.NEGATION_ABSTAIN)

    @property
    def is_zero_cause(self) -> bool:
        return self.code in ZERO_CAUSES

    @property
    def recovery(self) -> str:
        return recovery_for(self.code)

    def explain(self) -> str:
        return explain(self)

    def as_dict(self) -> dict:
        """The wire envelope. Bounded, JSON-safe, additive-only.

        Key order is fixed so a diff of two envelopes reads cleanly. Every
        surface emits THIS shape; none of them builds its own.
        """
        return {
            "code": self.code.value,
            "tokens": _clean_tokens(self.tokens),
            "gate": self.gate.value if self.gate is not None else None,
            "gate_drops": self.gates.as_dict(),
            "best_pre_gate_coverage": (
                round(self.gates.best_pre_gate_coverage, 4)
                if self.gates.best_pre_gate_coverage is not None else None
            ),
            "tokens_total": self.tokens_total,
            "tokens_scored": self.tokens_scored,
            "dropped_function": _clean_tokens(self.dropped_function),
            "candidates": self.candidates,
            "returned": self.returned,
            "abstained": self.abstained,
            "recovery": self.recovery,
            "explain": self.explain(),
        }


# --- constructors ------------------------------------------------------------
def ok_verdict(**kw) -> Verdict:
    return Verdict(code=VerdictCode.OK, **kw)


def no_match_verdict(**kw) -> Verdict:
    return Verdict(code=VerdictCode.NO_MATCH, **kw)


def empty_store_verdict(**kw) -> Verdict:
    return Verdict(code=VerdictCode.EMPTY_STORE, **kw)


def abstained_on_verdict(token: str, **kw) -> Verdict:
    return Verdict(code=VerdictCode.ABSTAINED_ON, tokens=[token], **kw)


def negation_abstain_verdict(tokens, **kw) -> Verdict:
    return Verdict(code=VerdictCode.NEGATION_ABSTAIN, tokens=list(tokens or []), **kw)


def gated_verdict(gate: GateCause, gates: GateCounters, **kw) -> Verdict:
    return Verdict(code=VerdictCode.GATED, gate=gate, gates=gates, **kw)


# --- plain language ----------------------------------------------------------
def explain(v: Verdict) -> str:
    """One sentence a human (or an agent) can act on, generated from the enum.

    Deliberately built from the verdict's own fields and fixed strings — never
    from stored text — so this string is safe to print in a terminal, embed in
    an MCP response, or show a user, with no scrubbing of its own.
    """
    if v.code is VerdictCode.OK:
        return f"{v.returned} row(s) matched."
    if v.code is VerdictCode.ABSTAINED_ON:
        tok = v.tokens[0][:MAX_TOKEN_CHARS] if v.tokens else "?"
        return (
            f"Nothing was returned because the word {tok!r} appears nowhere in this "
            f"store, and an unsupported content word blocks the whole query "
            f"(this is the gate that makes made-up terms return nothing). "
            f"Drop {tok!r} and ask again — the rest of the query is not the problem."
        )
    if v.code is VerdictCode.NEGATION_ABSTAIN:
        toks = ", ".join(repr(t) for t in v.tokens[:MAX_TOKENS_REPORTED]) or "a negation"
        return (
            f"Nothing was returned because the query is negated ({toks}) and full-text "
            f"search cannot honour a negation; answering would have returned the record "
            f"asserting the OPPOSITE. Ask the positive form and check the answer yourself."
        )
    if v.code is VerdictCode.GATED:
        gate = v.gate.value if v.gate else "a relevance gate"
        best = v.gates.best_pre_gate_coverage
        near = (f" The best candidate reached {best:.2f} coverage."
                if best is not None else "")
        return (
            f"{v.candidates if v.candidates is not None else 'Some'} candidate row(s) "
            f"were found and then dropped by the "
            f"{gate} relevance gate.{near} The store does hold related rows; the query "
            f"did not cover any of them strongly enough. Use more of the exact wording, "
            f"or search a single distinctive term."
        )
    if v.code is VerdictCode.EMPTY_STORE:
        return "Nothing was returned because this memory store holds no rows yet."
    if v.code is VerdictCode.NO_MATCH:
        if v.tokens_total == 0:
            return ("Nothing was returned because the query carried no searchable "
                    "terms (too short, or only stopwords).")
        return ("Nothing was returned: the query ran with every gate armed and "
                "matched no rows. A genuine miss, not a blocked query — try "
                "different or broader wording.")
    return "No cause recorded."  # pragma: no cover - unreachable while the enum is closed


# --- the carrier -------------------------------------------------------------
class SearchResults(list):
    """A list of hits that also carries WHY it is the length it is.

    Subclasses ``list`` on purpose. Every existing caller — `len(hits)`,
    `for h in hits`, `hits[0]`, `hits == []`, `json.dumps(hits)`,
    `isinstance(hits, list)` — keeps working byte-for-byte, so the verdict is
    additive to a released API rather than a breaking change. That is what makes
    "the verdict is part of the RETURN, not an optional kwarg" affordable: no
    caller has to opt in, and no caller has to be updated.

    Slicing, `hits + [...]` and `list(hits)` produce a PLAIN list and lose the
    verdict, which is correct: a derived list is not the search's answer, and a
    surface that wants the cause must read it from the object the search
    returned. `copy.copy` / `copy.deepcopy` / `pickle` DO round-trip the verdict
    (they go through `__reduce_ex__`, which carries `__slots__` state), so a
    result handed across a process boundary keeps its explanation.

    NOT a frozen view. Mutating the list in place (`append`, `+=`, `pop`) leaves
    `verdict.returned` describing what the SEARCH returned rather than what the
    list now holds. Nothing in this repo does that, and if something ever needs
    to, the answer is to build a new `SearchResults` — not to make the list
    immutable, because behaving exactly like the list it replaced is the whole
    reason this carrier could be added to a released API at all.
    """

    __slots__ = ("verdict",)

    def __init__(self, rows=(), verdict: Verdict | None = None):
        super().__init__(rows)
        self.verdict = verdict if verdict is not None else Verdict(returned=len(self))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SearchResults({list(self)!r}, verdict={self.verdict.code.value!r})"


def stamp(rows, verdict: Verdict) -> SearchResults:
    """THE single exit. Attach a verdict to a row list and return the carrier.

    Called from exactly one place per search entry point. A bare ``return []``
    anywhere below a search entry point is a contract violation, and the
    per-package contract tests assert it cannot happen.
    """
    rows = list(rows)
    verdict.returned = len(rows)
    if rows:
        # A non-empty result is OK by definition; a cause is only meaningful for
        # a zero. This keeps callers from having to reason about "gated but also
        # returned three rows".
        verdict.code = VerdictCode.OK
        verdict.gate = None
    elif verdict.code is VerdictCode.OK:
        # Defensive: an empty result must never ship as OK. If a future edit
        # forgets to classify, it lands on the honest miss rather than lying.
        verdict.code = VerdictCode.NO_MATCH
    return SearchResults(rows, verdict)


# --- store emptiness ---------------------------------------------------------
#: The tables a search can reach. `empty_store` means every one of them is empty
#: for this tenant — NOT that `entities` alone is empty (a store can hold only
#: journal or reference rows and still answer queries).
_SEARCHABLE_TABLES = ("entities", "state_documents", "reference_documents",
                      "journal_events")


def refine_zero(client, results) -> "SearchResults":
    """Upgrade a bare ``NO_MATCH`` zero to ``EMPTY_STORE`` when the store is empty.

    Costs one probe (``store_is_empty``) and is therefore called ONLY by a
    user-facing surface — the CLI, the MCP tool — once per zero-row response,
    never on the hot per-token recall path inside ``multi_record``. The engine
    itself stamps the cheap cause; the surface pays one COUNT to tell a new user
    "you haven't written anything yet" instead of "no match", which is the
    difference between a fixable state and a mysterious one.

    A non-empty result, or a zero that already carries a more specific cause, is
    returned untouched.
    """
    v = getattr(results, "verdict", None)
    if v is None or results or v.code is not VerdictCode.NO_MATCH:
        return results
    if store_is_empty(client):
        v.code = VerdictCode.EMPTY_STORE
        v.gate = None
        v.tokens = []
    return results


def store_is_empty(client) -> bool:
    """True only if the tenant has no searchable row in ANY tier.

    Costs up to four indexed ``COUNT(*)`` queries and is therefore called ONLY
    on the zero-result path, once per zero, at the surface that reports the
    verdict. It is never called on the hot per-token path inside stage-1
    recall. Any failure returns False — an unverifiable store is reported as a
    miss, never as "your store is empty", because the second claim is the one
    that would make an agent give up on a store that has data in it.
    """
    storage = getattr(client, "storage", None)
    if storage is None or not hasattr(storage, "count_rows"):
        return False
    tenant = None
    get_tenant = getattr(client, "get_tenant", None)
    if callable(get_tenant):
        try:
            tenant = get_tenant()
        except Exception:
            return False
    if tenant is None:
        return False
    try:
        for table in _SEARCHABLE_TABLES:
            if storage.count_rows(table, tenant) > 0:
                return False
    except Exception:
        return False
    return True
