# Changelog

All notable changes to `sibyl-memory-client` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [0.4.14] - 2026-06-19

### Fixed

- **Silent write loss under sustained load (CRITICAL; beta deadguy 2026-06-17,
  report 3.1).** When tier verification was unreachable (e.g. the check-write
  endpoint returning a rate-limit-shaped 401 under a heavy write burst) and there
  was no cached tier, the write was rejected with `TierVerificationError` -- and a
  caller that ignored ok/error lost the write silently. The check-write transport
  now does a bounded retry with backoff on transient codes (401/408/425/429/5xx),
  and a no-cache write whose verification is unreachable now FAILS OPEN (allows the
  write) up to a 4x safety ceiling, logging a warning, instead of dropping data.
  Durability is preserved during outages; the server reconciles tier/cap on the
  next reachable check. Past the ceiling it hard-blocks. Test: `tests/test_capcheck.py`.

### Added

- **Paraphrase zero-hit search fallback (beta deadguy 2026-06-14).** Natural-language
  queries miss under strict token-AND (+ Porter stem). `MemoryClient.search` now
  retries with relaxed variants (stopwords stripped, then rarest token) ONLY when
  the strict search returns nothing. Strictly additive: a non-empty strict result
  is returned untouched, and single-token / prefix queries (the `multi_record`
  path) never trigger it. Test: `tests/test_paraphrase_fallback_2026_06_19.py`.

- **Single-value size ceiling (red-team F5, 2026-06-17).** `_check_json` rejects a
  single serialized body over 1 MiB with a clear, recoverable error, so one
  oversized value can't flood agent context on recall/search.

- **Bounded learner scan (red-team F6, 2026-06-17).** `Learner._load_events` caps
  the per-run journal scan at 10k events (DoS backstop); the watermark advances so
  a large backlog drains across runs instead of spiking memory/CPU in one pass.

## [0.4.13] - 2026-06-16

### Added

- **Usage heartbeat (privacy-preserving).** Local-first memory operations never
  touch the network, so an account's request count under-reported real usage (a
  heavy user and a tire-kicker looked identical). The client now sends a
  debounced, fire-and-forget POST to `/api/plugin/heartbeat` carrying ONLY an
  aggregate operation COUNT -- no memory content, no query text, no PII beyond
  the `account_id` already held. Flushes every 15 ops or 10 min and once at
  process exit; no-op without an `account_id`; opt out with
  `SIBYL_MEMORY_TELEMETRY=0`. Never blocks or breaks a memory op; offline-safe.
  Closes the usage-visibility blind spot the beta reports surfaced (deadguy
  2026-06-14). Regression tests: `tests/test_heartbeat_2026_06_16.py` (7 cases).

### Documented

- **`forget`/archive is recoverable, not a hard delete.** Clarified that
  archiving moves an entity into `archived_entities` (recoverable, stored
  plaintext at rest) rather than destroying it; a hard-delete path is tracked
  separately. (big-patch PKG-11)

## [0.4.12] - 2026-06-11

### Fixed

- **`set_reference(key, body)` raised StorageError on a dict/list body**
  (beta report VRTX ISSUE-003, 2026-06-11). `body` now accepts a `str` or a
  JSON-serializable `dict`/`list`; mappings/sequences are coerced to canonical
  JSON (via the same `_check_json` guard used for metadata) before the INSERT.
  Any other type raises a typed `ValidationError` naming the `body` parameter
  instead of an opaque DB-layer failure. Regression test:
  `tests/test_set_reference_body_2026_06_11.py` (4 cases). (big-patch PKG-5)

## [0.4.11] - 2026-06-11

### Added

- **Cross-tenant search isolation regression test**
  (`tests/test_smoke.py::test_tenant_search_isolation`). Two tenants index
  near-identical "billing outage refund escalation ticket" vocabulary in the
  same database file; asserts `search_entities`, cross-tier `search()`, and
  `multi_record_search` each return only the calling tenant's rows (Discord
  2026-05-31 parallel-workflow report). Passes against current source: SQL
  `tenant_id` filtering holds on all three surfaces, so the reported sibling-
  case bleed is attributed to within-tenant topical ranking, addressed by the
  0.4.9 anchor-first resolver and 0.4.10 proximity re-rank. Tests only, no
  source change. (bugflow)

### Fixed

- **`search()` silently returned `[]` on unknown tier names.** Unknown values in
  `tiers` now raise `ValueError` (defense in depth behind the MCP-level
  whitelist; direct callers such as the Hermes provider inherit the fix).
  (bugflow)
- **Lint timestamp cutoffs were malformed and used deprecated
  `datetime.utcnow()`.** Python `%f` means microseconds (not SQLite's
  seconds-with-millis), so stale-entity and flagged-actor cutoffs rendered as
  `HH:MM:<microseconds>Z` with no seconds field, breaking the lexicographic
  comparison against stored `HH:MM:SS.sssZ` timestamps. Cutoffs now use
  `datetime.now(timezone.utc)` with an exactly aligned `%Y-%m-%dT%H:%M:%S.000Z`
  format. (bugflow)

## [0.4.10] - 2026-06-08

### Fixed

- **Multi-word search precision: "near-negative decoy" false positives** (chainriffs +
  KAPPA Discord reports against v0.4.2 / v0.4.4; triaged from the 2026-06-06 bug intake).
  The AND-of-tokens default (v0.4.2+) gives full recall but lets short rows that contain
  the query tokens in an unrelated context out-rank the real answer under BM25, which
  rewards term density over proximity (reported precision ~73% at recall 100%).
  `search()` and `search_entities()` now re-rank multi-word results by match tightness
  before the limit is applied: contiguous query phrase (bucket 0) > all tokens within a
  small window (bucket 1) > scattered tokens (bucket 2), with the existing BM25 `rank` as
  the in-bucket tiebreaker. No hit is dropped, so **recall is unchanged**: only the order
  changes. Single-token and `prefix=True` queries keep plain BM25 order, so
  `multi_record_search` (the anchor-first resolver, which only issues single-token
  searches) is unaffected. New module helpers `_match_tokens` / `_normalize_text` /
  `_proximity_bucket` / `_min_cover_span`; regression suite
  `tests/test_proximity_rerank_2026_06_08.py` (9 tests). Verified: scattered-decoy
  precision@1 0/6 -> 6/6 on the reproduction corpus, 119/119 suite green.

  Residual (out of scope, by design): a decoy that contains the *exact query phrase* is a
  genuine lexical-semantic collision, the documented graph-native / GNN-tier case, not
  resolvable by keyword ranking.

## [0.4.9] - 2026-06-06

### Fixed

- **Multi-record search recall/precision regression at scale (anchor-first hybrid resolver).**
  `multi_record_search` used a corpus-fraction selectivity cutoff
  (`round(0.15 * corpus_n)`) calibrated on a 24-record reconstruction. Past ~150
  records the cutoff lost meaning: almost every term read as "selective," so
  cross-cluster records cleared the gate and polluted results (tester Sylvain
  Runs 16/17, ~0.36 recall at 50-100 companies). The resolver is now anchor-first:
  anchor terms are the rarest tokens, defined RELATIVE to the rarest query term
  (`df <= ANCHOR_BAND * min_df`, scale-invariant). The gate is a HYBRID: a
  candidate survives if it is in the anchor's cluster (matches an anchor term) OR
  clears the high-coverage bar `ANCHOR_HYBRID_HI` (genuinely relevant despite
  lacking the rare anchor). A pure strict filter killed cross-cluster pollution
  but over-dropped natural-language evidence; the hybrid keeps both. Abstention
  (zero-support term) and the terminal/prep gates are unchanged. Validated two
  ways: (a) synthetic 480-record workflow A/B — full recall, 0 cross-cluster
  pollution vs the old code's 1,920 polluting hits over 120 queries (matches
  tester Runs 24-29); (b) real-data LongMemEval retrieval diagnostic — per-question
  (oracle) retrieval is not regressed (NEW >= OLD, +3.4pts), and in a combined-
  store contamination stress NEW cuts cross-question pollution ~29% for a small
  recall trade. Regression guard: `tests/test_anchor_resolver_2026_06_06.py`.

- **Cross-tier rank comparability.** `search()` BM25 ranks are not on a common
  scale across FTS tables (`journal_events_fts` is contentless). Added a tier
  tiebreaker so content tiers (entity/state/reference) sort before journal at equal
  rank, layered on the existing 0.4.7 journal cap. (tester email 19e7eb3096b4dae5)

### Added

- **`search_entities(category=...)`.** Optional exact-match category anchor on
  entity FTS, removing topical bleed across categories on multi-entity workloads
  (tester email 19e7e75af0b7780a). Backward compatible (defaults to all categories).

Sourced from Sylvain's beta Runs 24-29 + the bugflow batch dedup; this single
patch also supersedes ~20 already-fixed entries that had accumulated in the
bug-batch queue.

## [0.4.8] - 2026-06-04

### Fixed

- **Prefix-mode FTS5 crash on all-operator queries.** `_sanitize_fts5_query(prefix=True)`
  routed tokens through `_drop_fts5_operator_tokens`, whose keep-all fallback
  (`return kept or tokens`) re-introduced raw operator keywords when every token was an
  operator. The prefix path then appended `*`, producing invalid FTS5 (`OR*`, `AND*`,
  `NOT*`) that crashed the SQLite FTS5 parser with a syntax error. Prefix mode now
  hard-drops operator keywords with no fallback and returns an empty match for an
  all-operator query (no safe expansion exists). Non-prefix phrase mode is unchanged
  (quoted phrases keep `"OR"` literal and valid). Reported via the acerieus stress suite
  (LEARNING-SEARCH-PREFIX-OPERATOR-MUTATIONS-STAY-LITERAL, 2026-06-01). Found + verified
  by bugflow; operator-approved.

## [0.4.7] - 2026-06-02

Bundled bug-fix release from beta/UserSignal reports (sylvain, acerieus, cryptoxdylan), triaged + adversarially verified via bugflow.

### Security

- **Cap-enforcement bypass via a forged tier cache (SEC-13).** A local user could
  write `~/.sibyl-memory/tier_cache.json` with `account_id: null` and
  `cap_bytes: null`. For a pre-activation/free user (whose runtime `account_id`
  is also `None`), this matched the cache fast-path and returned "uncapped",
  letting an oversized write bypass the free-tier cap entirely offline. The
  uncapped fast-path now requires a real `account_id`; a null-account uncapped
  claim is distrusted and falls through to credentials-hint + server
  enforcement. A legitimately uncapped tier always carries an `account_id`.
- **Hardlink / symlink DB-path redirect across profiles (SEC-12).**
  `Storage.__init__` opened the SQLite DB after `Path.resolve()` (which follows
  symlinks) with no link guard, and `is_symlink()` is `False` for hardlinks. A
  symlinked db path or a hardlinked `memory.db` (`st_nlink > 1`) could redirect
  one profile's writes/reads into another profile's database at the SQLite
  layer. `__init__` now refuses a symlinked (final-component) or hardlinked DB
  file, raising `StorageError`. The check is on the db file only, not parent
  dirs, so symlinked / relocated home directories still work.

### Fixed

- **Search quality: journal entries drowned out real results.** On mixed-keyword
  queries, long journal entries (sharing common terms like "project",
  "research", "decision") dominated 50-80% of `search()` hits and buried
  entities / state / reference. The journal tier is now capped at one quarter of
  the global limit; the structured tiers keep the rest. The global rank-sort +
  limit still applies.

## [0.4.6] - 2026-06-01

### Fixed

- **A negative `limit` could broaden search instead of narrowing it.**
  `search()` and `search_entities()` passed `limit` straight into SQLite
  `LIMIT ?`, where `LIMIT -1` means unbounded, so `limit=-1` returned more
  rows rather than fewer. Both methods now clamp `limit` with `max(0, limit)`
  so an invalid negative limit can never broaden results.

## [0.4.5] - 2026-05-30

Adversarial QA remediation (Acer stress-test suite): two findings + a review hardening.

### Fixed

- **FTS5 corruption containment (high).** A poisoned/desynced external-content FTS5 index threw an uncontained `StorageError` out of `search()` / `search_entities()`, crashing the caller. Search now self-heals the index (`'rebuild'` from the intact base table) and retries once; contains to `[]` if unhealable (e.g. contentless journal FTS). A single poisoned row can no longer crash a search. New `_fts_query` helper routes every FTS query site; `_heal_fts` performs the rebuild.
- **Primitive entity/state bodies rejected (contract).** `set_entity` / `set_state` declared `body: dict | list` but silently accepted JSON primitives, so a bare string/number persisted and broke downstream consumers that assume structured bodies. They now raise `ValidationError`. `reference_documents` free-text `str` bodies are unaffected.

### Changed

- Corruption containment keys on the exception *class*, not a message substring (corruption surfaces under varied messages: "vtable constructor failed", "database disk image is malformed", ...). `ProgrammingError` is re-raised so a genuine code/binding bug is never masked as empty results.

Regression coverage: `tests/test_acer_stress_2026_05_30.py` (7 tests). 96/96 suite green.

### Added (Terminal B — multi-record retrieval, tester Run15)

- **`multi_record.py` — `multi_record_search(client, query, ...)`.** Two-stage
  retrieve-then-verify search for workflow / linked-record queries (whose answer
  spans several related records). Per-token recall, then verify gates: abstain on
  zero-support terms, drop purely-preparatory records on terminal-state queries,
  require a rare/selective term match, IDF-coverage rank. Drop-in for a single
  `search()` call (same hit shape); `recall()` unchanged. Fixes the tester Run15
  multi-record-miss class (bench 10/10 vs 4/10 single-pass). Uses only the public
  `MemoryClient` surface. NOTE: gate constants are bench-tuned on a 24-record
  reconstruction, not yet generalized — validate at scale or gate behind a flag
  before publish.

## [0.4.4] - 2026-05-28

Beta-tester bug-report remediation (chainriffs Discord + KAPPA rounds 3/4).

### Fixed

- **FTS5 search: uppercase operator keywords poisoned recall.** A
  natural-language query containing `AND` / `OR` / `NOT` / `NEAR`
  (e.g. `"auth AND db"`, `"cache NEAR eviction"`) had each token
  phrase-quoted into a *required literal* term, so a matched row had to
  literally contain the word "AND"/"NEAR" — recall silently collapsed to
  ~0 hits. These keywords are now dropped during tokenization so the
  remaining terms AND together (the natural intent). A query that is
  *only* operator keywords keeps them as literals so searching for the
  word "and" still resolves. (`_drop_fts5_operator_tokens`.)

### Security

- **Identifier validation: path-traversal + metacharacter defense-in-depth**
  (KAPPA #3 PARTIAL). `validate_identifier` now rejects the `..` traversal
  marker and the shell/redirection/quote metacharacters `< > | ; " \``. SQL
  was already parameterized; this guards downstream non-parameterized
  consumers (filesystem export, CLI display, logs). Apostrophe is
  deliberately allowed (legit in name-shaped keys). Bare `/` and `\` remain
  allowed per the v0.4.0 contract — rejecting raw separators is a contract
  change flagged for team decision.

## [0.4.3] - 2026-05-26

### Fixed

- **Cross-tier timestamp precision mismatch.** `_utc_now_iso()` produced
  6-digit microsecond timestamps (`45.525358Z`) while every SQL DEFAULT
  used SQLite's 3-digit milliseconds (`45.525Z`). The width difference
  broke lexicographic sorting across tiers: `'Z'` (0x5A) > `'3'` (0x33)
  at position 24, so a journal event written 0.358 ms after an entity
  update would sort *before* it in any `ORDER BY ts` merge. Now truncated
  to 3-digit milliseconds to match SQLite output. Affects journal_events,
  revenue_events, error_events, learning_runs.completed_at, and
  skill_proposals.reviewed_at. Existing rows retain their original
  precision (cosmetic, sort-correct within their own tier). Reported by
  external tester smoke test on sibyl-memory-mcp 0.1.2.

## [0.4.2] - 2026-05-22

`_sanitize_fts5_query` default mode flipped from phrase-match to
AND-of-tokens. Pre-0.4.2, multi-word natural-language queries were wrapped
as FTS5 phrases: required exact word sequence: so
`client.search("H&M tops bought")` returned 0 hits even when the haystack
contained all three words. Surfaced by the LongMemEval 50-Q benchmark on
2026-05-22 as the dominant default-UX gap for Hermes-plugin users (every
natural-language query against the plugin's search returned 0 hits).

### Changed

- `_sanitize_fts5_query(raw, *, prefix=False, as_phrase=False)`: new
  default behaviour: tokenize input into alphanumeric + underscore tokens,
  wrap each as a single-term phrase, join with spaces. FTS5 treats
  space-joined terms as implicit AND, so every token must appear in the
  matched row (in any order). Callers that need phrase-match semantics
  must now pass `as_phrase=True` explicitly.
- Empty / all-symbol input still falls back to phrase-wrapping rather than
  returning an empty match string: preserves prior safety posture.

### Added

- `tests/test_search_default_mode.py`: 8 regression tests pinning the
  new default behaviour, including end-to-end multi-word recall against
  live SQLite + FTS5 storage.

### Migration

- Callers who relied on phrase-match (rare: would have needed exact
  word sequences in stored content): pass `as_phrase=True`.
- Most callers see strictly better recall on natural-language queries with
  no code change.

## [0.4.1] - 2026-05-19

Auth-redesign wave 1 step 15: forward-compat with the server's bearer
model. `/api/plugin/check-write` accepts `Authorization: Bearer <token>`
headers in addition to the existing `session_token` body field. This
release sends both: body field for older servers, header for the new
protocol. The server populates device credentials at bind time,
so legacy `session_token`-as-bearer credentials still resolve.

### Changed

- `_capcheck.py:_default_check_write_fn` sends
  `Authorization: Bearer <token>` header on every check-write call.
  Token source priority: `payload["bearer_token"]` (server-issued in
  credentials.json schema_version >= 3) → `payload["session_token"]`
  (v1 backward compat). No behavior change against current production
  server. Companion: api-sibyllabs accepts both paths.

## [0.4.0] - 2026-05-18

KAPPA external-tester remediation release. Independent third-party install
test (KAPPA, peer Tulip-referred) against the v0.3.3 family surfaced one
blocker that broke `sibyl-memory-mcp` on PyPI plus four secondary findings.
This release lands the engine-side fixes. Companion releases:
`sibyl-memory-mcp` v0.1.2, `sibyl-memory-hermes` v0.3.2, `sibyl-memory-cli`
v0.1.3.

### Fixed

- **KAPPA-BLOCKER**. `CapExceededError` and `TierVerificationError`
  relocated from `_capcheck.py` to `exceptions.py` so they are importable
  from the canonical `sibyl_memory_client.exceptions` submodule path. The
  v0.3.3 family had them defined and re-exported only at the top-level
  package; the `.exceptions` submodule path (which `sibyl-memory-mcp`
  imports from) raised `ImportError`. `_capcheck.py` now imports them back
  for full backwards compatibility with anyone reaching into the private
  module.
- **KAPPA-RED**. `~/.sibyl-memory/memory.db` now chmod 0600 after the
  schema apply (was inheriting umask, typically 0644). WAL + SHM sidecar
  files also tightened to 0600 if present. Idempotent + non-fatal on
  chmod failure. Closes the file-perm gap KAPPA observed on a multi-user
  / CI / shared-dev-box install.
- **KAPPA-YELLOW**. `set_entity`, `set_state`, and `set_reference` now
  validate user-supplied identifiers (category, name, key) before write.
  Rejects: non-string, empty, control characters / null bytes, length
  > 1024. Raises `ValidationError` with a recovery hint. Read paths are
  unchanged: already-stored bad identifiers remain accessible so users
  can introspect and migrate. New module-level helper
  `validate_identifier(value, *, field_name)`.
- **KAPPA-YELLOW**. `search()` and `search_entities()` no longer silently
  swallow `sqlite3.OperationalError` into empty results. The error is now
  classified by `_classify_fts5_error()`:
  - schema-missing (`"no such table"`) returns empty (defense against
    partial schema state on very old DBs);
  - FTS5 syntax error (`"fts5"`, `"malformed match"`, `"syntax error near"`,
    `"no such column"`) raises `ValidationError` with the original cause
    chained;
  - anything else raises `StorageError` with the original cause chained.

### Added

- `validate_identifier(value, *, field_name)`: public helper for
  validating user-supplied identifiers consistently across the SDK.
- `_classify_fts5_error(err)`: internal helper for translating FTS5
  `OperationalError` into the appropriate exception type.

### Notes

- The 2 MB free-tier cap (KAPPA's product question) is NOT changed in this
  release. Operator decision to be made separately on whether to raise
  the cap or document the intent more explicitly.
- Existing 53/53 client tests pass unchanged. New tests covering the
  KAPPA-attributed fixes added in `tests/test_smoke.py`.

---

## [0.3.3] - 2026-05-18

Audit-remediation release. v0.3.0 pre-ship audit (2026-05-18T05:05Z) surfaced
10 critical findings across four lanes; this release lands the engine-side
fixes. Companion releases: `sibyl-memory-hermes` v0.3.1, `sibyl-memory-cli`
v0.1.2, `sibyl-memory-mcp` v0.1.1.

### Added

- `MemoryClient.search(query, *, limit=20, prefix=False, tiers=None)` -
  cross-tier FTS5 search over entities + state + reference + journal. Each
  hit is tier-tagged with `{tier, key, category, body, snippet, rank, ts}`.
  Pass `tiers=("entity", "state")` to restrict scope. The marketing claim of
  "FTS5 across all tiers" is now actually true.
- FTS5 query sanitization: every user-supplied query is wrapped as a single
  quoted FTS5 phrase before MATCH. Column-filter syntax (`name:foo`,
  `rowid:*`, etc.) can no longer escape into the FTS5 parser. Empty queries
  short-circuit to empty result (no SQL error leak).
- `_sanitize_fts5_query(raw, *, prefix=False)` helper exposed for callers
  building their own FTS5 queries.

### Changed (schema v3 migration)

- **Schema bumped to v3.** All four searchable tiers (entities, state,
  reference, journal) now have FTS5 indexes:
  - entities_fts → external-content (was standalone with body duplication)
  - state_documents_fts → NEW, external-content
  - reference_documents_fts → external-content (was standalone, never
    exposed in the public SDK)
  - journal_events_fts → NEW, contentless, payload = evaluated || acted ||
    forward || extra concatenated
- v2 → v3 migration runs automatically on first open. Detects v2's
  standalone entities_fts shape, drops it and the old reference_documents_fts,
  recreates in external-content form, and rebuilds the FTS5 indexes from
  the existing base-table data. No application data lost. ~50ms per 10k
  entities on first open after upgrade; idempotent thereafter.
- FTS5 disk footprint reduced ~50% on body-dominated tenants (v2 stored
  the entity body twice; v3 stores it once in the base table).
- FTS5 update trigger pattern fixed: was O(N) DELETE-by-UNINDEXED-column;
  now O(log N) external-content delete-by-rowid.
- `search_entities()` updated to join via rowid (the external-content
  primary key) instead of entity_id.
- `search_entities()` now returns empty list on malformed FTS5 queries
  rather than raising. Previously `client.search_entities('"')` would
  surface a `sqlite3.OperationalError` wrapped as `StorageError` with the
  full db_path interpolated into the message.

### Security

- **SEC-2**. Atomic 0600-at-create for `TierCache.store`. Previously
  used `write_text(...)` then `os.chmod(..., 0o600)`, leaving a
  world-readable window between syscalls every cache write. Now opens with
  `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0o600` set at creation
  time. No race window.
- **SEC-3**. FTS5 query sanitization on every MATCH path. Prevents
  FTS5 injection / DoS via malformed queries.
- **SEC-3**. `StorageError` messages no longer echo the absolute
  `db_path` or full SQLite error text. Original exception is chained via
  `from e` for debugging; user-visible message stays generic.
- **SEC-9**. `TierVerificationError` no longer echoes the server-side
  `error` body string in the user-visible message: strips to a generic
  "Retry shortly" pointer to avoid leaking internal server detail into
  user logs.
- **SEC-11**. `TierCache.load` refuses to follow symlinks. A
  low-privilege attacker who once had write to `~/.sibyl-memory` cannot
  redirect the cache to `/dev/null` or another file via symlink.

### Fixed

- **C2**. `__version__` no longer hardcoded. Now sourced from
  `importlib.metadata.version("sibyl-memory-client")` with the same
  `+source` fallback pattern as sibyl-memory-hermes v0.3.0. The wheel and
  the in-Python `__version__` can no longer drift (v0.3.2 published with
  `__init__.py` saying "0.3.1").
- HTTP User-Agent in `_default_check_write_fn` now built from
  `__version__` instead of hardcoded `"sibyl-memory-client/0.3.0"`. Server
  telemetry will accurately reflect the installed version.
- `from e` chaining added to `_default_check_write_fn`'s `HTTPError` and
  `URLError`/`TimeoutError`/`OSError` handlers so the original cause is
  preserved through `TierVerificationError`.

### Hygiene

- Dropped unused `Iterable` and `ConflictError` imports from `client.py`
  (DC1/DC2). Both remain in `__all__` via re-export.

## [0.3.2] - 2026-05-16

Audit-remediation release. Companion to api-sibyllabs payment-rail fixes
and the post-audit shipping pass. Closes T1-3, T1-4, T2-3 from the
2026-05-16 audit pass (full report: `memory/research/` + email
msg_id 19e33139dfc3e4d4).

### Changed

- **T1-3. `archive_entity` now goes through CapGate**. The audit found
  that `MemoryClient.archive_entity` bypassed the cap check, letting a
  free user at 1.9 MB archive their largest entities (body copied into
  archived_entities, doubling footprint) to keep writing past 2 MB. The
  method now reads the entity body first to size the proposed insert
  (`body + name + category + reason + 200B overhead`), then calls
  `self._cap_gate.check(proposed_delta_bytes=delta)` before the write
  transaction. NotFoundError still raised before any cap-gate side effect.
- **T1-3. `Learner.accept_proposal` now accepts an optional `cap_gate`**.
  `Learner.__init__` gains a `cap_gate: Any = None` parameter. When
  non-None, `accept_proposal` calls `cap_gate.check(proposed_delta_bytes=...)`
  before inserting the `reference_documents` row (skill body can be
  kilobytes). The convenience entry `MemoryClient.learner()` threads
  the client's CapGate through automatically. Direct-import callers can
  override `cap_gate=None` explicitly for tests.
- **T2-3. `_default_check_write_fn` no longer forges fake decisions on
  HTTP error**. Previously a transient 502 response synthesized
  `{ok: False, tier: "free"}` and the caller cached it as authoritative,
  locking a paid user out for up to 7 days. Now raises
  `TierVerificationError` on any HTTP error: the offline-grace path in
  `_refresh_and_check` decides whether to honor a recent cache or hard-cap.
- **T1-4. TierCacheEntry gains `server_expires_at` + `cache_token` fields**.
  `server_expires_at` is the server-supplied subscription expiry parsed
  from the `expires_at` field on the `/check-write` response. The cache
  is now honored only while `now < min(checked_at + grace_seconds,
  server_expires_at)`, which prevents the multi-grace-period attack
  where a user blackholes the network to keep using their cached paid
  tier past actual subscription expiry. Authoritative end-of-validity
  comes from the server's record, not from a refresh-able local timer.
  `cache_token` stores the credentials.signature as a defense-in-depth
  link between cache and credentials identity (sent on subsequent
  cap-checks for tamper telemetry).
- **TierCache.load/store round-trip the new fields**. Backwards
  compatible with v0.3.1 cache files (missing fields default to None).

### Schema

- TierCache file schema bumped (implicitly v2). v1 caches load fine
  with `server_expires_at=None` and `cache_token=None`; next successful
  `/check-write` upgrades them.

### Tests

- 53/53 unchanged, all green. The cap-gate addition in `archive_entity`
  fires under the default 2 MB cap on test data well below that
  threshold: no test changes needed.

### Notes for downstream

- `sibyl-memory-hermes` v0.2.2 ships in lockstep (narrows `recall()`
  exception handling to `NotFoundError` only, T2-2 fix). Earlier
  hermes versions still work; the bug they had was over-aggressive
  exception swallowing, harmless to the cap-gate plumbing.

## [0.3.1] - 2026-05-16

Tamper-evidence release. Companion to api-sibyllabs HMAC signing.

### Added

- `MemoryClient.__init__` and `MemoryClient.local()` accept two new
  optional kwargs: `credentials_claim` (dict of the canonical signed
  fields) and `credentials_signature` (hex HMAC). Both default to None
  for backwards compatibility with unsigned v0.3.0 credentials.
- `CapGate` accepts the same two kwargs and, when both are present,
  attaches them to every `/check-write` POST body. The server uses
  them to verify the signature and log `credentials_tamper_suspected`
  telemetry on mismatch. The cap-gate decision itself is unaffected -
  authoritative tier always comes from the database via
  `effectiveAccess`.

### Schema

- Credentials JSON schema v2 (server-issued 2026-05-16+): adds
  `signature` (HMAC-SHA256 hex, 64 chars) and `signed_at` (ISO ts).
  Old schema v1 credentials still load and work; the client just
  sends an unsigned request and the server skips the tamper check.

### Tests

- 53/53 unchanged, all green. The signing path is purely additive.

## [0.3.0] - 2026-05-15

Hard-cap enforcement release. Operator directive 2026-05-15: "how do
we hard-limit free users to the 2Mb size? and ensure they can't
circumvent this" → Level 1 (hard write cap) + Level 2 (signed
credentials.json, deferred) + server-authoritative tier check at the
boundary. Locked in: 7-day grace cache, hard cap on by default.

### Added

- **`_capcheck.py` module** with the cap-enforcement primitives:
  - `CapGate.check(proposed_delta_bytes)`: three fast paths plus one
    slow server-refresh path. Most writes never phone home. The slow
    path only fires when (a) a free-tier user is about to push past
    2 MB or (b) the local tier cache has expired.
  - `TierCache`: file-backed at `~/.sibyl-memory/tier_cache.json`,
    mode 0600, atomic write, JSON shape `{ account_id, tier,
    checked_at, cap_bytes }`. Honored as fresh for 7 days; honored
    for an extended 14-day grace if the user is offline.
  - `CapExceededError` (code `CAP_EXCEEDED`): carries `upgrade_url`.
  - `TierVerificationError`: raised only when the user is at the cap,
    offline, AND has no valid grace cache. Distinct from CAP_EXCEEDED
    so callers can route the two error states differently.
  - `_default_check_write_fn`: pure stdlib urllib transport. The
    default endpoint is `https://api.sibyllabs.org/api/plugin/check-write`.
    Replaceable for tests via the `check_fn` constructor kwarg.
  - Constants `FREE_TIER_CAP_BYTES = 2 * 1024 * 1024` and
    `GRACE_PERIOD_SECONDS = 7 * 24 * 60 * 60`.

- **`MemoryClient` cap wiring** (additive, non-breaking):
  - `__init__` and `local()` accept `account_id`, `session_token`,
    `tier`, and an optional `cap_gate` override.
  - Every write path (`set_entity`, `write_event`, `set_state`,
    `set_reference`) calls `self._cap_gate.check(proposed_delta_bytes=...)`
    with a JSON-byte-length estimate. Reads are never gated.
  - Pre-activation users (no `account_id`) get a strict local 2 MB cap
    with no server check possible: by design.

### Tests

- 13 new tests in `tests/test_capcheck.py` covering: under-cap (no
  server call), at-cap server says no, server upgrades a stale-cached
  user, paid-cache short-circuits server, stale paid cache triggers
  refresh, offline-at-cap with grace cache passes, offline-at-cap
  with no cache raises, pre-activation under/at cap, e2e MemoryClient
  free/paid, cache file mode is 0600, `invalidate_cache()` works.
  Full suite 53/53 green.

### Notes for downstream

- `sibyl-memory-hermes` v0.2.0 plumbs `account_id` and `session_token`
  through to the client. Earlier hermes versions still work but
  pre-activation users hit the strict local 2 MB cap.
- The Level 2 HMAC-signed `credentials.json` design is in
  `memory/research/2026-05-15-hard-cap-enforcement.md` (deferred until
  `PLUGIN_CREDENTIAL_SIGNING_KEY` is provisioned in Doppler/Vercel).

## [0.2.0] - 2026-05-15

Self-learning + memory-linting release. Operator directive 2026-05-15:
"add a self-learning cron + function to the memory deployment so the
memory learns and creates skills from things in the session just as you
do. could we also do memory linter?"

### Schema

- **v2 migration**: adds two tables. Idempotent. v1 databases auto-upgrade on next open.
  - `skill_proposals`: review queue for detected skills. Columns: id, tenant_id, created_at, pattern_kind, proposed_slug, proposed_title, proposed_body, evidence (JSON), confidence (REAL 0..1), summarizer, status (pending/accepted/rejected/superseded), reviewed_at, review_note, accepted_doc_key. UNIQUE indexes on (tenant_id, status, created_at) and (tenant_id, proposed_slug).
  - `learning_runs`: watermark log so detectors don't rescan ground they covered. Columns: id, tenant_id, started_at, completed_at, summarizer, events_scanned, proposals_made, cursor_after_ts, notes.

### Added

- **`learning.py` module** with the full self-learning loop:
  - `Learner` class: scans journal_events since last watermark, runs four pattern detectors, dedupes by slug, persists top-N proposals.
  - Four deterministic detectors: `repeated_action`, `structural_similarity`, `co_occurrence`, `temporal_routine`.
  - Three pluggable summarizer backends (per operator design directive 2026-05-15):
    - `LocalDeterministicSummarizer` (free tier default): pure SQL + Python templates, zero network.
    - `BYOKSummarizer` (paid tier opt-in): user supplies their own inference callable, SDK never holds the key.
    - `VeniceX402Summarizer` (paid tier hosted). Venice-routed via x402 against the user's pre-funded plugin balance. Endpoint design at `memory/research/2026-05-15-self-learning-design.md`.
  - Review queue API: `list_proposals`, `get_proposal`, `accept_proposal` (writes `reference_documents` row under `skill/<slug>` key with provenance metadata), `reject_proposal`.
  - Both LLM-backed summarizers gracefully fall back to local-deterministic output when the inference callable raises.

- **`lint.py` module**: local memory linter mirroring `scripts/memory-lint.mjs`:
  - `Linter` class with 9 checks across three severity tiers (critical / warning / info): schema-version, invalid-json-entity, invalid-json-state, invalid-json-journal, duplicate-entity, empty-reference, stale-entity, journal-without-acts, db-soft-cap, fts-rowcount-mismatch, flagged-actors-fresh.
  - `LintReport` dataclass with `to_dict()` (JSON-serializable) + `to_ascii()` (single-block boxed report for CLI).
  - Tunable thresholds: `soft_cap_bytes` (default 10 MB per operator decision), `stale_days` (default 90), `flag_recency_days` (default 30).

- **`MemoryClient` API surface (additive)**:
  - `client.learner(**kwargs)`: construct a tenant-bound Learner.
  - `client.learn()`: convenience: one-shot Learner.run() returning a LearningRunReport.
  - `client.list_skill_proposals(status='pending', limit=50)`.
  - `client.accept_skill_proposal(id, note=None)`.
  - `client.reject_skill_proposal(id, note=None)`.
  - `client.lint(**kwargs)`: returns a LintReport.

- **Public exports** (`__init__.py`): added `Learner`, `SkillProposal`, `LearningRunReport`, `Summarizer`, `LocalDeterministicSummarizer`, `BYOKSummarizer`, `VeniceX402Summarizer`, `Linter`, `LintReport`, `Finding`.

### Tests

- 22 new tests across two files:
  - `tests/test_learning.py`: 12 tests: schema migration v2, no-event runs, repeated-action detection, watermark dedup, structural-similarity detection, accept/reject lifecycle, BYOK invocation, Venice/x402 fallback on failure, multi-tenant isolation.
  - `tests/test_lint.py`: 10 tests: clean-DB baseline, duplicate-entity, empty-reference, stale-entity, journal-without-acts, soft-cap, ASCII report rendering, dict serialization, severity buckets, multi-tenant isolation.
- Total package coverage: 10 (existing smoke) + 12 (learning) + 10 (lint) = **32 tests, all green**.

### Compatibility

- v0.1.0 databases auto-upgrade to v2 on first open via existing idempotent `_ensure_schema()` path: no manual migration needed.
- `sibyl-memory-hermes` v0.1.0 is binary-compatible with v0.2.0 of this SDK (provider surface unchanged). Hermes-provider tests updated to expect schema_version=2.
- Local-first promise unchanged: free tier remains zero-network. BYOK / Venice routes are paid-tier opt-in only and the CLI gate enforces tier checks upstream.

### Notes for CLI integration (sibyl-labs-cli, next)

The CLI package will expose:
- `sibyl learn` → runs `client.learn()`.
- `sibyl learn review` → interactive walk of `client.list_skill_proposals()` with y/n/edit prompts.
- `sibyl lint` → runs `client.lint()`, prints `to_ascii()`, exits non-zero if `critical_count > 0`.
- Optional cron install during `sibyl init` (Linux/macOS cron, Windows Task Scheduler) for daily learn + lint.

## [0.1.0] - 2026-05-15

Initial release.

- SQLite + FTS5 port of the canonical `sibyl_memory.*` Postgres schema (10 base tables + 2 FTS5 virtuals + version table).
- `MemoryClient` public API with polymorphic constructor: `MemoryClient.local(path)`.
- Five-tier model: entities (WARM) / state_documents (HOT) / journal_events (COLD) / reference_documents (REFERENCE) / archived_entities (ARCHIVE) / flagged_actors (FLAGGED).
- Multi-tenant isolation via `tenant_id` column.
- `Storage` low-level wrapper with per-instance thread-local connection cache, WAL mode, foreign_keys=ON, busy_timeout=5000ms.
- Typed exception hierarchy (`SibylMemoryError` + subclasses).
- 10 smoke tests, all green.
- Zero runtime dependencies, MIT, Python 3.10+.
