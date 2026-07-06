# Changelog

All notable changes to `sibyl-memory-langgraph` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [0.1.0] - 2026-07-05

Initial release. `SibylStore`, a LangGraph `BaseStore` backed by Sibyl Memory's
local SQLite + FTS5 engine — durable, long-term, cross-thread agent memory with
no vector database and no embeddings (lexical FTS5 search only). Ships hardened
against the recovered findings from the Fable 10-lens audit
(`plugin-hardening-superpatch-plan-2026-07-05.md`) before first publish, so
these land as part of 0.1.0 rather than a follow-up patch.

### Added
- Full long-term `BaseStore` surface (`get`/`put`/`delete`/`search`/
  `list_namespaces`) implemented via `batch`/`abatch`. Not a checkpointer —
  short-term graph-state serialization is out of scope.
- Namespace tuple <-> Sibyl `category` mapping (`"/".join(namespace)`), key <->
  entity `name`, value dict <-> entity `body` (JSON). Namespace elements are
  validated as non-empty strings containing no `/` and no `..` so the join
  stays unambiguous and path-traversal-safe.
- Value-filter operators `$eq $ne $gt $gte $lt $lte $in $nin` plus implicit
  equality, evaluated with native Python ordering (not float coercion).
- `list_namespaces` with prefix/suffix match conditions and `max_depth`
  truncation.

### Fixed (pre-publish audit hardening)
- **O(categories) search fan-out (R14 / Hardening #2).** The naive
  implementation would issue one FTS `MATCH` per category and buffer up to the
  10,000-row pool EACH — worst case ~10^4 categories x 10^4 rows for a single
  query. `_search` now issues ONE FTS `MATCH` across all categories, then
  applies the namespace-prefix (and value-filter) as a post-filter, fetching
  the full pool only when post-filtering is needed so a filter-passing row
  ranked deeper than the page isn't truncated away first. Total rows
  materialized per call stays bounded by the client's `MAX_LIMIT` (10,000); a
  warning is logged if that ceiling is hit (results may be incomplete for
  stores larger than a single pass covers — no client-side cursor exists yet).
- **Pagination `TypeError`s and unbounded negative-limit slices (R32 / R33).**
  `search` and `list_namespaces` both normalize `(limit, offset)` through one
  `_clamp_page` helper: `limit=None` resolves to the op's documented default
  instead of tripping `offset + None` arithmetic; a negative limit clamps to 0
  instead of producing a negative-index slice that silently returned nearly
  every row; a negative/None offset clamps to 0; the limit is capped at the
  10,000-row candidate pool.
- **Filter operator crashes on malformed operands (R16).** `$gt`/`$gte`/`$lt`/
  `$lte` against an incomparable pair (e.g. dict vs int) no longer raises a raw
  `TypeError` that would abort an otherwise-valid batch — it now evaluates as
  "no match." `$in`/`$nin` validate the operand is iterable up front and raise
  a clean `ValueError` naming the operator instead of crashing on a
  non-iterable membership test.
- **Empty-dict filter vacuously matched every row (R34).** `{"f": {}}` was
  read as an (empty) operator map and matched unconditionally. Only a
  NON-EMPTY dict of `$`-prefixed keys is now treated as an operator map;
  anything else — including `{}` — falls to the equality branch, so `{"f":
  {}}` matches only rows where `f == {}`.
- **Unknown `match_type` failed open (R35).** `_ns_matches` returned `True`
  (matching every namespace) for an unrecognized `match_type`. It now raises
  `ValueError` naming the unsupported type, mirroring `_match_filter`'s
  unknown-operator handling — a typo'd/unsupported condition is loud instead
  of silently returning the entire namespace set.
- **`batch` was per-op best-effort with no pre-flight (R25).** Each `PutOp`
  commits independently through the client, so a raise partway through a batch
  could leave an earlier prefix committed with no signal. Every `PutOp` in a
  batch is now fully validated (namespace shape/traversal, non-empty string
  key, string dict keys, JSON-serializability, bounded nesting depth) BEFORE
  any op in the batch executes, so the common failure modes (a malformed op
  anywhere in the batch) fail the whole batch atomically. A failure that only
  surfaces during execution (I/O error, cap exceeded on the Nth write) can
  still leave prior writes applied — this is documented, not a full
  transactional guarantee.
- **Default store ran identity-blind on `DEFAULT_TENANT` (Hardening #5 /
  Contract T).** With no explicit `client=` or `tenant_id=`, `SibylStore()`
  now reads `credentials.json` beside the DB file (written by `sibyl init`)
  and resolves the tenant via the canonical ladder shared by every plugin
  surface: `credentials.tenant_id -> credentials.account_id ->
  DEFAULT_TENANT`. `DEFAULT_TENANT` is reached only when credentials are
  genuinely absent (un-activated). The credentials read is symlink-guarded
  (mirrors `sibyl-memory-hermes` SEC-11) and never raises — any error degrades
  to the un-activated default.
- **Telemetry/local-first posture undocumented (Hardening #7).** README now
  states explicitly: fully local reads/writes with no network round-trip for
  store operations; zero network while un-activated; once activated, only a
  privacy-preserving debounced usage heartbeat (aggregate operation count,
  `account_id` only — never memory content, query text, or entity names) plus
  the cap-verification ping, both fire-and-forget and offline-safe; opt out
  entirely with `SIBYL_MEMORY_TELEMETRY=0`.

### Metadata
- Published with a conservative upper bound on the third-party
  `langgraph-checkpoint` dependency (`>=2.0.0,<3`) rather than an unbounded
  `>=`, so a fresh install can't auto-pip a future breaking major (R29). The
  internal `sibyl-memory-client` pin stays `>=` (vendor-controlled name).
- `pyproject.toml` ships a `Repository` URL
  (`https://github.com/Sibyl-Labs/Sibyl-Memory`) from first publish (R27) —
  earlier sibling packages omitted it entirely or pointed at a foreign,
  nonexistent GitHub org.
