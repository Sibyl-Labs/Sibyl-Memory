# Changelog

All notable changes to `sibyl-memory-mcp` are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows
[SemVer](https://semver.org/).

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
