"""MemoryClient: the public API for sibyl-memory-client.

Polymorphic constructor: open by local path OR by hosted-tier URL (v2+, not
implemented yet). The local-first plugin v1 only uses the local path.

The API surface mirrors the canonical sibyl_memory.* table shape so callers
can move between local-SQLite-backed and Postgres-backed clients without
re-learning the model.
"""
from __future__ import annotations

import functools
import sqlite3
from pathlib import Path
from typing import Any

from .exceptions import NotFoundError, StorageError, TenantError, ValidationError
from .storage import Storage, db_size_bytes, dumps, loads, new_id, _utc_now_iso


# ----------------------------------------------------------------------
# Shared limit clamp (CORE-5, 2026-06-25 pre-launch audit)
# ----------------------------------------------------------------------
# SQLite treats LIMIT -1 as UNBOUNDED, so a caller passing limit=-1 (or any
# negative) to a read path previously got an unbounded result set — a DoS /
# context-flood vector. A huge positive limit is the same class of problem
# (materialize the whole table). Every public read path clamps through this
# helper: negatives floor to 0 (no rows), and the ceiling bounds the worst
# case. MAX_LIMIT is generous (well above any legitimate page) so it never
# truncates real use; it only stops pathological values.
MAX_LIMIT = 10_000


def _clamp_limit(limit: Any) -> int:
    """Clamp a caller-supplied limit to [0, MAX_LIMIT].

    Coerces to int (a non-int limit is a caller bug, not a broaden vector):
    anything that won't coerce floors to 0 so it cannot fall through to
    SQLite's unbounded LIMIT -1.
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 0
    return min(max(0, n), MAX_LIMIT)


# ----------------------------------------------------------------------
# Identifier validation (v0.4.0, KAPPA YELLOW finding)
# ----------------------------------------------------------------------
# Entity names, state keys, and reference doc keys are user-supplied
# identifiers. SQL is parameterized everywhere so injection is closed today,
# but null bytes break downstream consumers (logs, exports, CLI display),
# empty strings are nonsense as primary keys, and unbounded length is a
# latent vector if any code path ever spills to filesystem. Validate on
# WRITE only: reads of already-stored bad identifiers still work so users
# can introspect and migrate.

_IDENT_MAX_LENGTH = 1024

# Control chars (0x00-0x1F + DEL) are rejected. Tab/newline/CR included by
# design: identifiers are short single-line strings, not arbitrary payloads.
_IDENT_FORBIDDEN_CODE_POINTS = frozenset(range(0, 0x20)) | {0x7F}

# v0.4.4 (KAPPA #3 defense-in-depth): SQL is parameterized so injection is
# closed at the DB, but identifiers flow into consumers that do NOT parameterize
# -- filesystem export (a `name` becomes a path component), CLI display, log
# lines, future per-entity backends. Reject path-traversal shapes and the
# shell/redirection/quote metacharacters that have no place in a short flat key.
# Apostrophe is deliberately ALLOWED (legit in name-shaped keys like "o'brien");
# double-quote is rejected because it is also the FTS5 phrase delimiter.
#
# NOTE: we reject the traversal MARKER ".." (catches KAPPA's "../../etc/passwd"
# and "..\\..\\windows") but NOT bare "/" or "\\" -- the v0.4.0 contract
# explicitly permits slash-containing keys ("with/slash"). Rejecting raw path
# separators for export-safety would be a public-contract change; flagged for
# the team rather than taken unilaterally.
_IDENT_FORBIDDEN_SUBSTRINGS = ("..",)
_IDENT_FORBIDDEN_CHARS = frozenset('<>|;"`')


def validate_identifier(value: Any, *, field_name: str) -> str:
    """Validate a user-supplied identifier (entity name, state key, etc.).

    Rejects: non-string, empty, control characters / null bytes, length > 1024.

    Args:
        value: the identifier to validate.
        field_name: name of the field for error messages.

    Returns: the validated string (unchanged on success).
    Raises: ValidationError on rejection, with a recovery hint.
    """
    if not isinstance(value, str):
        raise ValidationError(
            f"{field_name} must be a string (got {type(value).__name__})",
            recovery=f"Pass a non-empty string for {field_name}.",
        )
    if not value:
        raise ValidationError(
            f"{field_name} cannot be empty",
            recovery=f"Pass a non-empty string for {field_name}.",
        )
    if len(value) > _IDENT_MAX_LENGTH:
        raise ValidationError(
            f"{field_name} too long ({len(value)} chars, max {_IDENT_MAX_LENGTH})",
            recovery=f"Use a shorter {field_name} (under {_IDENT_MAX_LENGTH} chars).",
        )
    for idx, ch in enumerate(value):
        if ord(ch) in _IDENT_FORBIDDEN_CODE_POINTS:
            raise ValidationError(
                f"{field_name} contains a forbidden control character "
                f"(code point 0x{ord(ch):02x} at index {idx})",
                recovery=(
                    f"Identifiers must be printable single-line strings. "
                    f"Remove control characters / null bytes / tabs / newlines."
                ),
            )
    # v0.4.4: path-traversal + dangerous metacharacter defense-in-depth.
    for bad in _IDENT_FORBIDDEN_SUBSTRINGS:
        if bad in value:
            raise ValidationError(
                f"{field_name} contains a forbidden path sequence ({bad!r})",
                recovery=(
                    "Identifiers are flat keys, not paths. Remove '/', '\\', "
                    "and '..' sequences."
                ),
            )
    bad_chars = sorted(_IDENT_FORBIDDEN_CHARS & set(value))
    if bad_chars:
        raise ValidationError(
            f"{field_name} contains forbidden character(s): {' '.join(bad_chars)}",
            recovery=(
                "Remove shell / redirection / quote metacharacters "
                "( < > | ; \" ` ) from the identifier. Apostrophe is allowed."
            ),
        )
    return value


# ----------------------------------------------------------------------
# FTS5 error surface (v0.4.0, KAPPA YELLOW finding)
# ----------------------------------------------------------------------
# Previously search() and search_entities() silently swallowed
# sqlite3.OperationalError into `return []` / `pass`. KAPPA's complaint:
# "a user has no signal whether their query was malformed or just genuinely
# returned nothing." Now we classify: schema-missing → silent (defensive
# against partial init), FTS5-syntax-error → ValidationError (caller bug),
# anything else → StorageError (real backend issue).

# Substrings that mark FTS5 query syntax errors. Matched case-insensitively
# against str(OperationalError). Curated against the actual messages SQLite
# emits in 3.38+ for FTS5 parse failures.
_FTS5_QUERY_ERROR_MARKERS = (
    "fts5",
    "malformed match",
    "syntax error near",
    "no such column",
)

# Substring marking the schema-missing case: keep silent (return empty)
# for defense against partial schema state on very old DBs.
_SCHEMA_MISSING_MARKER = "no such table"


def _classify_fts5_error(err: sqlite3.OperationalError) -> Exception | None:
    """Translate an FTS5-related sqlite OperationalError.

    Returns:
        None  → schema-missing case; caller should treat as empty results.
        ValidationError  → user-visible query syntax problem; raise.
        StorageError  → real backend issue; raise.
    """
    msg = str(err).lower()
    if _SCHEMA_MISSING_MARKER in msg:
        return None  # defensive: schema partially applied, return empty
    if any(marker in msg for marker in _FTS5_QUERY_ERROR_MARKERS):
        return ValidationError(
            f"FTS5 rejected the search query: {err}",
            recovery=(
                "The query passed sanitization but the FTS5 engine still "
                "rejected it. Pass plain text or simple word tokens; FTS5 "
                "operator syntax (NEAR, AND/OR/NOT, column filters) is "
                "treated as literal text after sanitization."
            ),
        )
    return StorageError(
        f"SQLite error during FTS5 search: {err}",
        recovery=(
            "Backend error. Check disk space, file permissions, and that "
            "the schema is intact. See exception chain for the underlying "
            "sqlite3 message."
        ),
    )


# External-content FTS5 indexes can be rebuilt from their base table via the
# 'rebuild' command. journal_events_fts is contentless and cannot — corruption
# there is contained (tier skipped), not self-healed. Names are a fixed
# allowlist, never user input, so interpolation below is injection-safe.
_EXTERNAL_CONTENT_FTS = frozenset({
    "entities_fts", "state_documents_fts", "reference_documents_fts",
})


def _heal_fts(conn: sqlite3.Connection, fts_table: str) -> bool:
    """Rebuild a corrupted external-content FTS5 index from its base table.

    Returns True only if the rebuild ran without error. A poisoned/desynced
    external-content index (sqlite3.DatabaseError: "database disk image is
    malformed") is reconstructed from the intact base table; the base data is
    never touched. Contentless or unknown tables return False (uncontainable
    by rebuild).
    """
    if fts_table not in _EXTERNAL_CONTENT_FTS:
        return False
    try:
        conn.execute(f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')")
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def _fts_query(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple,
    fts_table: str,
) -> list:
    """Run one FTS5 MATCH query with classification + corruption containment.

    OperationalError → classified (schema-missing → []; query-syntax →
    ValidationError; other → StorageError), preserving the v0.4.0 KAPPA
    behavior. A broader DatabaseError (index corruption) is contained:
    self-heal the external-content index once and retry; if the retry still
    fails — or the table is contentless — return [] so a single poisoned row
    can never crash the caller's search.

    Corruption surfaces under varied messages depending on failure mode
    ("vtable constructor failed", "database disk image is malformed", "file
    is not a database"), so containment keys on the exception CLASS, not a
    message substring. ProgrammingError is re-raised: it signals a code or
    binding bug in our own SQL and must never be masked as empty results.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        exc = _classify_fts5_error(e)
        if exc is None:
            return []
        raise exc from e
    except sqlite3.ProgrammingError:
        raise
    except sqlite3.DatabaseError:
        if _heal_fts(conn, fts_table):
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                return []
        return []


# ----------------------------------------------------------------------
# FTS5 query sanitization
# ----------------------------------------------------------------------
# v0.3.3 hardens search() / search_entities() against FTS5 injection + DoS
# (audit SEC-3). User input is wrapped as a single quoted FTS5 phrase so
# column-filter syntax (`name:`, `category:`, `rowid:`, etc.) and unclosed
# quotes can't escape into the FTS5 parser. Caller can still get prefix
# matching by passing prefix=True.

# Column names + FTS5 reserved operators we reject if they appear unquoted.
_FTS5_COLUMN_TOKENS = frozenset({"name", "category", "body", "tenant_id",
                                  "entity_id", "document_key", "doc_key",
                                  "payload", "ts", "rowid"})


# v0.4.4 (chainriffs Discord report + KAPPA #4): bare uppercase FTS5 operator
# keywords typed inside a natural-language query ("auth AND db", "cache NEAR
# eviction") were being phrase-quoted into REQUIRED LITERAL tokens, so a matched
# row had to literally contain the word "AND" / "NEAR" -- recall silently
# collapsed to ~0 hits. Users mean these as connectors, not search terms. Drop
# them during tokenization so the remaining terms AND together (FTS5's implicit
# space-join), which is the natural intent. If a query is ONLY operator keywords,
# keep them as literals so a genuine search for the word "and" still resolves.
_FTS5_OPERATOR_KEYWORDS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _drop_fts5_operator_tokens(tokens: list[str]) -> list[str]:
    """Drop standalone FTS5 operator keywords; keep all tokens if that empties it."""
    kept = [t for t in tokens if t.upper() not in _FTS5_OPERATOR_KEYWORDS]
    return kept or tokens


def _sanitize_fts5_query(raw: str, *, prefix: bool = False, as_phrase: bool = False) -> str:
    """Wrap a user query as a safe FTS5 MATCH expression.

    Three modes:
      - Default (``prefix=False, as_phrase=False``): tokenize input into
        alphanumeric + underscore tokens, wrap each as a single-term
        phrase, and join with spaces. FTS5 treats space-joined terms as
        implicit AND so every token must appear in the matched row
        (in any order). This is the natural-language behaviour most
        callers want: ``search("H&M tops bought")`` now matches rows
        containing "H", "M", "tops", and "bought" anywhere. Each token
        is phrase-quoted so embedded FTS5 operators stay literal.
      - Explicit phrase (``as_phrase=True``): wrap the entire input as a
        single double-quoted phrase. Use when consecutive-token phrase
        match is what the caller actually wants. Embedded double-quotes
        are doubled per FTS5 escape rules. Safe against injection.
      - Prefix (``prefix=True``, mutually exclusive with as_phrase;
        prefix wins): strip to alphanumeric tokens, append ``*`` to the
        last token for prefix matching.

    Empty / whitespace-only queries return an empty string; callers
    should short-circuit on empty.

    Behaviour change in v0.4.2 (2026-05-22): default mode flipped from
    phrase-match to AND-of-tokens. Phrase-match was an unintuitive
    default because it made natural-language queries fail silently -
    ``search("H&M tops bought")`` returned 0 hits even when the haystack
    contained all three words. Callers who relied on phrase semantics
    must now pass ``as_phrase=True`` explicitly. Surfaced by the
    LongMemEval 50-Q benchmark on 2026-05-22 as the dominant default-UX
    gap for Hermes-plugin users (every natural-language query hit 0).
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    # Strip control characters that could confuse the FTS5 tokenizer
    s = "".join(ch for ch in s if ch.isprintable() or ch in (" ", "\t"))
    if not s.strip():
        return ""

    if prefix:
        # Reduce to safe bare tokens: alphanumeric + underscore only.
        # Anything else (quotes, colons, hyphens, FTS5 operators) becomes
        # a space, then we split-and-rejoin to get clean whitespace.
        cleaned = "".join(ch if (ch.isalnum() or ch == "_") else " " for ch in s)
        tokens = [t for t in cleaned.split() if t]
        if not tokens:
            return ""
        # In prefix mode, never use the keep-all fallback: appending `*` to a
        # raw FTS5 operator keyword (OR*, AND*, NOT*) produces an invalid query
        # that crashes the FTS5 parser (acerieus stress test
        # LEARNING-SEARCH-PREFIX-OPERATOR-MUTATIONS-STAY-LITERAL, 2026-06-01).
        # Hard-drop operators with no fallback; an all-operator prefix query
        # has no safe FTS5 expansion so we return empty (no match).
        tokens = [t for t in tokens if t.upper() not in _FTS5_OPERATOR_KEYWORDS]
        if not tokens:
            return ""
        if len(tokens) == 1:
            return f"{tokens[0]}*"
        # Multiple tokens: all earlier tokens are literal, the last gets `*`.
        return " ".join(tokens[:-1]) + f" {tokens[-1]}*"

    if as_phrase:
        # Explicit phrase mode (legacy default before v0.4.2). Escape
        # embedded double-quotes per FTS5 rules.
        escaped = s.replace('"', '""')
        return f'"{escaped}"'

    # NEW default (v0.4.2+): tokenize into alphanumeric + underscore
    # tokens, wrap each as a single-term phrase, join with spaces. FTS5
    # treats space-joined terms as implicit AND.
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else " " for ch in s)
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        # All-symbol input: fall back to the legacy phrase wrap so the
        # query still has SOME defensible shape rather than empty.
        escaped = s.replace('"', '""')
        return f'"{escaped}"'
    tokens = _drop_fts5_operator_tokens(tokens)
    return " ".join(f'"{t}"' for t in tokens)


# ---------------------------------------------------------------------------
# Proximity re-ranking (v0.4.10): precision boost for multi-word search.
#
# The default sanitizer (v0.4.2+) ANDs query tokens, so every token must appear
# somewhere in a matched row, in any order. That gives full recall but lets
# "near-negative decoy" rows (short docs that contain the same tokens in an
# unrelated context) out-rank the real answer under BM25, which rewards term
# density over proximity (chainriffs + KAPPA Discord reports against v0.4.2 and
# v0.4.4: precision ~73% at recall 100%).
#
# Fix: after BM25 ranking, bucket each hit by how tightly it matches the query,
# then sort by (bucket, bm25_rank). Recall is untouched: no hit is dropped, the
# candidate set is identical, only the order changes before the limit applies.
# Single-token queries are a no-op (every hit is bucket 0), so the single-token
# searches issued by multi_record_search (the anchor-first resolver) are
# unaffected. Prefix searches are also skipped (different intent).
#
#   bucket 0: query tokens appear as a contiguous phrase, in order
#   bucket 1: all query tokens appear within a small window, any order
#   bucket 2: tokens are scattered, or cannot be located in the extracted text
_PROXIMITY_WINDOW_SLACK = 4


def _match_tokens(query: str) -> list[str]:
    """Lowercased alphanumeric+underscore tokens, FTS5 operator words dropped.

    Mirrors the tokenization the default sanitizer ANDs together, so the
    re-ranker reasons over the same tokens the MATCH actually required.
    """
    if not query or not isinstance(query, str):
        return []
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else " " for ch in query.lower())
    toks = [t for t in cleaned.split() if t]
    return _drop_fts5_operator_tokens(toks) if toks else []


def _normalize_text(value: Any) -> str:
    """Flatten a hit's searchable content to a single space-joined token string.

    Serializes structured bodies via JSON so the re-ranker sees the same text
    (keys + values) that FTS5 indexed for the row.
    """
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = dumps(value)
        except (TypeError, ValueError):
            raw = str(value)
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else " " for ch in raw.lower())
    return " ".join(cleaned.split())


def _min_cover_span(positions: dict[str, list[int]]) -> int | None:
    """Smallest window (max-min+1) of doc indices covering every token once."""
    merged = sorted((i, t) for t, idxs in positions.items() for i in idxs)
    if not merged:
        return None
    need = len(positions)
    have: dict[str, int] = {}
    best: int | None = None
    left = 0
    for right in range(len(merged)):
        have[merged[right][1]] = have.get(merged[right][1], 0) + 1
        while len(have) == need:
            width = merged[right][0] - merged[left][0] + 1
            if best is None or width < best:
                best = width
            tl = merged[left][1]
            have[tl] -= 1
            if have[tl] == 0:
                del have[tl]
            left += 1
    return best


def _proximity_bucket(query_tokens: list[str], text: str) -> int:
    """0 = contiguous phrase, 1 = tight window, 2 = scattered/absent."""
    n = len(query_tokens)
    if n < 2:
        return 0
    if f" {' '.join(query_tokens)} " in f" {text} ":
        return 0  # exact contiguous phrase, in query order
    doc_tokens = text.split()
    if not doc_tokens:
        return 2
    positions: dict[str, list[int]] = {t: [] for t in set(query_tokens)}
    for i, tok in enumerate(doc_tokens):
        if tok in positions:
            positions[tok].append(i)
    if any(not idxs for idxs in positions.values()):
        return 2  # at least one query token absent from the extracted text
    span = _min_cover_span(positions)
    if span is not None and span <= n + _PROXIMITY_WINDOW_SLACK:
        return 1
    return 2


# The default tenant for single-user local installs.
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


# Paraphrase zero-hit fallback (beta deadguy 2026-06-14): natural-language
# queries miss under strict token-AND. The fallback (in MemoryClient.search) only
# fires when the strict search returns NOTHING, so it is purely additive — it can
# never reorder or drop an existing non-empty result, and single-token / prefix
# queries (multi_record's path) are untouched.
_SEARCH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "do", "did", "does", "have", "has", "had", "i", "you",
    "he", "she", "it", "we", "they", "my", "your", "our", "their", "what",
    "which", "who", "whom", "whose", "when", "where", "why", "how", "to", "of",
    "in", "on", "at", "for", "with", "from", "by", "as", "this", "that",
    "these", "those", "not", "no", "can", "will", "would", "should", "could",
    "may", "might", "just", "also", "all", "any", "some", "more", "most",
    "into", "about", "over", "than", "then", "there", "here",
    # short function words + contraction tails (operator 2026-06-28): kept out of
    # the single-token fallback now that the len>=2 floor (CORE-11) lets short
    # tokens through, so "us"/"re"/"ll" can't trigger a junk last-resort search.
    "us", "me", "am", "re", "ll", "ve",
})


def _relaxed_query_strings(query: str):
    """Yield progressively relaxed query strings for the zero-hit search fallback.

    Each candidate is fed back through the normal search path (so it is
    re-sanitized by _sanitize_fts5_query — no raw FTS5 construction, no injection
    surface). Order: stopword-stripped (recovers most paraphrase misses), then
    the rarest single token (last-resort recall).
    """
    toks = _match_tokens(query)
    if len(toks) < 2:
        return  # single-token queries have nothing to relax
    content = [t for t in toks if t.lower() not in _SEARCH_STOPWORDS]
    seen: set[str] = set()
    # 1) stopwords stripped, still AND (recovers most paraphrase misses)
    if content and len(content) < len(toks):
        cand = " ".join(content)
        seen.add(cand)
        yield cand
    # 2) each content token alone, longest-first (length = cheap rarity proxy).
    #    The wrapper stops at the first variant that returns hits, so the most
    #    specific term is tried before more common ones. Last-resort recall.
    for tok in sorted(set(content or toks), key=len, reverse=True):
        # CORE-11 (2026-06-25 pre-launch audit): the old len>=3 floor silently
        # dropped short alphanumeric identifiers (q3, v2, k8, s3) — exactly the
        # discriminating tokens a developer searches for. Recover any token of
        # length >= 2 OR any token containing a digit (so single-letter+digit
        # identifiers like "k8" still retry). One-char pure-alpha tokens stay
        # excluded (too common to be useful as a last-resort recall term).
        if tok in seen:
            continue
        if len(tok) >= 2 or any(ch.isdigit() for ch in tok):
            seen.add(tok)
            yield tok


# F5 (red-team 2026-06-17): sanity ceiling on a single serialized body. The 2 MB
# free cap bounds TOTAL memory, but one oversized value still floods agent context
# on recall/search. This high ceiling rejects only pathological single values;
# per-hit search output is additionally truncated at the tool boundary (adapter).
_MAX_BODY_BYTES = 1024 * 1024  # 1 MiB per single memory value


def _check_json(payload: Any, field: str = "body") -> str:
    """Validate that payload is JSON-serializable, return the encoded string."""
    try:
        encoded = dumps(payload)
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"{field} is not JSON-serializable: {e}",
            recovery=f"Pass a dict, list, or JSON primitive as {field}.",
        ) from e
    size = len(encoded.encode("utf-8"))
    if size > _MAX_BODY_BYTES:
        raise ValidationError(
            f"{field} is too large ({size // 1024} KB; max "
            f"{_MAX_BODY_BYTES // 1024} KB per value).",
            recovery=(
                "Split the content across multiple smaller memories, or store a "
                "summary plus a reference to external storage."
            ),
        )
    return encoded


def _require_container(body: Any, field: str = "body") -> None:
    """Enforce the structured-body contract for entity + state writes.

    set_entity/set_state declare ``body: dict | list``. A bare primitive
    (str/int/float/bool/None) is valid JSON, so without this guard it would
    persist silently and break downstream tools that assume a structured
    container. reference_documents intentionally takes a free-text str body
    and does NOT go through here.
    """
    if not isinstance(body, (dict, list)):
        raise ValidationError(
            f"{field} must be a dict or list, got {type(body).__name__}",
            recovery=(
                f"Wrap the value in a container, e.g. {{'value': ...}} or "
                f"[...]. Primitive {field} values are rejected because "
                "downstream consumers assume structured entity/state bodies."
            ),
        )


def _track_op(kind: str):
    """Decorator: count one memory op on the client's usage heartbeat before the
    wrapped method runs. Fire-and-forget; never raises into the operation."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            hb = getattr(self, "_heartbeat", None)
            if hb is not None:
                try:
                    hb.record(kind)
                except Exception:
                    pass
            return fn(self, *args, **kwargs)
        return wrapper
    return deco


class MemoryClient:
    """Single canonical interface for reading and writing Sibyl Memory state."""

    # Paid-tier-only features. Free tier raises TierGateError; upgrading to any
    # paid tier unlocks both self-learning and the memory linter.
    _PAID_ONLY_TIERS = frozenset({"sync", "team", "lifetime", "stake", "enterprise"})

    def __init__(
        self,
        storage: Storage,
        *,
        tenant_id: str = DEFAULT_TENANT,
        tier: str = "free",
        account_id: str | None = None,
        session_token: str | None = None,
        cap_gate: Any = None,
        credentials_claim: dict[str, Any] | None = None,
        credentials_signature: str | None = None,
    ) -> None:
        self._storage = storage
        # CORE-8: validate the tenant_id at construction too (set_tenant guards
        # the runtime switch; this guards the initial value). Re-raise as
        # TenantError for a consistent failure mode.
        try:
            validate_identifier(tenant_id, field_name="tenant_id")
        except ValidationError as e:
            raise TenantError(str(e), recovery=e.recovery) from e
        self._tenant_id = tenant_id
        self._tier = tier
        self._account_id = account_id
        self._session_token = session_token

        # Cap gate: enforces the 2 MB free-tier cap with server-authoritative
        # tier verification at the boundary. See _capcheck.py for the design.
        if cap_gate is None:
            from ._capcheck import CapGate, TierCache, aggregate_db_size
            cap_gate = CapGate(
                account_id=account_id,
                session_token=session_token,
                # ACCOUNT-level cap (0.4.18): the FREE-tier cap is per account,
                # not per DB file, so the gate sizes EVERY memory store this
                # machine resolves (SDK default ~/.sibyl-memory, Hermes adapter
                # + per-profile stores, SIBYL_MEMORY_DB override, and the active
                # db_path), deduped by resolved path. Each store is still sized
                # WAL-inclusively via db_size_bytes (CAP-1: sizing memory.db
                # alone under-counts writes still sitting in memory.db-wal
                # during a burst), so this composes with CAP-1 rather than
                # regressing it.
                db_size_fn=lambda: aggregate_db_size(storage.db_path),
                local_tier_hint=tier,
                cache=TierCache(
                    Path(storage.db_path).parent / "tier_cache.json"
                ),
                credentials_claim=credentials_claim,
                credentials_signature=credentials_signature,
            )
        self._cap_gate = cap_gate

        # Usage heartbeat: local-first memory ops never hit the network, so the
        # server has no usage signal. This reports ONLY aggregate op COUNTS
        # (no content, no PII beyond account_id), debounced + fire-and-forget,
        # so an active account's request count finally reflects real use.
        # No-op without an account_id; opt out with SIBYL_MEMORY_TELEMETRY=0.
        try:
            from ._heartbeat import HeartbeatReporter
            self._heartbeat = HeartbeatReporter(account_id, session_token)
        except Exception:
            from ._heartbeat import _NullHeartbeat
            self._heartbeat = _NullHeartbeat()

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def local(
        cls,
        path: str | Path = "~/.sibyl-memory/memory.db",
        *,
        tenant_id: str = DEFAULT_TENANT,
        tier: str = "free",
        account_id: str | None = None,
        session_token: str | None = None,
        credentials_claim: dict[str, Any] | None = None,
        credentials_signature: str | None = None,
    ) -> "MemoryClient":
        """Open a local SQLite-backed MemoryClient.

        The directory at ``path``'s parent is created with mode 0700 if
        missing. The schema is applied on first open and is idempotent.

        Set ``tier`` to the user's plugin tier so paid-only features
        (self-learning + memory linter) gate correctly. Defaults to "free".

        Pass ``account_id`` and ``session_token`` from credentials.json so
        the SDK can verify the user's tier against the server when they
        approach the 2 MB free-tier cap. Without these, the SDK enforces
        a strict local 2 MB cap (no server check possible).
        """
        storage = Storage(path)
        return cls(
            storage,
            tenant_id=tenant_id,
            tier=tier,
            account_id=account_id,
            session_token=session_token,
            credentials_claim=credentials_claim,
            credentials_signature=credentials_signature,
        )

    # ------------------------------------------------------------------
    # Tenant management
    # ------------------------------------------------------------------
    def get_tenant(self) -> str:
        return self._tenant_id

    def set_tenant(self, tenant_id: str) -> None:
        """Switch the active tenant.

        CORE-8 (2026-06-25 pre-launch audit): tenant_id is now validated the
        same way every other user-supplied identifier is (non-empty string, no
        control characters / null bytes, no path-traversal or shell
        metacharacters, length <= 1024). An unvalidated tenant_id silently
        created a separate, unreachable data partition (a control char or empty
        string is a different key than the user thinks they typed), which both
        loses data and weakens isolation. validate_identifier raises
        ValidationError; we re-raise as TenantError to keep the documented
        set_tenant failure mode.
        """
        try:
            validate_identifier(tenant_id, field_name="tenant_id")
        except ValidationError as e:
            raise TenantError(str(e), recovery=e.recovery) from e
        self._tenant_id = tenant_id

    @property
    def storage(self) -> Storage:
        return self._storage

    def schema_version(self) -> int | None:
        return self._storage.schema_version()

    # ------------------------------------------------------------------
    # Tier (paid-tier-only feature gating)
    # ------------------------------------------------------------------
    def get_tier(self) -> str:
        return self._tier

    def set_tier(self, tier: str) -> None:
        """Update the user's tier. Called by the credentials loader when
        the activation flow returns a tier upgrade."""
        if not isinstance(tier, str) or not tier:
            raise ValidationError("tier must be a non-empty string")
        self._tier = tier

    def _effective_tier(self) -> str:
        """Best-available trustworthy tier for feature gating.

        CORE-10 (2026-06-25 pre-launch audit, SAFE-MINIMAL — see FLAG below):
        the paid-feature gates trusted the raw client-supplied ``self._tier``,
        so editing credentials.json to ``tier:"lifetime"`` unlocked the learner
        and linter for free. A full server round-trip on every learn()/lint()
        would be the authoritative fix but risks latency + offline regressions,
        so we use the cheapest server-authoritative signal we already hold: the
        CapGate's tier cache, which is populated by a server-verified
        /check-write boundary call.

        When the cache carries a FRESH, account-matched entry, its tier is the
        server's word and overrides the client hint — a tampered free->paid
        credentials edit is caught the moment a real cache exists. When there is
        no usable cache (user never approached the cap, or offline pre-cache),
        we fall back to the client hint (unchanged behavior, no new failure
        mode). This narrows the abuse window without a network dependency.
        """
        gate = getattr(self, "_cap_gate", None)
        cache = getattr(gate, "_cache", None)
        account_id = getattr(gate, "account_id", None)
        if cache is not None:
            try:
                cached = cache.load()
            except Exception:
                cached = None
            if (cached is not None and cached.is_fresh
                    and cached.account_id == account_id
                    and account_id is not None):
                return cached.tier
        return self._tier

    def _require_paid_tier(self, feature: str) -> None:
        """Raise TierGateError if the current tier is not paid-tier.

        CORE-10: gates on the server-authoritative tier when a fresh cap-gate
        cache is available, else on the client hint (see _effective_tier).
        """
        from .exceptions import TierGateError
        effective = self._effective_tier()
        if effective not in self._PAID_ONLY_TIERS:
            raise TierGateError(
                f"{feature} requires a paid tier. Current tier: {effective!r}.",
                feature=feature,
                current_tier=effective,
            )

    # ------------------------------------------------------------------
    # Entities (WARM tier): single source of truth per rule 43
    # ------------------------------------------------------------------
    @_track_op("set_entity")
    def set_entity(
        self,
        category: str,
        name: str,
        body: dict[str, Any] | list[Any],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update an entity.

        UNIQUE (tenant_id, category, name) is enforced at the DB level. On
        conflict the existing row is updated (body + status + updated_at).
        Returns the resulting entity row as a dict.

        Subject to the 2 MB free-tier cap when tier='free'. Raises
        CapExceededError if the write would push the local DB past the cap
        and the server-authoritative tier check confirms the account is
        still free.

        v0.4.0: category and name are validated as identifiers (non-empty
        string, no control characters, length <= 1024). Raises
        ValidationError on rejection."""
        validate_identifier(category, field_name="category")
        validate_identifier(name, field_name="name")
        _require_container(body)
        body_json = _check_json(body)
        # Cap gate: rough byte estimate (FTS5 + indexes add overhead)
        self._cap_gate.check(proposed_delta_bytes=len(body_json) + len(name) + len(category) + 200)
        with self._storage.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            ).fetchone()
            if existing is None:
                ent_id = new_id()
                conn.execute(
                    "INSERT INTO entities (id, tenant_id, category, name, status, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ent_id, self._tenant_id, category, name, status, body_json),
                )
            else:
                ent_id = existing["id"]
                conn.execute(
                    "UPDATE entities SET status = ?, body = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ?",
                    (status, body_json, ent_id),
                )
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)
        return self.get_entity(category, name)

    @_track_op("recall")
    def get_entity(self, category: str, name: str) -> dict[str, Any]:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT id, tenant_id, category, name, status, body, created_at, updated_at "
                "FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"entity {category}/{name} not found for tenant {self._tenant_id}")
        return self._row_to_entity(row)

    @_track_op("list_entities")
    def list_entities(
        self,
        category: str | None = None,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit)  # CORE-5: negative/huge limit must not broaden
        sql = "SELECT id, tenant_id, category, name, status, body, created_at, updated_at FROM entities WHERE tenant_id = ?"
        params: list[Any] = [self._tenant_id]
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._storage.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def delete_entity(self, category: str, name: str) -> bool:
        with self._storage.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # State documents (HOT tier)
    # ------------------------------------------------------------------
    @_track_op("set_state")
    def set_state(self, key: str, body: dict[str, Any] | list[Any]) -> None:
        """Insert or update a HOT-tier state document.

        v0.4.0: ``key`` is validated as an identifier (non-empty string, no
        control characters, length <= 1024). Raises ValidationError on
        rejection."""
        validate_identifier(key, field_name="key")
        _require_container(body)
        body_json = _check_json(body)
        self._cap_gate.check(proposed_delta_bytes=len(body_json) + len(key) + 150)
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO state_documents (tenant_id, document_key, body) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, document_key) DO UPDATE SET body = excluded.body, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (self._tenant_id, key, body_json),
            )
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT body, updated_at FROM state_documents WHERE tenant_id = ? AND document_key = ?",
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return {"body": loads(row["body"]), "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------
    # Journal (COLD tier): append-only event log
    # ------------------------------------------------------------------
    def write_event(
        self,
        *,
        evaluated: Any = None,
        acted: Any = None,
        forward: Any = None,
        extra: Any = None,
        ts: str | None = None,
    ) -> str:
        # Estimate byte cost from each non-None payload
        delta = 200  # row + index overhead
        for payload in (evaluated, acted, forward, extra):
            if payload is not None:
                try:
                    delta += len(dumps(payload))
                except (TypeError, ValueError):
                    delta += 100  # estimate; the JSON check below will catch real failures
        self._cap_gate.check(proposed_delta_bytes=delta)
        ev_id = new_id()
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO journal_events (id, tenant_id, ts, evaluated, acted, forward, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ev_id,
                    self._tenant_id,
                    ts or _utc_now_iso(),
                    _check_json(evaluated, "evaluated") if evaluated is not None else None,
                    _check_json(acted, "acted") if acted is not None else None,
                    _check_json(forward, "forward") if forward is not None else None,
                    _check_json(extra, "extra") if extra is not None else None,
                ),
            )
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)
        return ev_id

    def read_events(
        self,
        *,
        limit: int = 50,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = _clamp_limit(limit)  # CORE-5: negative limit = SQLite unbounded; clamp
        sql = "SELECT id, tenant_id, ts, evaluated, acted, forward, extra FROM journal_events WHERE tenant_id = ?"
        params: list[Any] = [self._tenant_id]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._storage.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
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

    # ------------------------------------------------------------------
    # Reference (REFERENCE tier): static lookup documents
    # ------------------------------------------------------------------
    @_track_op("set_reference")
    def set_reference(
        self,
        key: str,
        body: str | dict[str, Any] | list[Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or update a REFERENCE-tier document.

        v0.4.0: ``key`` is validated as an identifier (non-empty string, no
        control characters, length <= 1024). Raises ValidationError on
        rejection.

        v0.4.12 (beta report VRTX ISSUE-003): ``body`` accepts ``str`` or a
        JSON-serializable ``dict``/``list``. A mapping/sequence is coerced to a
        canonical JSON string (sorted keys) and stored as the reference body,
        so ``set_reference(key, {...})`` no longer raises StorageError on the
        INSERT. Any other type raises a clear ``ValidationError`` naming the
        ``body`` parameter instead of failing opaquely inside SQLite."""
        validate_identifier(key, field_name="key")
        if isinstance(body, (dict, list)):
            # Reuse the JSON guard used for metadata: rejects non-serializable
            # payloads with a typed error before it can reach the DB layer.
            body = _check_json(body, "body")
        elif not isinstance(body, str):
            raise ValidationError(
                f"body must be a str or a JSON-serializable dict/list, got {type(body).__name__}"
            )
        meta_json = _check_json(metadata, "metadata") if metadata is not None else None
        delta = len(body) + len(key) + (len(meta_json) if meta_json else 0) + 200
        self._cap_gate.check(proposed_delta_bytes=delta)
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO reference_documents (tenant_id, doc_key, body, metadata) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, doc_key) DO UPDATE SET body = excluded.body, "
                "metadata = excluded.metadata, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (self._tenant_id, key, body, meta_json),
            )
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)

    def get_reference(self, key: str) -> dict[str, Any] | None:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT body, metadata, updated_at FROM reference_documents WHERE tenant_id = ? AND doc_key = ?",
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return {"body": row["body"], "metadata": loads(row["metadata"]), "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------
    def archive_entity(self, category: str, name: str, reason: str | None = None) -> dict[str, Any]:
        """Move an entity to the archive table and delete from the active set.

        T1-3 fix: previously this bypassed the cap-gate. A free user at
        1.9 MB could archive their largest entities (body copied into
        archived_entities, doubling footprint temporarily before the
        DELETE lands) to keep writing past the 2 MB cap. Now gated on
        the size of the body being copied + 200 bytes overhead. Reads
        the body first so we know the actual delta. NotFoundError still
        raised before any cap-gate work.
        """
        # CORE-9 (2026-06-25 pre-launch audit): read the row, size the insert,
        # run the cap-check, and perform the archive write all inside ONE
        # BEGIN IMMEDIATE transaction. The previous two-phase shape (read +
        # cap-check in a plain connection, then a separate transaction for the
        # write) had a TOCTOU window: a concurrent writer could grow the DB
        # between the cap-check and the archive insert, so a free user near the
        # cap could still push past it. BEGIN IMMEDIATE takes the write lock up
        # front, closing the window. NotFoundError still propagates before any
        # write so a missing entity has no side effect.
        with self._storage.transaction() as conn:
            row = conn.execute(
                "SELECT id, body FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"entity {category}/{name} not found")
            body_bytes = len(row["body"] or "") if row["body"] else 0
            # The archive insert copies the body. Delta = body + name + category
            # + reason + ~200B SQLite/row overhead. Conservative estimate.
            delta = body_bytes + len(name) + len(category) + len(reason or "") + 200
            # Cap-check holds the write lock (BEGIN IMMEDIATE), so the size it
            # reads cannot be perturbed by another writer before our INSERT.
            self._cap_gate.check(proposed_delta_bytes=delta)
            arch_id = new_id()
            conn.execute(
                "INSERT INTO archived_entities (id, tenant_id, original_entity_id, category, name, body, archive_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (arch_id, self._tenant_id, row["id"], category, name, row["body"], reason),
            )
            conn.execute("DELETE FROM entities WHERE id = ?", (row["id"],))
        return {"archived_id": arch_id, "original_id": row["id"]}

    # ------------------------------------------------------------------
    # Self-learning + lint (v0.2.0): paid-tier only
    # ------------------------------------------------------------------
    # Both convenience entrypoints below gate on tier and raise
    # TierGateError for free-tier callers. The underlying Learner / Linter
    # classes remain available for power users via direct import, but
    # the documented surface is the gated convenience API.

    def learner(self, **kwargs: Any):
        """Return a Learner bound to this client's storage + tenant.

        Paid-tier only. Lazy import so the lower SDK stays usable without
        loading the learning module. Threads the client's CapGate into
        the Learner so accept_proposal calls go through the cap-check
        (T1-3 fix). Callers can override cap_gate=None explicitly to
        opt out for tests."""
        self._require_paid_tier("self-learning")
        from .learning import Learner
        kwargs.setdefault("cap_gate", self._cap_gate)
        return Learner(self._storage, tenant_id=self._tenant_id, **kwargs)

    def learn(self, **kwargs: Any):
        """Convenience: construct a default Learner and run one pass.
        Returns a LearningRunReport. Paid-tier only."""
        return self.learner(**kwargs).run()

    def list_skill_proposals(
        self, *, status: str = "pending", limit: int = 50,
    ) -> list[Any]:
        """Paid-tier only."""
        return self.learner().list_proposals(status=status, limit=limit)

    def accept_skill_proposal(
        self, proposal_id: str, *, note: str | None = None,
    ) -> dict[str, Any]:
        """Paid-tier only."""
        return self.learner().accept_proposal(proposal_id, note=note)

    def reject_skill_proposal(
        self, proposal_id: str, *, note: str | None = None,
    ) -> dict[str, Any]:
        """Paid-tier only."""
        return self.learner().reject_proposal(proposal_id, note=note)

    def lint(self, **kwargs: Any):
        """Run the local memory linter against this tenant. Returns a
        LintReport with `.findings`, `.counts`, `.ok`, and `.to_ascii()`.

        Paid-tier only. Free-tier callers raise TierGateError pointing at
        the upgrade page.
        """
        self._require_paid_tier("memory linter")
        from .lint import Linter
        # If the caller didn't supply soft_cap_bytes, look up by tier
        if "soft_cap_bytes" not in kwargs:
            from .lint import TIER_SOFT_CAPS, DEFAULT_SOFT_CAP_BYTES
            cap = TIER_SOFT_CAPS.get(self._tier, DEFAULT_SOFT_CAP_BYTES)
            # Paid tiers map to None: pass a huge cap so the check effectively never fires
            kwargs["soft_cap_bytes"] = cap if cap is not None else (1 << 62)
        return Linter(self._storage, tenant_id=self._tenant_id, **kwargs).run()

    # ------------------------------------------------------------------
    # Free-tier read access (no gating): visibility into the upgrade pressure
    # ------------------------------------------------------------------
    def free_tier_status(self) -> dict[str, Any]:
        """Return current free-tier state: DB size, soft cap, % used.

        Always available regardless of tier: free-tier callers use this
        to render the "you're at X% of your free cap" upgrade prompt
        without needing to call the (gated) linter.
        """
        from .lint import TIER_SOFT_CAPS, DEFAULT_SOFT_CAP_BYTES
        # CAP-1: WAL-inclusive sizing so the "% of cap" prompt matches what the
        # cap gate actually enforces (the main file alone under-reports during
        # write bursts).
        db_size = db_size_bytes(self._storage.db_path)
        cap = TIER_SOFT_CAPS.get(self._tier, DEFAULT_SOFT_CAP_BYTES)
        # Paid tier → no cap
        if cap is None:
            return {
                "tier": self._tier,
                "db_size_bytes": db_size,
                "soft_cap_bytes": None,
                "pct_used": None,
                "uncapped": True,
            }
        return {
            "tier": self._tier,
            "db_size_bytes": db_size,
            "soft_cap_bytes": cap,
            "pct_used": db_size / cap if cap else None,
            "uncapped": False,
            "at_or_above_warning": db_size >= 0.8 * cap,
            "at_or_above_cap": db_size >= cap,
            "upgrade_url": "https://sibyllabs.org/plugin#tier",
        }

    # ------------------------------------------------------------------
    # FTS5 search
    # ------------------------------------------------------------------
    @_track_op("search_entities")
    def search_entities(self, query: str, *, limit: int = 20, prefix: bool = False,
                        category: str | None = None) -> list[dict[str, Any]]:
        """Full-text search over entity name + category + body via FTS5.

        Returns warm-tier entity rows only. For cross-tier search (entities +
        state + reference + journal in one call), use ``search()``.

        Query is sanitized as a single FTS5 phrase: column-filter syntax
        (``name:foo``) and unclosed quotes can't escape into the parser.
        Set ``prefix=True`` for prefix matching on the final token.

        Pass ``category="<name>"`` to anchor the search to a single entity
        category (exact match); this removes topical bleed across categories on
        multi-entity workloads (tester email 19e7e75af0b7780a). Omit to search
        all categories.

        Returns: list of entity rows. Each row is a dict with keys
            id, tenant_id, category, name, status, body, created_at, updated_at
            (body is JSON-deserialized).

        Raises: StorageError on backend failure; empty list on empty / invalid query.
        """
        limit = _clamp_limit(limit)  # CORE-5: negative=unbounded; huge=full-scan. Clamp.
        match_q = _sanitize_fts5_query(query, prefix=prefix)
        if not match_q:
            return []
        # external-content FTS5: join by rowid back to base table.
        # _fts_query handles classification (v0.4.0 KAPPA) + corruption
        # containment (poisoned-index DatabaseError self-heals or returns []).
        #
        # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
        # `AND f.tenant_id = ?` is the ONLY thing keeping this query inside the
        # caller's tenant. tenant_id is UNINDEXED in the FTS5 table (see
        # schema.sql), so this is a trailing post-filter, NOT index-enforced
        # isolation. DO NOT remove, reorder, or make this clause conditional.
        # Index-level enforcement needs an FTS schema migration that would break
        # existing DBs — FLAGGED for lead review; guarded for now by this lock +
        # the zero-cross-tenant-leak regression test (test_core3_tenant_isolation).
        cat_clause = " AND e.category = ?" if category else ""
        params = ((match_q, self._tenant_id, category, limit) if category
                  else (match_q, self._tenant_id, limit))
        with self._storage.connection() as conn:
            rows = _fts_query(
                conn,
                "SELECT e.id, e.tenant_id, e.category, e.name, e.status, e.body, e.created_at, e.updated_at "
                "FROM entities_fts f "
                "JOIN entities e ON e.rowid = f.rowid "
                "WHERE entities_fts MATCH ? AND f.tenant_id = ?" + cat_clause + " "
                "ORDER BY rank LIMIT ?",
                params,
                "entities_fts",
            )
        ents = [self._row_to_entity(r) for r in rows]
        # v0.4.10: proximity re-rank (see search()). Multi-word, non-prefix only;
        # re-orders the fetched rows, never drops one.
        query_tokens = _match_tokens(query)
        if not prefix and len(query_tokens) >= 2 and len(ents) > 1:
            keyed = []
            for idx, e in enumerate(ents):
                text = " ".join((
                    _normalize_text(e.get("body")),
                    _normalize_text(e.get("name", "")),
                    _normalize_text(e.get("category") or ""),
                ))
                keyed.append((_proximity_bucket(query_tokens, text), idx, e))
            keyed.sort(key=lambda t: (t[0], t[1]))
            ents = [t[2] for t in keyed]
        return ents

    @_track_op("search")
    def search(self, query: str, *, limit: int = 20, prefix: bool = False,
               tiers: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Cross-tier search with a zero-hit paraphrase fallback.

        Runs the strict AND + proximity search first (``_search_strict``,
        unchanged behavior). ONLY when that returns nothing and the query is a
        multi-word non-prefix query does it retry with relaxed variants
        (stopwords stripped, then the rarest token). This is strictly additive: a
        non-empty strict result is returned untouched, so ranking and recall of
        existing hits cannot regress, and single-token / prefix queries (the
        multi_record path) never trigger the fallback. (paraphrase recall, beta
        deadguy 2026-06-14: NL queries miss under strict token-AND.)
        """
        hits = self._search_strict(query, limit=limit, prefix=prefix, tiers=tiers)
        if hits or prefix:
            return hits
        for relaxed in _relaxed_query_strings(query):
            hits = self._search_strict(relaxed, limit=limit, prefix=False, tiers=tiers)
            if hits:
                return hits
        return hits

    def _search_strict(self, query: str, *, limit: int = 20, prefix: bool = False,
                       tiers: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Cross-tier full-text search over entities + state + reference + journal.

        Each hit is tier-tagged so callers know which tier surfaced the match.

        Returns: list of dicts shaped:
            {
              "tier":  "entity" | "state" | "reference" | "journal",
              "key":   <entity name | state key | doc_key | journal id>,
              "category": <entity category or None>,
              "body":  <JSON-decoded payload or string>,
              "snippet": <FTS5 snippet, up to ~120 chars around the match>,
              "rank":  <FTS5 rank, lower is better>,
              "ts":    <ISO timestamp: updated_at or journal ts>
            }

        Ordered by FTS5 rank across the union. The default ``limit`` applies
        globally (combined across tiers). Pass ``tiers=("entity", "state")``
        to restrict.

        Query is sanitized as a single FTS5 phrase (see ``search_entities``
        notes). Empty / invalid queries return [].

        Raises: StorageError on backend failure; ValueError on unknown tier names.
        """
        limit = _clamp_limit(limit)  # CORE-5: negative=unbounded; huge=full-scan. Clamp.
        match_q = _sanitize_fts5_query(query, prefix=prefix)
        if not match_q:
            return []
        allowed = set(tiers) if tiers else {"entity", "state", "reference", "journal"}
        if tiers:
            unknown = sorted(allowed - {"entity", "state", "reference", "journal"})
            if unknown:
                raise ValueError(
                    f"unknown tiers: {', '.join(unknown)}; "
                    "valid: entity, state, reference, journal"
                )
        hits: list[dict[str, Any]] = []
        with self._storage.connection() as conn:
            # v0.4.0 (KAPPA YELLOW finding): per-tier OperationalError handling
            # now classifies via _classify_fts5_error. Schema-missing keeps the
            # previous behavior (skip this tier silently, other tiers continue).
            # FTS5 syntax / real backend errors raise: the query is bad for
            # ALL tiers, no point continuing through the union.
            #
            # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
            # EVERY query below carries `AND f.tenant_id = ?` as its ONLY tenant
            # boundary (tenant_id is UNINDEXED in the FTS5 tables — see
            # schema.sql — so isolation is a trailing post-filter, not
            # index-enforced). Dropping the clause from ANY ONE tier leaks that
            # tier across all tenants. DO NOT remove/reorder/conditionalize.
            # FLAGGED: index-level enforcement needs an FTS schema migration that
            # would break existing DBs. Guarded by test_core3_tenant_isolation.
            if "entity" in allowed:
                for r in _fts_query(
                    conn,
                    "SELECT 'entity' AS tier, e.name AS key, e.category, e.body, "
                    "       e.updated_at AS ts, "
                    "       snippet(entities_fts, 2, '[', ']', '...', 12) AS snip, "
                    "       rank "
                    "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (match_q, self._tenant_id, limit),
                    "entities_fts",
                ):
                    hits.append({
                        "tier": "entity", "key": r["key"],
                        "category": r["category"],
                        "body": loads(r["body"]), "snippet": r["snip"],
                        "rank": r["rank"], "ts": r["ts"],
                    })
            if "state" in allowed:
                for r in _fts_query(
                    conn,
                    "SELECT 'state' AS tier, s.document_key AS key, s.body, "
                    "       s.updated_at AS ts, "
                    "       snippet(state_documents_fts, 1, '[', ']', '...', 12) AS snip, "
                    "       rank "
                    "FROM state_documents_fts f JOIN state_documents s "
                    "  ON s.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE state_documents_fts MATCH ? AND f.tenant_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (match_q, self._tenant_id, limit),
                    "state_documents_fts",
                ):
                    hits.append({
                        "tier": "state", "key": r["key"], "category": None,
                        "body": loads(r["body"]), "snippet": r["snip"],
                        "rank": r["rank"], "ts": r["ts"],
                    })
            if "reference" in allowed:
                for r in _fts_query(
                    conn,
                    "SELECT 'reference' AS tier, d.doc_key AS key, d.body, "
                    "       d.updated_at AS ts, "
                    "       snippet(reference_documents_fts, 1, '[', ']', '...', 12) AS snip, "
                    "       rank "
                    "FROM reference_documents_fts f JOIN reference_documents d "
                    "  ON d.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE reference_documents_fts MATCH ? AND f.tenant_id = ? "
                    "ORDER BY rank LIMIT ?",
                    (match_q, self._tenant_id, limit),
                    "reference_documents_fts",
                ):
                    hits.append({
                        "tier": "reference", "key": r["key"], "category": None,
                        "body": r["body"], "snippet": r["snip"],
                        "rank": r["rank"], "ts": r["ts"],
                    })
            if "journal" in allowed:
                # Journal FTS5 is standalone/contentless: fetch event_id from
                # the FTS5 table, then join to journal_events by id (TEXT PK)
                # for typed body fields. Contentless tables can't 'rebuild',
                # so _fts_query contains corruption by returning [] (tier
                # skipped) rather than crashing the whole search.
                #
                # v0.4.7: cap the journal tier's contribution. Journal entries
                # are long and share many common terms (Project, Research,
                # Decision...), so on mixed-keyword queries they were dominating
                # 50-80% of hits and burying real entities/state/reference. Give
                # journal at most a quarter of the global limit; the structured
                # tiers keep the rest. The global rank-sort + limit still applies.
                journal_limit = max(1, limit // 4) if limit > 0 else 0
                for r in _fts_query(
                    conn,
                    "SELECT 'journal' AS tier, j.id AS key, j.ts, "
                    "       j.evaluated, j.acted, j.forward, j.extra, "
                    "       snippet(journal_events_fts, 1, '[', ']', '...', 12) AS snip, "
                    "       f.rank AS rank "
                    "FROM journal_events_fts f JOIN journal_events j "
                    "  ON j.id = f.event_id "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE journal_events_fts MATCH ? AND f.tenant_id = ? "
                    "ORDER BY f.rank LIMIT ?",
                    (match_q, self._tenant_id, journal_limit),
                    "journal_events_fts",
                ):
                    hits.append({
                        "tier": "journal", "key": r["key"], "category": None,
                        "body": {
                            "evaluated": loads(r["evaluated"]),
                            "acted": loads(r["acted"]),
                            "forward": loads(r["forward"]),
                            "extra": loads(r["extra"]),
                        },
                        "snippet": r["snip"], "rank": r["rank"], "ts": r["ts"],
                    })
        # Sort by rank (lower = better in FTS5), with a tier tiebreaker: at
        # comparable rank the content tiers (entity/state/reference) sort before
        # the contentless journal tier, whose BM25 scores are not on the same
        # scale (cross-tier rank comparability, tester email 19e7eb3096b4dae5).
        _tier_rank = {"entity": 0, "state": 0, "reference": 0, "journal": 1}
        # v0.4.10: proximity re-rank. For multi-word (non-prefix) queries, bucket
        # each hit by how tightly it matches (contiguous phrase > tight window >
        # scattered) and sort by (bucket, rank, tier). This demotes short
        # "near-negative decoy" rows that share the query tokens in an unrelated
        # context, without dropping any hit (recall unchanged). Single-token and
        # prefix queries keep the plain BM25 order, so multi_record_search (which
        # only issues single-token searches) is unaffected.
        query_tokens = _match_tokens(query)
        if not prefix and len(query_tokens) >= 2:
            keyed = []
            for h in hits:
                text = " ".join((
                    _normalize_text(h.get("body")),
                    _normalize_text(h.get("key", "")),
                    _normalize_text(h.get("category") or ""),
                ))
                keyed.append((
                    _proximity_bucket(query_tokens, text),
                    h["rank"],
                    _tier_rank.get(h["tier"], 0),
                    h,
                ))
            keyed.sort(key=lambda t: (t[0], t[1], t[2]))
            hits = [t[3] for t in keyed]
        else:
            hits.sort(key=lambda h: (h["rank"], _tier_rank.get(h["tier"], 0)))
        return hits[:limit]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _verify_committed_size(self, conn: sqlite3.Connection) -> None:
        """CAP-2: re-check the ABSOLUTE footprint inside the write transaction.

        Called after the INSERT/UPDATE rows are staged but before COMMIT, with
        the BEGIN IMMEDIATE write lock held. Reads the true logical size
        (page_count * page_size, which already counts the pending change) and
        gates on the absolute total rather than the pre-write byte estimate. If
        the resulting footprint exceeds the cap (and the gate confirms the
        account is free / over-cap), this raises CapExceededError, and the
        surrounding transaction rolls back the staged write — so a single
        near-cap write that would tip the DB over is rejected, not committed.
        """
        cap_gate = getattr(self, "_cap_gate", None)
        if cap_gate is None:
            return
        total = self._storage.logical_size_bytes(conn)
        # check_total_local is LOCAL-ONLY (no network) — it must not block on a
        # urlopen while we hold the BEGIN IMMEDIATE write lock (2026-06-25 audit
        # blocker). The pre-write check() already did any server verification.
        cap_gate.check_total_local(total)

    def _row_to_entity(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "category": row["category"],
            "name": row["name"],
            "status": row["status"],
            "body": loads(row["body"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
