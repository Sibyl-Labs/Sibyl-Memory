# Changelog

All notable changes to `sibyl-memory-langgraph` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [0.3.0] - Unreleased

Account-gated release (spec account-gated-plugin-spec-2026-09-27, 3.3).

### 2026-10-14 - Pre-release cleanup
- `sibyl_memory_langgraph.__version__` reports the installed version (it said 0.2.0 on 0.3.0).
  It is read from package metadata, like the other packages.

### Added (agent handoff messages, 2026-10-06)
Spec agent-handoff-messages-spec-2026-10-03 (rows L1, L2; needs sibyl-memory-client 0.9.0).
- No code change in the store: `EntitlementError` still propagates unchanged and now carries the
  handoff (`e.next`, `e.human_action`; `str(e)` ends with the block). README and the
  `SibylStore` docstring document both attributes.
- Tests: `tests/test_handoff_2026_10_03.py` (+7). Suite: 470 passed, 5 xfailed.

### Unchanged (paid tier R1, 2026-09-29)
- No automatic save or skill proposals in a framework store; README says where they run.

### Unchanged (save and debloat, 2026-09-27)
- No lifecycle for a framework store: the save and debloat procedures belong to agent
  sessions (MCP, Hermes, Claude Code hooks). README paragraph only.

### Changed (independent check of save and debloat, 2026-09-27, SD-7)
- A `SibylStore` client no longer counts writes into `~/.sibyl-memory/lifecycle.json` (the
  client's counting is now opt-in, and LangGraph has no lifecycle). README: run `sibyl skills
  uninstall` before uninstalling the CLI.

### Changed (independent check fix batch, 2026-09-27, GATE-15)
- README privacy section: the size check and the usage heartbeat also carry
  this computer's signed access token (account id, tier, cap, device hash,
  account flags; no personal data). No code change.

### Tests (gate fix before the independent check, 2026-09-27)
- conftest: the network guard is installed at import and every test fails if
  it attempted a non-loopback connection or lookup, even one it caught;
  `test_network_guard_2026_09_27.py` proves the guard. No langgraph code
  change; data written before accounts under `DEFAULT_TENANT` moves into the
  account tenant through client 0.9.0 (README updated).

### Changed
- **An account is required.** `SibylStore()` still constructs without one,
  but `batch` / `abatch` (so `get`, `put`, `search`, `delete`,
  `list_namespaces`) raise `sibyl_memory_client.EntitlementError` until
  `sibyl init` runs. A framework store propagates it; LangGraph surfaces it to
  the graph. `tenant_id=` (DEFAULT_TENANT included) chooses the tenant, never
  the access. The gate reads the `credentials.json` next to the DB file when
  there is one, else `~/.sibyl-memory/credentials.json`; an explicit
  `client=` keeps whatever gate that client carries (the embedding path).
- README: `DEFAULT_TENANT` is a constant for tests and pre-account data; the
  network disclosure lists the daily account check-in.
- Dependency floor: `sibyl-memory-client>=0.9.0`.

### Compatibility
- Verified in a fresh venv with the five built wheels against hermes-agent
  0.19.0 (current on PyPI at build time).

## [0.2.0] "Lucid" - 2026-08-31

Minor version: `SibylStore` gained `last_search_verdict`, and the underlying
`search_entities` now returns a `list` subclass. `BaseStore.search` still returns
`list[SearchItem]` and its rows are unchanged.

- Floors `sibyl-memory-client>=0.8.0`, raised from `>=0.5.0`. The store reads a
  verdict the client produces; over an old client `last_search_verdict` would be
  permanently `None` while the package advertised it. The floor also stops a
  partial upgrade pairing this store with a client whose search shadow uses the
  older stored rendering.
- `__version__` in `__init__.py` was stale at `0.1.0` while the package shipped
  as 0.1.1. It now tracks the pyproject version.

### Added
- **`SibylStore.last_search_verdict`** (branch `lang-core-verdict`,
  2026-08-31). langgraph's `BaseStore.search` signature is fixed as
  `list[SearchItem]`, so a zero cannot carry its own explanation through that
  API. What this store can do is stop throwing the cause away: the canonical
  verdict of the most recent query-backed search is exposed on the store, and an
  empty FTS result is logged at DEBUG with its cause, so an operator debugging
  "the store returns nothing" reads `empty_store` or `no_match` instead of
  silence.
- This package declares no cause vocabulary of its own — asserted by a test.

### Unchanged
- Row handling is untouched. `search_entities` now returns a `list` subclass, and
  the store's slicing, filtering and comprehension over it behave identically.

### Fixed (independent adversarial review, 2026-08-31)
- **A zero-row page could ship with `verdict.code == "ok"`.** The verdict was
  recorded from the raw FTS result, before the namespace-prefix filter, the value
  filter and the offset/limit slice, so a page emptied by any of those three
  reported `ok` and `explain(): "5 row(s) matched."` to a caller holding zero
  rows — a confidently wrong explanation, which is worse than the silence this
  contract replaces. Two further branches (`limit == 0`, and the non-query
  listing path) never set the verdict at all and retained the previous call's.
  It is now cleared first on every path and stamped after the final slice.

## [0.1.1] - 2026-08-06

### Changed
- **Dependency floor raised to `sibyl-memory-client>=0.5.0`** for multi-language
  search (schema v4). `SibylStore` search now resolves non-ASCII / non-Latin /
  CJK / Thai / compound-token queries that previously returned nothing
  (100-language sweep: 21/100 → 100/100). No `SibylStore` code change. See
  `sibyl-memory-client` 0.5.0.
- **Free-tier storage cap raised 2 MiB → 5 MiB (inherited).** The 0.5.0
  folded-trigram search shadow roughly doubles on-disk footprint; the client's
  default free cap (`FREE_TIER_CAP_BYTES`) was raised to 5 MiB (5,242,880 bytes)
  to compensate, and `SibylStore(tier="free")` inherits it — no `SibylStore` code
  change. `test_adv_fidelity.py::test_body_cap_boundary_no_truncation` (a ~1 MiB
  value that occupies ~2.3 MB once the shadow mirrors it) now stores under the
  5 MiB cap on its own merit rather than tripping `CapExceededError`; its cap
  expectation was updated to the 5 MiB boundary. See `sibyl-memory-client` 0.5.0.

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
