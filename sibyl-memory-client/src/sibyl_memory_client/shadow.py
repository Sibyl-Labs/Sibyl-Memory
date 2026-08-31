"""Folded-trigram search shadow (v0.5.0 multi-language search, spec §4.2).

A single standalone FTS5 ``trigram`` virtual table (``search_shadow``) holding a
FOLDED rendering of the searchable text across all four tiers (entity / state /
reference / journal), maintained by DB-side triggers and consulted ONLY as a
zero-hit fallback in ``MemoryClient.search()`` (client.py §4.3). The primary
``porter unicode61`` pipeline is never touched, so the fallback is strictly
additive: it fires only where today's answer is ``[]``.

Why a shadow (and why THIS shape):
  * ``trigram`` gives SUBSTRING semantics, which is the only thing that can match
    inside an unbroken indexed token — CJK scriptio-continua (``北京`` inside
    ``北京烤鸭``), Thai fragment glue, Zulu/Bantu locative compounds, German
    compounds — none of which any query-side change to ``porter unicode61`` can
    reach (a prefix ``*`` covers leading substrings only).
  * A FOLDED copy closes the non-decomposable gap (``ł ß ø æ đ ı œ þ ð``): no
    ``remove_diacritics`` setting folds these, so ``Belzyce`` cannot find a stored
    ``Bełżyce`` without an explicit fold map applied on BOTH sides. External-
    content trigram can't carry a divergent folded copy; a standalone table can.
  * ``trigram`` is built into SQLite (>= 3.34; >= 3.45 for ``remove_diacritics``);
    no native/loadable extension, so the plugin keeps shipping pure-Python wheels.

The fold map + runtime tokenizer clause are the single source of truth for both
the trigger/backfill SQL (index side) and the query side (``fold_py``); keeping
them here — not in the static ``schema.sql`` — is why the shadow DDL is generated.

Business-key linkage (NOT ``content_rowid``): shadow rows carry the base-table
business key, so a VACUUM that renumbers rowids can never desync the shadow from
its base table.

TRAP (do not reintroduce): the shadow tables are maintained with PLAIN INSERT and
PLAIN DELETE. The external-content ``INSERT INTO x(x, rowid, ...) VALUES('delete',
...)`` idiom fed by a ``SELECT`` from the shadow FAILS inside a trigger body
("SQL logic error": an FTS5 table cannot be read from within a trigger). A
standalone FTS5 table supports native ``DELETE`` — use it. And NEVER write to the
base tables with ``INSERT OR REPLACE``: with ``recursive_triggers`` off it skips
the DELETE triggers and silently desyncs the shadow. The existing write paths are
safe (set_entity does explicit INSERT-else-UPDATE; state/reference use
``ON CONFLICT ... DO UPDATE`` -> a true UPDATE fires the AU trigger).
"""
from __future__ import annotations

import json as _json
import re
import sqlite3

# ---------------------------------------------------------------------------
# Fold map — non-decomposables ONLY. Decomposable diacritics (é ñ ö ż ...) are
# the tokenizer's job (``remove_diacritics``), and fold_py deliberately does NOT
# touch them so the query side and the index side fold IDENTICALLY (both get the
# tokenizer's decomposable folding, or neither does on pre-3.45 SQLite). This map
# is applied identically by fold_sql (index side, in trigger + backfill SQL) and
# fold_py (query side).
# ---------------------------------------------------------------------------
FOLD_MAP = {
    "ł": "l",  "Ł": "l",    # ł Ł
    "ß": "ss", "ẞ": "ss",   # ß ẞ
    "ø": "o",  "Ø": "o",    # ø Ø
    "æ": "ae", "Æ": "ae",   # æ Æ
    "đ": "d",  "Đ": "d",    # đ Đ
    "ı": "i",                     # ı (dotless i; İ handled by tokenizer fold)
    "œ": "oe", "Œ": "oe",   # œ Œ
    "þ": "th", "Þ": "th",   # þ Þ
    "ð": "d",  "Ð": "d",    # ð Ð
}

SHADOW_TABLE = "search_shadow"

# The four searchable tiers this shadow mirrors.
_TIERS = ("entity", "state", "reference", "journal")

# The complete set of shadow-maintenance triggers (single source of truth,
# consumed by drop_shadow + the migration fast-path completeness check). entity/
# state/reference each get AI/AU/AD (3x3=9); journal is append-only (AI only) =
# 10 total. F1 (Fable hardening 2026-08-06): the v4 migration fast path must
# require ALL 10 to be present, not just the shadow TABLE — an out-of-band
# trigger drop would otherwise pass the table-only precondition and leave the
# shadow silently un-maintained (writes stop propagating). Mirrors how the v3
# FTS triggers self-heal on every open.
SHADOW_TRIGGER_NAMES = (
    "entities_ai_shadow", "entities_au_shadow", "entities_ad_shadow",
    "state_documents_ai_shadow", "state_documents_au_shadow",
    "state_documents_ad_shadow",
    "reference_documents_ai_shadow", "reference_documents_au_shadow",
    "reference_documents_ad_shadow",
    "journal_events_ai_shadow",
)


def fold_sql(expr: str) -> str:
    """SQL expression applying ASCII ``lower()`` + FOLD_MAP to ``expr``.

    Wraps ``lower(expr)`` in one nested ``replace()`` per FOLD_MAP entry. SQLite's
    built-in ``lower()`` folds ASCII only; the trigram tokenizer applies unicode
    case folding (and, on >= 3.45, diacritic removal) on top when it indexes the
    stored text, which is exactly the symmetry ``fold_py`` preserves on the query
    side. FOLD_MAP sources/targets are fixed ASCII/Latin-1 literals (never user
    input), so this string interpolation is injection-safe.
    """
    out = f"lower({expr})"
    for src, dst in FOLD_MAP.items():
        out = f"replace({out}, '{src}', '{dst}')"
    return out


def fold_py(text: str) -> str:
    """Query-side twin of ``fold_sql``: ASCII-lower + FOLD_MAP, nothing else.

    NEVER strips decomposable accents — that stays the tokenizer's job on BOTH
    sides (see FOLD_MAP note). ASCII case-folds; non-ASCII letters are left as-is
    for the trigram tokenizer to case/diacritic fold, keeping query and index
    folding identical.
    """
    out = text.lower() if text.isascii() else "".join(
        c.lower() if c.isascii() else c for c in text)
    for src, dst in FOLD_MAP.items():
        out = out.replace(src, dst)
    return out


# ---------------------------------------------------------------------------
# WRITE-TIME NORMALIZATION (v0.8.0 stage 2, 2026-08-30)
#
# ONE normalizer, two halves, both versioned together by NORMALIZER_VERSION:
#
#   normalize_sql / normalize_py  — the RENDERING. fold (above) + boundary
#       normalization (every punctuation/whitespace character becomes a single
#       space) + one leading and one trailing space. Applied at WRITE time by the
#       four per-tier expressions below (so triggers and backfill share it
#       verbatim) and applied identically on the query side by normalize_terms.
#       This is what makes WORD-START matching possible: the shadow stores
#       JSON-serialised bodies, where a word is very often preceded by `"` or `:`
#       rather than by a space, and the trigram tokenizer indexes punctuation as
#       ordinary content. Without this half, a query term can only be matched as
#       a free-floating substring, which is what made the 0.6.x stem probes so
#       unselective ('magaz' matching inside 'supermagazynowy').
#
#   normalize_terms                — the QUERY-side ending rule. Fusional
#       languages inflect by REPLACING endings (reklamacj-a/-e/-i share the stem
#       'reklamac'), so query-token == stored-token (porter FTS) and
#       query-is-substring-of-stored (raw trigram) both fail. Truncating the
#       query token to a stem turns ending-replacement into the substring problem
#       the folded trigram already solves, and the stem is then matched ANCHORED
#       at a word start, which is what keeps it selective.
#
# Why the ending rule is applied to the query and not to the stored text, even
# though this is the write-time build: truncation is a PREFIX operation, so
# trunc(q) is always a substring of trunc(s) implies trunc(q) is a substring of
# s. Truncating the stored side therefore matches a strict SUBSET of what
# truncating only the query side matches — it cannot add recall, it can only
# remove it, and it would destroy the tail of every unbroken CJK/Thai/compound
# run, which is the case the shadow exists for. SQLite also cannot express a
# per-token truncation in pure SQL, and doing it with an application-defined
# function inside the maintenance triggers would make every base-table write
# FAIL for any process that opens the DB without registering that function (a
# second venv on an older client, the sqlite3 CLI). See stage2-report.md §2.
#
# The truncation parameters are the tuned ones from the removed 0.6.0 D2L block
# (git history at 63a5ea9), kept verbatim: crude fixed-length truncation is
# deliberate and NOT a placeholder for a real stemmer. There is no Polish
# snowball analyzer in the stdlib, and precisely BECAUSE it is crude it survives
# the stem-internal palatalization a rule-based suffix stripper diverges on
# (wysyłka/wysyłce keep 'wysyl'; księgowość/księgowości keep 'księgowo').
# drop=3 covers the 2-3 char ending classes of Polish/Czech/Russian declension
# (drop=2 misses -ach locatives: magazynach); floor=5 keeps stems long enough to
# avoid cross-lemma collisions at scale.
# ---------------------------------------------------------------------------
NORMALIZER_VERSION = 1

# The schema marker that corresponds to the rendering above, stamped into
# PRAGMA user_version by storage.py. It lives HERE, next to the rendering, so the
# two writers of the rendering (storage's migration and shadow._heal) can both
# stamp it. Marker 4 was the fold-only rendering shipped as 0.7.0; 5 was an
# intermediate revision of this branch that never left it; 6 is
# NORMALIZER_VERSION 1 with the full boundary set. Any change to the four
# per-tier expressions must bump this or existing stores keep the old rendering.
SHADOW_MARKER = 6

_STEM_MIN_TOKEN = 5     # tokens shorter than this are never truncated
_STEM_DROP = 3          # drop up to this many trailing chars
_STEM_FLOOR = 5         # never truncate a stem below this many chars

# Cap on how many distinct terms one shadow query may probe. Mirrors
# multi_record's _MAX_FANOUT_TOKENS in intent: an arbitrarily long query must not
# turn into an arbitrarily long series of index scans. Longest-first, because
# length is the cheap rarity proxy.
_MAX_SHADOW_TERMS = 12

# Every character that must read as a WORD BOUNDARY in the stored rendering.
# Enumerated (not a character class) because the SQL side can only express this
# as a fixed chain of replace() calls, and the two sides must agree byte for
# byte. `_` is deliberately absent: it is a \w character, so it is part of a
# token on both sides (s3_bucket stays one token).
_BOUNDARY_CHARS = (
    # ASCII
    '"', "'", "`", "\\", "/", "|",
    "{", "}", "[", "]", "(", ")", "<", ">",
    ":", ";", ",", ".", "!", "?",
    "-", "=", "+", "*", "&", "%", "$", "#", "@", "~", "^",
    # Typographic (adversarial review 2026-08-30, finding 5). The ASCII-only set
    # left 18 characters that are word boundaries to a reader but not to the
    # rendering, so a word wrapped in them had no word START and the exactness
    # tie-break silently scored 0 for it. `\u201e ... \u201d` is the standard POLISH
    # quotation pair, so the build's own target language was in the gap, and
    # NBSP is the commonest of these in pasted content.
    "\u2013", "\u2014", "\u2015",              # en dash, em dash, horizontal bar
    "\u201c", "\u201d", "\u2018", "\u2019",     # curly double + single quotes
    "\u201e", "\u00ab", "\u00bb",              # low-9 double quote, guillemets
    "\u00a0", "\u200b",                      # NBSP, zero-width space
    "\u2026",                              # ellipsis
    "\u3001", "\u3002", "\u3010", "\u3011", "\uff0c",  # CJK punctuation + fullwidth comma
)

# Whitespace handled separately: a SQL string literal cannot carry a raw tab or
# newline portably, so these go through char().
_BOUNDARY_CHAR_CODES = (9, 10, 13)

# Scripts written without spaces between words. A token drawn from one of these
# is a whole phrase, not a word with an inflectional ending, so the truncation
# rule must never touch it (truncating '北京烤鸭' to 5 chars would make its tail
# unfindable — the exact capability the trigram shadow exists to provide). The
# same test decides which SHORT tokens are real words worth requiring: a 2-char
# Han/Hangul token is content, a 2-char Latin token ('są', 'is') is noise.
_UNSPACED_RANGES = (
    (0x2E80, 0x2FDF),    # CJK radicals
    (0x3040, 0x30FF),    # Hiragana + Katakana
    (0x3400, 0x4DBF),    # CJK ext A
    (0x4E00, 0x9FFF),    # CJK unified
    (0xAC00, 0xD7AF),    # Hangul syllables
    (0xF900, 0xFAFF),    # CJK compatibility
    (0x0E00, 0x0E7F),    # Thai
    (0x0E80, 0x0EFF),    # Lao
    (0x1000, 0x109F),    # Myanmar
    (0x1780, 0x17FF),    # Khmer
    (0x0F00, 0x0FFF),    # Tibetan
    (0x20000, 0x2FA1F),  # CJK ext B..F
)


def _is_unspaced(text: str) -> bool:
    """True if any character belongs to a script written without word spaces."""
    for ch in text:
        cp = ord(ch)
        for lo, hi in _UNSPACED_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _sql_str(ch: str) -> str:
    """A single-quoted SQL string literal for one boundary character."""
    return "'" + ch.replace("'", "''") + "'"


# SQLite's TRIGGER parser overflows ("parser stack overflow") at about 27 nested
# function calls, measured on 3.45.1. The normalized rendering needs 17 fold
# replacements plus the boundary set, well past that, so the chain is STAGED
# through nested subqueries: each stage applies at most _STAGE_OPS replace()
# calls and hands its result to the next as the column ``nv``, while the business
# key columns ride along untouched. Keep this well under the measured ceiling —
# it is a hard parse-time failure, not a runtime one, so an over-long chain would
# break DB creation outright rather than degrade.
_STAGE_OPS = 12


def _replace_ops() -> list[tuple[str, str]]:
    """Every (search, replacement) pair of the rendering, in application order.

    Search terms are SQL EXPRESSIONS (already quoted, or a ``char(n)`` call);
    replacements are SQL literals. Fold first, then boundaries — the order the
    Python twin uses. No fold target is a boundary character, so the two passes
    do not interact, but the order is fixed anyway so parity cannot drift.
    """
    ops = [(_sql_str(src), _sql_str(dst)) for src, dst in FOLD_MAP.items()]
    ops += [(_sql_str(ch), "' '") for ch in _BOUNDARY_CHARS]
    ops += [(f"char({code})", "' '") for code in _BOUNDARY_CHAR_CODES]
    return ops


def normalize_select(base_expr: str, carried: list[tuple[str, str]],
                     from_table: str | None) -> str:
    """A SELECT list + FROM producing the normalized rendering of ``base_expr``.

    Returns the text after ``INSERT INTO ... `` : ``SELECT <txt>, <carried...>
    FROM (...)``. ``carried`` is ``[(expr, alias), ...]`` for the business-key
    columns, which are carried through every stage RAW (they are the join key
    back to the base table and the DELETE triggers match them verbatim).
    ``from_table`` is None on the trigger side, where the innermost stage reads
    ``new.``/``old.`` and needs no FROM at all.

    This one function generates BOTH the trigger side and the backfill side, so a
    backfilled row stays byte-identical to a trigger-written one. Sources and
    targets are fixed ASCII/Latin-1 literals (never user input), so the string
    interpolation is injection-safe.
    """
    ops = _replace_ops()
    chunks = [ops[i:i + _STAGE_OPS] for i in range(0, len(ops), _STAGE_OPS)] or [[]]

    expr = f"lower({base_expr})"
    for src, dst in chunks[0]:
        expr = f"replace({expr}, {src}, {dst})"
    cols = [f"{expr} AS nv"] + [f"{e} AS {a}" for e, a in carried]
    sub = f"(SELECT {', '.join(cols)}"
    sub += f" FROM {from_table})" if from_table else ")"

    for chunk in chunks[1:]:
        expr = "nv"
        for src, dst in chunk:
            expr = f"replace({expr}, {src}, {dst})"
        cols = [f"{expr} AS nv"] + [a for _e, a in carried]
        sub = f"(SELECT {', '.join(cols)} FROM {sub})"

    out_cols = ["' ' || nv || ' '"] + [a for _e, a in carried]
    return f"SELECT {', '.join(out_cols)} FROM {sub}"


def normalize_py(text: str) -> str:
    """Query-side twin of the write-time rendering, byte-identical to it.

    NOT idempotent in the strict sense: a second pass adds a second pad, because
    the SQL side pads unconditionally and a conditional pad here would break the
    parity that is the whole contract. Idempotent on CONTENT, which is the
    property anything may rely on: ``normalize_py(normalize_py(x))`` equals
    ``" " + normalize_py(x) + " "``, and nothing in the package re-renders.
    """
    out = fold_py(text)
    for ch in _BOUNDARY_CHARS:
        out = out.replace(ch, " ")
    for code in _BOUNDARY_CHAR_CODES:
        out = out.replace(chr(code), " ")
    return " " + out + " "


def normalize_token(tok: str) -> str:
    """The ending rule for ONE already-folded token. Identity when it does not
    apply: short tokens, identifiers carrying a digit (q3, v2, k8, s3), and any
    token from an unspaced script."""
    if len(tok) < _STEM_MIN_TOKEN:
        return tok
    if any(ch.isdigit() for ch in tok):
        return tok
    if _is_unspaced(tok):
        return tok
    return tok[:max(_STEM_FLOOR, len(tok) - _STEM_DROP)]


def normalize_terms(query: str) -> list[tuple[str, bool, str]]:
    """``[(term, anchored, raw), ...]`` for the shadow MATCH, from the SAME
    normalizer the write side uses.

    ``anchored`` is True exactly when the ending rule shortened the token, i.e.
    the term is a morphological stem rather than the word itself. ``raw`` is the
    token before the ending rule, kept so a row that satisfies the word the user
    actually typed can outrank one that only satisfies its stem.
    """
    folded = normalize_py(query)
    out: list[tuple[str, bool, str]] = []
    seen: set[str] = set()
    for tok in re.findall(r"\w+", folded):
        if not tok:
            continue
        stem = normalize_token(tok)
        if stem in seen:
            continue
        seen.add(stem)
        out.append((stem, stem != tok, tok))
    return out


def trigram_tokenizer_clause() -> str:
    """Runtime-selected tokenizer clause across the SQLite 3.45 boundary.

    ``remove_diacritics`` reached the ``trigram`` tokenizer in 3.45.0. On older
    SQLite the option is rejected at vtable construction ("unrecognized"), so we
    fall back to a bare ``trigram``. The only degradation on pre-3.45 SQLite is
    accent-insensitive SUBSTRING matching in the fallback; whole-word accented
    matching still works via the primary porter-unicode61 index, and fold_py's
    non-decomposable map is unaffected.
    """
    if sqlite3.sqlite_version_info >= (3, 45, 0):
        return "tokenize = 'trigram remove_diacritics 1'"
    return "tokenize = 'trigram'"


# ---------------------------------------------------------------------------
# DDL / DML generators (single source of truth, consumed by storage.py migration)
# ---------------------------------------------------------------------------
def create_table_sql() -> str:
    """CREATE for the unified shadow table with the runtime tokenizer clause."""
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {SHADOW_TABLE} USING fts5(\n"
        "  txt, tier UNINDEXED, k1 UNINDEXED, k2 UNINDEXED, tenant_id UNINDEXED,\n"
        f"  {trigram_tokenizer_clause()}\n"
        ")"
    )


# Per-tier NORMALIZED renderings, IDENTICAL on the trigger side (new./old.) and
# the backfill side (bare columns). Mirrors the base-table columns each tier's
# primary FTS5 index feeds on; journal reuses the exact concat of
# journal_events_ai_fts.
#
# These four are the single write-time injection point (v0.8.0 stage 2). They
# went from a bare fold_sql expression to the full normalized rendering, which is
# what makes the stored text word-boundary-clean and therefore word-START
# matchable. Each returns the whole ``SELECT ... FROM (...)`` because the
# rendering has to be staged past SQLite's trigger parser ceiling; both write
# paths still come from this one place, so a backfilled row is byte-identical to
# a trigger-written one. Change these four and BOTH write paths change together —
# which is also why _SHADOW_MARKER must be bumped whenever they change, so every
# existing store drops, recreates and re-backfills its shadow on the next open.
def _entity_txt(p: str, src: str | None) -> str:
    return normalize_select(
        f"{p}name || ' ' || {p}category || ' ' || {p}body",
        [("'entity'", "tier"), (f"{p}category", "k1"), (f"{p}name", "k2"),
         (f"{p}tenant_id", "tid")], src)


def _state_txt(p: str, src: str | None) -> str:
    return normalize_select(
        f"{p}document_key || ' ' || {p}body",
        [("'state'", "tier"), ("''", "k1"), (f"{p}document_key", "k2"),
         (f"{p}tenant_id", "tid")], src)


def _reference_txt(p: str, src: str | None) -> str:
    return normalize_select(
        f"{p}doc_key || ' ' || COALESCE({p}body, '')",
        [("'reference'", "tier"), ("''", "k1"), (f"{p}doc_key", "k2"),
         (f"{p}tenant_id", "tid")], src)


def _journal_txt(p: str, src: str | None) -> str:
    return normalize_select(
        f"COALESCE({p}evaluated, '') || ' ' || COALESCE({p}acted, '') || ' ' || "
        f"COALESCE({p}forward, '') || ' ' || COALESCE({p}extra, '')",
        [("'journal'", "tier"), ("''", "k1"), (f"{p}id", "k2"),
         (f"{p}tenant_id", "tid")], src)


def trigger_sqls() -> list[str]:
    """The shadow-maintenance triggers.

    entity / state / reference each get AFTER INSERT / UPDATE / DELETE; journal
    gets AFTER INSERT only (append-only — mirrors ``journal_events_ai_fts``, which
    also has no AU/AD). All use PLAIN INSERT / PLAIN DELETE keyed on the tier's
    BUSINESS key (see module docstring TRAP note). Ordering the DELETE before the
    re-INSERT in the AU triggers keeps a rename/rekey clean.
    """
    ins = f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id)"
    stmts: list[str] = []

    # --- entities: business key (tenant_id, category, name) = (tenant, k1, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_ai_shadow
AFTER INSERT ON entities BEGIN
  {ins}
  {_entity_txt('new.', None)};
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_ad_shadow
AFTER DELETE ON entities BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'entity' AND k1 = old.category AND k2 = old.name
         AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS entities_au_shadow
AFTER UPDATE ON entities BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'entity' AND k1 = old.category AND k2 = old.name
         AND tenant_id = old.tenant_id;
  {ins}
  {_entity_txt('new.', None)};
END""")

    # --- state_documents: business key (tenant_id, document_key) = (tenant, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_ai_shadow
AFTER INSERT ON state_documents BEGIN
  {ins}
  {_state_txt('new.', None)};
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_ad_shadow
AFTER DELETE ON state_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'state' AND k2 = old.document_key AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS state_documents_au_shadow
AFTER UPDATE ON state_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'state' AND k2 = old.document_key AND tenant_id = old.tenant_id;
  {ins}
  {_state_txt('new.', None)};
END""")

    # --- reference_documents: business key (tenant_id, doc_key) = (tenant, k2)
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_ai_shadow
AFTER INSERT ON reference_documents BEGIN
  {ins}
  {_reference_txt('new.', None)};
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_ad_shadow
AFTER DELETE ON reference_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'reference' AND k2 = old.doc_key AND tenant_id = old.tenant_id;
END""")
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS reference_documents_au_shadow
AFTER UPDATE ON reference_documents BEGIN
  DELETE FROM {SHADOW_TABLE}
   WHERE tier = 'reference' AND k2 = old.doc_key AND tenant_id = old.tenant_id;
  {ins}
  {_reference_txt('new.', None)};
END""")

    # --- journal_events: append-only, AI only. Business key (tenant_id, id).
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS journal_events_ai_shadow
AFTER INSERT ON journal_events BEGIN
  {ins}
  {_journal_txt('new.', None)};
END""")

    return stmts


def backfill_sqls() -> list[str]:
    """One INSERT..SELECT per tier, rendering with the SAME expressions the
    triggers use so a backfilled row is byte-identical to a trigger-written one."""
    ins = f"INSERT INTO {SHADOW_TABLE}(txt, tier, k1, k2, tenant_id) "
    return [
        ins + _entity_txt("", "entities"),
        ins + _state_txt("", "state_documents"),
        ins + _reference_txt("", "reference_documents"),
        ins + _journal_txt("", "journal_events"),
    ]


def shadow_table_exists(conn: sqlite3.Connection) -> bool:
    """True iff the shadow virtual table is present (cheap sqlite_master lookup)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (SHADOW_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def shadow_trigger_count(conn: sqlite3.Connection) -> int:
    """Count how many of the canonical shadow triggers currently exist.

    Cheap ``sqlite_master`` lookup restricted to the known trigger names, so a
    stray user trigger can never inflate the count. Returns 0 on any read error
    (the safe direction: it forces the migration to re-run apply_shadow_migration
    rather than short-circuit on a bad read)."""
    placeholders = ", ".join("?" for _ in SHADOW_TRIGGER_NAMES)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM sqlite_master "
            f"WHERE type='trigger' AND name IN ({placeholders})",
            SHADOW_TRIGGER_NAMES,
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def shadow_triggers_complete(conn: sqlite3.Connection) -> bool:
    """True iff ALL 10 shadow-maintenance triggers are present (F1).

    The v4 migration fast path uses this alongside ``shadow_table_exists`` so an
    out-of-band trigger drop self-heals: a mismatch (count != 10) falls through
    to the idempotent ``apply_shadow_migration``, which recreates every trigger
    (CREATE TRIGGER IF NOT EXISTS) and re-backfills the shadow to consistency."""
    return shadow_trigger_count(conn) == len(SHADOW_TRIGGER_NAMES)


def rendering_is_current(conn: sqlite3.Connection) -> bool:
    """Cheap O(1) check that the stored rendering is the one this client writes.

    Samples ONE shadow row and requires the normalizer's edge pad. The marker
    alone is not sufficient evidence: it is written by storage.py, while the
    rendering has a second writer (``_heal``), and a third party could always
    write the table directly. An empty shadow is vacuously current. Returns True
    on any read error, the safe direction for a probe whose only job is to force
    an extra rebuild (the migration itself is idempotent, but a probe that failed
    open would rebuild on EVERY open)."""
    try:
        row = conn.execute(
            f"SELECT txt FROM {SHADOW_TABLE} LIMIT 1").fetchone()
    except sqlite3.Error:
        return True
    if row is None or row[0] is None:
        return True
    txt = row[0]
    return txt.startswith(" ") and txt.endswith(" ")


def apply_shadow_migration(conn: sqlite3.Connection) -> None:
    """Create the shadow table + triggers and (re)backfill all four tiers.

    Runs via INDIVIDUAL ``execute`` statements (NOT ``executescript``, which
    issues an implicit COMMIT and would break the caller's BEGIN IMMEDIATE). The
    caller (storage.py) wraps this in one transaction and stamps the schema
    marker in the SAME transaction, so a crash anywhere rolls the whole thing
    back and the next open retries — idempotent by construction. Clearing with
    ``DELETE`` before the backfill keeps a re-run (heal / crash-retry)
    duplicate-free.
    """
    conn.execute(create_table_sql())
    # v0.8.0 stage 2: DROP before CREATE. The trigger bodies embed the per-tier
    # rendering expression, so ``CREATE TRIGGER IF NOT EXISTS`` alone would leave
    # a pre-existing store running the OLD rendering in its triggers while the
    # backfill below writes the NEW one — a silently split index. Dropping first
    # makes this migration idempotent with respect to a CHANGED trigger body, not
    # just a missing one. Cheap (10 sqlite_master deletes) and still safe to
    # re-run: the whole thing is inside the caller's single transaction.
    for name in SHADOW_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for stmt in trigger_sqls():
        conn.execute(stmt)
    conn.execute(f"DELETE FROM {SHADOW_TABLE}")
    for stmt in backfill_sqls():
        conn.execute(stmt)


def rebuild_shadow(conn: sqlite3.Connection) -> None:
    """Clear + re-backfill the shadow from the base tables (heal/rebuild path).

    No-op when the shadow table is absent (e.g. a fresh DB whose shadow is
    created by the later v4 migration step, so the v3 FTS-rebuild that also calls
    this must not fail). Idempotent."""
    if not shadow_table_exists(conn):
        return
    conn.execute(f"DELETE FROM {SHADOW_TABLE}")
    for stmt in backfill_sqls():
        conn.execute(stmt)


def drop_shadow(conn: sqlite3.Connection) -> None:
    """Drop the shadow table + all its triggers. Used by the portability heal and
    documented as the rollback recovery (restores exact v3 behaviour when paired
    with ``PRAGMA user_version = 3``). Base-table data is never touched."""
    for name in SHADOW_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP TABLE IF EXISTS {SHADOW_TABLE}")


# Portability / corruption error markers: a DB created on SQLite >= 3.45 (tokenizer
# clause 'trigram remove_diacritics 1') opened on < 3.45 fails vtable construction
# with an "unrecognized"-class message; a corrupt shadow surfaces as a malformed /
# vtable-constructor error. Both are HEALABLE by dropping and recreating the
# shadow with the locally-supported clause. Matched case-insensitively.
_HEALABLE_MARKERS = (
    "unrecognized", "no such tokenize", "no such module",
    "vtable constructor", "malformed", "not a database",
)


def _is_healable(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _HEALABLE_MARKERS)


def _heal(conn: sqlite3.Connection) -> None:
    """Best-effort portability heal: rebuild the shadow with the local tokenizer
    clause. Runs inside the §4.2 containment so a failure here can never crash
    search. Wrapped in its own transaction; swallows any error.

    STAMPS THE MARKER (adversarial review 2026-08-30, finding 3). This is the
    SECOND writer of the rendering, after storage's migration, and it writes
    whatever rendering the RUNNING client has. Without the stamp, an old client
    healing a new store on the documented portability path left the new marker
    standing over the old fold-only rendering, and the new client then trusted
    its fast path forever: quality degraded silently and permanently. Stamping
    inside the same transaction as the rebuild keeps marker and rendering
    consistent for both writers."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        drop_shadow(conn)
        conn.execute(create_table_sql())
        for stmt in trigger_sqls():
            conn.execute(stmt)
        for stmt in backfill_sqls():
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {int(SHADOW_MARKER)}")
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


_LIKE_ESC = re.compile(r"([%_\\])")


def _tier_filter(allowed: set[str]) -> tuple[str, list[str]]:
    """Return (`` AND tier IN (?,...)``, params) restricting to ``allowed`` tiers,
    or ("", []) when all four tiers are allowed (no clause needed)."""
    if not allowed or allowed >= set(_TIERS):
        return "", []
    ordered = [t for t in _TIERS if t in allowed]
    placeholders = ", ".join("?" for _ in ordered)
    return f" AND tier IN ({placeholders})", ordered


def _shape_hit(conn: sqlite3.Connection, tenant_id: str, tier: str,
               k1: str, k2: str, txt: str, rank) -> dict | None:
    """Join a shadow row back to its base table by business key and return the
    exact dict shape ``_search_strict`` produces for that tier. Returns None when
    the base record no longer resolves (the shadow raced a delete) — the caller
    skips it. ``snippet`` = first ~120 chars of the folded text; ``rank`` = shadow
    BM25 but POSITIONAL-ONLY to the caller: since F2 (2026-08-12) shadow hits are
    APPENDED after the primary hits in MemoryClient.search (never re-sorted into
    them), this rank orders shadow candidates only among themselves and is never
    cross-compared with the primary BM25 index — so the two rank scales never mix."""
    snippet = txt[:120]
    if tier == "entity":
        row = conn.execute(
            "SELECT body, updated_at FROM entities "
            "WHERE tenant_id = ? AND category = ? AND name = ?",
            (tenant_id, k1, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "entity", "key": k2, "category": k1,
                "body": _json.loads(row[0]), "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "state":
        row = conn.execute(
            "SELECT body, updated_at FROM state_documents "
            "WHERE tenant_id = ? AND document_key = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "state", "key": k2, "category": None,
                "body": _json.loads(row[0]), "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "reference":
        row = conn.execute(
            "SELECT body, updated_at FROM reference_documents "
            "WHERE tenant_id = ? AND doc_key = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "reference", "key": k2, "category": None,
                "body": row[0], "snippet": snippet,
                "rank": rank, "ts": row[1]}
    if tier == "journal":
        row = conn.execute(
            "SELECT ts, evaluated, acted, forward, extra FROM journal_events "
            "WHERE tenant_id = ? AND id = ?",
            (tenant_id, k2),
        ).fetchone()
        if row is None:
            return None
        return {"tier": "journal", "key": k2, "category": None,
                "body": {
                    "evaluated": _json.loads(row[1]) if row[1] else None,
                    "acted": _json.loads(row[2]) if row[2] else None,
                    "forward": _json.loads(row[3]) if row[3] else None,
                    "extra": _json.loads(row[4]) if row[4] else None,
                },
                "snippet": snippet, "rank": rank, "ts": row[0]}
    return None


def _content_terms(
        terms: list[tuple[str, bool, str]]) -> list[tuple[str, bool, str]]:
    """The terms allowed to carry coverage weight.

    A term counts iff the ending rule SHORTENED it (so it is a morphological
    stem, which in an inflecting language means a word long enough to inflect) or
    it comes from an unspaced script (where 2-3 characters are a whole word).
    Everything else — 'and', 'the', 'gdzie', 'nasze', 'your', 'oraz' — is
    function-word-shaped by length and is dropped.

    This is the SAME predicate the normalizer already applies, reused rather than
    re-derived: there is no stoplist here, no language detection, and no
    per-query tuning. It exists because coverage without it is meaningless — an
    8-word injection query in which only 'and' has corpus support otherwise
    scores every English row in the store at coverage 1 and returns 20 of them.
    """
    return [(t, a, raw) for t, a, raw in terms
            if a or (len(t) >= 3 and _is_unspaced(t))]


def _append_order(v: dict):
    """Total, content-stable ordering for shadow candidates.

    Coverage first, then the idf-weighted score, then whole-word exactness, then
    word-start matches, then BM25. The final tie-break is the row TEXT, not its
    key: a journal row's business key is a uuid4 minted at write time, so ordering
    two otherwise-tied journal rows by k2 made the RESULT ORDER depend on which
    uuid happened to be generated, and the journal-heavy LongMemEval stores
    produced two adjacent journal rows swapping places between runs of identical
    code. Text is content-derived and therefore stable across rebuilds."""
    return (-len(v["cover"]), -v["score"], -v["exact"], -v["anchored"],
            v["rank"], v["tier"], v["k1"], v["txt"], v["k2"])


def _is_decisive(v: dict, match_terms: list[tuple[str, bool, str]]) -> bool:
    """The append guard, per candidate row. See _shadow_search_normalized.

    The bar scales with the corroboration the QUERY can offer. A single-token
    query has none: no second term to agree, no surrounding words, nothing but
    one truncated stem, so the only evidence accepted there is the token itself,
    verbatim. That is what keeps 'contracts' from reaching a row that opens with
    "Contrails": both truncate to a six-character stem at a word start and
    nothing shorter than the whole token separates them."""
    if any(raw in v["txt"] for _t, _a, raw in match_terms):
        return True
    if len(match_terms) < 2:
        return False
    if len(v["cover"]) > 1:
        return True
    return any(len(term) > _STEM_FLOOR for term in v["cover"])


def _shadow_search_normalized(conn: sqlite3.Connection, tenant_id: str, query: str,
                              limit: int, allowed: set,
                              decisive_only: bool = False) -> list[dict] | None:
    """The normalized shadow pass: match the normalized query against the
    normalized rendering, and return the rows that cover the MOST query terms.

    One probe per term. Never a sequential ladder: every term is probed, always,
    in a fixed order, with no early stop and no selectivity re-ordering. Then a
    single argmax on term coverage, tie-broken by how many of those terms matched
    at a WORD START. AND is the special case where some row covers everything, so
    no separate AND pass is needed, and a term nothing supports contributes
    nothing to any row and drops out on its own.

    Probes are UNANCHORED (free substring), which is what preserves the
    compound-interior and CJK-interior matching the trigram shadow exists for;
    the word-start test is applied afterwards as a RANKING signal, so selectivity
    is bought without giving up substring reach. Coverage is a ranking rule
    inside the rescue, not a gate deciding whether the rescue runs — the caller
    decides that, and this function always does the same work.

    ``decisive_only`` is the APPEND GUARD. It is set on both paths where the
    caller ALREADY has an answer and the shadow is only allowed to extend it: the
    single-token consult on a non-empty strict head, and the append onto a
    last-resort relaxed head. It is never set on the zero-hit path, where the
    alternative is returning nothing.

    A row survives the guard when it carries at least one of three kinds of
    evidence, in this order:

      1. it contains one of the query's tokens VERBATIM, so the match is a stored
         inflection that EXTENDS the token ('packshot' inside 'packshoty'), which
         is precisely what porter's whole-token matching cannot reach;
      2. it covers MORE THAN ONE query term, so several independent probes agree;
      3. the single term it covers is LONGER than _STEM_FLOOR, meaning the ending
         rule did not have to floor it and the stem still carries most of its
         word.

    All three are evidence that the row was singled out rather than swept up. The
    floor is where the collisions live: the review measured that 17 of 21 ordinary
    English content words truncate to exactly five characters, so 'contract'
    becomes 'contr' and an unguarded probe reaches control, contrast,
    contribution, contrary and contralto. That inflated df up to sevenfold, which
    deflated idf, which pushed correct rows under multi_record's coverage floor
    and removed them from the DEFAULT path (finding 2), and it doubled the rows a
    supported-token injection query could reach on the ladder path (finding 17).
    Meanwhile the Polish recoveries all clear the guard: 'inwentaryza' is 11
    characters and was never floored, and the rows for 'stawka ryczałtu' and
    'kurierem wysyłamy' carry their raw tokens outright.

    Ending-REPLACEMENT inflections ('reklamacje' against a stored 'reklamacja')
    are unaffected by the guard, because they reach the zero-hit path.

    Returns None when the query carries no content-shaped term at all, which
    tells the caller to fall through to the 0.5.0 raw-folded pass instead.
    """
    terms = normalize_terms(query)
    content = _content_terms(terms)
    match_terms = [(t, a, raw) for t, a, raw in content if len(t) >= 3]
    if not match_terms:
        return None
    # A short token is content only in a script written without word spaces. A
    # 2-char Latin token ('są', 'is', 'do') is stopword-grade and must not become
    # a hard AND requirement on every candidate row — which is exactly what the
    # pre-0.8.0 `not t.isascii()` test did to every accented 2-char Polish word.
    like_terms = [t for t, _a, _r in terms if len(t) < 3 and _is_unspaced(t)]
    if len(match_terms) > _MAX_SHADOW_TERMS:
        match_terms = sorted(match_terms, key=lambda p: len(p[0]), reverse=True
                             )[:_MAX_SHADOW_TERMS]

    tier_clause, tier_params = _tier_filter(allowed)
    fetch = max(limit, 1) * 4
    cand: dict = {}

    weight: dict = {}
    for term, _anchored, _raw in match_terms:
        # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
        # `AND tenant_id = ?` is the ONLY thing keeping this query inside the
        # caller's tenant (tenant_id is UNINDEXED -> trailing post-filter, not
        # index-enforced). DO NOT remove, reorder, or make this conditional.
        rows = conn.execute(
            f"SELECT txt, tier, k1, k2, rank FROM {SHADOW_TABLE} "
            f"WHERE {SHADOW_TABLE} MATCH ? AND tenant_id = ?" + tier_clause +
            " ORDER BY rank LIMIT ?",
            ['"' + term.replace('"', '""') + '"', tenant_id, *tier_params, fetch],
        ).fetchall()
        # Term weight = length / document frequency. Both halves are rarity
        # proxies this codebase already relies on: df is what multi_record scores
        # with, and length is the "cheap rarity proxy" _relaxed_query_strings
        # orders by. No threshold, no stoplist, no store-size lookup (every probe
        # shares one `fetch` cap, so df is comparable across terms).
        #
        # df carries 'termin rozpatrzenia reklamacji': 'termi' matches the forty
        # stored notes saying "termin przesuniety" and is worth 5/40, while
        # 'reklama' matches one row and is worth 7/1 — the difference between
        # returning the complaints row and returning forty notes. Length carries
        # the ties df cannot see: in 'kiedy robimy inwentaryzację' both 'robim'
        # and 'inwentaryza' match exactly one row, and the longer stem is the one
        # the question is actually about.
        weight[term] = len(term) / max(len(rows), 1)
        for txt, tier, k1, k2, rank in rows:
            key = (tier, k1, k2)
            e = cand.get(key)
            if e is None:
                e = cand[key] = {"txt": txt, "tier": tier, "k1": k1, "k2": k2,
                                 "rank": rank, "cover": set()}
            e["cover"].add(term)
            if rank < e["rank"]:
                e["rank"] = rank

    if not cand:
        return []
    if like_terms:  # short unspaced-script tokens stay hard requirements
        cand = {k: v for k, v in cand.items()
                if all(t in v["txt"] for t in like_terms)}
        if not cand:
            return []

    # Coverage top-up + word-start scoring, both from the text already in hand.
    # A very common term's own probe is capped at `fetch`, so a row that
    # genuinely carries it can be missing from that probe's window; the same
    # substring test the MATCH performs is re-run in Python. Union only — it can
    # add coverage, never remove it — and the SQL side stays the more generous of
    # the two (the trigram tokenizer is additionally diacritic-insensitive).
    for v in cand.values():
        anchored_hits = exact_hits = 0
        for term, _a, raw in match_terms:
            if term not in v["cover"] and term in v["txt"]:
                v["cover"].add(term)
            if (" " + term) in v["txt"]:
                anchored_hits += 1
            if raw != term and (" " + raw + " ") in v["txt"]:
                exact_hits += 1
        v["anchored"] = anchored_hits
        # A row carrying the WHOLE WORD the user actually typed beats one
        # carrying only its stem. Truncation is an approximation applied because
        # the exact form missed; where it did not need to miss, exactness wins.
        # Language-neutral, and only ever a tie-break, so it cannot change which
        # rows are eligible — it decides that 'where do we keep product
        # packshots' leads with the English row rather than its equally-covered
        # Polish twin. WHOLE WORD, not substring: the rendering pads every word
        # with spaces, so `' '+raw+' '` is an exact word test, and it has to be,
        # because 'magazynierow' contains the string 'magazynie' and a substring
        # test would hand that row the bonus and hide the warehouse row behind
        # it — the exact latent defect that made baseline 0.7.0 read 15/16.
        v["exact"] = exact_hits
        # Rounded so float addition order can never split an otherwise exact tie.
        v["score"] = round(sum(weight[t] for t in v["cover"]), 9)

    if decisive_only:
        cand = {k: v for k, v in cand.items() if _is_decisive(v, match_terms)}
        if not cand:
            return []

    # ELIGIBILITY is COVERAGE COUNT: how many distinct query terms the row
    # satisfies. Everything else (the idf-weighted score, whole-word exactness,
    # word-start matches, BM25) only ORDERS the rows that tied on it.
    #
    # That separation is load-bearing three times over. 'packshot' scores the
    # English and the Polish row identically and only the English one carries the
    # whole word, so letting exactness filter would drop the Polish twin and
    # re-create the masking this build exists to close. And letting the idf SCORE
    # filter was measured, on the LongMemEval English workload, to collapse a long
    # natural-language question to a single row: on 'How many tanks do I currently
    # have, including the one I set up for my friend's kid?' every candidate
    # covers exactly ONE of the three content terms, and a CO2 fertiliser row
    # matching the rare 'curren' outscored every row matching 'frien', so the four
    # rows that actually answered the question were discarded. Rarity is a fine
    # reason to rank one row above another and a terrible reason to delete the
    # rest: coverage says how much of the question a row answers, which is what
    # eligibility should mean, and it still returns exactly one row for
    # 'aktualizacja cennika hurtowego', where the pricelist row covers three terms
    # and the noise rows cover one.
    if decisive_only:
        # APPEND path. Narrowing to the single best-covered group here is what
        # made the branch retrieve a strict SUBSET of 0.7.0 on long English
        # questions: on 'How much did I earn at the Downtown Farmers Market on my
        # most recent visit?' the row stating the answer covers one term fewer
        # than the session record and was dropped for it. Taking EVERYTHING the
        # guard passes is the other extreme and is just as wrong: 'complaint
        # review deadline' then appends nineteen stored notes, every one of which
        # legitimately carries the word "deadline" and none of which is about a
        # complaint.
        #
        # The rule between them is CORROBORATION, the same principle _is_decisive
        # already applies to a single-token query: a row must satisfy at least TWO
        # of the query's content terms to be appended to an answer that already
        # exists. When no row in the store manages two, there is nothing to
        # corroborate with and the argmax stands, which is what keeps the
        # single-term twin recovery ('packshot' finding 'packshoty') working.
        best = max(len(v["cover"]) for v in cand.values())
        if best < 2:
            # Nothing in the store corroborates, so there is nothing to rank
            # against and every guarded row stands. This is the single-term twin
            # recovery ('packshot' finding 'packshoty').
            keep = list(cand.values())
        else:
            # Rows that satisfy two or more terms corroborate and all stand.
            # Rows that satisfy exactly ONE are weak evidence, so they are
            # BUDGETED rather than admitted or refused wholesale: at most as many
            # of them as the query has content terms, best-scoring first. Both
            # extremes were measured and both are wrong. Refusing them loses the
            # row that states the answer on 'What type of camera lens did I
            # purchase most recently?' and half the tank rows on 'How many tanks
            # do I currently have'. Admitting them all appends nineteen stored
            # notes to 'complaint review deadline', every one carrying the word
            # "deadline" and none about a complaint. The budget is the query's own
            # term count, the only query-derived bound available here, so it does
            # not scale with the store and cannot be tuned against a corpus.
            strong = [v for v in cand.values() if len(v["cover"]) >= 2]
            weak = [v for v in cand.values() if len(v["cover"]) == 1]
            weak.sort(key=_append_order)
            keep = strong + weak[:len(match_terms)]
    else:
        # ZERO-HIT path. Nothing else filters here, so coverage argmax is the only
        # precision control and it stays: 'aktualizacja cennika hurtowego' returns
        # the one pricelist row covering three terms instead of the forty noise
        # rows covering one, which is the whole of the N7 finding.
        best = max(len(v["cover"]) for v in cand.values())
        keep = [v for v in cand.values() if len(v["cover"]) == best]
    # The final tie-break is the row TEXT, not its key. A journal row's business
    # key is a uuid4 minted at write time, so ordering two otherwise-tied journal
    # rows by k2 made the RESULT ORDER depend on which uuid happened to be
    # generated: the LongMemEval store, which is journal-heavy, produced two
    # adjacent journal rows swapping places between runs of identical code. Text
    # is content-derived and therefore stable across rebuilds of the same store.
    keep.sort(key=_append_order)
    return _shape_rows(conn, tenant_id, [(v["txt"], v["tier"], v["k1"], v["k2"],
                                          v["rank"]) for v in keep], limit)


def shadow_search(conn: sqlite3.Connection, tenant_id: str, query: str,
                  *, limit: int = 20, tiers: tuple[str, ...] | None = None,
                  normalize: bool = False,
                  decisive_only: bool = False) -> list[dict]:
    """Folded-trigram substring fallback. Returns ``_search_strict``-shaped dicts.

    Substring semantics via the trigram MATCH (>=3-char folded tokens) with a
    bounded LIKE post-filter/scan for 1-2 char non-ASCII tokens (the CJK 2-char
    case — trigram MATCH cannot see below 3 chars). Short ASCII tokens are
    stopword-grade noise and dropped. A no-op ``[]`` when the shadow table is
    absent (pre-migration DB) so it is safe to call unconditionally.

    ``normalize=True`` (v0.8.0 stage 2) runs the NORMALIZED pass instead: the
    query goes through the same normalizer the stored rendering went through at
    write time, inflected tokens are truncated to a stem and matched at a word
    start, and the rows covering the most terms win. ``normalize=False`` is the
    0.5.0 raw-folded behaviour, kept because it is a different question (exact
    substring, no morphology) and several callers and tests ask exactly that.
    """
    if limit <= 0:  # CORE-5: a non-positive limit must never broaden — mirror clamp
        return []
    if not shadow_table_exists(conn):
        return []
    allowed = set(tiers) if tiers else set(_TIERS)
    allowed &= set(_TIERS)
    if not allowed:
        return []

    if normalize:
        try:
            out = _shadow_search_normalized(conn, tenant_id, query, limit, allowed,
                                            decisive_only=decisive_only)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
            if _is_healable(err):
                _heal(conn)
            return []
        if out is not None:
            return out
        # No content-shaped term: an all-short-token query ('form', 'the cat').
        # Fall through to the 0.5.0 raw pass, which is exactly what such a query
        # got before this build, rather than inventing an answer for it.

    folded = fold_py(query)
    toks = [t for t in re.findall(r"\w+", folded) if t]
    match_toks = [t for t in toks if len(t) >= 3]
    # short non-ASCII tokens are real words (2-char CJK/Hangul); short ASCII is noise
    like_toks = [t for t in toks if len(t) < 3 and not t.isascii()]
    if not match_toks and not like_toks:
        return []

    tier_clause, tier_params = _tier_filter(allowed)
    fetch = max(limit, 1) * 4

    try:
        rows: list = []
        if match_toks:
            mq = " ".join('"' + t.replace('"', '""') + '"' for t in match_toks)
            # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
            # `AND tenant_id = ?` is the ONLY thing keeping this query inside the
            # caller's tenant. tenant_id is UNINDEXED in the shadow FTS5 table, so
            # this is a trailing post-filter, NOT index-enforced isolation. DO NOT
            # remove, reorder, or make this clause conditional. Covered by the
            # cross-tenant leak test (test_trigram_shadow_2026_08_06).
            rows = conn.execute(
                f"SELECT txt, tier, k1, k2, rank FROM {SHADOW_TABLE} "
                f"WHERE {SHADOW_TABLE} MATCH ? AND tenant_id = ?" + tier_clause +
                " ORDER BY rank LIMIT ?",
                [mq, tenant_id, *tier_params, fetch],
            ).fetchall()
            if like_toks:
                rows = [r for r in rows if all(t in r[0] for t in like_toks)]
        elif like_toks:
            conds = " AND ".join(f"txt LIKE ? ESCAPE '\\'" for _ in like_toks)
            like_params = ["%" + _LIKE_ESC.sub(r"\\\1", t) + "%" for t in like_toks]
            # !!! CORE-3 TENANT-ISOLATION LOCK (2026-06-25 pre-launch audit) !!!
            # `tenant_id = ?` is the ONLY tenant boundary (tenant_id UNINDEXED ->
            # trailing post-filter, not index-enforced). DO NOT remove/reorder/
            # conditionalize. Covered by test_trigram_shadow_2026_08_06.
            rows = conn.execute(
                f"SELECT txt, tier, k1, k2, 0.0 AS rank FROM {SHADOW_TABLE} "
                f"WHERE tenant_id = ?" + tier_clause + " AND " + conds + " LIMIT ?",
                [tenant_id, *tier_params, *like_params, fetch],
            ).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
        # A broken shadow must NEVER take down search: contain the error, return
        # the primary path's empty result, and heal the shadow if the failure is
        # the portability/corruption class (next query then succeeds).
        if _is_healable(err):
            _heal(conn)
        return []

    return _shape_rows(conn, tenant_id, rows, limit)


def _shape_rows(conn: sqlite3.Connection, tenant_id: str, rows, limit: int) -> list[dict]:
    """Hit shaping, shared by the raw and the normalized pass.

    Journal is capped at max(1, limit//4) for symmetry with _search_strict
    (contentless journal rows share many common terms and would otherwise
    dominate). Rows whose base record no longer resolves are skipped.
    """
    journal_cap = max(1, limit // 4) if limit > 0 else 0
    journal_used = 0
    hits: list[dict] = []
    for txt, tier, k1, k2, rank in rows:
        if tier == "journal":
            if journal_used >= journal_cap:
                continue
        # F3 (Fable robustness 2026-08-06): shape ONE row at a time behind a
        # try/except so a single undecodable base-table JSON body (corrupt row,
        # partial write, manual edit — _shape_hit calls json.loads) is SKIPPED,
        # not allowed to void the entire fallback result set. Mirrors the §4.2
        # containment stance: a broken row must never take down search. Skips on
        # any per-row error (decode or a transient row-level sqlite error).
        try:
            shaped = _shape_hit(conn, tenant_id, tier, k1, k2, txt, rank)
        except Exception:
            continue
        if shaped is None:
            continue
        if tier == "journal":
            journal_used += 1
        hits.append(shaped)
        if len(hits) >= limit:
            break
    return hits
