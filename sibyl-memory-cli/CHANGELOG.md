# Changelog

All notable changes to `sibyl-memory-cli` are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows
[SemVer](https://semver.org/).

## [0.3.7] — 2026-05-22

Three changes to `sibyl setup`. Beta tester audit caught silent-failure
risk on Claude Code wiring and a missing Codex auto-wire path.

### Changed

- **`ClaudeCodeWirer` default settings_path: `~/.claude/settings.json`
  → `~/.claude.json`.** This was a silent-failure path. The wirer
  reported success but Claude Code reads MCP server config from
  `~/.claude.json` (the user-level config that holds `numStartups`,
  `userID`, `mcpServers`, etc.), not the older `~/.claude/settings.json`
  surface. Pre-0.3.7 testers who ran `sibyl setup claude-code` and
  didn't see the SIBYL MCP server appear in Claude Code hit this bug.
  Use `--claude-settings` to override when needed.

### Added

- **`CodexWirer`** — auto-wires Codex CLI by editing
  `~/.codex/config.toml`. Adds a `[mcp_servers.sibyl_memory]` table
  with `command = "sibyl-memory-mcp"`. Idempotent (re-running on an
  already-wired config returns `already`). Refuses to overwrite an
  existing `sibyl_memory` block with a different command unless
  `--force` is passed (matches the Hermes / Claude Code wirer pattern).
  Atomic write via `.toml.tmp` rename + `.toml.bak` backup.
- **`sibyl setup codex`** target — registered in `ALL_WIRERS` and
  in the parser `choices` list. Bare `sibyl setup` (no target) now
  also auto-detects and prompts for Codex when `codex` is on PATH
  or `~/.codex/config.toml` exists.
- **`--codex-config PATH`** override flag on `sibyl setup` for users
  with a non-standard Codex config location.

### Notes

- TOML idempotency check is text-based (regex on the table header +
  the canonical `command = "sibyl-memory-mcp"` line) rather than full
  TOML parse-and-rewrite. Trade-off: preserves operator hand-edits
  and comments in the rest of the file, at the cost of not catching
  pathological TOML edge cases (e.g., the table header split across
  lines). For the canonical Codex config layout this works cleanly.
- `requires-python` stays at `>=3.10`. No new dependency added —
  TOML reads use plain text scan rather than `tomllib` (3.11+ only).
- The Hermes README + the beta install page on beta.sibyllabs.org
  both document `claude mcp add sibyl-memory -- sibyl-memory-mcp` as
  the canonical Claude Code wire path. `sibyl setup claude-code`
  is now equally valid (writes to the same `~/.claude.json` file
  that `claude mcp add` writes to).

## [0.3.6] — 2026-05-22

New `sibyl update` subcommand. Closes the loop on the only meaningful
remote-push question the beta cohort raised: "can you push updates to me?"
We cannot remote-execute `pip install` on your machine — that's your venv,
your permissions. We can notify (server-side, via heartbeat response,
landing in a later cli release) and we can hand you a one-keystroke
upgrade. This release ships the lever.

### Added

- `sibyl update` — checks installed versions of `sibyl-memory-cli`,
  `sibyl-memory-hermes`, and `sibyl-memory-client` against PyPI. Prints
  a small diff table and the exact pip command to run. Pure stdlib;
  3 PyPI calls, ~500ms total.
- `sibyl update --apply` — runs `pip install -U` for the outdated
  packages in-place via `sys.executable -m pip`. Detects the install
  method (system pip / venv / pipx / PEP 668 externally-managed) and
  picks the right invocation. pipx installs are flagged with the
  correct `pipx upgrade` command instead of trying pip.
- `sibyl update --json` — machine-readable output for scripting.

### Notes

- Exit code 0 = all current. Exit code 2 = updates available (so this
  can be wired into shell prompts or CI). Exit code other values =
  hard error.
- The MCP package (`sibyl-memory-mcp`) is intentionally not in the
  default set — many users don't have it installed. Add via a future
  `--include-mcp` flag if the beta surfaces demand.
- Sources the version via `importlib.metadata.version()` and never
  hardcodes a number, so it survives package rename or any odd install
  layout. Matches the existing `_client_version()` helper pattern.

### Catch-up

This release also closes a source-drift gap: the monorepo on
`Sibyl-Labs/Sibyl-Memory` was at v0.3.2 while PyPI was at v0.3.5
because the v0.3.3-v0.3.5 cli iterations were committed only to the
legacy `/sibyl/packages/sibyl-memory-cli/` working copy. The v0.3.6
commit lands all of v0.3.3 (banner branding), v0.3.4 (timeout matched
constants — superseded), and v0.3.5 (structural pairing-TTL read from
server) inside the canonical monorepo, alongside the new `update`
command. Yank v0.3.4 from PyPI as hygiene; v0.3.6 is the recommended
upgrade target.

## [0.3.5] — 2026-05-21

Permanent fix for the silent-success activation foot-gun. 0.3.4 raised the
CLI poll to 30min and the server pairing TTL to 30min so the two windows
matched. Operator pushed back: matching constants is a temporary fix that
re-opens the moment either side moves. The structural fix is one source of
truth.

### Changed

- The CLI no longer carries its own activation deadline. The `/session-init`
  response already includes `pairing_ttl_seconds` (it always did — the CLI
  was just throwing it away). The CLI now captures that value and uses it
  as the poll deadline.
- `INIT_TIMEOUT_SEC` removed. Replaced by `INIT_TIMEOUT_FALLBACK_SEC`,
  used only when `/session-init` fails entirely or the response is missing
  the field. Drift between CLI and server is now impossible by
  construction.

### Why this is permanent

If the server-side TTL ever changes again, every CLI install in the wild
adopts the new value on the next `sibyl init` automatically. No CLI
re-publish required. No `bumped constant on one side` failure mode. The
server is the single source of truth; the CLI defers.

## [0.3.4] — 2026-05-21

Silent-success activation foot-gun. Multi-user reports of "email auth doesn't
work, no error message." Root cause: CLI `INIT_TIMEOUT_SEC` was 10min while
the server-side `PAIRING_TTL_SECONDS` was 15min. Users who took 10-15min to
find the pairing code in their inbox would hit the gap: server accepted the
bind, browser showed the success modal, but the local CLI had already exited
and `credentials.json` was never written. No error surfaced anywhere — the
plugin just failed to load on the next run.

### Changed

- `INIT_TIMEOUT_SEC` raised from `10 * 60` to `30 * 60` (cli.py:61). Matches
  the server's new 30min pairing-code TTL — the two windows now never
  disagree.
- `UPGRADE_TIMEOUT_SEC` raised to `30 * 60` for the same alignment reason on
  the upgrade flow.
- Activation-timeout terminal message now explicitly calls out the
  silent-success failure mode and tells the user to run
  `sibyl init --force`. Earlier message just said "Re-run sibyl init."

### Companion changes (same session)

- `api-sibyllabs/api/plugin/session-init.js`: `PAIRING_TTL_SECONDS` 15min →
  30min.
- `api-sibyllabs/api/plugin/email-bind.js`: error message for expired code
  updated from "15 min limit" to "30 min limit" + `sibyl init --force`.
- `sibyllabs/plugin/activate.html`: success modal gains a callout that
  prompts the user to run `sibyl init --force` if their terminal already
  showed the timeout message before they bound.

## [0.3.3] — 2026-05-20

Auth subdomain migration. Operator directive: "make sure the temp links are
being generated at install.sibyllabs.com/plugin/auth or something like this,
and not sibyllabs.org/install." Surfaced as a trust + phishing-resistance ask
for the URL that appears in the user's terminal at activation time.

### Changed

- `ACTIVATE_BASE` default changed from `https://sibyllabs.org/plugin/activate`
  to `https://auth.sibyllabs.org`. Activation URL shape moved from
  query-string (`?session=<uuid>`) to bare path (`/<uuid>`). The terminal
  output now reads `Opening https://auth.sibyllabs.org/<short-uuid>` —
  shorter, line-wraps less on narrow terminals, easier to verify visually.
- The Vercel rewrite on `auth.sibyllabs.org` serves the same `/plugin/activate`
  page but preserves the user-visible URL, so the wallet popup's "X wants you
  to sign in" header matches the URL bar. Phishing-conscious wallets (Rabby,
  MetaMask in security mode) skip the domain-mismatch warning.
- Companion api-sibyllabs change (same session): `bind.js` SIWE
  `expectedDomains` allowlist now includes `auth.sibyllabs.org` alongside
  `sibyllabs.org` and `sibylcap.com`.

### Backward compatibility

The legacy `https://sibyllabs.org/plugin/activate?session=<uuid>` URL still
resolves and works identically. Anyone on cli 0.3.2 or earlier keeps a
functioning activation flow until they upgrade. The new URL works on cli
0.3.3+ automatically with no env-var changes needed.

Override via `SIBYL_ACTIVATE_BASE` env var still works for staging /
self-hosted setups. The CLI auto-detects path-vs-query URL shape from the
base hostname (sibyllabs.org subdomain → path; everything else → query).

## [0.3.2] — 2026-05-20

Branding pass on the banner. Operator directive: "beneath the large
SIBYL title it needs to say underneath the memory you can hold in
your hand tagline, 'a Sibyl Labs LLC Product. Agentic Infrastructure
and Memory Products' or something similar."

### Changed

- `_banner.py` now emits a third line under the wordmark + tagline:
  `a Sibyl Labs LLC Product. Agentic Infrastructure and Memory Products`.
  Rendered in the same deepest-gold (`_GRADIENT[-1]` = `(106, 79, 31)`)
  as the tagline but with ANSI dim (`\033[2m`) applied so the visual
  hierarchy reads SIBYL > tagline > attribution at a glance. Plain-text
  fallback also includes the line for non-color terminals.

Preview captures at https://sibylcap.com/hud-2026-05-20 (scene 09
isolates the banner; scenes 01 + 05 show it inline with the rest of
the activation and install ceremonies).

## [0.3.1] — 2026-05-20

Operator-directed tuning: "typical app patterns — heavy menus on
install window and initial setup, light on dashboards etc." v0.3.0
applied the full section_header treatment uniformly across every
subcommand. v0.3.1 lightens the daily-use dashboards and keeps the
ceremony reserved for activation moments.

### Changed

- `sibyl status`, `sibyl whoami`, `sibyl devices`, `sibyl logout`,
  `sibyl health` — dropped the section_header opener. Same convention
  as `git status`, `ls -la`, `gh auth status`, `pg_isready`,
  `redis-cli ping`: utilitarian dashboards present data, not chrome.
  Eyebrow labels + kv rows + status lines remain.

### Unchanged

- `sibyl init` — keeps the full SIBYL gradient banner + section
  headers + numbered next-steps. This IS the install moment; it earns
  the ceremony.
- `sibyl upgrade` — keeps section header + KV. Mid-weight: tier-flip
  moment is install-ish but not first-run.
- `_aesthetic.py` library — unchanged. Applied differently across
  commands per the heavy/light convention.

## [0.3.0] — 2026-05-20

Visual identity pass across every subcommand. The `sibyl init` brand
moment (the SIBYL ASCII wordmark with pale-gold → deep-ochre vertical
gradient) was the only command with serious typography; every other
subcommand was plain text + ANSI 16-color. v0.3.0 brings the lab face
to the whole surface.

### Added

- New `_aesthetic.py` module — shared visual library for the entire CLI.
  Brand palette derived from the rule 46 creme paper face (PAPER, INK,
  ACCENT, JADE, PULSE, RULE, etc.). 24-bit truecolor → 256-color → plain
  text degradation cascade. Letter-spaced eyebrows, gradient titles,
  ASCII rule dividers, key/value rows, status chips with success/warn/
  error glyphs, multi-stop char-by-char gradient interpolation.
- `SIBYL_FORCE_COLOR=1` env override for non-tty rendering (CI logs,
  doc captures, harness inspection). Honors `NO_COLOR` as the wider
  precedence override per the standard.

### Changed

- `sibyl init`, `sibyl upgrade`, `sibyl status`, `sibyl whoami`,
  `sibyl devices`, `sibyl logout`, `sibyl health` all now open with a
  styled section header (gradient command-name + creme rule lines +
  dim subtitle), use eyebrow labels for sub-sections (uppercase
  letter-spaced ochre), and render key/value rows + status lines with
  the brand palette. Success states (Activated, Upgraded, Logged out)
  flow with a pulse → jade gradient. Cap warnings and errors use the
  measured warm-ochre / red palette tokens, not generic ANSI 31/33.
- `sibyl init` waiting spinner now reads "watching the network for your
  bind" in pulse-jade, aligned with the wallet-bind-watcher service
  language users see in their browser.
- `sibyl devices` list rendering: current device marked with `▶` in
  pulse + the device label flows in gold gradient; other devices show
  in calm ink with dim metadata. Index chips in pulse for "this device"
  or muted gray for the rest.

### Compatibility

- Backward compat preserved: existing `dim/bold/green/yellow/red/cyan`
  helpers stay in `cli.py` (used by the legacy `print_status` path
  which is now superseded but not removed). New `_aesthetic.a.*`
  helpers layer on top.
- All visual choices honor `NO_COLOR`. Plain text fallback is
  visually clean (no garbage escapes leak).
- Terminal capability detection identical to `_banner.py` for
  consistency (COLORTERM=truecolor, TERM_PROGRAM whitelist, TERM
  pattern match for kitty/alacritty/256color).

## [0.2.0] — 2026-05-19

Auth-redesign wave 2 — account-surface CLI commands. Adds `sibyl whoami`
for a one-line account summary (masked by default, `--full` opt-in) and
`sibyl devices` for listing active bearer tokens with per-device revoke.

### Added

- `sibyl whoami` — one-line summary: short account_id, tier, masked email
  (`a***@e***.tld`), masked wallet (`0xabcd…1234`), this device label.
  `--full` flag shows unmasked email + wallet for ops scenarios.
- `sibyl devices` — list active (non-revoked) bearer tokens for the
  account in issued_at DESC order. Marks current device with `▶` and
  shows revoke command for each other device.
- `sibyl devices revoke <index>` — POST `/api/plugin/devices` with the
  bearer_id at that index. Refuses to revoke the calling device.

### Server companion (deployed)

- `GET  /api/plugin/devices?account_id=<uuid>` — lists bearer_tokens.
- `POST /api/plugin/devices { bearer_id }` — revokes the bearer.
- Both auth via `Authorization: Bearer <session_token>`; caller must
  own the account.

## [0.1.4] — 2026-05-18

Maximum-efficiency onboarding release. New `sibyl setup` command auto-detects
agent frameworks on the user's machine and wires SIBYL as the memory provider
in one command. Replaces the prior three-step Hermes flow (`pip install
sibyl-memory-hermes` + `sibyl-memory-hermes install-plugin` + manual
`config.yaml` edit) with `sibyl setup`. Also handles Claude Code MCP wiring.

### Added

- **`sibyl setup`** — new subcommand. Auto-detects Hermes (`$HERMES_HOME` or
  `~/.hermes/` or `hermes` on PATH) and Claude Code (`~/.claude/settings.json`
  or `claude` on PATH). Prompts per stack with explicit confirmation:
  - Fresh add: `Set SIBYL as default memory provider in Hermes? [Y/n]` (default Y)
  - Overwrite existing: `Hermes currently uses 'mem0' as memory provider. Overwrite with SIBYL? [y/N]` (default N, never destroys user state without explicit y)
  - Already wired: noop with green status
  - Multi-framework: `Wire which? [h]ermes, [c]laude, [a]ll, [n]one (default: all)`
- **`sibyl setup hermes`** / **`sibyl setup claude-code`** — explicit targeting
  for power users (skips detection, wires only the named stack).
- **Flags**: `--yes` (accept all defaults, still respects destructive-default-N
  unless `--force` is also passed), `--force` (overwrite existing non-SIBYL
  configs), `--dry-run` (print intent without writing), `--hermes-home`,
  `--claude-settings` (override autodetect).
- **Atomic writes + backups**: every config edit creates a `.bak` sibling
  (`config.yaml.bak`, `settings.json.bak`) before atomic rename via tmpfile.
  Defensive against partial writes + user mistake recovery.
- **`HermesWirer`, `ClaudeCodeWirer`** classes in new `sibyl_memory_cli.setup`
  module. Composable wirer protocol (`is_present()` / `current_state()` /
  `wire()` / `WireOutcome`) ready for v0.1.5 addition of Codex / Cursor /
  Continue wirers.
- **33 new tests** in `tests/test_setup.py` covering: detection logic, prompt
  helpers, Hermes fresh / existing-sibyl / existing-other / force-overwrite /
  dry-run / config-preservation, Claude Code fresh / existing-other-mcps /
  existing-sibyl / mismatched-sibyl / force / dry-run.

### Changed

- **Dependencies**: added `pyyaml>=6.0` for Hermes `config.yaml` editing.
  Already a transitive dep for any Hermes user; small (~250 KB) for
  Claude-Code-only users.

### Notes

- Replaces the prior canonical three-step Hermes flow. Docs `install.html`
  Step 4 collapses from three commands to two: `pip install sibyl-memory-cli`
  + `sibyl setup`. The old `sibyl-memory-hermes install-plugin` path stays
  documented as a manual fallback for advanced users who want fine-grained
  control over each step.
- Codex / Cursor / Continue MCP wirers are scoped for v0.1.5. The wirer
  protocol in `setup.py` is ready to take them as drop-in classes.
- The shell installer (`curl ... | sh`) remains on the roadmap; combined with
  `sibyl setup` it collapses the full onboarding to a single curl line.



## [0.1.3] — 2026-05-18

KAPPA external-tester remediation release. Family-wide alignment with the
v0.4.0 client + v0.3.2 hermes (KAPPA-attributed fixes: exception export
path, db file perms, identifier validation, FTS5 error surfacing). No CLI
code changes in this release.

### Changed

- `sibyl-memory-client` pin: `>=0.3.3` → `>=0.4.0`.
- `sibyl-memory-hermes` pin: `>=0.3.1` → `>=0.3.2`.

### Notes

- `sibyl init / upgrade / status / health` surface is unchanged from
  v0.1.2. KAPPA's fixes flow through transparently via the dependency
  bump.

---

## [0.1.2] — 2026-05-18

Audit-remediation release. v0.3.0 plugin-family pre-ship audit (2026-05-18T05:05Z)
surfaced 10 critical findings; this release lands the CLI-side fixes.
Companion releases: `sibyl-memory-client` v0.3.3 (engine + schema v3 +
cross-tier search), `sibyl-memory-hermes` v0.3.1, `sibyl-memory-mcp` v0.1.1.

### Fixed

- **C3** — `__version__` no longer hardcoded. Now sourced from
  `importlib.metadata.version("sibyl-memory-cli")` with `+source` fallback.
  Same pattern as sibyl-memory-hermes v0.3.0+. Wheel and `__init__.py`
  can't drift.
- **C3** — HTTP User-Agent header now built from the runtime
  `_client_version()` helper instead of the hardcoded `"sibyl-memory-cli/0.1.0"`.
  Server telemetry will see real versions, not the stale literal.
- **C3** — `/api/plugin/session-init` payload's `client_version` field
  similarly switched from `__import__("sibyl_memory_cli").__version__` to
  the helper. Telemetry will accurately reflect 0.1.2+.
- **C4** — post-activation message rewritten. Removed the fictional
  `from hermes_agent import Agent; agent = Agent(memory=SibylMemoryProvider())`
  quickstart (the API never existed in any Hermes release). Replaced with:
  the real Hermes install flow (`sibyl-memory-hermes install-plugin` +
  config.yaml edit), the MCP install hint for Claude Code / Codex / Cursor /
  Continue users, and the direct-SDK path for any Python orchestration.

### Security

- **SEC-2** — `write_credentials_atomic` now creates files at mode 0o600
  set by the kernel at file-creation time via `os.open(O_WRONLY|O_CREAT|
  O_EXCL|O_NOFOLLOW, 0o600)`. Previously used `write_text()` then
  `os.chmod(0o600)`, leaving a world-readable window between syscalls every
  credential save. No race.
- **SEC-1** (CLI-side mitigation) — the URL parameter handed to the
  browser is now treated as an opaque pairing-session identifier, not as
  the long-lived bearer. After activation completes, the CLI prefers a
  server-issued `bearer_token` field from `/check` (post-fix server flow);
  if absent, falls back to the legacy session-echo flow. Full fix requires
  the api-sibyllabs server-side change to issue a separate bearer; this
  release prepares the CLI to consume it when the server-side lands.
  Internal variable renamed `session_token` → `session_id` in `cmd_init`
  to reflect the corrected meaning.
- **SEC-11** — `read_credentials` refuses to follow symlinks.

### Dependencies

- `sibyl-memory-client>=0.3.3` (was `>=0.3.0`)
- `sibyl-memory-hermes>=0.3.1` (was `>=0.2.0`) — picks up the fictional-API
  removal in the hermes package; earlier versions are structurally broken.

## [0.1.1] — 2026-05-17

### Added

- **SIBYL wordmark banner** at the top of `sibyl init`. ANSI Shadow boxchars,
  24-bit truecolor vertical gradient flowing cream/white at the top through
  warm gold to deep ochre at the bottom — aligned with the lab visual identity
  per the operator's brand-discipline rule (creme palette, `--accent #8a6a2a`).
  Plus a tagline: "memory you can hold in your hand".

### Implementation notes

- New module `sibyl_memory_cli._banner` with `render_banner()` and
  `print_banner()` helpers. Truecolor support is detected via `COLORTERM`,
  `TERM_PROGRAM`, and `TERM` — modern terminals (iTerm2, Alacritty, Kitty,
  wezterm, Ghostty, Windows Terminal, VS Code, Tabby) light up automatically.
- Gracefully degrades to plain text (still readable, no escape junk) when
  `NO_COLOR` is set, when stdout is not a TTY, or when `TERM=dumb`.
- Wired into `cmd_init` only — `status` / `health` / `upgrade` stay banner-free
  so they don't add noise to scripted invocations.
- Banner palette is encoded as 6 RGB tuples (one per row) in the module
  rather than computed at runtime — easier to tune and audit.

## [0.1.0] — 2026-05-16

### Changed (same-day revision before publish): terminal pairing code

`sibyl init` now generates a 6-digit pairing code locally (via
`secrets.randbelow`), prints it in the terminal, and POSTs only its
sha256 hash to `/api/plugin/session-init` BEFORE opening the browser.
The code itself never leaves the user's machine until they type it
into the browser's email panel. Replaces the earlier Resend-backed
email magic-code flow, removing the external dependency entirely.

The wallet (SIWE) path is unchanged — the pairing code only matters
for the email panel.



Initial release. Operator directive 2026-05-16: build the user-facing
CLI + upgrade page so the SDK + payment-auth machinery has a front door.

### Added

- **`sibyl init`** — browser activation. Generates a session UUID,
  opens `sibyllabs.org/plugin/activate?session=...` in the user's
  browser, polls `api.sibyllabs.org/api/plugin/check` every 3s with a
  10-min timeout. On bind, writes `~/.sibyl-memory/credentials.json`
  atomically at mode 0600.
- **`sibyl upgrade`** — opens `sibyllabs.org/plugin/upgrade?session=...`
  with the existing session token. Polls `/api/plugin/access` every 3s
  with a 15-min timeout until `tier` changes from the local value.
  On change: rewrites credentials.json, clears `tier_cache.json` so
  the next write picks up the new entitlement immediately.
- **`sibyl status`** — shows local credentials, DB size, tier cache
  state, plus the server's view of tier (subscription / staker /
  free). Flags local↔server tier drift.
- **`sibyl health`** — wraps `SibylMemoryProvider.health()`. Prints
  the JSON diagnostic dict.

### Design

- Pure stdlib HTTP via `urllib`. No `requests`, no `httpx`. The wheel
  installs in seconds.
- `session_token` printed only as short slice (`first8…last4`). Never
  full-length to stdout.
- Polling has explicit timeouts. No infinite loops. Ctrl-C exits 130.
- All endpoint URLs configurable via env (`SIBYL_API_BASE`,
  `SIBYL_ACTIVATE_BASE`, `SIBYL_UPGRADE_BASE`) for staging tests.

### Depends on

- `sibyl-memory-client>=0.3.0` (cap gate)
- `sibyl-memory-hermes>=0.2.0` (provider + credentials loader)

### Entry point

`pip install sibyl-memory-cli` installs the `sibyl` binary via the
`[project.scripts]` block in pyproject.
