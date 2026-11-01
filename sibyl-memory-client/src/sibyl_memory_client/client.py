"""MemoryClient: the public API for sibyl-memory-client.

Polymorphic constructor: open by local path OR by hosted-tier URL (v2+, not
implemented yet). The local-first plugin v1 only uses the local path.

The API surface mirrors the canonical sibyl_memory.* table shape so callers
can move between local-SQLite-backed and Postgres-backed clients without
re-learning the model.
"""
from __future__ import annotations

import functools
import re
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import _links
from . import tiers as _tiers  # shared tier module (search code uses a `tiers` parameter)
from .exceptions import NotFoundError, StorageError, TenantError, ValidationError
from .storage import Storage, db_size_bytes, dumps, loads, new_id, _utc_now_iso
from .verdicts import SearchResults, no_match_verdict, ok_verdict, stamp

_log = logging.getLogger(__name__)


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


# ----------------------------------------------------------------------
# FTS ranking + snippet constants (2.D scale fixes, 2026-10-22)
# ----------------------------------------------------------------------
# D2 NAME WEIGHTING. Every FTS query used to `ORDER BY rank`, i.e. bm25 with
# equal column weights over (name, category, body), so an entity whose NAME is
# the query lost to long bodies that merely mention it. Measured on a 70k-row
# store (memory-graph-g1.db, 2026-10-22), rank of the named hub in the top-50
# for two-token name queries, weight 1 -> 8: `sales enablement` 26 -> 1,
# `owen phillips` 4 -> 1, `observability platform` 26 -> 10, `aurelia systems`
# 2 -> 2. At 20.0 `maya patel` vanished from the top-50 because 103 shard rows
# also carry the name in `name`: a column weight cannot tell "the entity named
# X" from "entities whose name contains X", so 8.0 is the ceiling, and exact-
# name lookup is its own (later) stage, never a bigger weight. Rows whose name
# does not contain the term score exactly as before (bm25 weights only scale
# the matching column's term frequency). Reference / state keys get 4.0.
# Query-time only: no schema change, no index rebuild.
NAME_WEIGHT = 8.0
KEY_WEIGHT = 4.0

# D3 MATCH-CENTRED SNIPPET. 40 tokens (from 12) so a hit shows the passage that
# matched instead of a fragment; the MCP/Hermes surfaces cap it at 600 chars.
SNIPPET_TOKENS = 40

# The shadow's recall envelope in multi_record stage 1 (2.D): unchanged from
# 0.9.0's fixed per-token depth, independent of the scaled strict depth K.
_SHADOW_RECALL_LIMIT = 200


def _entity_rank_cfg() -> str:
    # Float-formatted module constants, never caller input: safe to inline.
    return f"bm25({float(NAME_WEIGHT)!r}, 1.0, 1.0)"


# Stage-0 name resolution (2.C). Exact matches returned for one query, the
# phrase-on-name partials added after them, and the longest query (in words)
# that still runs the partial phrase (a question is not a name).
NAME_EXACT_MAX = 5
NAME_PARTIAL_MAX = 3
NAME_PARTIAL_MAX_TOKENS = 8
# The stage-0 equality probe (index entities_tenant_lname). CORE-3: tenant clause.
_NAME_EXACT_SQL = ("SELECT rowid AS rid, name, category, updated_at FROM entities "
                   "WHERE tenant_id = ? AND lower(name) = ?")
# Rows read for one exact name before ordering newest-first (names are unique
# per category, so this bounds the categories that share one name).
_NAME_EXACT_SCAN = 200
# Sample edges per hit in the links summary (2.C hit shape).
HIT_LINK_SAMPLE = 5
# `<category>:<name>`: an identifier-shaped prefix (no spaces) before the colon.
_NAME_PREFIX_RE = re.compile(r"^\s*([\w.\-/]{1,128})\s*:\s*(\S.*)$", re.DOTALL)


def _ascii_fold(text: str) -> str:
    """Python-side twin of sqlite's built-in ``lower()``: ASCII A-Z only, so
    ``lower(name) = ?`` compares like with like and can use the index."""
    return text.translate(_ASCII_LOWER)


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _body_chars(body: Any) -> int | None:
    """Full surfaced length of a hit body (the MCP tool's ``chars``)."""
    if body is None:
        return None
    if isinstance(body, str):
        return len(body)
    import json as _json
    return len(_json.dumps(body, ensure_ascii=False, default=str))


def _meta_title(raw: Any) -> str | None:
    """``metadata.title`` of a reference row when it is a non-empty string."""
    if not raw:
        return None
    try:
        meta = loads(raw)
    except (TypeError, ValueError):
        return None
    t = meta.get("title") if isinstance(meta, dict) else None
    return t if isinstance(t, str) and t.strip() else None


def _hit_node(h: dict[str, Any]) -> tuple[str, str, str] | None:
    """A search hit as a memory_links node key, or None (journal / malformed)."""
    tier, key = h.get("tier"), h.get("key")
    if tier not in ("entity", "reference", "state") or not isinstance(key, str):
        return None
    if tier == "entity":
        cat = h.get("category")
        return ("entity", cat, key) if isinstance(cat, str) and cat else None
    return (tier, "", key)


def _keyed_rank_cfg() -> str:
    return f"bm25({float(KEY_WEIGHT)!r}, 1.0)"


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
# Identifier validation (v0.4.0, external-test YELLOW finding)
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

# v0.4.4 (external test #3 defense-in-depth): SQL is parameterized so injection is
# closed at the DB, but identifiers flow into consumers that do NOT parameterize
# -- filesystem export (a `name` becomes a path component), CLI display, log
# lines, future per-entity backends. Reject path-traversal shapes and the
# shell/redirection/quote metacharacters that have no place in a short flat key.
# Apostrophe is deliberately ALLOWED (legit in name-shaped keys like "o'brien");
# double-quote is rejected because it is also the FTS5 phrase delimiter.
#
# NOTE: we reject the traversal MARKER ".." (catches the external tester's "../../etc/passwd"
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
# FTS5 error surface (v0.4.0, external-test YELLOW finding)
# ----------------------------------------------------------------------
# Previously search() and search_entities() silently swallowed
# sqlite3.OperationalError into `return []` / `pass`. The external tester's complaint:
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
    ValidationError; other → StorageError), preserving the v0.4.0 external test
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


# Cross-tier tie-break for equal rank / equal coverage (spec 2.C, R3): the
# curated tiers (entity, state) first, then reference documents, then the
# journal. multi_record._TIER_PRIORITY holds the same values (a test pins it).
_TIER_RANK = {"entity": 0, "state": 0, "reference": 1, "journal": 2}


def _load_sql(prep_only: bool) -> dict[str, str]:
    """Batch-load SQL per tier for ``MemoryClient._load_rows`` (one ``{}``
    placeholder for the id list). Module level so the planner test can
    EXPLAIN exactly these statements."""
    return {
        # CORE-3: tenant clause on every load, even by rowid / id.
        # `+tenant_id` (unary plus) keeps the clause as a row filter but
        # stops the planner from driving the lookup through a tenant_id
        # index: with no ANALYZE stats SQLite picked entities_updated_at
        # (tenant_id=?) and scanned the whole tenant once per 500-id chunk
        # (18 ms per chunk at 100k rows vs 0.9 ms by primary key).
        "entity": "SELECT rowid AS rid, body, updated_at AS ts FROM entities "
                  "WHERE +tenant_id = ? AND rowid IN ({})",
        "state": "SELECT rowid AS rid, body, updated_at AS ts FROM state_documents "
                 "WHERE +tenant_id = ? AND rowid IN ({})",
        "reference": ("SELECT rowid AS rid, " +
                      ("substr(body, 1, 2000)" if prep_only else "body") +
                      " AS body, metadata, updated_at AS ts "
                      "FROM reference_documents WHERE +tenant_id = ? AND rowid IN ({})"),
        "journal": "SELECT id AS rid, ts, evaluated, acted, forward, extra "
                   "FROM journal_events WHERE +tenant_id = ? AND id IN ({})",
    }


# ----------------------------------------------------------------------
# FTS5 query sanitization
# ----------------------------------------------------------------------
# v0.3.3 hardens search() / search_entities() against FTS5 injection + DoS
# (audit SEC-3). User input is wrapped as a single quoted FTS5 phrase so
# column-filter syntax (`name:`, `category:`, `rowid:`, etc.) and unclosed
# quotes can't escape into the FTS5 parser. Caller can still get prefix
# matching by passing prefix=True.

# Hardening #9 (super-patch 2026-07-05, subsumes duplicate R15): upper bound on
# the raw search query length. _sanitize_fts5_query walks its input char-by-char
# up to three times and expands EVERY token into an ANDed, phrase-quoted term;
# that sanitized string is then MATCHed across up to four FTS5 tiers. With no
# ceiling, a multi-megabyte / ~200k-token query becomes a ~200k-term MATCH
# executed four times — a CPU + memory DoS reachable from client, MCP, and
# Hermes alike. Identifiers are already capped at 1024 (_IDENT_MAX_LENGTH) but
# the free-text query was not. This is the single choke point every search path
# funnels through, so bounding it here protects all callers at once. 4096 chars
# is far above any legitimate natural-language query (real queries are a handful
# of words), so normal use is never truncated; only pathological inputs are.
MAX_QUERY_CHARS = 4096


# v0.4.4 (Discord beta report, external test #4): bare uppercase FTS5 operator
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
    # Hardening #9 (super-patch 2026-07-05): bound the query BEFORE any per-char
    # walk or tokenization. Truncating here caps the worst case (a multi-MB /
    # ~200k-token query expanded into a giant MATCH) at a single choke point
    # shared by client / MCP / Hermes. Truncate rather than raise: search is a
    # read path, so best-effort matching on the first MAX_QUERY_CHARS characters
    # is friendlier than forcing every caller to catch a ValidationError, and it
    # keeps the token count implicitly bounded (<= MAX_QUERY_CHARS tokens). Real
    # queries are far under the ceiling and pass through untouched.
    if len(raw) > MAX_QUERY_CHARS:
        raw = raw[:MAX_QUERY_CHARS]
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
        # that crashes the FTS5 parser (a beta stress test
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
# density over proximity (Discord beta reports against v0.4.2 and
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


# Paraphrase zero-hit fallback (beta report 2026-06-14): natural-language
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
    for cand, _last_resort in _relaxed_query_variants(query):
        yield cand


def _relaxed_query_variants(query: str):
    """``(candidate, is_last_resort)`` for every relaxed variant, in order.

    Exactly the sequence ``_relaxed_query_strings`` yields; the flag distinguishes
    the two stages, which differ in quality by a lot and now have to be told
    apart (see MemoryClient.search):

      * stage 1, ``is_last_resort=False`` — the stopword-stripped CONJUNCTION,
        which still requires every content token and so answers the whole
        question.
      * stage 2, ``is_last_resort=True`` — ONE token, longest first. The
        docstring above already calls this last-resort recall: it is the weakest
        answer this client can produce.
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
        yield cand, False
    # 2) each content token alone, longest-first (length = cheap rarity proxy).
    #    The wrapper stops at the first variant that returns hits, so the most
    #    specific term is tried before more common ones. Last-resort recall.
    # (-len, token): longest first, ties broken by the token itself. The old
    # `key=len, reverse=True` was a stable sort over a SET, so equal-length
    # tokens came out in randomised string-hash order and the same query could
    # answer differently in different processes (review finding 11).
    for tok in sorted(set(content or toks), key=lambda t: (-len(t), t)):
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
            yield tok, True


# D2L (the coverage-gated stem rescue: _stem_token / _stem_truncated_query /
# _head_searchable_text / _uncovered_stem_tokens, plus _STEM_MIN_TOKEN,
# _STEM_DROP, _STEM_FLOOR and _GATE_ROW_BYTES) was REMOVED on 2026-08-30 by
# the lang-core-strip operator directive, together with its probe ladder in
# search(). It was a QUERY-time compensation for fusional morphology; the
# replacement is the WRITE-time normalization built on the shadow table (stage
# 2, 2026-08-30): ONE versioned normalizer in shadow.py renders the stored text
# at write time and the query at read time, and the truncation parameters that
# lived here are now shadow.normalize_token. Git history at 63a5ea9 holds the
# removed code verbatim if it is ever needed for reference.


def _unshortened_single_token(query: str) -> bool:
    """True for a ONE-token query whose token the ending rule left alone.

    The single shape that gets no shadow consult on a non-empty strict head. Such
    a token has nothing to recover and nothing to corroborate with: no inflected
    variant the truncation could reach, and no second term to agree with it. So
    'stock', 'form' and 'k8' behave exactly as they did on the stripped base,
    which is what keeps 'stock' from dragging in 'stocktake' through a substring
    append and keeps df at 1 where the strip put it.
    """
    from .shadow import normalize_terms
    terms = normalize_terms(query)
    return len(terms) == 1 and not terms[0][1]


# F5 (red-team 2026-06-17): sanity ceiling on a single serialized body. The 5 MB
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


def _check_str_body(body: str, field: str = "body") -> None:
    """The 1 MiB per-value bound for a free-text str value (REFERENCE tier),
    measured in UTF-8 bytes like ``_check_json``. A str that cannot be encoded
    (a lone surrogate) is rejected here with a typed error instead of failing
    inside SQLite."""
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError as e:
        raise ValidationError(
            f"{field} is not valid UTF-8 text: {e.reason}",
            recovery=f"Remove unpaired surrogate characters from {field}.",
        ) from e
    if size > _MAX_BODY_BYTES:
        raise ValidationError(
            f"{field} is too large ({size // 1024} KB; max 1 MiB "
            f"({_MAX_BODY_BYTES // 1024} KB) per value).",
            recovery=(
                "Split the document across several reference keys (for example "
                "<key>/part-1, <key>/part-2), or store a trimmed version."
            ),
        )


def _page_text(key: str, text: str, offset: int, max_chars: int,
               metadata: Any, updated_at: Any) -> dict[str, Any]:
    """One page of a reference body in code points (shared by the SDK's
    read_reference and the MCP tool, which pages the fence-scrubbed text)."""
    total = len(text)
    if offset >= total:
        piece, nxt = "", None
    else:
        end = min(offset + max_chars, total)
        piece, nxt = text[offset:end], (end if end < total else None)
    return {"key": key, "body": piece, "offset": offset, "chars": total,
            "next_offset": nxt, "metadata": metadata, "updated_at": updated_at}


def _prefix_successor(prefix: str) -> str | None:
    """Smallest string greater than every string that starts with ``prefix``
    (BINARY collation = UTF-8 byte order = code-point order). None when no
    such bound exists (the prefix is all U+10FFFF). Skips the surrogate block,
    which cannot be stored as UTF-8."""
    chars = list(prefix)
    while chars:
        cp = ord(chars[-1]) + 1
        if 0xD800 <= cp <= 0xDFFF:
            cp = 0xE000
        if cp <= 0x10FFFF:
            chars[-1] = chr(cp)
            return "".join(chars)
        chars.pop()
    return None


def _encode_ref_cursor(doc_key: str) -> str:
    import base64
    return base64.urlsafe_b64encode(doc_key.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_ref_cursor(cursor: Any) -> str:
    import base64
    import binascii
    bad = ValidationError("cursor is not a valid list_references cursor",
                          recovery="Pass next_cursor from the previous page unchanged, or omit it.")
    if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
            or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None):
        raise bad
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        return raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad from None


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
    """Decorator on every public memory operation: the ACCOUNT GATE, then the
    usage heartbeat, then the operation.

    0.9.0 (spec account-gated 3.1): ``self._entitlement_gate.require(kind)``
    runs FIRST and raises ``EntitlementError`` when there is no account or the
    device is locked. This is the single enforcement point for the whole
    plugin family (MCP, hermes, langgraph and direct SDK callers all reach
    storage through these methods). A refused op is not counted. The heartbeat
    part is fire-and-forget and never raises into the operation."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            ent = self._entitlement_gate.require(kind)
            if ent is not None and not getattr(self, "_adopt_checked", True):
                self._maybe_adopt(ent)
            hb = getattr(self, "_heartbeat", None)
            if hb is not None:
                try:
                    hb.record(kind)
                except Exception:
                    pass
            result = fn(self, *args, **kwargs)
            if kind in _LIFECYCLE_WRITE_KINDS and not _is_noop_write(kind, result):
                _note_lifecycle_write(self, kind, args, kwargs)
            return result
        return wrapper
    return deco


def _gate_only(kind: str):
    """The account gate of ``_track_op`` without the usage heartbeat.

    For internal read primitives that only ever run as part of an already
    tracked operation (multi_record's final hydration, 2.D): they must not be a
    way around the gate, and they must not double-count usage."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            self._entitlement_gate.require(kind)
            return fn(self, *args, **kwargs)
        return wrapper
    return deco


# Save/debloat bookkeeping (spec plugin-save-debloat 4.1): a successful write
# bumps an in-memory counter (flushed to ~/.sibyl-memory/lifecycle.json at most
# every 30 s or 10 writes); the save procedure's delta marker resets it.
# Counters only, never content; never raises into the operation. OPT-IN per
# client (SD-9): only a client the plugin's own surfaces enabled with
# ``enable_lifecycle_counting`` (the MCP server, the Hermes adapter, the CLI)
# counts; any other MemoryClient (a library user, LangGraph, a test) writes no
# lifecycle file, starts no timer and registers no exit hook.
_LIFECYCLE_WRITE_KINDS = frozenset({
    "set_entity", "set_state", "write_event", "set_reference", "delete_entity",
    "archive_entity", "learn", "accept_skill_proposal", "reject_skill_proposal",
    "delete_reference",
    "link", "unlink",
})
_SAVE_MARKER_KEY = "sibyl_save:last_save"


def _is_noop_write(kind: str, result: Any) -> bool:
    """A write that changed nothing is not counted toward the save trigger: an
    identical re-link (neither created nor changed) or an unlink that removed
    no edge."""
    if kind == "link" and isinstance(result, dict):
        return not result.get("created") and not result.get("changed")
    if kind == "unlink":
        return result == 0
    return False


def _note_lifecycle_write(client: Any, kind: str, args: tuple, kwargs: dict) -> None:
    home = getattr(client, "_lifecycle_home", None)
    if home is None:
        return
    try:
        from .lifecycle import state as _lc
        tid = getattr(client, "_tenant_id", "") or ""
        _lc.note_write(kind, tid, home=home)
        if kind == "set_state":
            key = args[0] if args else kwargs.get("key")
            if key == _SAVE_MARKER_KEY and not _lc.is_suppressed():
                _lc.note_save(tid, home=home)
    except Exception:
        pass
    if kind in _SIGNAL_WRITE_KINDS:
        # R2 (spec 1.2): the correction-loop evidence. Pro only (the recorder
        # checks the verified entitlement), outside ``suppressed()`` only; the
        # category and name are hashed with the tenant, never stored.
        try:
            from .lifecycle import signals as _sg
            category = args[0] if args else kwargs.get("category")
            name = args[1] if len(args) > 1 else kwargs.get("name")
            _sg.note_entity_write(home, kind, category, name, getattr(client, "_tenant_id", "") or "",
                                  client=client)
        except Exception:
            pass


_SIGNAL_WRITE_KINDS = frozenset({"set_entity", "delete_entity", "archive_entity"})


# Notice throttle (spec 3.2): one notice text at most once per process per 24 h,
# shared by every client in the process (MCP rebuilds its client on credential
# changes; the throttle must survive that).
_NOTICE_INTERVAL_SECONDS = 24 * 3600
_NOTICE_LOCK = threading.Lock()
_NOTICE_SHOWN: dict[str, float] = {}


def _consume_notice(notice: str | None) -> str | None:
    if not notice:
        return None
    now = time.monotonic()
    with _NOTICE_LOCK:
        last = _NOTICE_SHOWN.get(notice)
        if last is not None and now - last < _NOTICE_INTERVAL_SECONDS:
            return None
        _NOTICE_SHOWN[notice] = now
    return notice


def entity_ref(category: str, name: str) -> dict[str, Any]:
    """NodeRef for an entity (v6 relations)."""
    return {"tier": "entity", "category": category, "key": name}


def reference_ref(key: str) -> dict[str, Any]:
    """NodeRef for a reference document (v6 relations)."""
    return {"tier": "reference", "category": None, "key": key}


def state_ref(key: str) -> dict[str, Any]:
    """NodeRef for a state document (v6 relations)."""
    return {"tier": "state", "category": None, "key": key}


class MemoryClient:
    """Single canonical interface for reading and writing Sibyl Memory state."""

    # Paid-tier-only features. Free tier raises TierGateError; upgrading to any
    # paid tier (pro, elite) unlocks both self-learning and the memory linter.
    # The shared tiers set (canonical paid tiers plus legacy aliases); kept as a
    # class attribute because callers and tests read it.
    _PAID_ONLY_TIERS = _tiers.PAID_TIERS

    def __init__(
        self,
        storage: Storage,
        *,
        tenant_id: str = DEFAULT_TENANT,
        tier: str = _tiers.FREE,
        account_id: str | None = None,
        session_token: str | None = None,
        cap_gate: Any = None,
        credentials_claim: dict[str, Any] | None = None,
        credentials_signature: str | None = None,
        credentials_path: str | Path | None = None,
        entitlement_gate: Any = None,
        telemetry: bool | None = None,
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
        # Pre-sign-in adoption (0.9.0, _adopt.py): checked once per client, on
        # the first gated operation that returns a verified entitlement.
        self._adopt_checked = False
        self._adopt_lock = threading.Lock()
        self._adoption: Any = None
        self._adoption_notice: str | None = None
        # Save-reminder write counting: off until a plugin surface enables it.
        self._lifecycle_home: Path | None = None

        # ACCOUNT GATE (0.9.0, spec account-gated 3.1). Built from the
        # credentials file the caller loaded (default ~/.sibyl-memory/
        # credentials.json; the MCP server and the hermes provider pass the
        # path they read). Identity is re-read from that file on every
        # evaluation, so the gate is about the ACCOUNT, never the tenant: a
        # DEFAULT_TENANT client is gated exactly like any other. Construction
        # NEVER raises for entitlement reasons (the MCP server must start and
        # hermes initialize must not crash the agent): prime() evaluates
        # non-blocking and does a blocking refresh (4 s) only in bootstrap.
        # The only bypass is injecting a gate object here (tests use
        # sibyl_memory_client.testing.NullEntitlementGate); no environment
        # variable or config file can select one.
        if entitlement_gate is None:
            from ._entitlement import EntitlementGate
            cred_path = (Path(credentials_path).expanduser() if credentials_path is not None
                         else Path.home() / ".sibyl-memory" / "credentials.json")
            entitlement_gate = EntitlementGate(cred_path)
        self._entitlement_gate = entitlement_gate
        try:
            entitlement_gate.prime()
        except Exception:
            pass

        # Cap gate: enforces the 5 MB free-tier cap with server-authoritative
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
                # 0.9.0: the entitlement's tier is the local hint and the token
                # rides on /check-write INSTEAD of the HMAC claim (D11), which
                # also takes email and wallet off that wire.
                entitlement_tier_fn=lambda: self._entitlement_gate.tier,
                entitlement_token_fn=lambda: self._entitlement_gate.token,
                on_auth_denied=self._entitlement_gate.notify_server_rejected,
            )
        self._cap_gate = cap_gate

        # Usage heartbeat: local-first memory ops never hit the network, so the
        # server has no usage signal. This reports ONLY aggregate op COUNTS
        # (no content, no PII beyond account_id), debounced + fire-and-forget,
        # so an active account's request count finally reflects real use.
        # No-op without an account_id; opt out with SIBYL_MEMORY_TELEMETRY=0.
        # 0.10.0: ``telemetry=False`` turns it off for this client whatever the
        # environment says (the Hermes provider passes it unless the person
        # opted in); None or True follow the environment rule above.
        try:
            if telemetry is False:
                from ._heartbeat import _NullHeartbeat
                self._heartbeat = _NullHeartbeat()
            else:
                from ._heartbeat import HeartbeatReporter
                self._heartbeat = HeartbeatReporter(
                    account_id, session_token,
                    entitlement_fn=lambda: self._entitlement_gate.token,
                )
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
        tier: str = _tiers.FREE,
        account_id: str | None = None,
        session_token: str | None = None,
        credentials_claim: dict[str, Any] | None = None,
        credentials_signature: str | None = None,
        credentials_path: str | Path | None = None,
        entitlement_gate: Any = None,
        telemetry: bool | None = None,
    ) -> "MemoryClient":
        """Open a local SQLite-backed MemoryClient.

        The directory at ``path``'s parent is created with mode 0700 if
        missing. The schema is applied on first open and is idempotent.

        ``tier`` is a hint (default "free"). Since 0.9.0 the tier inside a
        verified entitlement wins for paid-only features (self-learning +
        memory linter); the hint is used only before the first entitlement.

        Pass ``account_id`` and ``session_token`` from credentials.json so
        the SDK can verify the user's tier against the server when they
        approach the 5 MB free-tier cap. Without these, the SDK enforces
        a strict local 5 MB cap (no server check possible).

        0.9.0: every memory operation requires an activated account with a
        valid, server-signed entitlement (``EntitlementError`` otherwise).
        ``credentials_path`` names the credentials.json to gate on (default
        ``~/.sibyl-memory/credentials.json``); ``entitlement_gate`` injects a
        gate object (tests: ``sibyl_memory_client.testing.NullEntitlementGate``).

        ``telemetry`` (0.10.0): False sends no usage count from this client;
        None (default) or True follow ``SIBYL_MEMORY_TELEMETRY`` (on unless "0").
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
            credentials_path=credentials_path,
            entitlement_gate=entitlement_gate,
            telemetry=telemetry,
        )

    # ------------------------------------------------------------------
    # Account gate (0.9.0)
    # ------------------------------------------------------------------
    @property
    def entitlement_gate(self) -> Any:
        return self._entitlement_gate

    def _maybe_adopt(self, ent: Any) -> None:
        """First verified sign-in for an account on this store: move the rows
        written before sign-in (DEFAULT_TENANT) into the account tenant, in one
        local transaction (_adopt.py). Only when this client's tenant IS the
        entitlement's ``ten``/``sub`` (the gate just verified and bound it),
        at most once per account per store, and never when the store has held
        another account (that case needs ``sibyl memory adopt``). Never raises
        into the operation; on any error nothing moved (the transaction rolled
        back) and the next client retries."""
        with self._adopt_lock:
            if self._adopt_checked:
                return
            self._adopt_checked = True
            if (self._tenant_id == DEFAULT_TENANT
                    or self._tenant_id not in (getattr(ent, "ten", None), getattr(ent, "sub", None))):
                return
            try:
                from ._adopt import adopt_default_tenant
                self._adoption = adopt_default_tenant(
                    self._storage, self._tenant_id, ent, trigger="auto")
            except Exception:
                self._adoption = None
            res = self._adoption
            n = getattr(res, "moved", 0) if getattr(res, "status", None) == "moved" else 0
            if n > 0:
                # Counts only: never a key, a category or an account id.
                self._adoption_notice = (
                    "1 memory written before sign-in was moved into your account "
                    "on this computer." if n == 1 else
                    f"{n} memories written before sign-in were moved into your "
                    "account on this computer.")

    @property
    def adoption(self) -> Any:
        """The automatic pre-sign-in adoption result for this client (None
        until the first gated operation, or when it did not apply)."""
        return self._adoption

    @_track_op("adopt")
    def adopt_pre_signin_memory(self, *, confirm_other_accounts: bool = False) -> Any:
        """Explicitly move this store's DEFAULT_TENANT rows into this client's
        (account) tenant: ``sibyl memory adopt``. Requires a verified sign-in
        whose ``ten``/``sub`` is this client's tenant. Idempotent. Raises
        ``_adopt.AdoptionRefused`` (``other_account``) unless
        ``confirm_other_accounts`` when the store has held another account."""
        from ._adopt import AdoptionRefused, adopt_default_tenant
        ent = self._entitlement_gate.require("adopt")
        if ent is None:
            raise AdoptionRefused("not_verified",
                                  "No verified sign-in on this computer. Run sibyl init first.")
        with self._adopt_lock:
            self._adopt_checked = True
            return adopt_default_tenant(self._storage, self._tenant_id, ent,
                                        trigger="command",
                                        confirm_other_accounts=confirm_other_accounts)

    def require_entitlement(self, op: str = "access") -> None:
        """Raise ``EntitlementError`` unless this device may use memory now.

        For surfaces that answer a tool call WITHOUT reaching a gated method
        (the MCP short-query guard): the refusal must be the same one."""
        self._entitlement_gate.require(op)

    def adoption_notice(self) -> str | None:
        """The one-time notice for the automatic pre-sign-in adoption ("N
        memories written before sign-in were moved into your account on this
        computer."), or None. Given out once per client (then cleared) through
        the same process throttle as every account notice; the move itself
        happens once per account per store, so a rebuilt client says nothing."""
        lock = getattr(self, "_adopt_lock", None)
        if lock is None:
            return None
        with lock:
            notice = getattr(self, "_adoption_notice", None)
            self._adoption_notice = None
        return _consume_notice(notice)

    def entitlement_notice(self) -> str | None:
        """The notice to attach to a SUCCESSFUL result (pre-sign-in adoption,
        stale band, clock, email nag), at most once per process per 24 h per
        text; the adoption notice first, and only once. Never contains a token.
        None when there is nothing to say."""
        adopted = self.adoption_notice()
        if adopted:
            return adopted
        try:
            notice = self._entitlement_gate.current().notice
        except Exception:
            return None
        return _consume_notice(notice)

    # ------------------------------------------------------------------
    # Tenant management
    # ------------------------------------------------------------------
    def enable_lifecycle_counting(self, home: str | Path | None = None) -> None:
        """Count this client's successful memory writes toward the save
        reminder, in ``<home>/.sibyl-memory/lifecycle.json`` (default: the
        user's home, where the Claude Code hooks and ``sibyl status`` read
        it). Off by default: the plugin's own surfaces (the MCP server, the
        Hermes adapter, ``sibyl save --fact``) turn it on; a library user of
        ``MemoryClient`` gets no lifecycle file, timer or exit hook. Counters
        only, never content."""
        self._lifecycle_home = Path(home).expanduser() if home is not None else Path.home()

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
        """Update the tier hint. Since 0.9.0 a verified entitlement's tier
        wins over it for feature gating (see ``_effective_tier``); nothing in
        the plugin calls this."""
        if not isinstance(tier, str) or not tier:
            raise ValidationError("tier must be a non-empty string")
        self._tier = tier

    def _effective_tier(self) -> str:
        """Best-available trustworthy tier for feature gating.

        CORE-10 (2026-06-25 pre-launch audit, SAFE-MINIMAL — see FLAG below):
        the paid-feature gates trusted the raw client-supplied ``self._tier``,
        so editing credentials.json to a paid ``tier`` label unlocked the learner
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
        # 0.9.0 (spec 3.1): a verified entitlement's tier wins over the tier
        # cache and the file hint. This closes the CORE-10 residual: no paid
        # feature unlocks from an edited credentials.json while an entitlement
        # (fresh or stale) is present. The cache/hint ladder below is reached
        # only in bootstrap (legacy credentials, no entitlement yet).
        ent_gate = getattr(self, "_entitlement_gate", None)
        try:
            ent_tier = ent_gate.tier if ent_gate is not None else None
        except Exception:
            ent_tier = None
        if isinstance(ent_tier, str) and ent_tier:
            return ent_tier
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

    def tier_for_display(self) -> str:
        """Shown-tier fix (2026-10-08): the tier a user-facing surface shows. The
        verified pass's tier when this computer holds a valid one (fresh or
        stale), else the saved credentials hint. Never the tier cache: one
        source per screen, so status lines never disagree. Never raises."""
        ent_gate = getattr(self, "_entitlement_gate", None)
        try:
            ent_tier = ent_gate.tier if ent_gate is not None else None
        except Exception:
            ent_tier = None
        if isinstance(ent_tier, str) and ent_tier:
            return ent_tier
        return self._tier

    def _require_paid_tier(self, feature: str) -> None:
        """Raise TierGateError if the current tier is not paid-tier.

        CORE-10: gates on the server-authoritative tier when a fresh cap-gate
        cache is available, else on the client hint (see _effective_tier).
        """
        from .exceptions import TierGateError
        effective = self._effective_tier()
        if not _tiers.is_paid(effective):
            if _tiers.known(effective):
                current = _tiers.display(effective)
            else:
                # tier_unknown: an unrecognised label is treated as Free; the
                # message names the raw label so a support ticket can see it.
                current = (f"{_tiers.display(effective)} (access label {str(effective)[:32]!r} "
                           f"is not recognised by this version; {_tiers.UNKNOWN_REASON})")
            raise TierGateError(
                f"{feature} requires a paid tier. Current tier: {current}.",
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
        links: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Insert or update an entity.

        ``links`` (v6, spec tiered-retrieval 2.B): optional ``[{"to": NodeRef,
        "relation": str, "metadata"?: dict}]``. The entity is the source of every
        edge; the row and its edges are written in ONE transaction and every
        target must already exist in this tenant (strict), else NotFoundError
        and nothing is written. Adds or upserts the listed edges only; existing
        edges not listed are kept. Edge bytes count toward the free-tier cap.

        UNIQUE (tenant_id, category, name) is enforced at the DB level. On
        conflict the existing row is updated (body + status + updated_at).
        Returns the resulting entity row as a dict. On INSERT (a new row) it
        also carries ``inherited_links``: how many edges were already keyed to
        this (category, name) before the call, i.e. edges written for an
        earlier, archived or deleted entity of the same name. They attach by
        name and read ``stale: true`` until re-linked (link() re-stamps) or
        unlinked.

        Subject to the 5 MB free-tier cap when tier='free'. Raises
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
        src_node = ("entity", category, name)
        link_specs = _links.norm_link_specs(links)
        # Cap gate: rough byte estimate (FTS5 + indexes add overhead); edges count.
        self._cap_gate.check(proposed_delta_bytes=len(body_json) + len(name) + len(category) + 200
                             + _links.spec_bytes(link_specs, src_node))
        with self._storage.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            ).fetchone()
            inherited: int | None = None
            if existing is None:
                ent_id = new_id()
                conn.execute(
                    "INSERT INTO entities (id, tenant_id, category, name, status, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ent_id, self._tenant_id, category, name, status, body_json),
                )
                # Identity stamp: edges already keyed to this (category, name)
                # were written for an earlier row (archived or deleted out of
                # band). They attach by name but read as stale until re-linked
                # or unlinked. Counted before this call's own links= are written.
                inherited = _links.count_touching(conn, self._tenant_id, src_node)
            else:
                ent_id = existing["id"]
                conn.execute(
                    "UPDATE entities SET status = ?, body = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ?",
                    (status, body_json, ent_id),
                )
            written = (_links.write_specs(conn, self._tenant_id, src_node, link_specs)
                       if link_specs else None)
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)
        ent = self.get_entity(category, name)
        if inherited is not None:
            ent["inherited_links"] = inherited
        if written is not None:
            # Additive, only when links= was given: what the edges did.
            ent["links"] = _links.write_summary(written)
        return ent

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

    @_track_op("delete_entity")
    def delete_entity(self, category: str, name: str) -> bool:
        """Hard-delete an entity. Every edge touching it (either direction) is
        deleted in the same transaction (v6). Use archive_entity to keep them."""
        with self._storage.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM entities WHERE tenant_id = ? AND category = ? AND name = ?",
                (self._tenant_id, category, name),
            )
            deleted = cur.rowcount > 0
            if deleted:
                self._cascade_node_links(conn, "entity", category, name)
        return deleted

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

    @_track_op("get_state")
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
    @_track_op("write_event")
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

    @_track_op("read_events")
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
        links: list[dict[str, Any]] | None = None,
    ) -> None:
        """Insert or update a REFERENCE-tier document.

        ``links`` (v6, spec tiered-retrieval 2.B): optional ``[{"to": NodeRef,
        "relation": str, "metadata"?: dict}]`` with this document as the source;
        written in the same transaction as the document, strict on target
        existence (NotFoundError rolls the document write back too).

        v0.4.0: ``key`` is validated as an identifier (non-empty string, no
        control characters, length <= 1024). Raises ValidationError on
        rejection.

        v0.4.12 (beta report): ``body`` accepts ``str`` or a
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
        else:
            # Tiered-retrieval 2.A (2026-10-22): the str branch used to skip
            # _check_json, so a paid caller could write one unbounded string.
            # Same 1 MiB per-value bound as every other value, in UTF-8 bytes.
            _check_str_body(body)
        meta_json = _check_json(metadata, "metadata") if metadata is not None else None
        src_node = ("reference", "", key)
        link_specs = _links.norm_link_specs(links)
        delta = (len(body) + len(key) + (len(meta_json) if meta_json else 0) + 200
                 + _links.spec_bytes(link_specs, src_node))
        self._cap_gate.check(proposed_delta_bytes=delta)
        with self._storage.transaction() as conn:
            conn.execute(
                "INSERT INTO reference_documents (tenant_id, doc_key, body, metadata) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, doc_key) DO UPDATE SET body = excluded.body, "
                "metadata = excluded.metadata, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (self._tenant_id, key, body, meta_json),
            )
            written = self._write_node_links(conn, src_node, link_specs) if link_specs else None
            # CAP-2: absolute-footprint recheck inside the same transaction.
            self._verify_committed_size(conn)
        # Additive (was None): the key and, when links= was given, what the
        # edges did ({created, updated, unchanged}); None without links=.
        return {"key": key,
                "links": _links.write_summary(written) if written is not None else None}

    @_track_op("get_reference")
    def get_reference(self, key: str) -> dict[str, Any] | None:
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT body, metadata, updated_at FROM reference_documents WHERE tenant_id = ? AND doc_key = ?",
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return {"body": row["body"], "metadata": loads(row["metadata"]), "updated_at": row["updated_at"]}

    @_track_op("read_reference")
    def read_reference(
        self,
        key: str,
        *,
        offset: int = 0,
        max_chars: int = 20_000,
    ) -> dict[str, Any] | None:
        """Paged read of a REFERENCE-tier document (tiered retrieval 2.A).

        ``offset`` and ``max_chars`` count Python str characters (code points),
        so a page never splits a multi-byte character. Returns None for an
        unknown key, else ``{key, body: <slice>, offset, chars: <total>,
        next_offset: int | None, metadata, updated_at}``. ``offset >= chars``
        is not an error: it returns an empty body with ``next_offset`` None.
        Raises ValidationError on a negative/non-int ``offset`` or a
        ``max_chars`` below 1 (the SDK is strict; the MCP tool clamps)."""
        validate_identifier(key, field_name="key")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0",
                                  recovery="Pass 0 or the next_offset from the previous page.")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValidationError("max_chars must be an int >= 1",
                                  recovery="Pass a page size such as 20000.")
        with self._storage.connection() as conn:
            row = conn.execute(
                "SELECT body, metadata, updated_at FROM reference_documents "
                "WHERE tenant_id = ? AND doc_key = ?",
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return _page_text(key, row["body"] or "", offset, max_chars,
                          loads(row["metadata"]), row["updated_at"])

    @_track_op("list_references")
    def list_references(
        self,
        *,
        prefix: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List REFERENCE-tier documents in ``doc_key`` order (tiered
        retrieval 2.A). Returns ``{items: [{key, chars, metadata,
        updated_at}], next_cursor}``; bodies are never loaded (``chars`` is
        the body length in characters).

        ``prefix`` is an exact, case-sensitive, literal prefix (``%`` and
        ``_`` are plain characters): it is the half-open key range
        ``[prefix, successor(prefix))``, which seeks the ``(tenant_id,
        doc_key)`` primary key. ``cursor`` is the opaque ``next_cursor`` of
        the previous page (keyset on doc_key); pass it with the same
        ``prefix``."""
        limit = _clamp_limit(limit)
        if limit < 1:
            limit = 1
        sql = ("SELECT doc_key, length(body) AS chars, metadata, updated_at "
               "FROM reference_documents WHERE tenant_id = ?")
        params: list[Any] = [self._tenant_id]
        if prefix is not None:
            if not isinstance(prefix, str):
                raise ValidationError("prefix must be a string",
                                      recovery="Pass a key prefix such as 'doc/'.")
            if prefix:
                sql += " AND doc_key >= ?"
                params.append(prefix)
                upper = _prefix_successor(prefix)
                if upper is not None:
                    sql += " AND doc_key < ?"
                    params.append(upper)
        if cursor is not None:
            sql += " AND doc_key > ?"
            params.append(_decode_ref_cursor(cursor))
        sql += " ORDER BY doc_key LIMIT ?"
        params.append(limit + 1)
        with self._storage.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {"key": r["doc_key"], "chars": r["chars"] or 0,
             "metadata": loads(r["metadata"]), "updated_at": r["updated_at"]}
            for r in rows
        ]
        next_cursor = _encode_ref_cursor(rows[-1]["doc_key"]) if more and rows else None
        return {"items": items, "next_cursor": next_cursor}

    @_track_op("delete_reference")
    def delete_reference(self, key: str) -> bool:
        """Hard-delete a REFERENCE-tier document (like ``delete_entity``; there
        is no archive for references). Returns True when a row was removed,
        False when the key did not exist in this tenant. Tenant scoped by the
        ``(tenant_id, doc_key)`` primary key; the FTS index follows through
        the existing delete trigger. In the same transaction: every edge
        touching the document (either direction) is deleted (v6), as in
        delete_entity, and an accepted skill proposal whose
        ``accepted_doc_key`` is this key is marked ``superseded`` (its
        ``accepted_doc_key`` cleared, the key kept in ``review_note``), so no
        proposal points at a document that no longer exists. Use
        ``delete_reference_details`` for the counts."""
        return self._delete_reference(key)["deleted"]

    @_track_op("delete_reference")
    def delete_reference_details(self, key: str) -> dict[str, Any]:
        """``delete_reference`` with the counts: ``{"deleted": bool,
        "edges_removed": int, "proposals_superseded": int}``."""
        return self._delete_reference(key)

    def _delete_reference(self, key: str) -> dict[str, Any]:
        validate_identifier(key, field_name="key")
        edges = proposals = 0
        with self._storage.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM reference_documents WHERE tenant_id = ? AND doc_key = ?",
                (self._tenant_id, key),
            )
            deleted = cur.rowcount > 0
            if deleted:
                edges = self._cascade_node_links(conn, "reference", "", key)
                # V1 panel fix (LARCH 5): an accepted proposal must not keep
                # pointing at a document that is gone. CORE-3: tenant clause.
                proposals = conn.execute(
                    "UPDATE skill_proposals SET status = 'superseded', "
                    "accepted_doc_key = NULL, "
                    "review_note = trim(coalesce(review_note, '') || ' [document ' || ? "
                    "|| ' deleted ' || strftime('%Y-%m-%dT%H:%M:%fZ', 'now') || ']') "
                    "WHERE tenant_id = ? AND status = 'accepted' AND accepted_doc_key = ?",
                    (key, self._tenant_id, key),
                ).rowcount or 0
        return {"deleted": deleted, "edges_removed": int(edges or 0),
                "proposals_superseded": int(proposals)}

    # ------------------------------------------------------------------
    # Relations (v6, spec tiered-retrieval 2.B): a node graph across tiers
    # ------------------------------------------------------------------
    # Nodes are NodeRef dicts: {"tier": "entity", "category": c, "key": name},
    # {"tier": "reference", "key": doc_key}, {"tier": "state", "key": key}.
    # Build them with entity_ref / reference_ref / state_ref. CORE-3: every
    # statement in _links carries this client's tenant; endpoint existence is
    # checked inside it, so naming another tenant's key cannot create an edge.

    def _cascade_node_links(self, conn: sqlite3.Connection, tier: str, category: str | None,
                            key: str) -> int:
        """Hard-delete cascade: delete every edge touching the node, in the
        caller's open transaction. delete_entity and delete_reference call it.
        Never call it from archive_entity."""
        return _links.cascade_node(conn, self._tenant_id, (tier, category or "", key))

    def _write_node_links(self, conn: sqlite3.Connection, src: tuple[str, str, str],
                          specs: list) -> list[dict[str, Any]]:
        """MERGE POINT (links= on a node write): stage the edges of an already
        staged node in the caller's open transaction (strict endpoints)."""
        return _links.write_specs(conn, self._tenant_id, src, specs)

    @_track_op("link")
    def link(self, src: dict[str, Any], dst: dict[str, Any], relation: str, *,
             metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create (or upsert the metadata of) a typed edge ``src --relation--> dst``.

        Idempotent on (src, dst, relation). Both endpoints MUST exist in this
        tenant (entities: the active table; reference/state: their tables), else
        NotFoundError naming the missing side. ``relation`` is case-folded and
        at most 64 chars; ``metadata`` is an object of at most 4 KB (None keeps
        any existing metadata). Self-loops are allowed. Edge bytes count toward
        the free-tier cap. Both entity endpoints are (re-)stamped with their
        live row ids, which clears ``stale``. Returns ``{"id", "created",
        "changed"}``; ``changed`` is True only when an existing edge's metadata
        or stamps differed (an identical re-link is a no-op and is not counted
        as a write)."""
        s = _links.norm_node(src, side="from")
        d = _links.norm_node(dst, side="to")
        rel = _links.norm_relation(relation)
        meta = _links.norm_metadata(metadata)
        self._cap_gate.check(proposed_delta_bytes=_links.edge_bytes(s, d, rel, meta))
        with self._storage.transaction() as conn:
            _links.require_exists(conn, self._tenant_id, s, "source (from)")
            _links.require_exists(conn, self._tenant_id, d, "target (to)")
            res = _links.upsert_edge(conn, self._tenant_id, s, d, rel, meta)
            self._verify_committed_size(conn)
        return res

    @_track_op("unlink")
    def unlink(self, src: dict[str, Any], dst: dict[str, Any], relation: str | None = None) -> int:
        """Remove the edge(s) from ``src`` to ``dst``: one relation, or every
        relation between the two when ``relation`` is None. Endpoints need not
        exist (an archived endpoint's edges can be removed). Returns rows removed."""
        s = _links.norm_node(src, side="from")
        d = _links.norm_node(dst, side="to")
        rel = _links.norm_relation(relation) if relation is not None else None
        with self._storage.transaction() as conn:
            return _links.delete_edges(conn, self._tenant_id, s, d, rel)

    @_track_op("neighbors")
    def neighbors(self, node: dict[str, Any], *, relation: str | list[str] | None = None,
                  direction: str = "both", tier: str | None = None,
                  category: str | None = None, limit: int = 50, cursor: str | None = None,
                  preview: bool = False) -> dict[str, Any]:
        """The edges of ``node``, paginated.

        Filters: ``relation`` (one or a list), ``direction`` (out | in | both),
        ``tier`` / ``category`` of the OTHER endpoint. Keyset pagination on
        (created_at, id); ``limit`` clamped to [1, 200]; pass ``next_cursor``
        back as ``cursor``. A self-loop is listed once (as "out") under "both".
        Each edge's ``node`` is a NodeRef plus ``exists`` / ``status`` /
        ``updated_at``; an archived entity endpoint keeps ``tier: "entity"``
        (so the ref stays reusable) with ``exists: false, archived: true,
        status: "archived", archived_at``. ``preview=True`` adds up to 200 chars
        of the other node's body. Returns ``{"node", "edges", "degree":
        {"out", "in"}, "next_cursor"}``. ``node`` is the queried node with its
        own ``exists`` / ``archived`` / ``status`` (a mistyped name has
        ``exists: false, archived: false``). ``degree`` counts a self-loop
        once, as out. On a store whose edges carry endpoint identity stamps,
        each edge has ``stale`` (an entity endpoint's stamp differs from the
        live entity of that name)."""
        n = _links.norm_node(node)
        if direction not in _links.DIRECTIONS:
            raise ValidationError("direction must be one of out, in, both")
        if tier is not None and tier not in _links.TIERS:
            raise ValidationError("tier must be one of entity, reference, state")
        if category is not None:
            validate_identifier(category, field_name="category")
        rels = _links._norm_relations(relation)
        try:
            lim = int(limit)
        except (TypeError, ValueError) as e:
            raise ValidationError("limit must be an integer") from e
        lim = min(max(lim, 1), _links.NEIGHBORS_LIMIT_MAX)
        cur = _links.decode_cursor(cursor)
        with self._storage.connection() as conn:
            return _links.neighbors(conn, self._tenant_id, n, relations=rels,
                                    direction=direction, tier=tier, category=category,
                                    limit=lim, cursor=cur, preview=bool(preview))

    @_track_op("links_of")
    def links_of(self, nodes: list[dict[str, Any]], *, per_node: int = 5) -> dict[tuple, dict]:
        """Batch edge summary for up to 200 nodes (search-hit decoration, 2.C):
        ``{(tier, category_or_None, key): {"degree": {"out", "in"}, "by_relation":
        {rel: n}, "sample": [{"relation", "direction", "node"}][:per_node],
        "hub"?: True}}``. One sample edge per (direction, relation), largest
        group first; index-only, O(degree), a constant number of statements
        whatever the number of nodes. Self-loops count once, as out."""
        if not isinstance(nodes, (list, tuple)):
            raise ValidationError("nodes must be a list of NodeRef objects")
        if len(nodes) > _links.LINKS_OF_MAX_NODES:
            raise ValidationError(f"links_of takes at most {_links.LINKS_OF_MAX_NODES} nodes")
        normed = [_links.norm_node(x, side=f"nodes[{i}]") for i, x in enumerate(nodes)]
        try:
            per = int(per_node)
        except (TypeError, ValueError) as e:
            raise ValidationError("per_node must be an integer") from e
        per = min(max(per, 0), _links.LINKS_OF_PER_NODE_MAX)
        with self._storage.connection() as conn:
            raw = _links.links_of(conn, self._tenant_id, normed, per)
        return {(t, c if t == "entity" else None, k): v for (t, c, k), v in raw.items()}

    @_track_op("links_of")
    def doc_neighborhood(self, category: str, name: str, *, small_hub_max: int = 60,
                         sibling_cap: int = 60) -> dict[str, Any] | None:
        """The owning hubs and small-hub siblings of one
        document entity (``_links.doc_neighborhood``); None when it has no
        source_of owner."""
        with self._storage.connection() as conn:
            return _links.doc_neighborhood(conn, self._tenant_id, ("entity", category, name),
                                           small_hub_max=small_hub_max, sibling_cap=sibling_cap)

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------
    @_track_op("archive_entity")
    def archive_entity(self, category: str, name: str, reason: str | None = None) -> dict[str, Any]:
        """Move an entity to the archive table and delete from the active set.

        T1-3 fix: previously this bypassed the cap-gate. A free user at
        1.9 MB could archive their largest entities (body copied into
        archived_entities, doubling footprint temporarily before the
        DELETE lands) to keep writing past the 5 MB cap. Now gated on
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
    # TierGateError for free-tier callers. The documented surface is this
    # gated API.

    @_track_op("learner")
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

    @_track_op("learn")
    def learn(self, **kwargs: Any):
        """Convenience: construct a default Learner and run one pass.
        Returns a LearningRunReport. Paid-tier only."""
        return self.learner(**kwargs).run()

    @_track_op("list_skill_proposals")
    def list_skill_proposals(
        self, *, status: str = "pending", limit: int = 50,
    ) -> list[Any]:
        """Paid-tier only."""
        return self.learner().list_proposals(status=status, limit=limit)

    @_track_op("accept_skill_proposal")
    def accept_skill_proposal(
        self, proposal_id: str, *, note: str | None = None,
    ) -> dict[str, Any]:
        """Paid-tier only."""
        return self.learner().accept_proposal(proposal_id, note=note)

    @_track_op("reject_skill_proposal")
    def reject_skill_proposal(
        self, proposal_id: str, *, note: str | None = None,
    ) -> dict[str, Any]:
        """Paid-tier only."""
        return self.learner().reject_proposal(proposal_id, note=note)

    @_track_op("lint")
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
            cap = _tiers.soft_cap(self._effective_tier())
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
        # CAP-1: WAL-inclusive sizing so the "% of cap" prompt matches what the
        # cap gate actually enforces (the main file alone under-reports during
        # write bursts).
        db_size = db_size_bytes(self._storage.db_path)
        # Shown-tier fix: the tier of the current pass, not the sign-in-time hint.
        tier = self.tier_for_display()
        cap = _tiers.soft_cap(tier)
        # Paid tier → no cap
        if cap is None:
            return {
                "tier": tier,
                "db_size_bytes": db_size,
                "soft_cap_bytes": None,
                "pct_used": None,
                "uncapped": True,
            }
        return {
            "tier": tier,
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
                        category: str | None = None) -> SearchResults:
        """Full-text search over entity name + category + body via FTS5.

        Returns warm-tier entity rows only. For cross-tier search (entities +
        state + reference + journal in one call), use ``search()``.

        THE VERDICT CONTRACT (stage 3, 2026-08-31): this is the third and last
        search entry point in the SDK, and it leaves through the same single
        ``stamp()`` as the other two. A ``verdicts.SearchResults`` (a ``list``
        subclass — every existing caller is byte-for-byte unaffected) carrying
        ``OK`` or ``NO_MATCH``. Like ``search()`` it is a policy-free primitive:
        no abstention, no relevance gate, so no other cause is reachable, and
        the ``EMPTY_STORE`` probe is left to whichever surface reports the zero
        (``verdicts.refine_zero``).

        Query is sanitized as a single FTS5 phrase: column-filter syntax
        (``name:foo``) and unclosed quotes can't escape into the parser.
        Set ``prefix=True`` for prefix matching on the final token.

        Pass ``category="<name>"`` to anchor the search to a single entity
        category (exact match); this removes topical bleed across categories on
        multi-entity workloads (tester report). Omit to search
        all categories.

        Returns: list of entity rows. Each row is a dict with keys
            id, tenant_id, category, name, status, body, created_at, updated_at
            (body is JSON-deserialized).

        Raises: StorageError on backend failure; empty list on empty / invalid query.
        """
        limit = _clamp_limit(limit)  # CORE-5: negative=unbounded; huge=full-scan. Clamp.
        match_q = _sanitize_fts5_query(query, prefix=prefix)
        if not match_q:
            # An empty / invalid query is a zero like any other, and it names a
            # cause like any other. `tokens_total=0` is what lets explain() say
            # "the query carried no searchable terms" instead of guessing.
            return stamp([], no_match_verdict(tokens_total=0))
        # external-content FTS5: join by rowid back to base table.
        # _fts_query handles classification (v0.4.0 external test) + corruption
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
                # D2: name-weighted bm25 (NAME_WEIGHT), query-time rank config.
                "AND rank MATCH '" + _entity_rank_cfg() + "' "
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
        return stamp(ents, ok_verdict())

    @_track_op("search")
    def search(self, query: str, *, limit: int = 20, prefix: bool = False,
               tiers: tuple[str, ...] | None = None) -> SearchResults:
        """Cross-tier search that always says why it returned what it returned.

        THE VERDICT CONTRACT (stage 3, 2026-08-31). Thin wrapper over
        ``_search_rows`` — the unchanged search body, ladder and all — whose only
        job is to be the SINGLE EXIT: whatever the ladder inside returns leaves
        through one ``stamp()`` and arrives carrying a ``verdict``.

        The return is a ``verdicts.SearchResults``: a ``list`` subclass, so every
        existing caller (``len``, iteration, indexing, ``== []``,
        ``json.dumps``, ``isinstance(x, list)``) is byte-for-byte unaffected,
        with ``.verdict`` attached. Only two causes are reachable at this level,
        because ``search()`` is a policy-free primitive with no abstention and no
        relevance gate: ``OK`` when rows came back, ``NO_MATCH`` when they did
        not. A user-facing surface (the CLI, the MCP tool) may pay one probe via
        ``verdicts.refine_zero`` to upgrade that ``NO_MATCH`` to ``EMPTY_STORE``.
        The probe is deliberately NOT taken here: ``multi_record_search`` calls
        this method once per query token, and a COUNT per token would be a real
        per-query cost for an explanation only the outermost caller needs.

        Which rows come back is unchanged. See ``_search_rows`` below for the
        search behaviour and its history.
        """
        rows = self._search_rows(query, limit=limit, prefix=prefix, tiers=tiers)
        return stamp(rows, ok_verdict())

    def _search_rows(self, query: str, *, limit: int = 20, prefix: bool = False,
                     tiers: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Cross-tier search with a zero-hit paraphrase fallback.

        Runs the strict AND + proximity search first (``_search_strict``,
        unchanged behavior). ONLY when that returns nothing and the query is a
        multi-word non-prefix query does it retry with relaxed variants
        (stopwords stripped, then the rarest token). This is strictly additive: a
        non-empty strict result is returned untouched, so ranking and recall of
        existing hits cannot regress, and single-token / prefix queries (the
        multi_record path) never trigger the fallback. (paraphrase recall, beta
        beta report 2026-06-14: NL queries miss under strict token-AND.)

        LANG-CORE-STRIP (operator directive, 2026-08-30): the 0.6.0/0.6.1/0.7.0
        QUERY-TIME rescue layers that used to sit here were removed and this body
        reverted to its 0.5.0 form. Removed: the F2 unconditional shadow append
        (the shadow is once again a zero-hit-only fallback), the D2L coverage-
        gated stem rescue and its selectivity-ordered probe ladder (N3/N3'), and
        the N2 relaxed-single holdback/backfill that existed only to give that
        rescue headroom. search() is now exactly: strict head, relaxed variants
        on zero, zero-hit shadow fallback, nothing else.

        What is deliberately NOT removed: shadow.py in full (the folded-trigram
        shadow table, its triggers, backfill and diacritic folding are the 0.5.0
        foundation and the delivery vehicle for the stage-2 write-time
        normalization), and multi_record.py in full (the abstention side — N4
        any-df function-word drop, N5 negation abstention, the df==0 abstention
        and the diagnostics channel — is not part of this removal).

        Rationale: four external evaluation cycles accreted these layers at
        QUERY time, each one compensating for the previous one's side effects
        (F2's unconditional append polluted df, N2 held rows back to make room
        for D2L, N3 reordered D2L's probes, N3' removed its early stop and cost
        18 junk rows on a single query). The fix belongs at WRITE time. This
        strip restores a clean base to build that on; it is a deliberate,
        measured recall regression in Polish, not a bug.
        """
        hits = self._search_strict(query, limit=limit, prefix=prefix, tiers=tiers)
        if prefix:
            return hits
        if hits:
            # THE NON-EMPTY-HEAD APPEND. The strict head is returned first and
            # untouched, always; the shadow may only extend its tail, under the
            # decisive guard, and only when it has something the head lacks.
            #
            # Two distinct recalls ride on this, and they were closed at different
            # times for different reasons.
            #
            # TWIN MASKING (2026-08-07 Finding 2). A single-token query can be
            # SATISFIED by a row in the other language and thereby hide the row in
            # the query's own: 'packshot' porter-matches the English 'packshots'
            # row, the head is non-empty, and the Polish 'Packshoty produktów' row
            # is never reached. A whole-query zero gate structurally cannot see
            # this. It is also the shape multi_record's df probes take, so this is
            # what makes df truthful for inflected forms.
            #
            # LONG NATURAL-LANGUAGE RECALL (LongMemEval parity, 2026-08-31). The
            # 0.7.0 F2 append also served real recall on MULTI-WORD queries with a
            # non-empty head, and removing it wholesale made this branch retrieve a
            # strict SUBSET of 0.7.0 on that workload: 9 of 100 questions lost
            # answer-bearing context and 2 lost it entirely, because a long
            # question's strict AND head is often one weak row and everything that
            # actually answered it arrived in the appended tail. F2's diseases came
            # from the append being UNGUARDED, not from the append existing, so the
            # append is back for multi-token queries with the guard in front of it.
            #
            # The single exception is a single token the ending rule left alone
            # ('stock', 'form', 'k8'): those have no inflected form to recover and
            # no second term to corroborate, so they get no probe at all, which is
            # what preserves the precision the strip bought.
            if not _unshortened_single_token(query):
                hits = self._append_shadow(hits, query, limit=limit, tiers=tiers,
                                           decisive_only=True)
            return hits
        # v0.5.0 multi-language search (spec §4.3): the folded-trigram shadow,
        # reached ONLY when the strict pass produced nothing, so a non-empty
        # STRICT result is never reordered or dropped. prefix queries already
        # returned above (prefix intent != substring fallback). The shadow gives
        # substring semantics (matches inside an unbroken CJK/Thai/Bantu/compound
        # token) + a folded copy for the non-decomposable ł/ß/ø/... class, in any
        # language. Since v0.8.0 it runs the NORMALIZED pass: the same normalizer
        # the write side used, inflected tokens truncated to a stem, rows ranked
        # by idf-weighted coverage of the query's content terms.
        #
        # ORDER: the relaxed ladder runs FIRST and the shadow last, exactly as in
        # 0.5.0. An earlier v0.8.0 revision put the shadow first and that was
        # wrong (adversarial review 2026-08-30, finding 1). The relaxed ladder
        # searches with FTS5 PORTER, which reaches 'story' from 'stories'; the
        # shadow only does substring on a fixed-length truncation, and 'stori' is
        # not a substring of 'story' but IS one of 'historic'. Shadow-first
        # therefore answered with an unrelated row and the ladder never ran, so
        # the correct row was LOST — the same shape for entries/centrifuge,
        # queries/querist, and for every digit-bearing identifier, which is never
        # shortened and so contributes nothing to the shadow while CORE-11 exists
        # precisely to give it last-resort recall.
        # How the two rescues share the space below an empty strict head:
        #
        #   stage-1 head (the stopword-stripped CONJUNCTION) wins outright. It
        #     still required every content token, so it answered the question,
        #     and it is a PORTER answer, which reaches inflections the shadow's
        #     fixed-length truncation cannot ('stories' -> 'story').
        #   stage-2 head (ONE token, last resort) keeps its rows and its order,
        #     and the shadow is APPENDED after it under the decisive guard. When
        #     the head already fills the caller's limit there is no room, so the
        #     head is returned untouched. That is 0.5.0 semantics and it is all
        #     that happens here.
        #
        # NOTHING OVERRIDES THE HEAD. Two revisions of this method carried an
        # exception that discarded a last-resort head which filled the limit, on
        # the theory that saturation meant the token had matched indiscriminately.
        # Both discriminators were refuted by a fresh case that lost correct rows:
        # saturation alone killed 'quarterly reconciliation' (20 reconciliation
        # rows replaced by one quartermaster row), and saturation-plus-degraded
        # killed 'quarterly audits' the same way, because the ladder orders by
        # raw token LENGTH, a rarity proxy and not a relevance one, so degrading
        # onto a shorter token is no evidence that the token is wrong. The
        # exception is deleted rather than narrowed a third time.
        #
        # The accepted cost is one query shape: 'termin rozpatrzenia reklamacji'
        # relaxes to 'termin', which sweeps up the boilerplate phrase in every
        # stored note and fills the cap with it, so that query is now no better
        # than released 0.7.0. Deliberate residual, recorded in stage2-report.md
        # §5 with the acceptance amendment. Ranking a weak head below a better
        # answer is scoring work, not a query-time special case.
        relaxed_hits: list[dict[str, Any]] = []
        for relaxed, _last_resort in _relaxed_query_variants(query):
            found = self._search_strict(relaxed, limit=limit, prefix=False,
                                        tiers=tiers)
            if found:
                relaxed_hits = found
                break
        if relaxed_hits:
            # Either stage's head keeps its rows and its order, and the shadow is
            # APPENDED after it under the guard. Until 2026-08-31 a stage-1
            # conjunction head returned outright with no append, and the
            # LongMemEval parity run measured what that costs: on 'How much did I
            # earn at the Downtown Farmers Market on my most recent visit?' the
            # conjunction answers with one raw session and the row that states the
            # figure was one guarded probe away. A head is a reason to rank the
            # shadow below it, not a reason to refuse to look.
            return self._append_shadow(relaxed_hits, query, limit=limit,
                                       tiers=tiers, decisive_only=True)
        return self._shadow_fallback(query, limit=limit, tiers=tiers,
                                     normalize=True)

    def _append_shadow(self, hits: list[dict[str, Any]], query: str, *, limit: int,
                       tiers: tuple[str, ...] | None,
                       decisive_only: bool = False) -> list[dict[str, Any]]:
        """Append normalized shadow rows after an existing head, de-duped, capped.

        Never reorders, never drops, never re-ranks the head — the head list is
        returned unchanged when the shadow adds nothing, and the shadow's own rank
        scale stays positional-only (see shadow._shape_hit)."""
        if len(hits) >= limit:
            return hits
        extra = self._shadow_fallback(query, limit=limit, tiers=tiers,
                                      normalize=True, decisive_only=decisive_only)
        if not extra:
            return hits
        seen = {(h.get("tier"), h.get("key"), h.get("category")) for h in hits}
        out = list(hits)
        # The journal cap is a property of the RESULT, not of one pass. Both
        # _search_strict and the shadow apply max(1, limit//4) to their own rows;
        # without this the two caps stack and a contentless journal can retake
        # the result the cap exists to protect (test_journal_does_not_dominate_
        # search, 2026-06-02). Count what the head already spent.
        journal_cap = max(1, limit // 4)
        journal_used = sum(1 for h in hits if h.get("tier") == "journal")
        for h in extra:
            ident = (h.get("tier"), h.get("key"), h.get("category"))
            if ident in seen:
                continue
            if h.get("tier") == "journal":
                if journal_used >= journal_cap:
                    continue
                journal_used += 1
            seen.add(ident)
            out.append(h)
            if len(out) >= limit:
                break
        return out

    def _shadow_fallback(self, query: str, *, limit: int = 20,
                         tiers: tuple[str, ...] | None = None,
                         normalize: bool = False,
                         decisive_only: bool = False) -> list[dict[str, Any]]:
        """Delegate a zero-hit search to the folded-trigram shadow (shadow.py).

        A no-op ``[]`` when the shadow table is absent (a pre-migration or
        read-only DB), and the shadow's own execution is error-contained
        (OperationalError/DatabaseError -> ``[]`` + heal), so a broken or missing
        shadow can never take down the primary search path. Any unexpected error
        here is swallowed to ``[]`` for the same reason."""
        # CORE-5: honor the same limit clamp as the primary path so the fallback
        # can never broaden a negative/huge limit into SQLite's unbounded scan.
        limit = _clamp_limit(limit)
        if not limit:
            return []
        try:
            from .shadow import shadow_search
            with self._storage.connection() as conn:
                return shadow_search(conn, self._tenant_id, query,
                                     limit=limit, tiers=tiers,
                                     normalize=normalize, decisive_only=decisive_only)
        except Exception:
            return []

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
            # v0.4.0 (external-test YELLOW finding): per-tier OperationalError handling
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
                    "       snippet(entities_fts, 2, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
                    "       rank "
                    "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
                    "AND rank MATCH '" + _entity_rank_cfg() + "' "  # D2 column weights
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
                    "       snippet(state_documents_fts, 1, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
                    "       rank "
                    "FROM state_documents_fts f JOIN state_documents s "
                    "  ON s.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE state_documents_fts MATCH ? AND f.tenant_id = ? "
                    "AND rank MATCH '" + _keyed_rank_cfg() + "' "  # D2 column weights
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
                    "       snippet(reference_documents_fts, 1, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
                    "       rank "
                    "FROM reference_documents_fts f JOIN reference_documents d "
                    "  ON d.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE reference_documents_fts MATCH ? AND f.tenant_id = ? "
                    "AND rank MATCH '" + _keyed_rank_cfg() + "' "  # D2 column weights
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
                    "       snippet(journal_events_fts, 1, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
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
        # scale (cross-tier rank comparability, tester report). 2.C (R3): the
        # curated tiers also sort before reference documents, which sort before
        # the journal (_TIER_RANK, same values as multi_record._TIER_PRIORITY).
        _tier_rank = _TIER_RANK
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
    # Stage-1 recall primitives for multi_record_search (2.D, 2026-10-22)
    # ------------------------------------------------------------------
    # Why these exist: multi_record_search used to call ``search(t, limit=200)``
    # once per query token, which JSON-decoded and snippeted every hit body only
    # to read (tier, key, category, rank) from it, and which capped df at 200.
    # At 42k rows that cap made IDF identical for every common token and pushed
    # gold rows out of coverage (spec 1.4). The fix: the same four SELECTs minus
    # body/snippet (ids only), the true per-tier COUNT for df, and bodies loaded
    # once for the final rows. Every statement keeps the CORE-3 tenant clause and
    # the same ``_fts_query`` containment as ``_search_strict``.

    def _strict_id_rows(self, conn: sqlite3.Connection, match_q: str,
                        allowed: set[str], limit: int
                        ) -> tuple[list[dict[str, Any]], dict[str, bool]]:
        """``_search_strict``'s four SELECTs minus body and snippet.

        Same LIMITs (journal at ``max(1, limit // 4)``), same rank config, same
        concatenation order (entity, state, reference, journal) so the stable
        sort in ``_candidate_ids`` breaks ties exactly as ``_search_strict``
        does for a single-token query. Returns the rows and, per tier, whether
        the tier's LIMIT bound (only those tiers need a COUNT for true df)."""
        rows: list[dict[str, Any]] = []
        saturated: dict[str, bool] = {}
        if "entity" in allowed:
            got = _fts_query(
                conn,
                "SELECT f.rowid AS rid, e.name AS key, e.category, rank "
                "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
                # CORE-3 lock: tenant boundary — do not remove.
                "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
                "AND rank MATCH '" + _entity_rank_cfg() + "' "
                "ORDER BY rank LIMIT ?",
                (match_q, self._tenant_id, limit),
                "entities_fts",
            )
            saturated["entity"] = len(got) >= limit
            rows += [{"tier": "entity", "key": r["key"], "category": r["category"],
                      "rank": r["rank"], "_rid": r["rid"]} for r in got]
        if "state" in allowed:
            got = _fts_query(
                conn,
                "SELECT f.rowid AS rid, s.document_key AS key, rank "
                "FROM state_documents_fts f JOIN state_documents s "
                "  ON s.rowid = f.rowid "
                # CORE-3 lock: tenant boundary — do not remove.
                "WHERE state_documents_fts MATCH ? AND f.tenant_id = ? "
                "AND rank MATCH '" + _keyed_rank_cfg() + "' "
                "ORDER BY rank LIMIT ?",
                (match_q, self._tenant_id, limit),
                "state_documents_fts",
            )
            saturated["state"] = len(got) >= limit
            rows += [{"tier": "state", "key": r["key"], "category": None,
                      "rank": r["rank"], "_rid": r["rid"]} for r in got]
        if "reference" in allowed:
            got = _fts_query(
                conn,
                "SELECT f.rowid AS rid, d.doc_key AS key, rank "
                "FROM reference_documents_fts f JOIN reference_documents d "
                "  ON d.rowid = f.rowid "
                # CORE-3 lock: tenant boundary — do not remove.
                "WHERE reference_documents_fts MATCH ? AND f.tenant_id = ? "
                "AND rank MATCH '" + _keyed_rank_cfg() + "' "
                "ORDER BY rank LIMIT ?",
                (match_q, self._tenant_id, limit),
                "reference_documents_fts",
            )
            saturated["reference"] = len(got) >= limit
            rows += [{"tier": "reference", "key": r["key"], "category": None,
                      "rank": r["rank"], "_rid": r["rid"]} for r in got]
        if "journal" in allowed:
            journal_limit = max(1, limit // 4) if limit > 0 else 0
            got = _fts_query(
                conn,
                "SELECT j.id AS key, f.rank AS rank "
                "FROM journal_events_fts f JOIN journal_events j "
                "  ON j.id = f.event_id "
                # CORE-3 lock: tenant boundary — do not remove.
                "WHERE journal_events_fts MATCH ? AND f.tenant_id = ? "
                "ORDER BY f.rank LIMIT ?",
                (match_q, self._tenant_id, journal_limit),
                "journal_events_fts",
            )
            saturated["journal"] = len(got) >= journal_limit
            rows += [{"tier": "journal", "key": r["key"], "category": None,
                      "rank": r["rank"], "_rid": r["key"]} for r in got]
        return rows, saturated

    def _tier_count(self, conn: sqlite3.Connection, tier: str, match_q: str) -> int:
        """True document frequency of ``match_q`` in one tier (D1).

        ``count(*)`` with MATCH + tenant: an FTS5 index walk, no row decode
        (measured 16 ms for 15 tokens at 70k rows, df up to 38k)."""
        table = {"entity": "entities_fts", "state": "state_documents_fts",
                 "reference": "reference_documents_fts",
                 "journal": "journal_events_fts"}[tier]
        got = _fts_query(
            conn,
            # CORE-3 lock: tenant boundary — do not remove.
            f"SELECT count(*) AS n FROM {table} f "
            f"WHERE {table} MATCH ? AND f.tenant_id = ?",
            (match_q, self._tenant_id),
            table,
        )
        return int(got[0]["n"]) if got else 0

    def _candidate_ids(self, query: str, *, limit: int,
                       tiers: tuple[str, ...] | None = None
                       ) -> tuple[list[dict[str, Any]], int]:
        """Ids-only strict head for ONE token, plus its true strict df.

        Returns ``(rows, strict_df)``: ``rows`` is exactly the (tier, key,
        category, rank) sequence ``_search_strict(query, limit=limit)`` returns
        for a single-token query, with no body and no snippet; ``strict_df`` is
        the summed per-tier COUNT (a tier is only COUNTed when its LIMIT bound;
        otherwise its count is the rows it returned)."""
        limit = _clamp_limit(limit)
        match_q = _sanitize_fts5_query(query)
        if not match_q or not limit:
            return [], 0
        allowed = set(tiers) if tiers else {"entity", "state", "reference", "journal"}
        with self._storage.connection() as conn:
            rows, saturated = self._strict_id_rows(conn, match_q, allowed, limit)
            df = 0
            for tier in ("entity", "state", "reference", "journal"):
                if tier not in saturated:
                    continue
                if saturated[tier]:
                    df += self._tier_count(conn, tier, match_q)
                else:
                    df += sum(1 for r in rows if r["tier"] == tier)
        _tier_rank = _TIER_RANK   # the same tie-break as _search_strict
        rows.sort(key=lambda h: (h["rank"], _tier_rank.get(h["tier"], 0)))
        return rows[:limit], df

    @_track_op("search")  # one tracked search per token, exactly as 0.9.0
    def _recall_token(self, token: str, *, limit: int,
                      tiers: tuple[str, ...] | None = None
                      ) -> tuple[list[dict[str, Any]], int]:
        """Stage-1 recall for one multi_record token: ``(candidates, df)``.

        Mirrors ``_search_rows(token, limit=limit)`` row for row: the ids-only
        strict head, then the same guarded shadow append (non-empty head, not an
        unshortened single token) or, on an empty head, the same shadow
        fallback. Strict rows carry ``_rid`` and no body (hydrated later, for
        the final rows only); shadow rows come back hydrated from shadow.py and
        pass through unchanged.

        df is TRUE df: the strict COUNT plus the shadow rows actually appended
        (the append is what makes df truthful for inflected forms; see
        ``_search_rows``). Wherever no per-token cap binds this equals the old
        ``len(search(token, limit=200))`` exactly.

        A token the proximity re-ranker would treat as multi-word (two or more
        match tokens) needs bodies to order its head and may take the relaxed
        ladder, so it goes through ``_search_rows`` unchanged (df = rows).

        ``tiers`` (2.C ``scope="graph"``) restricts every step, head, df COUNT
        and shadow alike, to those tiers; None is all four, as before."""
        if len(_match_tokens(token)) >= 2:
            hits = self._search_rows(token, limit=limit, tiers=tiers)
            return hits, len(hits)
        head, strict_df = self._candidate_ids(token, limit=limit, tiers=tiers)
        # The shadow keeps its 0.9.0 envelope at every depth: it is consulted
        # only below 200 strict rows and returns at most 200 in total. Scaling
        # it with K would decode bodies in stage 1 (shadow rows come back
        # shaped), which D1 forbids: measured on a 22k-row store, letting the
        # append run at K=1,108 cost 1.5 s of a 1.9 s query. A token with
        # >= 200 strict rows never reached the shadow in 0.9.0 either.
        shadow_limit = min(limit, _SHADOW_RECALL_LIMIT)
        if head:
            if _unshortened_single_token(token) or len(head) >= shadow_limit:
                return head, strict_df
            out = self._append_shadow(head, token, limit=shadow_limit, tiers=tiers,
                                      decisive_only=True)
            return out, strict_df + (len(out) - len(head))
        fb = self._shadow_fallback(token, limit=shadow_limit, tiers=tiers,
                                   normalize=True)
        return fb, len(fb)

    @_gate_only("search")
    def _ranked_or_rows(self, tokens: list[str], *, limit: int,
                        tiers: tuple[str, ...] | None, exclude: set[tuple],
                        exclude_categories: frozenset[str]) -> list[dict[str, Any]]:
        """One ranked OR pass over entities_fts (and reference_documents_fts when
        the scope includes it). tokens are multi_record significant tokens
        (\\w+ only), each quoted as its own phrase. Returns client.search()-shaped
        hits with match="ranked", best first, skipping `exclude` identities.

        Used only when SIBYL_MEMORY_RANKED_FALLBACK=1 (default off)."""
        limit = int(limit)
        toks = [t for t in tokens if t][:24]
        if limit <= 0 or not toks:
            return []
        match_q = " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)
        fetch = limit + len(exclude) + 50
        hits: list[dict[str, Any]] = []
        with self._storage.connection() as conn:
            if tiers is None or "entity" in tiers:
                cat_sql, cat_args = "", ()
                if exclude_categories:
                    cats = sorted(exclude_categories)
                    cat_sql = "AND e.category NOT IN (" + ",".join("?" for _ in cats) + ") "
                    cat_args = tuple(cats)
                for r in _fts_query(
                    conn,
                    "SELECT 'entity' AS tier, e.name AS key, e.category, e.body, "
                    "       e.updated_at AS ts, "
                    "       snippet(entities_fts, 2, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
                    "       rank "
                    "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE entities_fts MATCH ? AND f.tenant_id = ? "
                    "AND rank MATCH '" + _entity_rank_cfg() + "' "  # D2 column weights
                    + cat_sql +
                    "ORDER BY rank LIMIT ?",
                    (match_q, self._tenant_id) + cat_args + (fetch,),
                    "entities_fts",
                ):
                    hits.append({
                        "tier": "entity", "key": r["key"],
                        "category": r["category"],
                        "body": loads(r["body"]), "snippet": r["snip"],
                        "rank": r["rank"], "ts": r["ts"],
                    })
            if tiers is None or "reference" in tiers:
                for r in _fts_query(
                    conn,
                    "SELECT 'reference' AS tier, d.doc_key AS key, d.body, "
                    "       d.updated_at AS ts, "
                    "       snippet(reference_documents_fts, 1, '[', ']', '...', " + str(int(SNIPPET_TOKENS)) + ") AS snip, "
                    "       rank "
                    "FROM reference_documents_fts f JOIN reference_documents d "
                    "  ON d.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE reference_documents_fts MATCH ? AND f.tenant_id = ? "
                    "AND rank MATCH '" + _keyed_rank_cfg() + "' "  # D2 column weights
                    "ORDER BY rank LIMIT ?",
                    (match_q, self._tenant_id, fetch),
                    "reference_documents_fts",
                ):
                    hits.append({
                        "tier": "reference", "key": r["key"], "category": None,
                        "body": r["body"], "snippet": r["snip"],
                        "rank": r["rank"], "ts": r["ts"],
                    })
        hits.sort(key=lambda h: h["rank"])
        out: list[dict[str, Any]] = []
        for h in hits:
            if (h["tier"], h["key"], h["category"]) in exclude:
                continue
            h["match"] = "ranked"
            out.append(h)
            if len(out) >= limit:
                break
        return out

    @_gate_only("search")
    def _load_rows(self, cands: list[dict[str, Any]], *, prep_only: bool = False
                   ) -> dict[tuple[str, Any], dict[str, Any]]:
        """Batch-load stored rows for ids-only candidates, keyed (tier, _rid).

        ``prep_only=False``: ``{"body", "ts"}`` in ``_search_strict``'s hit
        shape (entity/state bodies JSON-decoded, reference raw, journal the
        four-field dict). ``prep_only=True``: ``{"prep"}``, the lowercased text
        the terminal/prep gate reads. For entity/state/journal that is
        ``json.dumps(<decoded body>).lower()``, byte-identical to the 0.9.0
        gate input. For reference rows (D4) it is ``metadata.status`` when
        present, else the first 2,000 chars of the body: long documents mention
        "draft" or "proposed" almost always, so the whole text is no PREP
        signal. The TERM side is whole-document (V1 panel fix): a head that is
        pure prep is replaced by ``""`` (not pure prep) when the full body
        matches ``_TERM_RE`` (pre-screened by ``_term_candidates``, then the
        regex, lookbehind included, on the full serialised body).
        """
        import json as _json
        by_tier: dict[str, list] = {}
        for h in cands:
            if "_rid" in h:
                by_tier.setdefault(h["tier"], []).append(h["_rid"])
        out: dict[tuple[str, Any], dict[str, Any]] = {}
        sql = _load_sql(prep_only)
        late_term: list[Any] = []   # reference rids whose head is pure prep
        with self._storage.connection() as conn:
            for tier, ids in by_tier.items():
                if prep_only:
                    ids = self._prep_candidates(conn, tier, ids)
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    q = sql[tier].format(",".join("?" * len(chunk)))
                    for r in conn.execute(q, (self._tenant_id, *chunk)).fetchall():
                        if tier == "journal":
                            body: Any = {
                                "evaluated": loads(r["evaluated"]),
                                "acted": loads(r["acted"]),
                                "forward": loads(r["forward"]),
                                "extra": loads(r["extra"]),
                            }
                        elif tier == "reference":
                            body = r["body"]
                        else:
                            body = loads(r["body"])
                        if not prep_only:
                            out[(tier, r["rid"])] = {"body": body, "ts": r["ts"]}
                            continue
                        src = body
                        if tier == "reference":
                            try:
                                meta = loads(r["metadata"]) if r["metadata"] else None
                            except (TypeError, ValueError):
                                meta = None
                            if isinstance(meta, dict) and meta.get("status") not in (None, ""):
                                src = meta["status"]
                        prep = _json.dumps(src).lower()
                        out[(tier, r["rid"])] = {"prep": prep}
                        if tier == "reference" and src is body:
                            from .multi_record import _pure_prep
                            if _pure_prep(prep):
                                late_term.append(r["rid"])
            if late_term:
                self._clear_late_terminal(conn, late_term, out)
        return out

    def _clear_late_terminal(self, conn: sqlite3.Connection, rids: list[Any],
                             out: dict[tuple[str, Any], dict[str, Any]]) -> None:
        """TERM side of the prep gate, whole-document (V1 panel fix, NOCTURNE
        EXP4): for reference rows whose head is pure prep, read the full body
        of those the FTS pre-screen says COULD hold a terminal marker, and
        clear the gate input (``""``, not pure prep) when ``_TERM_RE`` (its
        ``not `` lookbehind included) matches the whole serialised body."""
        import json as _json
        from .multi_record import _term_anywhere
        cand = self._term_candidates(conn, rids)
        ids = [i for i in rids if i in cand]
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            for r in conn.execute(
                    "SELECT rowid AS rid, body FROM reference_documents "
                    # CORE-3: tenant clause on every load (see _load_rows).
                    "WHERE +tenant_id = ? AND rowid IN ({})".format(",".join("?" * len(chunk))),
                    (self._tenant_id, *chunk)):
                if _term_anywhere(_json.dumps(r["body"]).lower()):
                    out[("reference", r["rid"])] = {"prep": ""}

    def _term_candidates(self, conn: Any, rids: list[Any]) -> set[Any]:
        """The subset of reference ``rids`` whose body COULD match ``_TERM_RE``
        anywhere: one rowid-only FTS query (``multi_record._TERM_FTS_QUERY``, a
        superset). Any FTS error returns every rid (the exact, slower path)."""
        from .multi_record import _TERM_FTS_QUERY
        try:
            marked = {r[0] for r in conn.execute(
                "SELECT f.rowid FROM reference_documents_fts f "
                "WHERE reference_documents_fts MATCH ? "
                # CORE-3 lock: tenant boundary — do not remove.
                "AND f.tenant_id = ?",
                (f"body : ({_TERM_FTS_QUERY})", self._tenant_id))}
        except sqlite3.Error:
            return set(rids)
        return {i for i in rids if i in marked}

    def _prep_candidates(self, conn: sqlite3.Connection, tier: str,
                         ids: list[Any]) -> list[Any]:
        """The subset of ``ids`` whose prep text COULD match ``_PREP_RE``.

        A superset by construction (see ``multi_record._PREP_FTS_QUERY``): one
        rowid-only FTS query for the rows carrying a prep marker token, plus,
        for reference rows, every row whose metadata may hold ``status``. The
        rest would serialise to text the prep regex cannot match, so the gate
        reads "" for them, exactly its answer on the full text. Any FTS error
        returns ``ids`` unchanged (the exact, slower path)."""
        from .multi_record import _PREP_FTS_QUERY
        table, col, key = {
            "entity": ("entities_fts", "body", "rowid"),
            "state": ("state_documents_fts", "body", "rowid"),
            "reference": ("reference_documents_fts", "body", "rowid"),
            "journal": ("journal_events_fts", "payload", "event_id"),
        }[tier]
        try:
            marked = {r[0] for r in conn.execute(
                f"SELECT f.{key} FROM {table} f WHERE {table} MATCH ? "
                # CORE-3 lock: tenant boundary — do not remove.
                "AND f.tenant_id = ?",
                (f"{col} : ({_PREP_FTS_QUERY})", self._tenant_id))}
            if tier == "reference":
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marked.update(r[0] for r in conn.execute(
                        "SELECT rowid FROM reference_documents "
                        # CORE-3: tenant clause on every load (see _load_rows).
                        "WHERE +tenant_id = ? AND rowid IN ({}) "
                        "AND metadata IS NOT NULL AND (instr(metadata, 'status') > 0 "
                        "OR instr(metadata, '\\u') > 0)".format(",".join("?" * len(chunk))),
                        (self._tenant_id, *chunk)))
        except sqlite3.Error:
            return ids
        return [i for i in ids if i in marked]

    def _snippet_for(self, conn: sqlite3.Connection, tier: str, rid: Any,
                     terms: list[str]) -> str | None:
        """A SNIPPET_TOKENS-token snippet of one row, centred on the densest
        match of ``terms`` (an OR of the query tokens this row matched)."""
        phrases = [p for p in (_sanitize_fts5_query(t) for t in terms) if p]
        if not phrases:
            return None
        match_q = " OR ".join(phrases)
        n = str(int(SNIPPET_TOKENS))
        if tier == "journal":
            table = "journal_events_fts"
            sql = ("SELECT snippet(journal_events_fts, 1, '[', ']', '...', " + n + ") AS s "
                   "FROM journal_events_fts f WHERE journal_events_fts MATCH ? "
                   # CORE-3 lock: tenant boundary — do not remove.
                   "AND f.tenant_id = ? AND f.event_id = ?")
        else:
            table, col = {"entity": ("entities_fts", 2),
                          "state": ("state_documents_fts", 1),
                          "reference": ("reference_documents_fts", 1)}[tier]
            sql = (f"SELECT snippet({table}, {col}, '[', ']', '...', " + n + ") AS s "
                   f"FROM {table} f WHERE {table} MATCH ? "
                   # CORE-3 lock: tenant boundary — do not remove.
                   "AND f.tenant_id = ? AND f.rowid = ?")
        try:
            got = _fts_query(conn, sql, (match_q, self._tenant_id, rid), table)
        except Exception:
            return None
        return got[0]["s"] if got else None

    @_gate_only("search")
    def _hydrate_hits(self, items: list[tuple[dict[str, Any], list[str]]]
                      ) -> list[dict[str, Any]]:
        """Turn final ids-only candidates into ``_search_strict``-shaped hits.

        ``items`` is ``[(candidate, snippet_terms)]`` in final order. Rows that
        already carry a body (shadow rows, multi-word-token fallback rows) pass
        through untouched. A row deleted between recall and hydration is dropped:
        it no longer exists, and returning it would invent it."""
        loaded = self._load_rows([h for h, _ in items])
        out: list[dict[str, Any]] = []
        with self._storage.connection() as conn:
            for h, terms in items:
                if "_rid" not in h:
                    out.append(h)
                    continue
                row = loaded.get((h["tier"], h["_rid"]))
                if row is None:
                    continue
                out.append({
                    "tier": h["tier"], "key": h["key"], "category": h["category"],
                    "body": row["body"],
                    "snippet": self._snippet_for(conn, h["tier"], h["_rid"], terms),
                    "rank": h["rank"], "ts": row["ts"],
                })
        return out

    # ------------------------------------------------------------------
    # Tier-aware search primitives (spec tiered-retrieval 2.C, R3)
    # ------------------------------------------------------------------
    @_gate_only("search")
    def _name_matches(self, query: str) -> list[dict[str, Any]]:
        """Stage 0 of multi_record_search: the entities this query NAMES.

        Ids-only candidates (``_rid``, no body), best first, each tagged
        ``match``:

        * ``"name"``: ``lower(name)`` equals the ASCII-folded query (index
          ``entities_tenant_lname``), or, for ``<category>:<name>``, the name
          after the prefix within that category. Up to NAME_EXACT_MAX rows,
          newest first.
        * ``"name_partial"``: up to NAME_PARTIAL_MAX further rows whose NAME
          column holds the query as a phrase (``name : "<query>"``), ordered by
          the name-weighted bm25 (NAME_WEIGHT). Only for queries of at most
          NAME_PARTIAL_MAX_TOKENS words: a question is not a name.

        Exact or phrase-on-name only: body text never qualifies a row here, so
        this is not a way around the verify gates for body text. The partial
        phrase is skipped for a single-token query shorter than 4 chars (the
        exact probe still runs: "IBM" is a name); everything is skipped for a
        query longer than an identifier. sqlite's ``lower()`` folds ASCII only, so a
        non-ASCII name matches case-sensitively in its non-ASCII letters. The
        shard case (one parent card and many rows whose names contain the
        parent's name) resolves to the parent: its name is the exact one.
        CORE-3: every statement carries this client's tenant; the equality is
        parameterised and the phrase goes through FTS5 string quoting."""
        q = (query or "").strip()
        if not q or len(q) > _IDENT_MAX_LENGTH:
            return []
        # V1 panel fix (NOCTURNE): the exact probe runs for every length (IBM,
        # AWS, Bob are names); only the partial phrase keeps the 4-char guard,
        # where a short token would phrase-match half the store's names.
        name_q, cat_q = q, None
        m = _NAME_PREFIX_RE.match(q)
        exact: list[dict[str, Any]] = []
        with self._storage.connection() as conn:
            def _exact(name: str, category: str | None) -> list[sqlite3.Row]:
                # No ORDER BY in SQL: with one, SQLite (no ANALYZE stats)
                # drove the probe through entities_updated_at (tenant_id) and
                # scanned the whole tenant (57 ms at 100k rows) instead of the
                # one-key lookup on entities_tenant_lname. The few rows that
                # share a name are ordered here instead.
                sql = _NAME_EXACT_SQL
                args: list[Any] = [self._tenant_id, _ascii_fold(name)]
                if category is not None:
                    sql += " AND lower(category) = ?"
                    args.append(_ascii_fold(category))
                sql += " LIMIT ?"
                args.append(_NAME_EXACT_SCAN)
                got = conn.execute(sql, args).fetchall()
                got.sort(key=lambda r: r["category"])                           # tie: category
                got.sort(key=lambda r: r["updated_at"] or "", reverse=True)    # newest first
                return got[:NAME_EXACT_MAX]

            rows = _exact(q, None)
            if not rows and m:
                # `person:maya patel` -> `maya patel` in category `person`. The
                # whole query is tried first: names may themselves contain ':'.
                cat_q, name_q = m.group(1).strip(), m.group(2).strip()
                rows = _exact(name_q, cat_q)
            elif rows:
                m = None   # the whole query is the name; no prefix reading
            if m is None:
                name_q, cat_q = q, None
            exact = [{"tier": "entity", "key": r["name"], "category": r["category"],
                      "rank": 0.0, "_rid": r["rid"], "match": "name"} for r in rows]
            seen = {h["_rid"] for h in exact}
            partial: list[dict[str, Any]] = []
            short = len(name_q.split()) == 1 and len(name_q) < 4
            if not short and 0 < len(_match_tokens(name_q)) <= NAME_PARTIAL_MAX_TOKENS:
                phrase = 'name : "' + name_q.replace('"', '""') + '"'
                cat_clause = " AND lower(e.category) = ?" if cat_q is not None else ""
                args2: list[Any] = [phrase, self._tenant_id]
                if cat_q is not None:
                    args2.append(_ascii_fold(cat_q))
                args2.append(len(exact) + NAME_PARTIAL_MAX)
                got = _fts_query(
                    conn,
                    "SELECT f.rowid AS rid, e.name AS key, e.category, rank "
                    "FROM entities_fts f JOIN entities e ON e.rowid = f.rowid "
                    # CORE-3 lock: tenant boundary — do not remove.
                    "WHERE entities_fts MATCH ? AND f.tenant_id = ?" + cat_clause + " "
                    "AND rank MATCH '" + _entity_rank_cfg() + "' "
                    "ORDER BY rank LIMIT ?",
                    tuple(args2),
                    "entities_fts",
                )
                ranks = {r["rid"]: r["rank"] for r in got}
                for h in exact:
                    h["rank"] = ranks.get(h["_rid"], 0.0)
                for r in got:
                    if r["rid"] in seen or len(partial) >= NAME_PARTIAL_MAX:
                        continue
                    seen.add(r["rid"])
                    partial.append({"tier": "entity", "key": r["key"],
                                    "category": r["category"], "rank": r["rank"],
                                    "_rid": r["rid"], "match": "name_partial"})
        return exact + partial

    @_gate_only("search")
    def _decorate_hits(self, hits: list[dict[str, Any]], *,
                       with_links: bool = False,
                       info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Add the 2.C hit fields to final, hydrated hits (new dicts; order kept).

        ``chars``: length of the full stored body as surfaced (reference: the
        document string; other tiers: its JSON rendering), so an agent can tell
        whether the 1,500-char body is the whole thing. ``title``:
        ``metadata.title`` for reference hits (one keyed batch read), None
        otherwise. ``links`` (only when ``with_links``): the node's edge summary
        ``{"degree", "by_relation", "sample"[:5]}`` from ONE batch statement
        over memory_links for all hits (``_links.links_of``); journal hits are
        not graph nodes and get ``None``. Never called by the raw ``search()``
        primitive, which stays link-free.

        Decoration never fails a search (panel theme F): a sqlite3.Error /
        StorageError while reading titles or edges (a missing memory_links
        index, a damaged table) leaves ``title`` / ``links`` None on every hit
        and records why in ``info["note"]``. ``info`` (optional, filled in
        place) also gets ``links_ms``, the wall time of the edge summary."""
        out = [dict(h) for h in hits]
        if not out:
            return out
        ref_keys = list(dict.fromkeys(
            h["key"] for h in out if h.get("tier") == "reference" and h.get("key")))
        titles: dict[str, str | None] = {}
        links: dict[tuple, dict[str, Any]] | None = {}
        notes: list[str] = []
        with self._storage.connection() as conn:
            try:
                for i in range(0, len(ref_keys), 500):
                    chunk = ref_keys[i:i + 500]
                    for r in conn.execute(
                            "SELECT doc_key, metadata FROM reference_documents "
                            # CORE-3: tenant clause on every load.
                            "WHERE tenant_id = ? AND doc_key IN ({})".format(
                                ",".join("?" * len(chunk))),
                            (self._tenant_id, *chunk)).fetchall():
                        titles[r["doc_key"]] = _meta_title(r["metadata"])
            except (sqlite3.Error, StorageError) as e:
                titles = {}
                notes.append(f"titles unavailable ({type(e).__name__})")
            if with_links:
                nodes = list(dict.fromkeys(
                    n for n in (_hit_node(h) for h in out) if n is not None))
                if nodes:
                    t0 = time.perf_counter()
                    try:
                        links = _links.links_of(conn, self._tenant_id,
                                                nodes[:_links.LINKS_OF_MAX_NODES],
                                                HIT_LINK_SAMPLE)
                    except (sqlite3.Error, StorageError) as e:
                        links = None
                        notes.append(f"links unavailable ({type(e).__name__}); "
                                     "hits returned without edge summaries")
                    if info is not None:
                        info["links_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        if info is not None and notes:
            info["note"] = "; ".join(notes)
        for h in out:
            if "chars" not in h:
                h["chars"] = _body_chars(h.get("body"))
            h["title"] = titles.get(h.get("key")) if h.get("tier") == "reference" else None
            if with_links:
                node = _hit_node(h)
                h["links"] = (links.get(node) if node is not None and links is not None
                              else None)
        return out

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
        # Hardening #16 (super-patch 2026-07-05): storage.logical_size_bytes
        # returns 0 on ANY internal error (it catches sqlite3.Error / TypeError /
        # IndexError and falls back to 0). A 0 here would FAIL OPEN the CAP-2
        # recheck — check_total_local(0) trivially passes, so a near-cap write
        # that should tip the DB over the cap would commit silently. But this
        # method only ever runs INSIDE a write transaction with an INSERT/UPDATE
        # already staged, so a logical size of 0 is never a legitimate post-write
        # footprint: it signals the measurement was unavailable. In that case,
        # fall back to the gate's own db_size_fn (the WAL-inclusive account-level
        # aggregate_db_size wired in __init__, which never under-counts) before
        # gating, so the cap is still enforced instead of silently bypassed.
        try:
            total = self._storage.logical_size_bytes(conn)
        except Exception as exc:  # measurement path itself blew up
            _log.warning(
                "logical_size_bytes raised during CAP-2 recheck (%s); "
                "falling back to the cap gate's db_size_fn", exc,
            )
            total = 0
        if not total:  # 0 (or None) => measurement unavailable, DO NOT fail open
            fallback = self._fallback_committed_size(cap_gate)
            if fallback is not None:
                _log.warning(
                    "CAP-2 recheck got a 0-byte in-transaction size "
                    "(measurement unavailable); enforcing the cap via the "
                    "db_size_fn fallback (%d bytes) instead of failing open.",
                    fallback,
                )
                total = fallback
            else:
                _log.warning(
                    "CAP-2 recheck got a 0-byte in-transaction size and no "
                    "usable db_size_fn fallback; proceeding with 0.",
                )
        # check_total_local is LOCAL-ONLY (no network) — it must not block on a
        # urlopen while we hold the BEGIN IMMEDIATE write lock (2026-06-25 audit
        # blocker). The pre-write check() already did any server verification.
        cap_gate.check_total_local(total)

    @staticmethod
    def _fallback_committed_size(cap_gate: Any) -> int | None:
        """Best-effort WAL-inclusive size from the cap gate's own db_size_fn.

        Hardening #16 fallback for when the in-transaction logical size is
        unavailable (returned 0 / raised). The gate is constructed with
        ``db_size_fn=lambda: aggregate_db_size(storage.db_path)`` — the same
        account-level, WAL-inclusive measurement the pre-write gate uses — so it
        is a strictly-not-under-counting substitute for the CAP-2 recheck.
        Returns a positive byte count, or None if no usable size can be obtained
        (never raises: a broken fallback must not crash the write path).
        """
        size_fn = getattr(cap_gate, "_db_size_fn", None)
        if size_fn is None:
            return None
        try:
            value = int(size_fn())
        except Exception:
            return None
        return value if value > 0 else None

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
