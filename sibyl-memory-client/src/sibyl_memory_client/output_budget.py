"""Output budget for agent-facing read results (2026-10-22).

Why this exists: Claude Code does not inline a large MCP tool result. Per
https://code.claude.com/docs/en/mcp ("MCP output limits and warnings"), a
result over 50,000 characters (for a tool without the
``anthropic/maxResultSizeChars`` annotation), or over MAX_MCP_OUTPUT_TOKENS
(default 25,000 tokens), is saved to a file and replaced by a pointer, and a
warning shows past 10,000 tokens. An agent without a file-read tool then sees
nothing. V1 search hits (body prefix + snippet + title + links) put
``memory_search`` at ~69,000 chars at limit=30, so the result vanished.

The surfaces (MCP server, Hermes adapter) hold one result to an ESTIMATED
token budget (default 22,000: ``estimate_tokens`` = ceil(ascii/4) + one per
non-ASCII char, conservative for CJK) and a hard character cap (190,000 under
a ``maxResultSizeChars`` annotation of 200,000 when the host honours it, else
45,000, under the 50,000-char persistence line), and degrade in a fixed order,
so the hit SET only changes as the last resort:

1. a hit that carries a match ``snippet`` loses its body prefix first
   (500, 150, then omitted): the snippet carries the match, and ``chars``
   says how long the full body is;
2. snippets shorten from 600 to 250 chars, re-centred on the first match
   marker;
3. link summaries drop their ``sample`` (degree and per-relation counts stay);
4. only then are whole trailing hits dropped, and the caller reports how many.

Everything here is pure: the caller supplies ``fits(hits) -> bool``, which
measures the result the way its own wire renders it.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

#: Fallback hard character cap per result, for a host that does not honour the
#: ``anthropic/maxResultSizeChars`` tool annotation (under Claude Code's
#: default 50,000-char persist-to-file line).
DEFAULT_OUTPUT_BUDGET = 45_000
MIN_OUTPUT_BUDGET = 4_000
MAX_OUTPUT_BUDGET = 500_000
#: The per-tool ``_meta["anthropic/maxResultSizeChars"]`` the MCP server
#: declares on its large read tools, and the char cap used when the host
#: honours it (kept under the annotation so it never binds first).
RESULT_SIZE_META_CHARS = 200_000
ANNOTATED_OUTPUT_BUDGET = 190_000
#: Estimated-token budget per result: just under MAX_MCP_OUTPUT_TOKENS'
#: default 25,000, with margin for the estimate.
DEFAULT_TOKEN_BUDGET = 22_000
MIN_TOKEN_BUDGET = 1_000
MAX_TOKEN_BUDGET = 125_000

#: Body-prefix lengths tried, in order, before a body is omitted.
BODY_STEPS = (500, 150)
#: Snippet length after the body stages (match-centred).
SNIPPET_STEP = 250

_MATCH_MARKER = re.compile(r"\[[^\[\]\s]{1,64}\]")


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ceil(ascii_chars / 4) + non_ascii_chars.
    English JSON runs well under this; CJK (about a token per char) is
    counted at one per char."""
    ascii_n = len(text.encode("ascii", "ignore"))
    return -(-ascii_n // 4) + (len(text) - ascii_n)

ELLIPSIS = "…"


def budget_from_env(var: str, default: int = DEFAULT_OUTPUT_BUDGET, *,
                    lo: int = MIN_OUTPUT_BUDGET, hi: int = MAX_OUTPUT_BUDGET) -> int:
    """Read an integer budget from ``var``; unset or junk means ``default``.
    Clamped to [lo, hi] (chars by default: the upper bound is Claude Code's
    hard per-tool ceiling for annotated tools)."""
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip().replace("_", ""))
    except ValueError:
        return default
    return min(max(value, lo), hi)


def token_budget_from_env(var: str, default: int = DEFAULT_TOKEN_BUDGET) -> int:
    return budget_from_env(var, default, lo=MIN_TOKEN_BUDGET, hi=MAX_TOKEN_BUDGET)


def within(text: str, *, tokens: int, chars: int) -> bool:
    """True when ``text`` is under both the token estimate and the char cap."""
    return len(text) <= chars and estimate_tokens(text) <= tokens


def _shrink_snippet(hit: dict[str, Any], cap: int) -> dict[str, Any]:
    s = hit.get("snippet")
    if not isinstance(s, str) or len(s) <= cap + 1:
        return hit
    m = _MATCH_MARKER.search(s)
    centre = m.start() if m else 0
    start = max(0, min(centre - cap // 3, len(s) - cap))
    piece = s[start:start + cap]
    if start > 0:
        piece = ELLIPSIS + piece
    if start + cap < len(s):
        piece = piece + ELLIPSIS
    return {**hit, "snippet": piece, "truncated": True}


def _has_snippet(hit: dict[str, Any]) -> bool:
    s = hit.get("snippet")
    return isinstance(s, str) and bool(s.strip())


def _rendered(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _shrink_body(hit: dict[str, Any], cap: int | None) -> dict[str, Any]:
    if "body" not in hit or hit.get("body") is None:
        return hit
    out = dict(hit)
    if cap is None:
        del out["body"]
        out["body_omitted"] = True
        out["truncated"] = True
        return out
    text = _rendered(out["body"])
    if text.endswith(ELLIPSIS) and out.get("truncated"):
        text = text[:-1]
    if len(text) > cap:
        out["body"] = text[:cap] + ELLIPSIS
        out["truncated"] = True
    return out


def _counts_only(hit: dict[str, Any]) -> dict[str, Any]:
    links = hit.get("links")
    if not isinstance(links, dict) or "sample" not in links:
        return hit
    trimmed = {k: v for k, v in links.items() if k not in ("sample", "owners_total")}
    trimmed["sample_omitted"] = True
    return {**hit, "links": trimmed}


def shape_hits(hits: list[dict[str, Any]], fits: Callable[[list[dict[str, Any]]], bool], *,
               snippet_only: bool = True,
               body_steps: tuple[int, ...] = BODY_STEPS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fit ``hits`` (already per-hit capped) into the caller's budget.

    ``snippet_only=True`` (search): only hits with a match snippet lose body,
    and their body is finally omitted. ``False`` (list): every body is
    shortened through ``body_steps`` but never omitted, since there it is the
    only content. Returns (hits, info) where info has ``degraded`` (stage names
    applied, in order) and ``truncated`` (trailing hits dropped). The first
    stage that fits wins, so a result that already fits is returned unchanged.
    """
    info: dict[str, Any] = {"degraded": [], "truncated": 0}
    if not hits or fits(hits):
        return hits, info

    def _bodies(cap: int | None) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def _apply(h: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(h, dict) or (snippet_only and not _has_snippet(h)):
                return h
            return _shrink_body(h, cap)
        return _apply

    stages: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = [
        (f"body_{n}", _bodies(n)) for n in body_steps]
    if snippet_only:
        stages.append(("body_omitted", _bodies(None)))
    stages.append((f"snippet_{SNIPPET_STEP}",
                   lambda h: _shrink_snippet(h, SNIPPET_STEP) if isinstance(h, dict) else h))
    stages.append(("links_counts_only",
                   lambda h: _counts_only(h) if isinstance(h, dict) else h))

    current = hits
    for name, fn in stages:
        nxt = [fn(h) for h in current]
        if nxt == current:
            continue
        current = nxt
        info["degraded"].append(name)
        if fits(current):
            return current, info

    # Last resort: the largest prefix that fits (fits() is monotone in the
    # prefix length). Always keep the top hit, so a result is never emptied
    # by its own shaping.
    lo, hi = 1, len(current) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(current[:mid]):
            lo = mid
        else:
            hi = mid - 1
    kept = current[:lo]
    info["truncated"] = len(current) - len(kept)
    return kept, info


TRUNCATION_HINT = (
    "the result was cut to fit the output budget: call again with a smaller "
    "limit, or read one record in full with memory_recall (entity) / "
    "memory_get_reference (reference document)")
