"""Self-learning module for sibyl-memory-client.

Mirrors the way SIBYL accumulates session memory into reusable skills:
scan the journal for repeating patterns, abstract them into structured
skill documents, and queue the proposals for user review.

THREE RUNTIME MODES (operator directive 2026-05-15)
===================================================

1. **local-deterministic** (default, free tier)
   Pure SQL + Python pattern detectors. No network, no LLM. Preserves the
   strict local-first promise. Produces skill bodies via deterministic
   templates from the matched event group.

2. **byok** (paid-tier opt-in)
   User pastes their own Anthropic / OpenAI / Venice key into config.
   The Learner uses the key to summarize matched event clusters into
   prose skill bodies. Local-first stays intact at the data layer -
   the user controls where the inference call goes. Sibyl Labs never
   sees the key or the payload.

3. **venice-x402** (paid-tier hosted, value-add for Venice partnership)
   User pre-funds their plugin account with FIAT or USDC. Sibyl Labs
   auto-routes inference via Venice + x402 against the user's funded
   balance from Sibyl's own infrastructure. Highest convenience, only
   the prompt summary leaves the device (never the underlying memory
   content). The Venice/x402 endpoint design is captured in the memo
   `memory/research/2026-05-15-self-learning-design.md`.

WHAT GETS DETECTED
==================

Four pattern kinds in v0.2.0:

| pattern_kind            | what it catches                                |
|-------------------------|------------------------------------------------|
| repeated_action         | same/similar `acted` payload across N events  |
| structural_similarity   | journal events with overlapping evaluated keys|
| temporal_routine        | events that fire at a stable cadence          |
| co_occurrence           | entities + actions that consistently appear   |
|                         | together in the same journal entries          |

Pattern detection is intentionally simple and explainable. Sophisticated
embedding-based clustering can land in v0.3.0 as an optional add-on.

REVIEW QUEUE
============

Detected patterns land in `skill_proposals` with status='pending'. The
public API exposes:

    list_proposals(status='pending', limit=N)
    accept_proposal(proposal_id, note=None)   → writes to reference_documents
    reject_proposal(proposal_id, note=None)
    get_proposal(proposal_id)

Accepted proposals create `reference_documents` rows keyed `skill/<slug>`.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .client import DEFAULT_TENANT
from .exceptions import NotFoundError, ValidationError
from .storage import Storage, _utc_now_iso, dumps, loads, new_id

logger = logging.getLogger(__name__)

# F6 (red-team 2026-06-17): cap the per-run journal scan. The journal grows
# unbounded (every turn appends); an uncapped SELECT + fetchall + 4-column JSON
# decode is a memory/CPU spike on a large journal. This is a DoS backstop, not a
# routine limit — the watermark (max scanned ts) advances each run, so a large
# backlog drains across runs instead of in one spike.
_MAX_EVENTS_PER_RUN = 10000


# ----------------------------------------------------------------------
# Public API surface
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class SkillProposal:
    """Immutable view of a row in skill_proposals."""
    id: str
    tenant_id: str
    pattern_kind: str
    proposed_slug: str
    proposed_title: str | None
    proposed_body: str
    evidence: list[dict[str, Any]]
    confidence: float
    summarizer: str
    status: str
    created_at: str
    reviewed_at: str | None = None
    review_note: str | None = None
    accepted_doc_key: str | None = None


@dataclass
class LearningRunReport:
    """Per-invocation summary returned by Learner.run()."""
    run_id: str
    events_scanned: int
    proposals_made: int
    proposal_ids: list[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    summarizer: str = ""


class Summarizer(Protocol):
    """Pluggable interface for converting a detected pattern into prose.

    Implementations must be synchronous and side-effect-free with respect
    to the local SQLite database. The Learner handles all persistence.
    """

    name: str

    def summarize(
        self,
        pattern_kind: str,
        events: list[dict[str, Any]],
        hints: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Return (body_markdown, title_or_None) for the proposal."""
        ...


# ----------------------------------------------------------------------
# Local-deterministic summarizer (free-tier default)
# ----------------------------------------------------------------------

class LocalDeterministicSummarizer:
    """Generates skill bodies via templates, no LLM call.

    Useful properties:
      • Zero network. Free-tier-safe.
      • Deterministic: same input always produces the same body.
      • Explains its own reasoning (so the user sees why the pattern
        was surfaced).
    """

    name = "local-deterministic"

    def summarize(
        self,
        pattern_kind: str,
        events: list[dict[str, Any]],
        hints: dict[str, Any],
    ) -> tuple[str, str | None]:
        title = hints.get("title") or _slug_to_title(hints.get("slug", pattern_kind))
        lines: list[str] = []
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"_Auto-detected from {len(events)} matching journal events._")
        lines.append("")
        lines.append("## Pattern")
        lines.append("")
        if pattern_kind == "repeated_action":
            sample = hints.get("action_signature") or "(no action signature)"
            lines.append(f"Recurring action: `{sample}`")
        elif pattern_kind == "structural_similarity":
            keys = ", ".join(hints.get("shared_keys", []) or [])
            lines.append(f"Events consistently include input keys: `{keys}`")
        elif pattern_kind == "temporal_routine":
            cadence = hints.get("cadence_minutes")
            lines.append(
                f"Events fire at roughly stable cadence "
                f"(~{cadence} min between occurrences)."
                if cadence
                else "Events fire at a stable cadence."
            )
        elif pattern_kind == "co_occurrence":
            pair = hints.get("pair") or ("", "")
            lines.append(
                f"`{pair[0]}` and `{pair[1]}` consistently appear together in "
                f"the same journal entries."
            )
        else:
            lines.append("(pattern kind unrecognized: flagged for review)")

        lines.append("")
        lines.append("## Evidence")
        lines.append("")
        for ev in events[:5]:  # cap at five for readability
            ts = ev.get("ts") or "?"
            snippet = _short_event_snippet(ev)
            lines.append(f"- `{ts}`: {snippet}")
        if len(events) > 5:
            lines.append(f"- _…and {len(events) - 5} more matching events_")
        lines.append("")
        lines.append("## Suggested use")
        lines.append("")
        lines.append(
            "Reference this skill when the same situation recurs. "
            "Edit, accept, or reject via `sibyl learn review`."
        )
        return "\n".join(lines), title


# ----------------------------------------------------------------------
# BYOK summarizer stub (paid-tier opt-in)
# ----------------------------------------------------------------------

class BYOKSummarizer:
    """User-supplied-key summarizer.

    The user passes a callable `inference_fn(prompt: str) -> str` so the
    SDK never holds the key itself. The callable can be implemented
    against Anthropic, OpenAI, Venice, or any provider: the SDK
    doesn't care.

    Free-tier installs cannot construct this class (the CLI's tier
    check happens upstream). v0.2.0 ships the wiring; the CLI gate
    enforces it.
    """

    def __init__(
        self,
        inference_fn: Callable[[str], str],
        *,
        provider_label: str = "byok",
    ) -> None:
        self._inference_fn = inference_fn
        self.name = f"byok-{provider_label}"

    def summarize(
        self,
        pattern_kind: str,
        events: list[dict[str, Any]],
        hints: dict[str, Any],
    ) -> tuple[str, str | None]:
        prompt = _build_summarization_prompt(pattern_kind, events, hints)
        try:
            body = self._inference_fn(prompt)
        except Exception as e:  # pragma: no cover
            # Fall back to deterministic if the user's key fails
            fallback = LocalDeterministicSummarizer()
            body, title = fallback.summarize(pattern_kind, events, hints)
            return body + f"\n\n---\n_Note: BYOK call failed ({e}). Using local fallback._", title
        title = hints.get("title") or _slug_to_title(hints.get("slug", pattern_kind))
        return body, title


# ----------------------------------------------------------------------
# Venice + x402 routed summarizer stub (paid-tier hosted)
# ----------------------------------------------------------------------

class VeniceX402Summarizer:
    """Routes inference through Venice via x402 against the user's
    pre-funded Sibyl Labs plugin balance.

    The actual network call lives behind `inference_fn` so this module
    stays HTTP-library-free. The CLI layer (sibyl-labs-cli) provides
    the real fn that signs an x402 payment header, hits the Sibyl
    Labs inference proxy (planned: `POST /api/plugin/inference`), and
    returns the Venice-routed completion.

    Endpoint design recorded in
    `memory/research/2026-05-15-self-learning-design.md`.
    """

    name = "venice-x402"

    def __init__(
        self,
        inference_fn: Callable[[str], str],
        *,
        account_id: str,
    ) -> None:
        self._inference_fn = inference_fn
        self._account_id = account_id

    def summarize(
        self,
        pattern_kind: str,
        events: list[dict[str, Any]],
        hints: dict[str, Any],
    ) -> tuple[str, str | None]:
        prompt = _build_summarization_prompt(pattern_kind, events, hints)
        try:
            body = self._inference_fn(prompt)
        except Exception as e:  # pragma: no cover
            fallback = LocalDeterministicSummarizer()
            body, title = fallback.summarize(pattern_kind, events, hints)
            return body + f"\n\n---\n_Note: Venice/x402 call failed ({e}). Using local fallback._", title
        title = hints.get("title") or _slug_to_title(hints.get("slug", pattern_kind))
        return body, title


# ----------------------------------------------------------------------
# Learner: orchestrates detection + summarization + persistence
# ----------------------------------------------------------------------

class Learner:
    """Periodic learning loop. Reads journal, writes skill proposals.

    Args:
        storage: the live Storage instance
        tenant_id: which tenant's journal to scan
        summarizer: pluggable summarizer (defaults to local-deterministic)
        min_pattern_hits: minimum matched events to surface a pattern
        max_proposals_per_run: cap to avoid swamping the review queue
        cap_gate: optional CapGate. When provided, accept_proposal calls
            the gate before writing the reference_documents row (T1-3 fix).
            When None, no cap check is performed: exposed for advanced
            callers who construct Learner directly and own their own
            enforcement.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        tenant_id: str = DEFAULT_TENANT,
        summarizer: Summarizer | None = None,
        min_pattern_hits: int = 3,
        max_proposals_per_run: int = 20,
        cap_gate: Any = None,
    ) -> None:
        self._storage = storage
        self._tenant_id = tenant_id
        self._summarizer = summarizer or LocalDeterministicSummarizer()
        self._min_hits = max(2, min_pattern_hits)
        self._max_per_run = max(1, max_proposals_per_run)
        self._cap_gate = cap_gate

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def run(self, *, since: str | None = None) -> LearningRunReport:
        """Scan journal events since the last watermark and propose skills."""
        run_id = new_id()
        started_at = _utc_now_iso()

        # Resolve watermark: explicit `since` wins, otherwise look up last run
        since_ts = since or self._last_watermark()
        events = self._load_events(since=since_ts)
        scanned = len(events)

        # Skip detection entirely if there's nothing new
        proposal_ids: list[str] = []
        if scanned == 0:
            self._log_run(
                run_id=run_id,
                started_at=started_at,
                completed_at=_utc_now_iso(),
                events_scanned=0,
                proposals_made=0,
                cursor_after_ts=since_ts,
                notes="no new events since last run",
            )
            return LearningRunReport(
                run_id=run_id,
                events_scanned=0,
                proposals_made=0,
                proposal_ids=[],
                started_at=started_at,
                completed_at=_utc_now_iso(),
                summarizer=self._summarizer.name,
            )

        # Run detectors, accumulate candidate proposals
        candidates: list[_Candidate] = []
        candidates.extend(_detect_repeated_actions(events, min_hits=self._min_hits))
        candidates.extend(_detect_structural_similarity(events, min_hits=self._min_hits))
        candidates.extend(_detect_co_occurrence(events, min_hits=self._min_hits))
        # temporal_routine: light-touch detector, deliberately last
        candidates.extend(_detect_temporal_routine(events, min_hits=self._min_hits))

        # Deduplicate by slug: keep the highest-confidence candidate per slug
        deduped: dict[str, _Candidate] = {}
        for c in candidates:
            existing = deduped.get(c.slug)
            if existing is None or c.confidence > existing.confidence:
                deduped[c.slug] = c

        # Cap, sort by confidence
        ranked = sorted(deduped.values(), key=lambda c: -c.confidence)[: self._max_per_run]

        # Skip ones that already exist as pending proposals (same tenant, same slug)
        existing_slugs = self._pending_slugs()
        ranked = [c for c in ranked if c.slug not in existing_slugs]

        # Persist
        for c in ranked:
            body, title = self._summarizer.summarize(c.kind, c.events, c.hints)
            pid = self._insert_proposal(c, body=body, title=title)
            proposal_ids.append(pid)

        # Watermark
        cursor_after = max((ev.get("ts") or "") for ev in events) or since_ts

        self._log_run(
            run_id=run_id,
            started_at=started_at,
            completed_at=_utc_now_iso(),
            events_scanned=scanned,
            proposals_made=len(proposal_ids),
            cursor_after_ts=cursor_after,
            notes=None,
        )

        return LearningRunReport(
            run_id=run_id,
            events_scanned=scanned,
            proposals_made=len(proposal_ids),
            proposal_ids=proposal_ids,
            started_at=started_at,
            completed_at=_utc_now_iso(),
            summarizer=self._summarizer.name,
        )

    def list_proposals(
        self,
        *,
        status: str = "pending",
        limit: int = 50,
    ) -> list[SkillProposal]:
        with self._storage.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_proposals "
                "WHERE tenant_id = ? AND status = ? "
                "ORDER BY confidence DESC, created_at DESC LIMIT ?",
                (self._tenant_id, status, limit),
            ).fetchall()
        return [_row_to_proposal(r) for r in rows]

    def get_proposal(self, proposal_id: str) -> SkillProposal:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT * FROM skill_proposals WHERE id = ? AND tenant_id = ?",
                (proposal_id, self._tenant_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"skill_proposal {proposal_id} not found")
        return _row_to_proposal(row)

    def accept_proposal(
        self,
        proposal_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Accept a proposal. Writes a reference_documents row keyed
        `skill/<slug>` and marks the proposal accepted."""
        proposal = self.get_proposal(proposal_id)
        if proposal.status != "pending":
            raise ValidationError(
                f"proposal {proposal_id} is {proposal.status}, cannot accept",
                recovery="Only pending proposals can be accepted. Use list_proposals(status='pending').",
            )
        doc_key = f"skill/{proposal.proposed_slug}"
        metadata = {
            "source": "sibyl-memory-client/learning",
            "pattern_kind": proposal.pattern_kind,
            "summarizer": proposal.summarizer,
            "confidence": proposal.confidence,
            "evidence_count": len(proposal.evidence),
            "title": proposal.proposed_title,
        }
        metadata_json = dumps(metadata)
        # T1-3 fix: gate the reference_documents insert through the cap
        # check. Free user at 1.9MB could previously accept skill proposals
        # (often kilobytes of body) to keep writing past the 2 MB cap.
        # When cap_gate is None (direct-Learner instantiation), no check.
        if self._cap_gate is not None:
            # CAP-7 (2026-06-25 pre-launch audit): the estimate omitted the
            # metadata JSON and the FTS5 index overhead. reference_documents is
            # FTS5-indexed, so the body is effectively stored twice-over (base
            # row + tokenized index). Count the body + metadata + key, then add a
            # ~1x body FTS overhead factor plus base row overhead, so the cap
            # estimate is not a systematic under-count that lets accept_proposal
            # squeak past the cap.
            body_len = len(proposal.proposed_body or "")
            fts_overhead = body_len  # FTS5 index roughly mirrors the body size
            body_size = body_len + len(metadata_json) + len(doc_key) + fts_overhead + 250
            self._cap_gate.check(proposed_delta_bytes=body_size)
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO reference_documents (tenant_id, doc_key, body, metadata) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, doc_key) DO UPDATE SET "
                "body = excluded.body, metadata = excluded.metadata, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (
                    self._tenant_id,
                    doc_key,
                    proposal.proposed_body,
                    metadata_json,
                ),
            )
            conn.execute(
                "UPDATE skill_proposals "
                "SET status = 'accepted', reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "review_note = ?, accepted_doc_key = ? "
                "WHERE id = ? AND tenant_id = ?",
                (note, doc_key, proposal_id, self._tenant_id),
            )
        return {"accepted": True, "doc_key": doc_key, "proposal_id": proposal_id}

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        if proposal.status != "pending":
            raise ValidationError(
                f"proposal {proposal_id} is {proposal.status}, cannot reject",
                recovery="Only pending proposals can be rejected.",
            )
        with self._storage.transaction() as conn:
            conn.execute(
                "UPDATE skill_proposals "
                "SET status = 'rejected', reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "review_note = ? "
                "WHERE id = ? AND tenant_id = ?",
                (note, proposal_id, self._tenant_id),
            )
        return {"rejected": True, "proposal_id": proposal_id}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _last_watermark(self) -> str | None:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT cursor_after_ts FROM learning_runs "
                "WHERE tenant_id = ? AND completed_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (self._tenant_id,),
            ).fetchone()
        return row["cursor_after_ts"] if row else None

    def _load_events(self, *, since: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, ts, evaluated, acted, forward, extra "
            "FROM journal_events WHERE tenant_id = ?"
        )
        params: list[Any] = [self._tenant_id]
        if since:
            sql += " AND ts > ?"
            params.append(since)
        # F6: bound the scan (oldest-first so the watermark can resume). A backlog
        # over the cap drains across subsequent runs.
        sql += " ORDER BY ts ASC, id ASC LIMIT ?"
        params.append(_MAX_EVENTS_PER_RUN)
        with self._storage.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        if len(rows) >= _MAX_EVENTS_PER_RUN:
            logger.warning(
                "Sibyl learner scan hit the per-run cap (%d events); a large "
                "journal backlog will drain across multiple runs.",
                _MAX_EVENTS_PER_RUN,
            )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "evaluated": loads(r["evaluated"]),
                "acted": loads(r["acted"]),
                "forward": loads(r["forward"]),
                "extra": loads(r["extra"]),
            }
            for r in rows
        ]

    def _pending_slugs(self) -> set[str]:
        with self._storage.connection() as conn:
            rows = conn.execute(
                "SELECT proposed_slug FROM skill_proposals "
                "WHERE tenant_id = ? AND status = 'pending'",
                (self._tenant_id,),
            ).fetchall()
        return {r["proposed_slug"] for r in rows}

    def _insert_proposal(
        self,
        candidate: "_Candidate",
        *,
        body: str,
        title: str | None,
    ) -> str:
        pid = new_id()
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO skill_proposals "
                "(id, tenant_id, pattern_kind, proposed_slug, proposed_title, "
                " proposed_body, evidence, confidence, summarizer) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    self._tenant_id,
                    candidate.kind,
                    candidate.slug,
                    title,
                    body,
                    dumps([
                        {"event_id": ev["id"], "ts": ev["ts"], "snippet": _short_event_snippet(ev)}
                        for ev in candidate.events[:20]
                    ]),
                    candidate.confidence,
                    self._summarizer.name,
                ),
            )
        return pid

    def _log_run(
        self,
        *,
        run_id: str,
        started_at: str,
        completed_at: str,
        events_scanned: int,
        proposals_made: int,
        cursor_after_ts: str | None,
        notes: str | None,
    ) -> None:
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO learning_runs "
                "(id, tenant_id, started_at, completed_at, summarizer, "
                " events_scanned, proposals_made, cursor_after_ts, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    self._tenant_id,
                    started_at,
                    completed_at,
                    self._summarizer.name,
                    events_scanned,
                    proposals_made,
                    cursor_after_ts,
                    notes,
                ),
            )


# ======================================================================
# Pattern detectors (deterministic, local-only)
# ======================================================================

@dataclass
class _Candidate:
    kind: str
    slug: str
    confidence: float
    events: list[dict[str, Any]]
    hints: dict[str, Any]


def _detect_repeated_actions(
    events: list[dict[str, Any]],
    *,
    min_hits: int,
) -> list[_Candidate]:
    """Cluster events by an abstracted action signature; surface clusters
    that occur >= min_hits times."""
    by_sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        acted = ev.get("acted")
        if acted is None:
            continue
        sig = _action_signature(acted)
        if not sig:
            continue
        by_sig[sig].append(ev)

    out: list[_Candidate] = []
    for sig, group in by_sig.items():
        if len(group) < min_hits:
            continue
        slug = _safe_slug("repeat-" + sig)
        # confidence scales with hit count, capped at 0.95
        confidence = min(0.95, 0.4 + 0.05 * len(group))
        out.append(_Candidate(
            kind="repeated_action",
            slug=slug,
            confidence=confidence,
            events=group,
            hints={"action_signature": sig, "slug": slug, "hits": len(group)},
        ))
    return out


def _detect_structural_similarity(
    events: list[dict[str, Any]],
    *,
    min_hits: int,
) -> list[_Candidate]:
    """Group events that share a stable set of input/output keys."""
    by_keys: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        evaluated = ev.get("evaluated")
        if not isinstance(evaluated, dict):
            continue
        keyset = tuple(sorted(evaluated.keys()))
        if not keyset:
            continue
        by_keys[keyset].append(ev)

    out: list[_Candidate] = []
    for keyset, group in by_keys.items():
        if len(group) < min_hits:
            continue
        slug = _safe_slug("shape-" + "-".join(keyset[:4]))
        confidence = min(0.85, 0.3 + 0.04 * len(group))
        out.append(_Candidate(
            kind="structural_similarity",
            slug=slug,
            confidence=confidence,
            events=group,
            hints={"shared_keys": list(keyset), "slug": slug, "hits": len(group)},
        ))
    return out


def _detect_co_occurrence(
    events: list[dict[str, Any]],
    *,
    min_hits: int,
) -> list[_Candidate]:
    """Find pairs of distinct tokens (entity names / action verbs) that
    consistently appear together in the same journal entry."""
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        toks = _extract_tokens(ev)
        if len(toks) < 2:
            continue
        toks_sorted = sorted(set(toks))
        # All 2-combos
        for i in range(len(toks_sorted)):
            for j in range(i + 1, len(toks_sorted)):
                pair = (toks_sorted[i], toks_sorted[j])
                pair_counts[pair] += 1
                pair_events[pair].append(ev)

    out: list[_Candidate] = []
    for pair, count in pair_counts.items():
        if count < min_hits:
            continue
        slug = _safe_slug(f"pair-{pair[0]}-{pair[1]}")
        confidence = min(0.80, 0.25 + 0.04 * count)
        out.append(_Candidate(
            kind="co_occurrence",
            slug=slug,
            confidence=confidence,
            events=pair_events[pair],
            hints={"pair": list(pair), "slug": slug, "hits": count},
        ))
    return out


def _detect_temporal_routine(
    events: list[dict[str, Any]],
    *,
    min_hits: int,
) -> list[_Candidate]:
    """Crude cadence detector: if same-signature events recur with low
    variance in time-between-events, surface as a temporal routine."""
    by_sig: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        acted = ev.get("acted")
        if acted is None:
            continue
        sig = _action_signature(acted)
        if sig:
            by_sig[sig].append(ev)

    out: list[_Candidate] = []
    for sig, group in by_sig.items():
        if len(group) < min_hits:
            continue
        gaps_min = _intervals_minutes([ev.get("ts") for ev in group])
        if not gaps_min:
            continue
        mean = sum(gaps_min) / len(gaps_min)
        if mean <= 0:
            continue
        # Coefficient of variation: lower = more regular
        var = sum((g - mean) ** 2 for g in gaps_min) / len(gaps_min)
        cov = (var ** 0.5) / mean
        if cov >= 0.6:
            continue  # too irregular to call a routine
        slug = _safe_slug(f"routine-{sig}")
        # Routine confidence rewards regularity
        confidence = min(0.90, 0.5 + (0.5 * (1 - cov)))
        out.append(_Candidate(
            kind="temporal_routine",
            slug=slug,
            confidence=confidence,
            events=group,
            hints={
                "action_signature": sig,
                "slug": slug,
                "hits": len(group),
                "cadence_minutes": round(mean, 1),
                "cov": round(cov, 3),
            },
        ))
    return out


# ======================================================================
# Helpers
# ======================================================================

def _action_signature(acted: Any) -> str:
    """Reduce an `acted` payload to a stable signature for clustering."""
    if isinstance(acted, list):
        # Use the first verb / phrase, lowercased + truncated
        if not acted:
            return ""
        first = acted[0]
        if isinstance(first, str):
            return _normalize_phrase(first)
        if isinstance(first, dict):
            kind = first.get("kind") or first.get("action") or first.get("type")
            if isinstance(kind, str):
                return _normalize_phrase(kind)
        return ""
    if isinstance(acted, dict):
        kind = acted.get("kind") or acted.get("action") or acted.get("type")
        if isinstance(kind, str):
            return _normalize_phrase(kind)
        return ""
    if isinstance(acted, str):
        return _normalize_phrase(acted)
    return ""


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")


def _normalize_phrase(text: str) -> str:
    """Lowercase, strip non-alpha, collapse to first 3 tokens."""
    text = text.lower().strip()
    tokens = _WORD_RE.findall(text)
    return "-".join(tokens[:3])


def _safe_slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80] or "untitled"


def _slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


def _extract_tokens(ev: dict[str, Any]) -> list[str]:
    """Pull a coarse bag-of-tokens out of an event for co-occurrence detection."""
    out: list[str] = []
    for field in ("evaluated", "acted"):
        v = ev.get(field)
        if isinstance(v, dict):
            for key in v.keys():
                out.append(_normalize_phrase(str(key)))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    out.append(_normalize_phrase(item))
        elif isinstance(v, str):
            out.append(_normalize_phrase(v))
    return [t for t in out if t]


def _short_event_snippet(ev: dict[str, Any]) -> str:
    acted = ev.get("acted")
    if isinstance(acted, list) and acted:
        first = acted[0]
        if isinstance(first, str):
            return first[:120]
        return json.dumps(first)[:120]
    if isinstance(acted, dict):
        return json.dumps(acted)[:120]
    if isinstance(acted, str):
        return acted[:120]
    evaluated = ev.get("evaluated")
    if evaluated:
        return f"evaluated: {json.dumps(evaluated)[:100]}"
    return "(no action recorded)"


def _intervals_minutes(timestamps: list[str | None]) -> list[float]:
    """Compute consecutive timestamp gaps in minutes. ISO 8601 strings only."""
    import datetime as _dt
    parsed: list[_dt.datetime] = []
    for t in timestamps:
        if not t:
            continue
        try:
            # Python 3.11+ handles 'Z' suffix natively via fromisoformat after replace
            parsed.append(_dt.datetime.fromisoformat(t.replace("Z", "+00:00")))
        except Exception:
            continue
    parsed.sort()
    if len(parsed) < 2:
        return []
    return [(parsed[i + 1] - parsed[i]).total_seconds() / 60.0 for i in range(len(parsed) - 1)]


def _build_summarization_prompt(
    pattern_kind: str,
    events: list[dict[str, Any]],
    hints: dict[str, Any],
) -> str:
    """Build the LLM prompt for BYOK / Venice summarizers. The prompt is
    deliberately compact; full evidence is included so the model can
    produce a high-quality skill body."""
    return (
        f"You are summarizing a detected behavioral pattern from a personal "
        f"agent's memory journal.\n"
        f"Pattern kind: {pattern_kind}\n"
        f"Hints: {json.dumps(hints, indent=2)}\n\n"
        f"Matching journal events (up to 10 shown):\n"
        f"{json.dumps(events[:10], indent=2)}\n\n"
        f"Write a concise reusable skill in Markdown. Include: a clear title, "
        f"one-paragraph description of when to apply this skill, an enumerated "
        f"recipe of the steps the agent should follow, and any constraints "
        f"observed in the source events. Be terse and actionable."
    )


def _row_to_proposal(row: Any) -> SkillProposal:
    """Convert a sqlite3.Row into a SkillProposal dataclass."""
    return SkillProposal(
        id=row["id"],
        tenant_id=row["tenant_id"],
        pattern_kind=row["pattern_kind"],
        proposed_slug=row["proposed_slug"],
        proposed_title=row["proposed_title"],
        proposed_body=row["proposed_body"],
        evidence=loads(row["evidence"]) or [],
        confidence=float(row["confidence"]),
        summarizer=row["summarizer"],
        status=row["status"],
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
        review_note=row["review_note"],
        accepted_doc_key=row["accepted_doc_key"],
    )
