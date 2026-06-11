# Changelog

All notable changes to `sibyl-memory-hermes` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [0.3.9] - 2026-06-11

### Fixed

- **Hermes 0.7+ did not discover the plugin as a memory provider** (beta report
  Sylvain, 2026-06-11, Hermes Agent v0.7.0). `install-plugin` wrote only the
  legacy user-plugin path `$HERMES_HOME/plugins/sibyl`, which shows in
  `hermes plugins list` but is NOT scanned for memory providers on 0.7+. The
  installer now ALSO targets the 0.7+ memory-provider scan path
  `<hermes pkg>/plugins/memory/sibyl` (auto-detected via importlib, or set with
  the new `--memory-provider-path` flag), keeping the user-plugin write for
  older Hermes. A `PermissionError` on a root-owned site-packages dir is
  non-fatal: the user-plugin write stands and the exact `sudo` copy command is
  printed. When the Hermes package can't be detected, a clear note tells the
  user to rerun with `--memory-provider-path`. A "discovery paths" summary now
  prints which Hermes versions read which path. (big-patch PKG-1)

### Changed

- **system_prompt block coaches keyword/proper-noun search** over
  natural-language questions: "search matches stored TEXT, not meaning … for a
  multi-concept query, search each key term separately and merge." Closes the
  default-UX gap where an agent's first natural-language query returns 0 hits.
  (big-patch PKG-10, priority #7 remainder)

Regression tests: `tests/test_provider_path_2026_06_11.py` (6 cases).

## [0.3.8] - 2026-06-01

### Fixed

- **`prefetch()` output is now fenced as untrusted data (prompt-injection hardening).**
  `prefetch()` returns stored memory bodies, which can contain prompt-injection
  payloads. The block is now wrapped in an explicit `[UNTRUSTED MEMORY CONTEXT
  BEGIN] ... [UNTRUSTED MEMORY CONTEXT END]` fence telling the host agent to treat
  it as reference data, never as instructions. The closing fence survives length
  trimming. (security; beta report dor_alpha)

## [0.3.7] - 2026-05-30

Coerce-on-Adapter: pairs with the client 0.4.5 structured-body contract.

### Changed

- `remember()` and `set_state()` coerce a primitive body to `{"value": body}` before the client write (new `_coerce_body`). The client (>=0.4.5) hard-enforces dict/list bodies; the adapter keeps the agent-facing surface forgiving so a `sibyl_remember(..., body="a fact")` call never fails. dict/list bodies pass through untouched; on recall the payload is under the `"value"` key.
- Requires `sibyl-memory-client>=0.4.5`.

Regression coverage: `tests/test_coa_coercion_2026_05_30.py` (12 tests). 52/52 suite green.

### Changed (Terminal B — multi-record retrieval, tester Run15)

- The `sibyl_search` agent tool now routes through `provider.search_multi_record`
  (new) → `multi_record_search` (client 0.4.5), so workflow queries spanning
  several linked records surface them all instead of only the strongest single
  match. `provider.search()` (the SDK primitive) and `prefetch()` are unchanged.

## [0.3.6] - 2026-05-29

Per-profile memory isolation for multi-profile Hermes setups.

### Fixed

- **Multiple Hermes profiles collapsed into one memory store.** The adapter
  keyed its SQLite DB only off `HERMES_HOME` (`<HERMES_HOME>/sibyl/memory.db`).
  Hermes' `get_hermes_home()` falls back to `~/.hermes` whenever `HERMES_HOME`
  is unset (and warns this causes cross-profile corruption), so profiles not
  each launched with a distinct `HERMES_HOME` all wrote to the same DB: only
  the default profile's data was effectively visible and specialist profiles
  lost their own history across sessions. `initialize()` now resolves the
  active profile (via the `agent_identity` kwarg, then the on-disk
  `active_profile` file Hermes itself uses, then `"default"`) and gives each
  non-default profile its own DB at
  `<HERMES_HOME>/sibyl/profiles/<name>/memory.db`. The default profile keeps
  the legacy path, so existing single-profile installs need no migration.
  Reported by a beta tester running an orchestrator plus specialist profiles.

## [0.3.5] - 2026-05-22

Plugin default-UX fixes surfaced by the LongMemEval 50-Q benchmark on
2026-05-22. Three coordinated changes: depend on `sibyl-memory-client>=0.4.2`
(which flipped `search()` default from phrase-match to AND-of-tokens),
upgrade `SibylAdapter.prefetch()` to multi-strategy retrieval, and add
explicit search-mode coaching to `system_prompt_block()`.

### Changed

- `dependencies`: bumped pin to `sibyl-memory-client>=0.4.2` so the new
  default AND-of-tokens search semantics flow through automatically. Every
  Hermes user's first natural-language search now returns matches
  consistently (was: 0 hits for any query with 2+ words).
- `SibylAdapter.prefetch(query)`: replaced single passive `search(query)`
  call with a multi-strategy retrieval: tries the full query first, then
  tops up with per-significant-token searches if recall is thin, merges
  by per-key match count + best FTS5 rank. Stopwords + short tokens
  filtered out. Caps per-token searches at 5 to keep prefetch cheap.
- `SibylAdapter.system_prompt_block()`: added explicit guidance to LLMs
  using the plugin: `sibyl_search` now AND-tokenizes by default; for
  consecutive-phrase match wrap input in double-quotes
  (`'"Christopher Nolan"'`). Closes the gap where agents would form
  multi-word queries assuming phrase-match was the right shape.

### Why

The 2026-05-22 LongMemEval 50-Q benchmark in
`/mnt/sibyl-data/plugin-lme-test/` showed plugin v0.3.4 lost 8.5pp to a
no-plugin baseline (80.9% vs 89.4%) when used naïvely. The cause was
isolated to retrieval, not storage. With a runner-side workaround
matching what v0.3.5 now ships internally, the plugin matched the no-plugin
baseline at 89.4% (+1 win on preferences for an incl-all 86% vs 84%).
The plugin imposes ZERO LLM cost on users: all storage + retrieval is
local SQLite + FTS5 + tier-check pings.

## [0.3.4] - 2026-05-20

Branding pass on the vendored banner. Matches the change shipped in
`sibyl-memory-cli` v0.3.2 in the same session. Operator directive:
"beneath the large SIBYL title it needs to say underneath the memory
you can hold in your hand tagline, 'a Sibyl Labs LLC Product.
Agentic Infrastructure and Memory Products' or something similar."

### Changed

- Vendored `_banner.py` adds an attribution line under the tagline:
  `a Sibyl Labs LLC Product. Agentic Infrastructure and Memory Products`.
  Same deepest-gold color as the tagline + ANSI dim so the install
  ceremony reads SIBYL > tagline > attribution at a glance. Visible
  on both `install-plugin` and `uninstall-plugin` since both commands
  print the banner before their section header.

## [0.3.3] - 2026-05-20

Visual identity pass on the `install-plugin` and `uninstall-plugin`
commands. Operator directive: "typical app patterns: heavy menus on
install window and initial setup, light on dashboards etc." The
install-plugin command is THE second-most-ceremonial moment a user has
with SIBYL (after `sibyl init`), so it gets the full SIBYL banner +
sectioned numbered onboarding menu treatment.

### Added

- Vendored `_aesthetic.py` and `_banner.py` from `sibyl-memory-cli`
  (small, stable files; avoids a hard runtime dep on the CLI package).
- `install-plugin` output: SIBYL gradient banner → section header
  ("install-plugin · hermes memory provider · drops adapter at...") →
  KV rows for paths → "WRITING PAYLOAD" eyebrow with ✓ glyphs on each
  write → success line → "next steps" section header → 3 numbered
  chips with bold step titles and contextual help underneath each →
  divider with uninstall hint and docs link.
- `uninstall-plugin` output: same banner + section header treatment,
  matching the ceremonial bookend.
- Status / warning / error lines use the brand palette (jade pulse for
  success, warm ochre for warn, measured red for error) instead of
  generic ANSI 31/33.

### Compatibility

- No API changes. Same install_plugin entry point, same flags
  (--hermes-home, --force, --dry-run).
- All visual choices honor `NO_COLOR`. `SIBYL_FORCE_COLOR=1` available
  for non-tty rendering (CI, doc captures).
- Plain-text fallback preserves structure (still readable in dumb
  terminals or pipes).

## [0.3.2] - 2026-05-18

KAPPA external-tester remediation release. Family-wide alignment with the
v0.4.0 client (KAPPA-attributed fixes: exception export path, db file
perms, identifier validation, FTS5 error surfacing). No Hermes adapter
code changes in this release.

### Changed

- `sibyl-memory-client` pin: `>=0.3.3` → `>=0.4.0`.
- KAPPA's fixes flow through automatically. Hermes tools (`sibyl_remember`,
  `sibyl_recall`, `sibyl_search`, `sibyl_list`) now reject empty / null-byte
  / oversized identifiers on write and surface malformed FTS5 queries as
  `ValidationError` instead of silently returning empty.

### Notes

- 40/40 hermes tests pass unchanged. The provider + adapter contract is
  unchanged from v0.3.1.

---

## [0.3.1] - 2026-05-18

Audit-remediation release. v0.3.0 pre-ship audit (2026-05-18T05:05Z)
surfaced 10 critical findings across four lanes. This release lands the
Hermes-side fixes. Companion releases: `sibyl-memory-client` v0.3.3 (engine
+ schema v3 + cross-tier search), `sibyl-memory-cli` v0.1.2,
`sibyl-memory-mcp` v0.1.1.

### Added

- `tests/test_adapter.py`: full regression coverage for the bundled Hermes
  adapter. Validates: module imports cleanly off-Hermes (guarded ABC
  import + tool_error fallback), all 4 tool schemas resolve, end-to-end
  remember+recall round-trips through `handle_tool_call`, list filtering,
  cross-tier search hits all four tiers, malformed FTS5 queries don't
  crash or leak, missing required args produce structured errors,
  shutdown sets the stop flag, sync_turn during shutdown skips cleanly.
  Closes audit H1.

### Changed

- **`SibylMemoryProvider.search()` now spans all four tiers**: entities +
  state + reference + journal. Returns tier-tagged hits (`{tier, key,
  category, body, snippet, rank, ts}`). The marketing claim of "FTS5
  across all tiers" is now true. Caller can restrict scope with
  `tiers=("entity",)` for the pre-v0.3.1 behavior. Backed by the new
  `MemoryClient.search()` in client v0.3.3.
- `_hermes_plugin/adapter.py`. Hermes ABC + `tool_error` imports guarded
  with try/except. The bundled module imports cleanly off-Hermes with
  no-op fallbacks. Audit P1.
- `SibylAdapter.sync_turn` retries on transient failure (SQLITE_BUSY etc.)
  with exponential backoff up to 3 attempts. On final failure escalates
  from DEBUG to WARNING log. Audit P-C1.
- `SibylAdapter.shutdown` sets `_shutting_down` BEFORE joining the daemon
  thread. The worker checks the flag and exits cleanly without issuing
  a slow cap-gate refresh. Audit P-C2.
- `SibylAdapter.handle_tool_call` exception path now returns exception
  class name only, not `str(e)`. Prevents echoing arg contents back to
  the agent on backend errors. Audit SEC-10.
- Adapter type hints converted to PEP 604 unions throughout. Audit N5.
- Default search/list limits extracted as named module constants
  (`_DEFAULT_SEARCH_LIMIT=10`, `_DEFAULT_LIST_LIMIT=50`). Audit O1.
- `RECALL_SCHEMA` description documents the row-wrapper return shape
  explicitly. `SEARCH_SCHEMA` updated for cross-tier coverage. Audit H2.
- `provider.py`. `recall`, `forget`, `archive`, `set_state`, `get_state`,
  `set_reference`, `get_reference` docstrings now include explicit
  `Raises:` sections and document return-shape asymmetry per tier.
  Audit H2/H3.
- `install_plugin.py` type hints converted to PEP 604 unions. Audit N5.

### Security

- **SEC-2**. `credentials.write_credentials` now creates files atomically
  with mode 0o600 set at creation via `os.open(O_WRONLY|O_CREAT|O_EXCL|
  O_NOFOLLOW, 0o600)`. No more world-readable window between `write_text()`
  and `os.chmod()` syscalls.
- **SEC-5**. `install-plugin --force` and `uninstall-plugin` refuse to
  `shutil.rmtree` any directory that doesn't contain a recognized prior
  Sibyl install (`plugin.yaml` with `name: sibyl` in the first 10 lines).
  Prevents destruction of arbitrary user-writable trees from misconfigured
  HERMES_HOME. Both commands also refuse symlinked destinations.
- **SEC-11**. `load_credentials` refuses to follow symlinks. Checks
  `is_symlink()` BEFORE `resolve()`.
- **SEC-10**. `handle_tool_call` error response carries only the
  exception class name.

### Fixed

- **H7**. `hermes_bound` property emits `DeprecationWarning` on read.
  Always returns `False` (unchanged behavior). Slated for removal in v0.4.
- `test_smoke.py`: schema_version assertion loosened to `>= 2` (audit T4);
  hermes_bound assertion tightened from `isinstance(..., bool)` to
  `is False` (audit T3).
- README quickstart rewritten: removed the fictional
  `Agent(memory=SibylMemoryProvider())` pattern (audit C5). Replaced
  with the real flow: `pip install` → `install-plugin` → config.yaml.
- README "Hermes contract" section rewritten: removed the false claim
  that `SibylMemoryProvider` inherits Hermes' ABC at import time.

### Dependencies

- `sibyl-memory-client>=0.3.3` (was `>=0.3.2`). Required for cross-tier
  `MemoryClient.search()` and atomic 0600-at-create.
- Optional `hermes-agent>=0.13.0` unchanged.

### How to upgrade from v0.3.0

```
pip install --upgrade sibyl-memory-hermes
sibyl-memory-hermes install-plugin --force
```

The local SQLite schema auto-migrates from v2 to v3 on first open after
upgrade. No application data is lost. FTS5 indexes rebuild from base
tables. ~50ms per 10k entities on first open, idempotent thereafter.

## [0.3.0] - 2026-05-17

Real Hermes plugin landing. v0.2.x was structurally incompatible with
Hermes' actual `MemoryProvider` ABC (wrong soft-bind import path, missing
abstract methods, no plugin-loader awareness). Diagnosed end-to-end against
the installed hermes-agent 0.13.0 wheel: full ABC source extracted, the
bundled byterover reference implementation read for the idiomatic pattern,
side-by-side method mapping built, discovery contract traced through
`plugins/memory/__init__.py`. Adapter written from that ground-truth read
and validated via Hermes' own `load_memory_provider('sibyl')` loader.

### Architecture shift

- **Split into SDK + adapter.** `SibylMemoryProvider` is now a pure SDK
  class: framework-agnostic, no ABC inheritance, no Hermes-specific glue.
  All Hermes contract code lives in the bundled adapter at
  `_hermes_plugin/adapter.py`, copied to `$HERMES_HOME/plugins/sibyl/` by
  the new `sibyl-memory-hermes install-plugin` console script.
- **Hermes uses filesystem discovery, NOT pip entry points.** Verified
  against `plugins/memory/__init__.py` source: there is no
  `importlib.metadata.entry_points()` call anywhere in Hermes' loader.
  `pip install sibyl-memory-hermes` is necessary but not sufficient; the
  install-plugin script bridges the gap.

### Added

- **`_hermes_plugin/adapter.py`**: full `MemoryProvider` ABC implementation.
  - 4 tools exposed: `sibyl_remember`, `sibyl_recall`, `sibyl_search`,
    `sibyl_list`.
  - Mandatory methods: `name`, `is_available`, `initialize`,
    `get_tool_schemas`, `handle_tool_call`.
  - Recommended overrides: `system_prompt_block` (model-facing tool list),
    `prefetch` (FTS5 + load_context block, with noise filter),
    `queue_prefetch` (no-op: local SQLite is fast), `sync_turn`
    (daemon-threaded per byterover pattern, 5s join + 10s shutdown).
  - Optional hooks: `on_session_switch`, `on_pre_compress`
    (paired user+assistant flush), `on_delegation`, `on_memory_write`
    (accepts `metadata=None` kwarg: avoids the byterover signature bug).
  - Defensive: `agent_context != 'primary'` guard in sync_turn so cron /
    subagent runs don't corrupt the user's representation.
  - `_stable_key()` uses blake2b for deterministic content addressing,
    so add+remove on the same content actually targets the same entity.
  - Validated end-to-end via `load_memory_provider('sibyl')` dry-run + all
    4 tool schemas resolved in OpenAI function-calling format.
- **`_hermes_plugin/plugin.yaml`**. Hermes plugin metadata
  (name, description, version, homepage).
- **`sibyl_memory_hermes.install_plugin`** + console script
  `sibyl-memory-hermes install-plugin`:
  - Detects HERMES_HOME from CLI flag → `$HERMES_HOME` env var → `~/.hermes`.
  - Copies bundled adapter via `importlib.resources` (no fragile path math).
  - Renames `adapter.py` → `__init__.py` at destination (source can't be
    `__init__.py` because the Hermes-only imports would TypeError under
    our standalone tests).
  - Prints activation steps (config.yaml edit + `sibyl init` reminder).
  - Flags: `--hermes-home <path>`, `--force`, `--dry-run`.
- **`sibyl-memory-hermes uninstall-plugin`** counterpart for clean removal.

### Changed (breaking, but no users were affected: the prior path was broken)

- **`SibylMemoryProvider` no longer subclasses `MemoryProvider`.** The
  conditional soft-bind in v0.2.x always failed (wrong import path); the
  class was effectively `object`-derived already. v0.3.0 makes that
  explicit and moves all Hermes glue to the adapter. The `hermes_bound`
  property and `health()` field are kept for backwards compatibility but
  always return False; they're deprecated for removal in a future major.
- **`__init__.py` docstring rewritten.** The fictional `from hermes_agent
  import Agent; Agent(memory=SibylMemoryProvider())` quickstart is gone -
  that API never existed in any Hermes release. Replaced with the real
  install flow (`pip install` → `install-plugin` → config.yaml edit).
- **`__version__` is now single-sourced** from `importlib.metadata.version(
  'sibyl-memory-hermes')`. The v0.2.x drift (`__init__.py` said 0.2.1
  while the wheel was 0.2.2) is no longer possible.

### Fixed

- `provider.py:57` no longer attempts `from hermes_agent.memory import
  MemoryProvider`. That import path does not exist in hermes-agent: the
  real module is `agent.memory_provider`. Removed entirely; the SDK class
  doesn't inherit from the ABC anymore (see Architecture shift above).

### Dependencies

- `sibyl-memory-client>=0.3.2` (was `>=0.3.1`: picks up the cap-gate +
  HTTPError fixes from yesterday's audit pass).
- Optional: `hermes-agent>=0.13.0` (was `>=0.10.0`: the ABC is documented
  for v0.13 specifically; older releases may work but aren't validated).

### How to upgrade from v0.2.x

```
pip install --upgrade sibyl-memory-hermes
sibyl-memory-hermes install-plugin
# edit ~/.hermes/config.yaml: memory.provider: sibyl
hermes                                # picks up the new tools
```

If you were importing `from hermes_agent import Agent` (per the old
docstring), that never worked: delete those lines. If you were using
`SibylMemoryProvider()` directly from a non-Hermes Python orchestration,
no changes needed; the SDK surface is unchanged.

### Verification

- Adapter dry-run via Hermes' own `load_memory_provider('sibyl')` returns
  the SibylAdapter instance with `name='sibyl'`, `is_available()=True`,
  and all 4 tool schemas resolved.
- File-bundle validated: `importlib.resources.files('sibyl_memory_hermes.
  _hermes_plugin').joinpath('adapter.py').read_bytes()` returns the
  expected 18,118-byte payload.
- install-plugin smoke-tested against a `/tmp/fake_hermes_home` target -
  files land at `$HERMES_HOME/plugins/sibyl/{__init__.py, plugin.yaml}`.

### Authorship

Developed by SIBYL, Sibyl Labs LLC. Adapter contract derived from the
installed hermes-agent 0.13.0 wheel source. `agent/memory_provider.py`
(the ABC) and `plugins/memory/byterover/` (the idiomatic threading +
schema pattern). MIT licensed.

## [0.2.2] - 2026-05-16

Audit-remediation release. Companion to `sibyl-memory-client` v0.3.2.

### Changed

- **T2-2. `SibylMemoryProvider.recall()` narrows exception handling to
  `NotFoundError` only**. Previously caught bare `Exception`, which
  swallowed `StorageError` / `TenantError` / `SchemaError` and returned
  `None` (the Hermes-style soft-miss). That masked real storage failures
  end-to-end: exactly the silent-fallback pattern that caused the
  production Bug 2 on the server side. Now `NotFoundError` returns
  `None` as intended; every other exception propagates so the caller
  can surface or retry.

### Tests

- 21/21 unchanged, all green. The narrower exception class is a strict
  subset of the prior catch-all behavior for the soft-miss case.

### Notes

- Depends on `sibyl-memory-client>=0.3.2` to pick up the matching
  `_check_fn` raise-on-HTTPError fix (T2-3). v0.3.1 still works; the
  changes are independent.

## [0.2.1] - 2026-05-16

HMAC signed-credentials plumbing. Companion to `sibyl-memory-client`
v0.3.1 and the api-sibyllabs credential-signer release.

### Changed

- `Credentials` dataclass gained `signature: str | None = None` and
  `signed_at: str | None = None` fields. Backwards compatible -
  schema v1 credentials still load with these fields as `None`.
- `SibylMemoryProvider.__init__` reads the signature + canonical
  claim from credentials.json and passes them through to
  `MemoryClient.local()`, which forwards to `CapGate`. The cap gate
  attaches them to every server-side cap-check request so the server
  can verify and log tampering.
- `load_credentials` / `write_credentials` round-trip the new fields.

The verification itself is server-side only (HMAC requires a shared
secret the client cannot hold). The client's job is to faithfully
echo back what was issued. Authoritative tier always comes from the
database.

### Tests

- 21/21 unchanged, all green.

## [0.2.0] - 2026-05-15

Hard-cap plumbing release. Companion to `sibyl-memory-client` v0.3.0.

### Changed

- `SibylMemoryProvider.__init__` now passes `account_id`,
  `session_token`, and `tier` from `credentials.json` through to
  `MemoryClient.local()`. The v0.3.0 cap gate uses these to verify
  the user's actual tier against the server when the local DB
  approaches 2 MB. Without them, the SDK enforces a strict local
  2 MB cap with no server-check fallback.
- `Credentials` dataclass gained an optional `session_token` field
  (the long-lived bearer issued by the activation flow). Backwards
  compatible: existing v0.1.x credentials files load fine,
  `session_token` simply lands as `None`.

### Notes

- Depends on `sibyl-memory-client>=0.3.0`. Earlier clients lack the
  cap-gate plumbing.
- Pre-activation users (no credentials.json) still work: they hit
  the strict local 2 MB cap with no upgrade path until they run
  `sibyl init`.

## [0.1.1] - 2026-05-15

Patch: stripped placeholder GitHub URLs from pyproject metadata
(operator scar: never write a link to a domain not verified to
exist). No code changes.

## [0.1.0] - 2026-05-15

First real release. Replaces the v0.0.1 PyPI name-reservation placeholder.

### Added
- `SibylMemoryProvider`. Hermes-compatible memory provider on top of
  `sibyl-memory-client`. Auto-inherits Hermes' `MemoryProvider` ABC when
  Hermes is installed; degrades to standalone object base when not.
- Five-tier memory routing: journal (`save_context`/`load_context`),
  entities (`remember`/`recall`/`list`/`forget`), state (`set_state`/
  `get_state`), references (`set_reference`/`get_reference`), archive
  (`archive`), FTS5 (`search`).
- `Credentials` dataclass + `load_credentials` /  `write_credentials` for
  the activation file at `~/.sibyl-memory/credentials.json`.
- `CredentialsNotFoundError` with explicit recovery message pointing the
  user to `sibyl init`.
- Auto-detect activation: provider reads credentials.json on construction
  by default; explicit `tenant_id=` overrides; missing credentials
  degrade to `DEFAULT_TENANT` so tests / pre-activation use works.
- `health()` diagnostic dict (used by `sibyl status`).
- Comprehensive smoke test suite covering provider construction, tier
  routing, search, archive, credentials, multi-tenant isolation, and
  Hermes binding state.

### Notes
- Depends on `sibyl-memory-client>=0.1.0`. No Hermes hard dep: install
  via `pip install sibyl-memory-hermes[hermes]` to opt into the ABC.
- License: MIT.
- Compatible with Python 3.10+.

## [0.0.1] - 2026-05-15 (name-reservation placeholder)

Initial PyPI upload to reserve the package name. Empty package; not
intended for use. Superseded by v0.1.0 in the same session.
