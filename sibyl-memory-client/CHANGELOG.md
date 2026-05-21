# Changelog

All notable changes to `sibyl-memory-client` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [0.4.1] — 2026-05-19

Auth-redesign wave 1 step 15 — forward-compat with the server's v6 bearer
model. `/api/plugin/check-write` accepts `Authorization: Bearer <token>`
headers in addition to the existing `session_token` body field. This
release sends both: body field for older servers, header for the new
protocol. Server-side schema v6 populated `bearer_tokens` at bind time,
so legacy `session_token`-as-bearer credentials still resolve.

### Changed

- `_capcheck.py:_default_check_write_fn` sends
  `Authorization: Bearer <token>` header on every check-write call.
  Token source priority: `payload["bearer_token"]` (server-issued in
  credentials.json schema_version >= 3) → `payload["session_token"]`
  (v1 backward compat). No behavior change against current production
  server. Companion: api-sibyllabs accepts both paths since v6 schema.

## [0.4.0] — 2026-05-18

KAPPA external-tester remediation release. Independent third-party install
test (KAPPA, peer Tulip-referred) against the v0.3.3 family surfaced one
blocker that broke `sibyl-memory-mcp` on PyPI plus four secondary findings.
This release lands the engine-side fixes. Companion releases:
`sibyl-memory-mcp` v0.1.2, `sibyl-memory-hermes` v0.3.2, `sibyl-memory-cli`
v0.1.3.

### Fixed

- **KAPPA-BLOCKER** — `CapExceededError` and `TierVerificationError`
  relocated from `_capcheck.py` to `exceptions.py` so they are importable
  from the canonical `sibyl_memory_client.exceptions` submodule path. The
  v0.3.3 family had them defined and re-exported only at the top-level
  package; the `.exceptions` submodule path (which `sibyl-memory-mcp`
  imports from) raised `ImportError`. `_capcheck.py` now imports them back
  for full backwards compatibility with anyone reaching into the private
  module.
- **KAPPA-RED** — `~/.sibyl-memory/memory.db` now chmod 0600 after the
  schema apply (was inheriting umask, typically 0644). WAL + SHM sidecar
  files also tightened to 0600 if present. Idempotent + non-fatal on
  chmod failure. Closes the file-perm gap KAPPA observed on a multi-user
  / CI / shared-dev-box install.
- **KAPPA-YELLOW** — `set_entity`, `set_state`, and `set_reference` now
  validate user-supplied identifiers (category, name, key) before write.
  Rejects: non-string, empty, control characters / null bytes, length
  > 1024. Raises `ValidationError` with a recovery hint. Read paths are
  unchanged: already-stored bad identifiers remain accessible so users
  can introspect and migrate. New module-level helper
  `validate_identifier(value, *, field_name)`.
- **KAPPA-YELLOW** — `search()` and `search_entities()` no longer silently
  swallow `sqlite3.OperationalError` into empty results. The error is now
  classified by `_classify_fts5_error()`:
  - schema-missing (`"no such table"`) returns empty (defense against
    partial schema state on very old DBs);
  - FTS5 syntax error (`"fts5"`, `"malformed match"`, `"syntax error near"`,
    `"no such column"`) raises `ValidationError` with the original cause
    chained;
  - anything else raises `StorageError` with the original cause chained.

### Added

- `validate_identifier(value, *, field_name)` — public helper for
  validating user-supplied identifiers consistently across the SDK.
- `_classify_fts5_error(err)` — internal helper for translating FTS5
  `OperationalError` into the appropriate exception type.

### Notes

- The 2 MB free-tier cap (KAPPA's product question) is NOT changed in this
  release. Operator decision to be made separately on whether to raise
  the cap or document the intent more explicitly.
- Existing 53/53 client tests pass unchanged. New tests covering the
  KAPPA-attributed fixes added in `tests/test_smoke.py`.

---

## [0.3.3] — 2026-05-18

Audit-remediation release. v0.3.0 pre-ship audit (2026-05-18T05:05Z) surfaced
10 critical findings across four lanes; this release lands the engine-side
fixes. Companion releases: `sibyl-memory-hermes` v0.3.1, `sibyl-memory-cli`
v0.1.2, `sibyl-memory-mcp` v0.1.1.

### Added

- `MemoryClient.search(query, *, limit=20, prefix=False, tiers=None)` —
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

- **SEC-2** — Atomic 0600-at-create for `TierCache.store`. Previously
  used `write_text(...)` then `os.chmod(..., 0o600)`, leaving a
  world-readable window between syscalls every cache write. Now opens with
  `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0o600` set at creation
  time. No race window.
- **SEC-3** — FTS5 query sanitization on every MATCH path. Prevents
  FTS5 injection / DoS via malformed queries.
- **SEC-3** — `StorageError` messages no longer echo the absolute
  `db_path` or full SQLite error text. Original exception is chained via
  `from e` for debugging; user-visible message stays generic.
- **SEC-9** — `TierVerificationError` no longer echoes the server-side
  `error` body string in the user-visible message — strips to a generic
  "Retry shortly" pointer to avoid leaking internal server detail into
  user logs.
- **SEC-11** — `TierCache.load` refuses to follow symlinks. A
  low-privilege attacker who once had write to `~/.sibyl-memory` cannot
  redirect the cache to `/dev/null` or another file via symlink.

### Fixed

- **C2** — `__version__` no longer hardcoded. Now sourced from
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

## [0.3.2] — 2026-05-16

Audit-remediation release. Companion to api-sibyllabs payment-rail fixes
and the post-audit shipping pass. Closes T1-3, T1-4, T2-3 from the
2026-05-16 audit pass (full report: `memory/research/` + email
msg_id 19e33139dfc3e4d4).

### Changed

- **T1-3 — `archive_entity` now goes through CapGate**. The audit found
  that `MemoryClient.archive_entity` bypassed the cap check, letting a
  free user at 1.9 MB archive their largest entities (body copied into
  archived_entities, doubling footprint) to keep writing past 2 MB. The
  method now reads the entity body first to size the proposed insert
  (`body + name + category + reason + 200B overhead`), then calls
  `self._cap_gate.check(proposed_delta_bytes=delta)` before the write
  transaction. NotFoundError still raised before any cap-gate side effect.
- **T1-3 — `Learner.accept_proposal` now accepts an optional `cap_gate`**.
  `Learner.__init__` gains a `cap_gate: Any = None` parameter. When
  non-None, `accept_proposal` calls `cap_gate.check(proposed_delta_bytes=...)`
  before inserting the `reference_documents` row (skill body can be
  kilobytes). The convenience entry `MemoryClient.learner()` threads
  the client's CapGate through automatically. Direct-import callers can
  override `cap_gate=None` explicitly for tests.
- **T2-3 — `_default_check_write_fn` no longer forges fake decisions on
  HTTP error**. Previously a transient 502 response synthesized
  `{ok: False, tier: "free"}` and the caller cached it as authoritative,
  locking a paid user out for up to 7 days. Now raises
  `TierVerificationError` on any HTTP error — the offline-grace path in
  `_refresh_and_check` decides whether to honor a recent cache or hard-cap.
- **T1-4 — TierCacheEntry gains `server_expires_at` + `cache_token` fields**.
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
  threshold — no test changes needed.

### Notes for downstream

- `sibyl-memory-hermes` v0.2.2 ships in lockstep (narrows `recall()`
  exception handling to `NotFoundError` only, T2-2 fix). Earlier
  hermes versions still work; the bug they had was over-aggressive
  exception swallowing, harmless to the cap-gate plumbing.

## [0.3.1] — 2026-05-16

Tamper-evidence release. Companion to api-sibyllabs HMAC signing.

### Added

- `MemoryClient.__init__` and `MemoryClient.local()` accept two new
  optional kwargs: `credentials_claim` (dict of the canonical signed
  fields) and `credentials_signature` (hex HMAC). Both default to None
  for backwards compatibility with unsigned v0.3.0 credentials.
- `CapGate` accepts the same two kwargs and, when both are present,
  attaches them to every `/check-write` POST body. The server uses
  them to verify the signature and log `credentials_tamper_suspected`
  telemetry on mismatch. The cap-gate decision itself is unaffected —
  authoritative tier always comes from the database via
  `effectiveAccess`.

### Schema

- Credentials JSON schema v2 (server-issued 2026-05-16+) — adds
  `signature` (HMAC-SHA256 hex, 64 chars) and `signed_at` (ISO ts).
  Old schema v1 credentials still load and work; the client just
  sends an unsigned request and the server skips the tamper check.

### Tests

- 53/53 unchanged, all green. The signing path is purely additive.

## [0.3.0] — 2026-05-15

Hard-cap enforcement release. Operator directive 2026-05-15: "how do
we hard-limit free users to the 2Mb size? and ensure they can't
circumvent this" → Level 1 (hard write cap) + Level 2 (signed
credentials.json, deferred) + server-authoritative tier check at the
boundary. Locked in: 7-day grace cache, hard cap on by default.

### Added

- **`_capcheck.py` module** with the cap-enforcement primitives:
  - `CapGate.check(proposed_delta_bytes)` — three fast paths plus one
    slow server-refresh path. Most writes never phone home. The slow
    path only fires when (a) a free-tier user is about to push past
    2 MB or (b) the local tier cache has expired.
  - `TierCache` — file-backed at `~/.sibyl-memory/tier_cache.json`,
    mode 0600, atomic write, JSON shape `{ account_id, tier,
    checked_at, cap_bytes }`. Honored as fresh for 7 days; honored
    for an extended 14-day grace if the user is offline.
  - `CapExceededError` (code `CAP_EXCEEDED`) — carries `upgrade_url`.
  - `TierVerificationError` — raised only when the user is at the cap,
    offline, AND has no valid grace cache. Distinct from CAP_EXCEEDED
    so callers can route the two error states differently.
  - `_default_check_write_fn` — pure stdlib urllib transport. The
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
    with no server check possible — by design.

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

## [0.2.0] — 2026-05-15

Self-learning + memory-linting release. Operator directive 2026-05-15:
"add a self-learning cron + function to the memory deployment so the
memory learns and creates skills from things in the session just as you
do. could we also do memory linter?"

### Schema

- **v2 migration** — adds two tables. Idempotent. v1 databases auto-upgrade on next open.
  - `skill_proposals` — review queue for detected skills. Columns: id, tenant_id, created_at, pattern_kind, proposed_slug, proposed_title, proposed_body, evidence (JSON), confidence (REAL 0..1), summarizer, status (pending/accepted/rejected/superseded), reviewed_at, review_note, accepted_doc_key. UNIQUE indexes on (tenant_id, status, created_at) and (tenant_id, proposed_slug).
  - `learning_runs` — watermark log so detectors don't rescan ground they covered. Columns: id, tenant_id, started_at, completed_at, summarizer, events_scanned, proposals_made, cursor_after_ts, notes.

### Added

- **`learning.py` module** with the full self-learning loop:
  - `Learner` class — scans journal_events since last watermark, runs four pattern detectors, dedupes by slug, persists top-N proposals.
  - Four deterministic detectors: `repeated_action`, `structural_similarity`, `co_occurrence`, `temporal_routine`.
  - Three pluggable summarizer backends (per operator design directive 2026-05-15):
    - `LocalDeterministicSummarizer` (free tier default) — pure SQL + Python templates, zero network.
    - `BYOKSummarizer` (paid tier opt-in) — user supplies their own inference callable, SDK never holds the key.
    - `VeniceX402Summarizer` (paid tier hosted) — Venice-routed via x402 against the user's pre-funded plugin balance. Endpoint design at `memory/research/2026-05-15-self-learning-design.md`.
  - Review queue API: `list_proposals`, `get_proposal`, `accept_proposal` (writes `reference_documents` row under `skill/<slug>` key with provenance metadata), `reject_proposal`.
  - Both LLM-backed summarizers gracefully fall back to local-deterministic output when the inference callable raises.

- **`lint.py` module** — local memory linter mirroring `scripts/memory-lint.mjs`:
  - `Linter` class with 9 checks across three severity tiers (critical / warning / info): schema-version, invalid-json-entity, invalid-json-state, invalid-json-journal, duplicate-entity, empty-reference, stale-entity, journal-without-acts, db-soft-cap, fts-rowcount-mismatch, flagged-actors-fresh.
  - `LintReport` dataclass with `to_dict()` (JSON-serializable) + `to_ascii()` (single-block boxed report for CLI).
  - Tunable thresholds: `soft_cap_bytes` (default 10 MB per operator decision), `stale_days` (default 90), `flag_recency_days` (default 30).

- **`MemoryClient` API surface (additive)**:
  - `client.learner(**kwargs)` — construct a tenant-bound Learner.
  - `client.learn()` — convenience: one-shot Learner.run() returning a LearningRunReport.
  - `client.list_skill_proposals(status='pending', limit=50)`.
  - `client.accept_skill_proposal(id, note=None)`.
  - `client.reject_skill_proposal(id, note=None)`.
  - `client.lint(**kwargs)` — returns a LintReport.

- **Public exports** (`__init__.py`): added `Learner`, `SkillProposal`, `LearningRunReport`, `Summarizer`, `LocalDeterministicSummarizer`, `BYOKSummarizer`, `VeniceX402Summarizer`, `Linter`, `LintReport`, `Finding`.

### Tests

- 22 new tests across two files:
  - `tests/test_learning.py` — 12 tests: schema migration v2, no-event runs, repeated-action detection, watermark dedup, structural-similarity detection, accept/reject lifecycle, BYOK invocation, Venice/x402 fallback on failure, multi-tenant isolation.
  - `tests/test_lint.py` — 10 tests: clean-DB baseline, duplicate-entity, empty-reference, stale-entity, journal-without-acts, soft-cap, ASCII report rendering, dict serialization, severity buckets, multi-tenant isolation.
- Total package coverage: 10 (existing smoke) + 12 (learning) + 10 (lint) = **32 tests, all green**.

### Compatibility

- v0.1.0 databases auto-upgrade to v2 on first open via existing idempotent `_ensure_schema()` path — no manual migration needed.
- `sibyl-memory-hermes` v0.1.0 is binary-compatible with v0.2.0 of this SDK (provider surface unchanged). Hermes-provider tests updated to expect schema_version=2.
- Local-first promise unchanged: free tier remains zero-network. BYOK / Venice routes are paid-tier opt-in only and the CLI gate enforces tier checks upstream.

### Notes for CLI integration (sibyl-labs-cli, next)

The CLI package will expose:
- `sibyl learn` → runs `client.learn()`.
- `sibyl learn review` → interactive walk of `client.list_skill_proposals()` with y/n/edit prompts.
- `sibyl lint` → runs `client.lint()`, prints `to_ascii()`, exits non-zero if `critical_count > 0`.
- Optional cron install during `sibyl init` (Linux/macOS cron, Windows Task Scheduler) for daily learn + lint.

## [0.1.0] — 2026-05-15

Initial release.

- SQLite + FTS5 port of the canonical `sibyl_memory.*` Postgres schema (10 base tables + 2 FTS5 virtuals + version table).
- `MemoryClient` public API with polymorphic constructor: `MemoryClient.local(path)`.
- Five-tier model: entities (WARM) / state_documents (HOT) / journal_events (COLD) / reference_documents (REFERENCE) / archived_entities (ARCHIVE) / flagged_actors (FLAGGED).
- Multi-tenant isolation via `tenant_id` column.
- `Storage` low-level wrapper with per-instance thread-local connection cache, WAL mode, foreign_keys=ON, busy_timeout=5000ms.
- Typed exception hierarchy (`SibylMemoryError` + subclasses).
- 10 smoke tests, all green.
- Zero runtime dependencies, MIT, Python 3.10+.
