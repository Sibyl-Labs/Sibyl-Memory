# Changelog

All notable changes to `sibyl-memory-mcp` are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows
[SemVer](https://semver.org/).

## [0.2.0] - 2026-08-31

Minor version, not a patch: the `memory_search` **response schema changed**. Every
response, including every zero, now carries an additive `verdict` object, and a
client that pins the old flat `{ok, query, count, results}` shape should read this
entry before upgrading. Nothing was removed from the response and no field changed
meaning.

- Floors `sibyl-memory-client>=0.8.0` and `sibyl-memory-hermes>=0.4.0`. The verdict
  is produced by the client and forwarded by this server, so an MCP server on the
  new response schema over an old client would advertise a field the engine below
  it cannot fill. The hermes floor is there for the same reason: this package's
  install can pull the provider, and pip's default only-if-needed upgrade strategy
  leaves an already-satisfying older sibling in place, which is exactly the
  packaging hazard the 0.1.14 floor raise was for.
- **Docker-image users must rebuild.** A self-built `sibyl-memory-mcp` image pins
  its dependencies at image build time; a `pip install -U` on the host does not
  reach inside it. Rebuild the image to pick this up.

### Added
- **`memory_search` responses now carry a `verdict`** (branch
  `lang-core-verdict`, 2026-08-31). Until this release `server.py` called
  `multi_record_search(client, query, limit=safe_limit)` with **no
  `diagnostics=` kwarg**, so the SDK's optional explanation channel — which had
  existed since 0.1.14 — was unreachable from the tool an agent actually calls.
  A `count: 0` reached the agent as `{ok, query, count, results}` and could not
  be told apart from an empty store. The verdict now rides on the RETURN of
  `multi_record_search`, so there is no call-site kwarg left to forget, and the
  response gains one additive, bounded `verdict` object naming exactly one of
  `abstained_on` / `negation_abstain` / `gated` / `empty_store` / `no_match`,
  with `recovery` and a plain-language `explain`.
- The verdict passes through `_scrub_value` and sits beside the fenced payload.
  It carries numbers, enum values and the caller's own query tokens — never
  stored-record text — so it composes with the MH-1 fence and the MH-2 output
  budget rather than becoming a second, unfenced channel out of the store. It is
  reconciled against `count` after `_bound_hits`, so the two can never disagree.
- **The `_MIN_QUERY_LEN` short-query guard names a cause too.** It was the one
  place a `count: 0` shipped with nothing attached at all.
- The tier-filtered (`tiers=`) path carries a verdict as well: bypassing the
  linker does not bypass the contract.

### Changed
- **The `memory_search` docstring no longer teaches `tiers="entity"` as the
  escape hatch from an abstention.** That advice routed around every precision
  gate, including the one that makes injection-shaped queries return nothing. It
  is replaced by the cause-scoped recovery: on `abstained_on`, drop the one token
  named in `verdict.tokens[0]` and retry with every gate still armed, at most
  twice. `tiers` remains documented as the linker bypass, with its precision
  tradeoff stated. **No server-side auto-retry** — the loop stays agent-side,
  because a silent server-side retry is indistinguishable from the silent zero
  this contract deletes. A test asserts the server calls `multi_record_search`
  exactly once per request.

### Unchanged
- Search behaviour is untouched. The MCP stdio path is byte-identical to
  `ef98f5b` on every quality field of the full multilingual battery.

### Fixed (independent adversarial review, 2026-08-31)
- **The empty-store probe ran twice on the default path** (8 `COUNT(*)` per zero
  where the pre-contract build took 1): the engine resolves `EMPTY_STORE` at its
  own single exit, and this tool called `refine_zero` on top. `refine_zero` now
  runs on the `tiers` branch only, where `client.search` genuinely does not
  probe. Back to 1 `COUNT(*)` per zero.
- The no-server-retry test asserts the absence of `ast.For` / `ast.While` rather
  than grepping for the words, which had it firing on the tool docstring and on a
  legitimate generator expression. The docstring's cause names are now asserted
  against the `VerdictCode` values, so a rename cannot ship a stale instruction
  to the agent with a green suite.

## [0.1.14] - 2026-08-22

### Changed
- **Dependency floor raised to `sibyl-memory-client>=0.7.0`, closing a
  packaging hazard** (cryptoxdylan, independent verification, 2026-08-18): the
  prior floor (`>=0.5.0`) meant `pip install -U sibyl-memory-mcp` alone was a
  silent no-op once a newer client existed on PyPI — pip's default
  only-if-needed upgrade strategy leaves an already-satisfying older client in
  place, so an MCP-only install (a self-built Docker image, a `pipx`-isolated
  install, anything not going through `sibyl-memory-cli`'s tighter floor)
  could sit on unpatched retrieval code indefinitely while reporting a clean
  install. Picks up the client 0.7.0 N4/N5/N1'-diagnostics fixes.
- **`memory_search` docstring documents the default-path abstention
  contract.** No behavior change — the untiered path has always been able to
  return `count: 0` on an ordinary paraphrase carrying one unsupported content
  word, indistinguishable from an empty store. The docstring now says so
  explicitly and tells a caller to retry with `tiers="entity"` (or the
  expected tier) when a query that should match returns nothing.

## [0.1.13-fixes] - 2026-08-16 (folded into 0.1.13, no separate release)

### Fixed
- **Default `memory_search` path (tiers omitted) now answers question-shaped
  queries.** No `server.py` change — the untiered path routes through the client's
  `multi_record_search`, which previously abstained (`count == 0`) whenever a
  query carried a zero-support *function* word (`kiedy`, `gdzie`, `when`, `who`,
  `how`, ...). The client's N1 fix classifies zero-df tokens so function-shaped
  ones are dropped while content-shaped absences (injection / `rejected` class)
  still abstain. Default-path recall on a PL/EN question battery went **1/8 →
  8/8**; the tool-boundary abstention contract is unchanged (`co0001
  nonexistenttokenzzzq report` and `was the co0001 order rejected` still return
  `count == 0`). Requires `sibyl-memory-client` with the N1/N2/N3 recall fixes
  (0.6.x follow-up to 0.6.0). New coverage:
  `tests/test_default_path_recall_2026_08_16.py`.

### Added
- **First-party Docker packaging (repo-root `Dockerfile`,
  `docker-compose.yml`, `.dockerignore`) and a README "Run with Docker"
  section.** Non-root user, pinned slim Python base, no secrets baked in,
  memory on a mounted volume. These are repo-level infra and docs only: the
  published wheel contents are unchanged at the time this section was written
  — see 0.1.14 above, which is the release that actually needed a version
  bump for retrieval fixes. The Docker files ship via the GitHub source sync.
  **Caveat surfaced 2026-08-22**: an image built with `docker build` from this
  Dockerfile bakes in whatever `sibyl-memory-client` is on PyPI at BUILD time
  (`pip install /app/sibyl-memory-mcp` inside the image, not a floating
  install) — rebuild the image (`docker build -t sibyl-memory-mcp:local .`)
  to pick up a newer client; a running or previously-built container does not
  update itself.

## [0.1.13] - 2026-08-06

### Changed
- **Dependency floor raised to `sibyl-memory-client>=0.5.0`** to pick up
  multi-language search (schema v4). The untiered `memory_search` path (which
  routes through the client's `multi_record` linker + `MemoryClient.search`) now
  resolves non-ASCII / non-Latin / CJK / Thai / compound-token queries that
  previously returned nothing — a 100-language write+query sweep went from 21/100
  to 100/100. No `server.py` code change: the improvement is entirely in the
  client the MCP server calls. See `sibyl-memory-client` 0.5.0.

## [0.1.12] - 2026-07-05

Super-patch: recovery + adjudication of the remaining Fable 10-lens audit
findings (`plugin-hardening-superpatch-plan-2026-07-05.md`).

### Fixed
- **Client-cache rebuild dropped the old `MemoryClient` without closing it
  (R26).** `_open_client` rebuilds the cached client on a `credentials.json`
  mtime change (or post-init/post-logout appearance/disappearance), but
  discarded the previous client directly, stranding every per-thread SQLite
  connection it had registered. Repeated credential-mtime changes
  accumulated open connections. The old client's storage is now closed
  (best-effort — a missing/failing `close()` never blocks serving the newly
  built client) before the cache is swapped.
- **`~/.sibyl-memory` could be created world-readable on first touch (R30).**
  `_build_client` used a bare `mkdir(parents=True, exist_ok=True)` with no
  mode; on an already-existing directory `mkdir`'s mode argument is a no-op
  too. The memory directory is now created (and, for the pre-existing case,
  explicitly `chmod`'d) at `0o700`, mirroring the CLI's credential-writing
  path and the client `Storage` hardening.
- **`memory_search` unknown-tier error had no `code` field (R31).** An
  unknown value in the `tiers` CSV param raised a builtin `ValueError`,
  which fell through `_err`'s typed exception chain and produced an error
  envelope with no `code` — inconsistent with every other tool error. It now
  raises the SDK's `ValidationError`, mapped to `code: "VALIDATION_ERROR"`;
  `_err` also gained a fallback `payload.setdefault("code", "ERROR")` so no
  future untyped exception can produce a code-less envelope again.
- **Tenant resolution had no `account_id` fallback rung (Contract T).**
  `_build_client` resolved `tenant_id=creds.get("tenant_id") or
  DEFAULT_TENANT` directly, so an activated account with a missing/empty
  `tenant_id` (legacy credentials, or a present-but-empty field) fell back
  straight to the shared `DEFAULT_TENANT` instead of its own account. Now
  resolves via the canonical ladder shared by every plugin surface:
  `tenant_id -> account_id -> DEFAULT_TENANT`.

### Metadata
- `pyproject.toml`'s `Repository` URL pointed at a foreign, nonexistent
  `sibyllabs` (no hyphen) GitHub org that 404s in live PyPI metadata.
  Corrected to `https://github.com/Sibyl-Labs/Sibyl-Memory` (R27).
- Third-party dependency `mcp` was pinned `>=1.0.0` with no upper bound, so
  a fresh install could auto-pip a future major with breaking changes.
  Capped to `mcp>=1.0.0,<2` (R29). Internal `sibyl-memory-*` pins are
  unaffected (stay `>=`, vendor-controlled names).

## [0.1.11] - 2026-06-25

Pre-launch security audit hardening.

### Security
- Ported the prompt-injection fence + per-call nonce + body/snippet size caps
  onto all four read tools (`memory_recall`, `memory_search`, `memory_list`,
  `memory_get_state`). Previously only the Hermes adapter carried this; the MCP
  server returned raw stored bodies with no fence or size cap.

### Fixed
- `memory_search` early-returns on a sub-3-character query (mirrors the adapter).

## [0.1.10] - 2026-06-19

### Fixed

- **SDK-layer argument-validation errors were plain text, not JSON (beta deadguy
  2026-06-14).** A pydantic validation failure on tool arguments returned an
  `Error executing tool: ...` string instead of the `{ok:false,code,...}` envelope
  the handler-layer errors use, so a fraction of malformed inputs broke a caller's
  JSON parse. The argument-validation guard now emits the same JSON envelope
  (`code: "VALIDATION_ERROR"`); the offending value is still never echoed back
  (SEC-14). Test: `tests/test_arg_validation_leak_2026_06_02.py`.

## [0.1.9] - 2026-06-11

### Fixed

- **`memory_search` silently returned 0 hits on tier typos.** The `tiers` CSV
  param is now validated against the `entity, state, reference, journal`
  whitelist; unknown values (e.g. `entities`) raise a clear `ToolError`
  (`isError=true`) instead of an empty ok result. (bugflow)

## [0.1.8] - 2026-06-06

### Changed

- **Pin `sibyl-memory-client>=0.4.9`.** Picks up the anchor-first hybrid
  multi-record resolver (client 0.4.9): `memory_search` now strict-filters
  multi-record / linked-record queries to the query's anchor cluster while
  keeping high-coverage natural-language evidence, eliminating cross-cluster
  pollution at scale. No MCP code change; routing through `multi_record_search`
  is unchanged.

## [0.1.7] - 2026-06-05

### Fixed

- **Tool errors now set the MCP `isError` flag (agent error-detection).**
  `_err()` previously returned a plain dict, which FastMCP delivered as a
  *successful* tool result (`isError: false`) with the error nested inside the
  payload, so an agent keying off the protocol-level `isError` flag could not
  detect the failure at all. `_err()` now raises `ToolError` carrying the same
  structured payload encoded as JSON, so callers both (a) see `isError: true`
  and (b) can still parse `error`/`code`/`recovery`/`upgrade_url` from the
  message. No tool signatures change; only the error envelope is corrected.
  Regression coverage: `tests/test_err_toolerror_2026_06_05.py`. (bugflow)

## [0.1.6] - 2026-06-04

### Added

- **`tiers` filter on `memory_search`.** The MCP `memory_search` tool now accepts an
  optional comma-separated `tiers` argument (`entity`, `state`, `reference`,
  `journal`). When set, it bypasses the multi-record linker and calls `client.search()`
  directly with the tier filter, so callers can restrict retrieval to a tier subset.
  This resolves journal-entry domination of generic-keyword queries at scale
  (cryptoxdylan, 2026-06-02): journal entries previously accounted for 50-80%+ of hits
  on shared terms like "Project"/"Research"/"Budget", outranking relevant entities.
  Omit `tiers` (or pass null) for the existing all-tier multi-record behaviour. Bumped
  `sibyl-memory-client>=0.4.8` to pull the prefix-mode FTS5 crash fix. Found + verified
  by bugflow; operator-approved.

## [0.1.5] - 2026-06-02

### Security

- **Argument-validation secret-leak guard (SEC-14).** When a caller passed a
  type-invalid argument value (e.g. `limit="sk-live-..."`), the MCP SDK's
  `Tool.run` wrapped the pydantic `ValidationError` as a `ToolError` whose
  message echoed the raw `input_value` back to the wire as an error result, so a
  secret fat-fingered into a typed argument would be reflected to the caller. The
  server now wraps the lowlevel `CallToolRequest` handler (the real dispatch
  path — reassigning `mcp.call_tool` is dead code because FastMCP binds it at
  construction) and replaces any argument-validation error message with a
  generic one that does not echo the value. Bumped `sibyl-memory-client>=0.4.7`
  to pull the cap-bypass + DB link-guard fixes through.

Regression coverage: `tests/test_arg_validation_leak_2026_06_02.py` exercises the
real lowlevel `request_handlers[CallToolRequest]` path and asserts no `input_value`
leak.

## [0.1.4] - 2026-05-30

Coerce-on-Adapter: pairs with the client 0.4.5 structured-body contract.

### Changed

- `memory_remember` / `memory_set_state` coerce a primitive body to `{"value": body}` (new `_coerce_body`), mirroring the hermes adapter. The `body` parameter is widened from `dict` to `Any` so primitives reach the coercion instead of being rejected by FastMCP's pydantic validation at the protocol layer. dict/list bodies pass through untouched.
- Requires `sibyl-memory-client>=0.4.5`.

Regression coverage: `tests/test_coa_coercion_2026_05_30.py` (12 tests, real `call_tool` path). 14/14 suite green.

### Changed (Terminal B — multi-record retrieval, tester Run15)

- **`memory_search` now routes through `multi_record_search`** (new in
  `sibyl-memory-client` 0.4.5) instead of a single `client.search()` pass.
  Workflow queries whose answer spans several linked records now surface them all
  instead of returning only the single strongest match. Same result shape. The
  client pin is already `>=0.4.5`, which ships `multi_record.py`.

## [0.1.3] - 2026-05-28

Beta-tester bug-report remediation (sylvain1550 Discord + QA note).

### Fixed

- **First-use writes failed with an opaque `SQLite IntegrityError`
  pre-activation.** With no `credentials.json`, `_build_client()` passed
  `tenant_id=None` *explicitly*, overriding the SDK's `DEFAULT_TENANT`
  default. Every write then violated the `entities.tenant_id NOT NULL`
  constraint while reads + tool discovery still worked — so a broken
  install looked healthy. Now falls back to `DEFAULT_TENANT`, matching
  `sibyl-memory-hermes`' provider behavior. Free local pre-activation
  writes succeed. (Regression test: `tests/test_first_use_tenant.py`.)
- **`__version__` drift.** The hardcoded `"0.1.0"` had drifted from the
  `0.1.2` published wheel. Now single-sourced from installed metadata via
  `importlib.metadata` (mirrors `sibyl-memory-client`), so it can never
  drift again.

### Changed

- Pin bumped to `sibyl-memory-client>=0.4.4` (FTS5 + identifier fixes).

## [0.1.2] - 2026-05-18

KAPPA external-tester remediation release. v0.1.1 was functionally broken
on PyPI: `pip install sibyl-memory-mcp` followed by the entry-point invocation
raised `ImportError: cannot import name 'CapExceededError' from
'sibyl_memory_client.exceptions'`. Reported by KAPPA (independent
third-party install test, peer Tulip-referred) after the v0.3.3 family ship.
The 93/93 audit tests passed only because they ran in-tree; there was no
clean-venv install smoke test in CI. Gap closed by the companion
`tmp-test/clean-venv-install-smoke.sh` guardrail.

### Fixed

- **KAPPA-BLOCKER**. `sibyl-memory-mcp` now imports cleanly in a fresh
  venv. The fix lives in the companion `sibyl-memory-client` v0.4.0 which
  exports `CapExceededError` and `TierVerificationError` from the
  `.exceptions` submodule path. This release bumps the client pin to
  `>=0.4.0` to consume that fix and rolls the version forward so anyone
  on `pip install sibyl-memory-mcp` picks up the working release.

### Changed

- `sibyl-memory-client` pin: `>=0.3.3` → `>=0.4.0`.
- `sibyl-memory-hermes` pin: `>=0.3.1` → `>=0.3.2`.

### Notes

- Server code (`server.py`) is unchanged from v0.1.1. The 8-tool surface
  (memory_remember / memory_recall / memory_search / memory_list /
  memory_forget / memory_set_state / memory_get_state / memory_record_event)
  remains stable.
- v0.1.1 has been yanked on PyPI.

---

## [0.1.1] - 2026-05-18

Audit-remediation release. v0.3.0 plugin-family pre-ship audit (2026-05-18T05:05Z)
flagged this package's `memory_record_event` tool as broken end-to-end (every
invocation raised TypeError). This release lands the MCP-side fixes.
Companion releases: `sibyl-memory-client` v0.3.3, `sibyl-memory-hermes` v0.3.1,
`sibyl-memory-cli` v0.1.2.

### Fixed

- **C1**. `memory_record_event` now calls the SDK's actual signature
  ``client.write_event(*, evaluated, acted, forward, extra, ts)``. The
  previous call ``client.write_event(kind, body, category=category,
  name=name)`` referenced parameters that don't exist and raised
  TypeError on every invocation. The high-level (kind, body, category,
  name) contract is preserved by translating: kind+body → `acted={kind,
  body}`, optional category+name → `extra={category, name}`.
- **H2**. `memory_get_state` now unpacks the SDK's `{body, updated_at}`
  return shape into a flat response: `{ok, key, body: <user payload>,
  updated_at: <iso ts>}`. Previously returned `body` containing the full
  wrapper, so "body" meant two different things at different nesting
  depths in the same response.
- **N3**. `memory_list` `category` parameter is now Optional. Matches
  the SDK + Hermes adapter behavior: pass it to filter, omit to list
  across all categories.

### Changed

- **P-H1**. `MemoryClient` is cached at module scope. Previously rebuilt
  on every tool call (reading schema.sql from disk + bootstrapping FTS5
  vtables: 10-50 ms per call). Cache invalidates on credentials.json
  mtime change so `sibyl upgrade` is still picked up without a server
  restart. Net effect: agent recall/search latency drops to single-digit
  milliseconds.
- **memory_search now spans all four tiers** (entities + state +
  reference + journal). Backed by the new `MemoryClient.search()` in
  client v0.3.3. Each hit carries a `tier` tag. The MCP server marketing
  description and tool docstring now match the actual behavior.
- Query sanitization handled by the client SDK (FTS5 column-filter
  syntax can't break out into the parser). MCP server didn't need
  its own sanitization: it's downstream of the SDK fix.

### Security

- **SEC-4 / SEC-11**. `_load_credentials` refuses to follow symlinks.
  Previously called `read_text()` on the resolved path, which would
  silently follow.

### Dependencies

- `sibyl-memory-client>=0.3.3` (was `>=0.3.2`)
- `sibyl-memory-hermes>=0.3.1` (was `>=0.2.2`)

## [0.1.0] - 2026-05-17

Initial release. Operator question 2026-05-17: "currently i'm only seeing
instructions for Hermes agent, how could this be used with claude code or
codex?": answer: an MCP server wrapping `MemoryClient.local()`. Both Claude
Code and Codex CLI consume MCP, so a single server unlocks both.

### Added

- **MCP server** (`sibyl-memory-mcp` console script + `python -m sibyl_memory_mcp`)
  using the official `mcp>=1.0.0` Python SDK with FastMCP convenience layer.
- **8 tools** exposed over stdio transport:
  - `memory_remember`. `set_entity(category, name, body)`
  - `memory_recall`. `get_entity(category, name)`
  - `memory_search`. `search_entities(query, limit)` (FTS5)
  - `memory_list`. `list_entities(category, limit)`
  - `memory_forget`. `archive_entity(category, name, reason)`
  - `memory_set_state`. `set_state(key, body)` (HOT tier)
  - `memory_get_state`. `get_state(key)`
  - `memory_record_event`. `write_event(kind, body, category, name)` (COLD tier)
- **Auto-reads** `~/.sibyl-memory/credentials.json` on every tool call so tier
  changes from `sibyl upgrade` are picked up without restarting the server.
- **Typed error envelope** mapping SDK exceptions to MCP-friendly payloads:
  `CAP_EXCEEDED` (with `upgrade_url`), `TIER_GATED`, `TIER_VERIFICATION_FAILED`,
  `NOT_FOUND`, `VALIDATION_ERROR`. Agents can reason about the right next move.
- **Env overrides**: `SIBYL_MEMORY_DB`, `SIBYL_CREDENTIALS` for non-default
  install locations + multi-account scenarios.

### Design notes

- Re-opens `MemoryClient.local()` on every tool call. SQLite open is
  sub-millisecond and this keeps the server stateless: no stale tier cache
  in the process, every call sees the current credentials.
- Free-tier 2 MB cap is enforced server-side against the database (HMAC-signed
  credentials prevent local tampering). The MCP server has no way to bypass it.
- Tool names are prefixed `memory_` so they namespace cleanly when an agent
  has multiple MCP servers loaded.

### Depends on

- `mcp>=1.0.0` (official Anthropic Python SDK)
- `sibyl-memory-client>=0.3.2` (cap-gate + signed credentials)
- `sibyl-memory-hermes>=0.2.2` (credentials loader)

### Compatible with

- **Claude Code**: add to `~/.claude/settings.json` or project `.mcp.json`
- **Codex CLI**: add to `~/.codex/config.toml`
- **Cursor**: add to `~/.cursor/mcp.json`
- **Continue**: add to `~/.continue/config.json` mcpServers block
- Any other MCP-spec-compliant client.

### License

MIT.
