# Changelog

All notable changes to `sibyl-memory-cli` are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning follows
[SemVer](https://semver.org/).

## [0.5.0] - Unreleased

### 2026-10-14 - Pre-release cleanup
- `sibyl migrate` tells Hermes users to run `sibyl setup hermes` instead of editing
  `~/.hermes/config.yaml` by hand.
- `sibyl setup --help` mentions Codex, and `--claude-settings` now says what it really sets: the
  path to Claude Code's `settings.json` (default `~/.claude/settings.json`).
- The account host shown while waiting for approval and after `sibyl logout` follows
  `SIBYL_ACCOUNT_BASE` when it is set. With the default, the text is unchanged.
- README: activation and status samples match what the commands print today (the free cap is
  5,242,880 bytes, not 2,097,152), tier drift points at `sibyl refresh`, `sibyl --version` is
  listed, the Upgrade section describes the current Pro pricing and flow, and the PyPI
  description names Pro / Elite instead of the retired staker / subscription tiers.
- Removed unused internal helpers and constants; docstrings describe current behavior.
- `sibyl upgrade`, when it opens the plugin upgrade page instead of the account billing page, now
  names the current offer: Pro $12/month or $108/year (USDC for one fixed period, or by card plus
  applicable tax at sibyllabs.org/checkout followed by `sibyl claim`), or free Pro while 100,000 $SIBYL stays
  staked. It no longer mentions a quarterly USDC plan.
- Comments, docstrings, test file names and this CHANGELOG describe each finding by what it fixed
  instead of naming testers or internal QA rounds. Text and test file names only; no code changed.

### 2026-10-10 - sign-in and display fixes
- F-1 (contract C1): the api's 401 `signed_out_on_claim` (this computer was signed out when the
  account's email was first confirmed on the account page) is named everywhere the CLI used to
  say only "signed out · run sibyl init" or "session expired": the `sibyl status` Access row (from
  the gate's refresh) and server row, `sibyl email`, `sibyl email verify|add|confirm`, `sibyl
  devices`, `sibyl upgrade` and `sibyl claim` print `signed out: this computer was signed out
  when your email was confirmed on the account page (<date> UTC). run: sibyl init --force`, and
  the handoff is `next: sibyl init --force`. Any other 401 keeps its old line.
  `gate_http_line` / `gate_http_handoff` take the error body. Tests:
  `tests/test_signin_display_fixes_2026_10_10.py` (F-1 block).
- F-1a (contract C2): `sibyl init` with `cli_email_verify` on and the email unconfirmed sends the
  code without the "confirm it now?" question; the only prompt is for the code (Enter skips).
  Every path that leaves the email unconfirmed (Enter at the code prompt, a wrong or failed code,
  no terminal, `--no-email`, and `cli_email_verify` off) prints `until you confirm your email
  here, signing in on the account page signs this computer out. confirm later with: sibyl email
  verify`. With no terminal no code is mailed unasked (the handoff stays `sibyl email verify`).
  Tests: `tests/test_accounts_patch_2026_09_26.py` (two old rows rewritten, three added).
- `sibyl setup <agent> --dry-run` compares with what is installed and says "already
  in" for the skills (`skills v<v>: SAVE.md, DEBLOAT.md already in <dir>`), each shim (`shim
  <path>: already in`), the Claude Code hooks (`hooks: 5 already in <settings>`) and the Codex
  AGENTS.md block (`block: already in <path>`); missing, outdated and edited files say what a real
  run would do. Still reads only.
- times shown to people carry a zone, in UTC: `sibyl devices` (`last seen 2026-10-09
  19:08 UTC`), the computer-limit list at `sibyl init`, `sibyl status` (Tier cache checked,
  offline "as of", Expires, Issued), `sibyl claim` (Covers through), `sibyl memory` (updated),
  and the save/autosave rows (`last save`, `last 19:08 UTC`). New module `_timefmt.py`.
- `sibyl --version` prints every installed Sibyl package on one line, the CLI first:
  `sibyl cli 0.5.0+dev.5 · client 0.9.0+dev.5 · mcp 0.3.0+dev.5` (hermes and langgraph when
  installed). Still exits 0 without touching ~/.sibyl-memory.
- `sibyl init` offers "send USDC from any mobile wallet" (the section line, the
  subtitle and the agent's ask line) only when session-init minted this session's USDC-send bind
  (`bind_amount_micros` and `bind_bus_wallet` set); while USDC sign-in is off the api sends both
  null and init lists two paths, desktop wallet and email plus code.
- Suite: 961 passed (was 930).

### Fixed (`sibyl update` covers the MCP server, 2026-10-09)
- `sibyl update` now also updates the MCP server (and LangGraph when installed). It checks every
  Sibyl package that is installed among `sibyl-memory-cli`, `-client`, `-hermes`, `-mcp` and
  `-langgraph`, and `--apply` upgrades the outdated ones in one `pip install -U` call; a package
  that is not installed is not shown and is never installed. Before, the MCP server that the
  installer puts on every install (`sibyl-memory-cli[mcp]`) stayed on its old version. The
  `--json` document has the same keys, with one row per installed package; the pipx hint and the
  exit codes are unchanged. Tests: `tests/test_update_installed_pkgs_2026_10_09.py` (+13); two
  pipx handoff rows list the five packages. Suite: 930 passed (was 917).

### Added (version row, 2026-10-08)
- `sibyl --version` prints `sibyl <version>` (for example `sibyl 0.5.0` or `sibyl 0.5.0+dev.4`) and
  exits 0. It needs no subcommand and reads or writes nothing under `~/.sibyl-memory`.
- `sibyl status` opens with a `plugin` eyebrow and one `Version  cli <v> · client <v>` row (client
  `not installed` when `sibyl-memory-client` is missing), in both the "Not activated." and the
  activated layout, so a pasted status always carries the build. `sibyl whoami` is unchanged.
  Tests: `tests/test_version_2026_10_08.py`. Tester guide line: `sibyl --version` replaces `pip list`.

### Fixed (shown tier and plan end, 2026-10-08)
- F1: every tier the CLI shows is the tier of this computer's current verified pass, the saved
  credentials tier only when there is no valid pass (`_account_gate.display_tier` /
  `peek_tier`): `sibyl status` (LOCAL Tier row, the uncapped / percent-of-cap DB size, the
  local vs server drift check), the `sibyl init` "already activated" and "Activated." rows,
  `sibyl whoami` and the `sibyl upgrade` current tier. `sibyl status` takes the access check-in
  before printing the LOCAL rows (output order unchanged) and re-reads the credentials the
  check-in may have rewritten. A tier cache that disagrees with a valid pass reads
  `superseded by the access pass (checked ...)` instead of a second tier. `sibyl refresh` and
  the background re-check rewrite the saved tier (client `sync_credentials_tier`). Looking up
  the pass with no `entitlement.json` evaluates nothing, so it never leaves a file behind.
- F2: the Access row says `active · re-checks in <h>h` (the pass re-check), not `renews in`,
  which read like a billing renewal. A `Plan` row under it shows the billing date from the
  pass's `acct.plan`: `Pro · ends 8 Oct 2027` (a cancellation, or a fixed term) or
  `Pro · renews 8 Oct 2027` (a renewing card plan), in local time; no row when the server sends
  no date (older api, Free, staker, Lifetime). The tier word is the one the Tier row shows.
- README: the status sample and the Access table use `re-checks in`, and describe the Plan row.
- Tests: `tests/test_tier_plan_2026_10_08.py` (+13); three label assertions updated to
  `re-checks in`. Suite: 911 passed (was 898).

### Changed (web-first onboarding QA patch, 2026-10-08)
Decision D21 of onboarding-friction-final-analysis-2026-10-08 (findings F7, F12, P1). Messages
only: flows, exit codes, streams and JSON keys are unchanged.
- F7: after `Activated.`, `sibyl init` ends `next: sibyl status` instead of `next: sibyl setup`
  when an agent on this computer is already wired (the installer runs `sibyl setup` before
  `sibyl init`, so this is the common case). The check is the one `sibyl email confirm` already
  uses (row E24: Hermes or Codex config, or `sibyl-memory` in `~/.claude.json` or
  `~/.claude/settings.json`; file reads only, no subprocess). When wired, `later: sibyl setup`
  is no longer queued behind `sibyl email add <address>` or `sibyl email verify`. With nothing
  wired the output is unchanged.
- Tests: seven new init rows (old api, verified, email required with none, unverified required
  and not required, email confirmation not open, Codex wired), each with a harness wired.
- P1: the approve hint under the code reads "already have a Sibyl Memory account (website or
  another computer)? approve this one at <url> instead (sign in there, then type this code)".
  The account may exist only on the website, so "already set up with this email on another
  computer?" was the wrong question.
- F12: with `features.device_approval` on, the `ask your human:` line printed while `sibyl init`
  waits puts the likeliest path first: "if you already have a Sibyl Memory account, approve this
  computer at <url> with the code <code>; otherwise open <activation page> and sign in with email
  plus that code, a desktop wallet, or USDC from a mobile wallet; I pick it up here". An agent
  relays one decision instead of four equal options. With the feature off the sentence is
  unchanged. Tests assert the order, the code, the single `?r=` link and the unchanged
  feature-off wording.
- The "wire it into your agent" block after `Activated.` no longer tells people to hand-edit
  `~/.hermes/config.yaml`, `pip install sibyl-memory-mcp` or import the provider by hand (it
  contradicted the install flow: installer, then `sibyl setup`, then `sibyl init`, and the
  dd0ce76 Hermes fix). With nothing wired it is one line, "wire it into your agent: sibyl setup
  (wires Claude Code, Codex and Hermes)", and the handoff stays `next: sibyl setup`. With an
  agent already wired (the F7 check) nothing is printed and the handoff ends
  `next: sibyl status`. Two new tests cover both cases and the absence of the old commands.

### Fixed (agent handoff check, 2026-10-07)
Findings of the adversarial check of the handoff build (no loop left for an agent that follows
only `next:`). Messages only: flows, exit codes, streams and JSON keys are unchanged.
- Email, when the api does not take confirmation from the terminal (`features.cli_email_verify`
  absent or false, row E11): `sibyl init` and the `sibyl email` state no longer hand back
  `sibyl email add <address>` (which could only fail with `not_open` and ask for the address
  again). They ask the person to add and confirm the email at the account page; init ends
  `next: sibyl setup` with `later: sibyl email`, `sibyl email` ends `next: done`.
- `sibyl setup`: a wirer error with no specific handoff (a Hermes or Codex config write, a Hermes
  install-plugin failure) ends `next: done` with `ask your human:` naming the harness and the
  error, instead of `next: retry`. A server that did not start ends `next: done` with "run
  sibyl-memory-mcp in a terminal and tell me the first error line" (a missing credential:
  `next: sibyl status`). An already-wired harness gets no restart ask; only a changed one does.
- `sibyl update --apply`: a non-zero pip exit ends `next: done` with "pip exited N upgrading the
  packages (output above); tell me what it said", instead of `next: retry`.
- `sibyl logout` (rows O1 to O3) ends `next: done` with `later: sibyl init`: signing out no
  longer starts a sign-in.
- File labels in `sibyl debloat apply|auto|undo` handoffs, the undo hint and the migrate
  "not trimmed" line are shell-quoted, so a label with a space still runs verbatim.
- Tests: the convention lint sweep now covers memory search and recall, adopt A3/A4/A6/A7,
  debloat auto, manual and undo, skills install, update --apply (pip failed, pip missing, pipx,
  upgraded), setup X4 and X6 to X16 through the real wirers, migrate N3 to N10 through
  `cmd_migrate`, health H1c/H2c, plus the E11 email and logout rows; each new scenario also
  asserts it reached the path it is named for. Suite: 879 passed, 1 skipped (the installed
  Claude Code binary contract test, which skips without `claude` on PATH).

### Added (agent handoff messages, 2026-10-06)
Spec agent-handoff-messages-spec-2026-10-03 (section 2 rows I, E, U, K, S, R, H, W, D, O, V, A,
F, G, P, T, Y, Z, X, N). Messages only: no flow, command, exit code or server contract changes.
- Every stop of a `sibyl` command ends with the handoff block, on stdout, TTY or not: zero or
  more `later: <command>`, at most one `ask your human: <sentence>`, then `next: <command>`
  (or `done`, `retry`, `retry in N minutes`), always the last line. Plain text at column 0, no
  glyph, no colour. A placeholder (`<code>`, `<address>`, `<index>`, `<cs_code>`, `<file>`) only
  appears with the `ask your human:` line that names it.
- Waits print `ask your human:` and `waiting: <what> (up to m:ss)` once when the watching
  starts: `sibyl init` (the bind, the computer-limit sign-out, device approval) and
  `sibyl upgrade` (the plan change).
- `--json` (`sibyl update`, `sibyl debloat`) prints no block; the document gains `next` and
  `human_action`. `sibyl debloat --fyi` (one-line startup format) and `sibyl hook` (JSON for
  Claude Code) print none.
- `_handoff.py` (the block, waits, the JSON keys) and `_account_gate.access_handoff()`, so
  `status`, `refresh`, `health` and the gate lines agree; the values come from the client's
  `ENTITLEMENT_NEXT` / `ENTITLEMENT_HUMAN_ACTION`.
- `sibyl claim` success line: "Run the next command and agents pick it up on their next call."
  (was "Run `sibyl status` to see it. Your agent picks it up next session."). The tier-drift hint
  in `sibyl status` names `sibyl refresh` (was `sibyl upgrade`, which opens a purchase page).
- Non-interactive runs say which default they took: `no terminal: default Y|N taken` (setup
  questions), `no terminal: proceeding (pass --yes to say so)` (migrate), the extraction and
  trim lines of the guided migration.
- Setup with nothing detected: `later: sibyl setup <target>`, the question, `next: sibyl status`.

### Fixed (agent handoff messages, 2026-10-06; spec section 5.3)
- `sibyl setup` with two or more harnesses detected, no `--yes` and no terminal no longer raises
  a traceback at the picker: it wires all detected, as `--yes` does, and says so.
- `sibyl save --fact` on a full free cap prints the cap line (`later: sibyl upgrade`,
  `next: done`) instead of a `CapExceededError` traceback.
- `sibyl migrate` with nothing extracted (a run without a terminal verifies at once) prints
  "Backed up and wired. Extraction has not run yet." instead of "Done. Your memory now lives in
  Sibyl".
- The email `expired` fallback line needed no change (it is the copy when the server sends no
  message); both paths are now tested.

### Tests (agent handoff messages)
- `tests/conftest.py`: `handoff(out)`, `json_handoff(payload)`, `assert_convention(out, err)`.
- `tests/test_handoff_rows_2026_10_03.py`: one scenario per row, each through the lint.
- `tests/test_handoff_convention_2026_10_03.py`: the lint, a sweep over every scenario, and a
  source check (no em dash, glyph or ANSI in handoff copy).
- `tests/test_agent_follows_next_2026_10_03.py`: an agent that reads only `next:` lines runs
  init, email add, email confirm, setup, status to `done` against a stub api, and init, setup,
  status to `done` against an old api.
- The claim copy helper in `test_accounts_patch_2026_09_26.py` drops the block lines. Suite:
  736 passed (453 before).

### Changed (tier convention, 2026-10-05)
Spec tier-convention-spec-2026-10-05 section 5 (same coordinated release; version unchanged).
- `sibyl status` prints the tier as Free / Pro / Elite (local and server view, through the
  client's shared `tiers` module); a legacy local label (stake) next to the server's canonical
  one (pro) is no longer reported as drift. The computers line names the plan (`1 of 10 (Elite)`)
  and the size line uses `tiers.is_paid`. Other tier lines (`init`, `whoami`, `claim`, `upgrade`)
  keep their upper-case style but map legacy labels to the canonical word.
- Access row for an unrecognised signed label: `active · access label "<raw>" is not recognised
  by this version, treated as Free (tier_unknown) · run sibyl update`; a 503 `tier_unknown` from
  the server reads `renewal paused · access label check (tier_unknown) · keep working; it retries
  on its own` and keeps the last valid token.
- Hooks: the stdlib-only `PAID_TIERS` mirror gains `elite` and still equals
  `sibyl_memory_client.tiers.PAID_TIERS` (asserted); the unverified claim is stripped and
  lowercased (`claim_label`); a `tier_unknown` answer is written to lifecycle.log.
- `tests/smoke/venv-compat.sh`: `sibyl status` with an elite entitlement prints Elite and exits 0.
- Tests: `tests/test_tiers_cli_2026_10_05.py` (+15, including the source guard: no legacy tier
  literal outside the single hooks.py mirror line, which parses to the client set);
  `test_f11_paid_tier_literal_matches_the_client` compares against `tiers.PAID_TIERS` and a sibling
  covers elite / " Elite " / gold claims. Suite 437 -> 453.

### Added (paid tier R1, 2026-09-29, R1B-2)
Plan plugin-paid-tier-2026-09-29, operator defaults D1-D15 (same coordinated release; version unchanged).
- The Claude Code Stop hook asks for a headless save by ACTIVE time on Pro: once early (5 memory
  writes or a compaction), then every 60 active minutes (`sibyl autosave`), at most 8 a session and
  12 a day, only with something to save. Each request is numbered ("save 2 of this session") so
  the transcript guard works per request. `UserPromptSubmit` counts prompts and active time on
  both tiers (a gap after a turn counts at most 5 minutes; the model's own turn at most 30). A
  scripted `claude -p` run (no transcript, or fewer than two prompts) is never continued.
  SessionStart adds the tier-change line and, on Pro, up to 3 named skill proposals (read-only).
- `sibyl autosave [status|on|off|every <30-240>]` (free: says it is on Pro and keeps the setting).
- `sibyl refresh`: one entitlement check now, the before/after diff (tier, cap, computers, email,
  access), the procedures, shims, Codex block and (already wired) hooks refreshed, the lifecycle
  rows. Exit 0 (1 on an authoritative denial, 2 offline); with no account it exits 0 and says to
  run `sibyl init`.
- `sibyl skills proposals [list|show|accept|reject|mute|unmute]` (Pro; free sees "on Pro").
  `accept` writes a validated SKILL.md for the Claude Code, Codex and Hermes folders present (or
  `--to <dir>`) plus the reference copy in memory; `reject` cools the slug off 30 days.
- Computer limit: `sibyl init` sends `replace: {session_token}` on session-init when this computer
  already holds credentials (`--replace` implies `--force`); a `device_limit` answer (409, or
  `/check` bound:false with `limit`) prints the computers and, in a terminal, says this terminal
  cannot sign another computer out and asks "Open the account page to sign <label> out? [y/N]"
  (yes opens the account Security page, says to sign it out there and finish the sign-in in the
  browser, and keeps waiting), otherwise clear instructions and exit 2. The Free upgrade line
  names the Pro limit only when the api sends one (`pro_limit`), else "every computer you use". `sibyl devices` and `sibyl status` show
  `1 of 1 (Free) · Pro: up to 10`; the read-only access row names the computer limit.
- Codex AGENTS.md block: obey a Sibyl Memory `notice` asking for a save.

### Fixed (paid tier R1 check, 2026-09-29, R1B-4)
- Over-the-limit copy says new memories save only on "the computer that signed in most recently"
  (the server keeps the newest sign-in writable), not "the most recently used computer".
- The device-limit prompt no longer offers to "sign out ... and use this computer instead": this
  terminal cannot sign another computer out, so it asks to open the account page and says what to
  do there; the Free upgrade line has no hard-coded "up to 10".
- Stop hook: an automatic-save request is recorded (per-session and per-day caps, the interval)
  only when it is emitted; one suppressed by the transcript guard costs nothing and the next one is
  numbered past it. A compaction makes only its own session due (the per-session
  `compaction_seen`); the write count stays the account's (plan D6.5).

One coordinated release with the accounts page. Two things change for users:
every Sibyl Memory account needs a confirmed email for receipts, security
notices, purchases and recovery (email does not gate memory), and **an
activated account is now required for agents to use memory** (MCP, Hermes,
LangGraph, the SDK). This is a FIVE-package release, not the CLI-only publish
this entry first described: `sibyl-memory-client` 0.9.0, `sibyl-memory-hermes`
0.5.0, `sibyl-memory-mcp` 0.3.0, `sibyl-memory-langgraph` 0.3.0 and this CLI
(the number stays 0.5.0, decision D8). Floors: `client>=0.9.0`,
`hermes>=0.5.0`, `mcp>=0.3.0` (extra), so a partial upgrade cannot pair a gated
adapter with an un-gated engine. `credentials.json` schema is unchanged; a new
`entitlement.json` sits beside it. After a confirm the credentials file is
rewritten from the credentials the server re-signed; a local tier hint drops
the old signature (see Security). `sibyl memory` never needs an account.
Verified in a fresh venv with the five built wheels against hermes-agent 0.19.0
(current on PyPI at build).

### Added (save and debloat, 2026-09-27, SD-2)
Spec plugin-save-debloat-spec-2026-09-27 (same coordinated release; version unchanged).
- `sibyl save` (`--fact`, `--capsule` on Pro, `--preview`, `--status`; bare prints the three
  ways to run the save procedure and writes nothing).
- `sibyl debloat` (report, `--fyi`, `--json` with the plan when a policy exists, `--root`,
  `--config`, `--include`; always exits 0), `sibyl debloat plan`, `apply [<file>] [--yes]`
  (free: sibling `*-history` file; Pro: a memory reference document; a verified backup first),
  `auto <file>` / `manual <file>` (the standing per-file authorisation), `undo [<file>]`.
- `sibyl skills install|status|reset|uninstall`, `sibyl reminders on|off|quiet`, and
  `sibyl hook <event>` (installed by setup; not for manual use).
- `sibyl setup` / `sibyl migrate` wiring: Claude Code gets `~/.claude/skills/sibyl-save` and
  `sibyl-debloat` plus five hooks in `~/.claude/settings.json` (SessionStart, UserPromptSubmit,
  Stop, PreCompact, SessionEnd; absolute `sibyl` path; timeout 5 s; merged without touching
  any other hook); Codex gets a marked block in `~/.codex/AGENTS.md` (no custom-prompt files:
  Codex CLI 0.135.0 has no `~/.codex/prompts` loader, spec D9 fallback); Hermes gets
  `$HERMES_HOME/skills/`. `sibyl init` installs the procedures right after `entitlement.json`;
  `sibyl update --apply` refreshes them. `sibyl status` gains the `lifecycle` rows.
- The Claude Code hooks: free = reminders (debloat FYI and unsaved-last-session at start,
  save-due on a prompt); Pro = the start runs the debloat plan and reports, Stop asks the model
  to run the save procedure headless (R1: by active time, see above; at 5 writes or after a compaction; never
  on `stop_hook_active`; never twice), PreCompact and SessionEnd write the session capsule.
  Output shapes verified against Claude Code 2.1.283's hook schemas.

### Changed (save and debloat)
- `sibyl migrate`'s trim step covers every instruction file the scan found (`CLAUDE.md`,
  `AGENTS.md`, `.cursorrules`, ...), one question per file, each backup re-verified; config
  files are never trimmed; `.cursor/rules` is measured, not trimmed; a project `.debloat.json`
  surface narrows the candidates; a lean version keeping under 10 % or over 90 % is skipped.
  The summary prints one `trimmed` line per file with its name. `--codex-config` now reaches the
  Codex wirer.

### Tests (save and debloat)
- `test_lifecycle_cli_2026_09_27.py` 32, `test_migrate_trim_2026_09_27.py` 9; three existing
  tests adjusted for the per-file trim records and the trim safety bounds (test_orchestrator,
  test_prelaunch_fixes CLI-9, test_setup no-op). `tests/smoke/venv-compat.sh` gains the
  save/debloat checks.

### Fixed (second independent check of save and debloat, 2026-09-27, SD-11 to SD-13)
- A symlinked folder above an instruction file no longer disables debloat (Fedora Atomic's
  `/home -> var/home`, a stow-style `~/.claude -> ~/dotfiles/claude`, a project opened through a
  link): the file is trimmed at its real location, never written through the link. `--root` is
  resolved to its real path; `auto`, `manual` and `undo` use the real-path identity. A file whose
  real location is outside your home and the project is refused.
- `sibyl debloat undo` without a file undoes the exact file the last apply trimmed (recorded by
  path); in another project it says where that file is instead of restoring the same name here.
- An interactive `sibyl debloat apply` interrupted right after the rewrite keeps the backup.

### Tests (second independent check)
- `test_sd_final_cli_2026_09_27.py` (5): `--root` through a symlink, `auto` stores the real path,
  a stow `~/.claude` free apply, bare undo across two projects, a record without an id.

### Fixed (independent check of save and debloat, 2026-09-27, SD-7 to SD-10)
- `sibyl debloat auto <file>` authorises that file at its path ("Automatic context trim for this
  file at <path> means: ..."); `manual` takes a name or a path; name-only entries from the earlier
  build are dropped with a one-time notice (`sibyl debloat`, `sibyl status`).
- `sibyl debloat undo` restores the backup the file's last apply recorded and refuses one that is
  identical to the current file.
- Claude Code hooks never touch the network: the hook client's entitlement gate never refreshes,
  its usage heartbeat is off, and a trim or capsule is written only when the cap gate decides
  locally and within 2.5 s of the hook's start (otherwise the start plans only). Measured worst
  case 0.29 s (was 17.5 s with a slow network); the timeout stays 5 s.
- `sibyl` starts at `sibyl_memory_cli._entry.main`: `sibyl hook <event>` runs with the standard
  library only until a hook needs the client; the free Stop returns on the unverified tier claim
  (a paid claim is always verified); free PreCompact / SessionEnd neither verify nor open
  memory.db. Free Stop 0.19 s -> 0.04 s. The package `__init__` is lazy.
- Hook commands are `[ -x <sibyl> ] && exec <sibyl> hook <event>; exit 0`: they exit 0 quietly
  once the package is gone; setup replaces entries of the earlier form. `sibyl skills uninstall
  --help` says to run it before `pip uninstall`.
- Codex: two skill shims in `~/.codex/skills/` (the format Codex CLI 0.135.0 validates).
- `sibyl migrate` trim pointer: "a full copy of this file is in the backup ...; the migrate
  extraction stored entries in Sibyl Memory".
- `sibyl status` / `sibyl save` say when the Pro save nudge comes.
- `venv-compat.sh`: four more checks (Codex dry run, the entry point, a wheel `sibyl hook stop`,
  the guarded command with the binary gone).

### Tests (independent check of save and debloat)
- `test_sd_fixes_cli_2026_09_27.py`; adjusted `test_lifecycle_cli_2026_09_27.py` (per-path
  authorisation, warm tier cache for Pro hook writes, the guarded hook command, Codex skills) and
  `test_migrate_trim_2026_09_27.py` (pointer text).

### Fixed (independent check fix batch, 2026-09-27, GATE-15)
- `_bearer_headers` (used by `/access`, `/email-verify/request`,
  `/email-verify/confirm`, `/upgrade-nonce`, `/stripe-claim`) now refreshes
  first when this computer holds no usable token (the same ensure-fresh path
  the logout / revoke calls use: one blocking `/entitlement` attempt, then
  whatever usable token exists; a failed refresh sends none). Before, a device
  back from offline past `exp + og`, or one that had not finished setup, sent
  no token and would meet the cutoff's 403.
- A 403 `client_too_old` while this computer holds no usable token now prints
  the setup line ("setup is not finished on this computer. connect to the
  internet once to finish setup. nothing was deleted."), not "update the
  plugin": the plugin is current, the api refused the absent token. With a
  token (or no client gate available) the update line stays. Applies to
  email, devices, claim and upgrade.
- Repo README: the fork table says `/check-write` refuses "from the client
  cutoff"; the privacy paragraph says the size check and the usage count also
  carry this computer's signed access token.
- Tests: `test_release_fixes_2026_09_27.py` 15 (13 fail before; the two
  guards, a usable token is not refreshed and `client_too_old` with a token
  still says update, pass before and after).

### Added (gate fix before the independent check, 2026-09-27)
- `sibyl memory adopt [--yes]`: moves the store's rows written before sign-in
  (the pre-account default tenant) into the signed-in account (client
  `_adopt`: one local transaction, nothing deleted, a key the account already
  has kept as `<key> (before sign-in)`, an audit row in the store, no network
  of its own). Needs a verified sign-in. When the store has held another
  account it explains and asks (`--yes` skips the question; a non-terminal
  without `--yes` moves nothing). A re-run says there is nothing to move.
- `sibyl init` does the same move on the first sign-in right after writing
  `entitlement.json`, and prints the row count only (never content). A store
  that has held another account is left alone with a pointer to
  `sibyl memory adopt`.
- `sibyl status`: the default-tenant hint names `sibyl memory adopt`; a new
  `access` row `setup not finished · connect to the internet once to finish
  setup` for credentials that have not received their first entitlement (the
  client no longer grants an offline bootstrap grace).

### Added (account gate, 2026-09-27)
- `sibyl init` writes `entitlement.json` (0600, atomic, symlink-refusing) from
  the `entitlement` the api sends next to `credentials` on `/check`, right after
  credentials.json, and drops a token bound to a previous sign-in. An api that
  sends none prints nothing new; the client bootstraps on first use. A
  malformed value (not a string, over 4096 bytes, not SE1-shaped) is ignored.
- `sibyl email confirm` writes the re-issued `entitlement` from the confirm
  answer (the account state inside it must not lag a week).
- Every session-authenticated call carries `X-Sibyl-Entitlement` when this
  computer holds a verified, bound token inside its offline grace (spec 5.6);
  a token the api would refuse is never sent. `sibyl claim` now sends the
  bearer headers too.
- `sibyl devices`, `sibyl devices revoke` and the logout revoke: with no
  usable token they try one `/api/plugin/entitlement` refresh first, and on a
  403 `entitlement_invalid` they retry once without the header, so signing out
  or revoking a lost computer never blocks on the gate.
- A 403 `entitlement_invalid` prints "this computer's sign-in file is damaged,
  expired or belongs to another computer. run sibyl init to sign in again.";
  a 403 `client_too_old` prints "update the plugin: pip install -U ..." (email,
  devices, claim, upgrade).
- `sibyl status`: an `access` row after one check-in attempt (`active · renews
  in <h>h`, `offline · <n> days of grace left · connect once to renew`,
  `paused · offline too long ...`, `signed out · run sibyl init`, `under review
  · email sibyl@sibyllabs.org`, ...); when the server is unreachable, the
  account rows from the last check-in labelled `as of <date>`; with no
  credentials, "agents cannot use memory until you do. your local data is
  kept."; a count-only hint when the default store holds entities under the
  pre-account default tenant.
- `sibyl logout` also removes `entitlement.json` (listed under `removed`).
- `sibyl health` prints the entitlement state and the pinned key ids.
- `sibyl memory list|search|recall` read local data with no account and no
  network (a constructor-injected test gate, on purpose) and print "reading
  local data without an account. agents need sibyl init." when there is none.
  `sibyl migrate` says the extraction step needs `sibyl init`.

### Added
- `sibyl email`: shows whether the account email is confirmed, not confirmed,
  or missing. `sibyl email add <address>` and `sibyl email verify` send a
  six-digit code (`POST /api/plugin/email-verify/request`) and, in a terminal,
  prompt for it; `sibyl email confirm <code>` confirms it from scripts
  (`POST /api/plugin/email-verify/confirm`). Confirming from this computer keeps
  it signed in and signs out every other computer set up on the account before.
- `sibyl init` finishes the account right after `Activated.`: a wallet
  activation is asked for an address, an email activation is offered a code to
  confirm the address. Terminal only; `--no-email`, a non-TTY shell or
  `SIBYL_NONINTERACTIVE=1` skip the prompt and print the one-line reminder.
- `sibyl init` follows web device approval: when the email already has an
  owner it prints `approve this computer at https://account.sibyllabs.org/activate?r=...`
  once, switches the spinner to `waiting for approval at account.sibyllabs.org`,
  and stops with exit 2 when the owner chose "Not me". The link names the
  request by a sha256 handle, never the session id.
- `sibyl status` rows `Email`, `Account` (incomplete, the grace date, and
  whether purchases are refused) and `Dashboard`, when the server reports them.
- `SIBYL_ACCOUNT_BASE` override for the account page origin (testing only).

### Changed
- `sibyl dashboard` opens the account page the server advertises
  (`features.web_dashboard_url`) with no token in the URL; you sign in there
  with your email or wallet. While the page is not open it prints
  `the account page is not open yet.` and shows `sibyl status` as before.
- `sibyl upgrade` opens the account billing page when the server offers card
  checkout there, otherwise the stake / USDC page through the one-time
  `upgrade-nonce` exchange link. An expired session prints `session expired.
  re-run sibyl init.` (exit 1, nothing opened); any other failure except a
  missing endpoint prints `could not start the upgrade. try again.` and exits 2.
  It warns `a confirmed email is required before buying` whenever the server
  says purchases are blocked (`account.purchase_blocked`), not only past grace.
- `sibyl claim` explains the two new refusals by their `code`:
  `unclaimed_account` (run `sibyl email verify`, or `sibyl email add` when no
  email is on file, then claim again) and `email_required` (add and confirm an
  email, then claim again).
- `sibyl devices` / `devices revoke` print `this account is under review.
  email sibyl@sibyllabs.org.` for a review hold instead of a raw body, and
  `already revoked` for a device revoked earlier. `sibyl logout` says
  `signed out on the server too.` when the server confirmed the revoke.
- `sibyl whoami` is unchanged and still offline: it does not show whether the
  email is confirmed. Use `sibyl email` or `sibyl status`.

### Security
- A signed field of `credentials.json` (account, tenant, tier, email, wallet,
  issued_at, schema_version) never changes under an old signature.
  `sibyl email confirm` writes the credentials the server re-signed (through
  the same field allowlist as `sibyl init`, keeping this computer's bearer) and
  changes nothing when the server sends none. `sibyl upgrade` and `sibyl claim`
  still write the new `tier` (and the staker `wallet`) as a local hint, and
  drop `signature` and `signed_at` from the file with that edit (neither is in
  the allowlist; `sibyl init` never writes them), so the plugins send no
  signed claim for it. 0.4.1 kept the old signature next to the edited field,
  which the server reports as a stale or tampered credential.
- `sibyl upgrade` no longer puts the bearer in the browser URL. It uses the
  one-time exchange code (or the account page, which takes no token). The old
  `?session=` link is used only against an api that has no `upgrade-nonce`
  endpoint at all.
- `sibyl dashboard` no longer opens `SIBYL_DASHBOARD_BASE?session=<bearer>`;
  the variable is ignored.
- Links printed or opened on the account origin are checked first: an approval
  link must be `https://account.sibyllabs.org/activate?r=<64 hex>`, and a
  dashboard link must be that origin exactly (a lookalike host such as
  `account.sibyllabs.org.example` is refused).

### Compatibility
- Against an api from before this release (no `features`, no `account`, no
  email-verify endpoints) 0.5.0 prints what 0.4.1 printed: no prompts, no new
  status rows; `sibyl email` says `this server does not report email state
  yet.`, and `sibyl email add/verify` say email confirmation is not available
  on this server yet.

## [0.4.1] - 2026-09-04

### Added
- `sibyl claim <code>`: links a card purchase (Stripe checkout) made at
  sibyllabs.org/pro to the account activated on this machine. Works with
  email and wallet activation alike. Sends the bearer as session_token so
  the server verifies the caller controls the destination account, not
  just the receipt. On success the local tier hint and tier cache sync
  immediately.
- **Platform line, and a native-Windows gate.** Sibyl Memory runs on Linux,
  macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is
  not supported. `sibyl init` and `sibyl setup` now print the detected platform
  and, on native Windows, refuse with a pointer to
  https://docs.sibyllabs.org/memory/install#windows and the exit hatch
  `SIBYL_ALLOW_NATIVE_WINDOWS=1`. Every other command is untouched, so a machine
  that already activated on native Windows keeps `status`, `upgrade`, `claim`,
  `devices`, `logout`, `health`, `memory` and `update` working. WSL2 reports as
  Linux to Python and to us, so it passes with the label "Linux, WSL2". A
  platform nobody has tested warns and continues; it is never blocked.
  `sibyl status` gained a `Platform` row and `sibyl whoami` now names the
  platform in the device line instead of the bare OS family.
- `sibyl init` reports `os_version`, `arch` and `wsl` alongside `os_family` in
  the session-init payload, so the account record can tell WSL2 from native
  Windows. Kernel release string and machine type only, no PII.

### Changed
- The account id now prints IN FULL on every display surface (`whoami`,
  `status`, `upgrade`, `init`) instead of the truncated `12345678…abcd`
  form. It is a routing identifier the claim flow and the upcoming
  accounts page require users to read verbatim; it is not a bearer secret,
  and the never-print-the-bearer invariant is unchanged. Email and wallet
  masking are unchanged (`--full` still governs those).
- **Dependency floor raised to `sibyl-memory-client>=0.8.1`**, which carries
  the shared `_trust` context and declares `certifi` as a real dependency.
  Without the floor bump a fresh install could resolve an older client, the
  import fallback would quietly return the same broken default context, and
  the fix would not actually ship. The floor is what makes certifi land in
  every install path, pipx included.
- Package metadata now names the supported platforms: `Operating System ::
  POSIX :: Linux` and `Operating System :: MacOS`. Sibyl Memory runs on Linux,
  macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is
  not supported. Classifiers are informational; pip does not enforce them, and
  WSL2 installs look like Linux to pip, which is correct.

### Fixed
- **`sibyl init` failed on macOS python.org "Framework" builds: `Warning:
  session-init failed (0)`, browser pairing ended in "session not valid /
  Bind failed", and `sibyl status` showed `server error: 0`.** That build
  ships no CA bundle wired into Python's own OpenSSL (users are told to run
  `Install Certificates.command`; many never do), so every
  `urllib.request.urlopen` with the stdlib default context raised `[SSL:
  CERTIFICATE_VERIFY_FAILED]` against api.sibyllabs.org, surfacing through
  `http_request`'s URLError branch as status 0, while Safari and curl worked
  (they read the system Keychain), which made the CLI look selectively
  broken. Both CLI network paths (`http_request` and the `_pypi_latest`
  update check, which was silently returning nothing on these machines) now
  pass an explicit verified context from
  `sibyl_memory_client._trust.https_context()`: platform trust store PLUS
  certifi's CA bundle, additive only, hostname and chain verification fully
  on. With no `sibyl-memory-client` importable (bare source tree) the CLI
  degrades to the stdlib default context instead of crashing.
  Reported by @keyurbodar (Sibyl-Labs/Sibyl-Memory#29); independent
  implementation.

## [0.4.0] "Lucid" - 2026-08-31

Minor version: `sibyl memory search` prints a new `cause:` line on the zero path,
so anything parsing this command's output should read this entry.

- Floors `sibyl-memory-client>=0.8.0`, `sibyl-memory-hermes>=0.4.0` and, in the
  `mcp` extra, `sibyl-memory-mcp>=0.2.0`. The CLI prints causes it does not own;
  an old client underneath a new CLI would leave it printing a cause line for a
  verdict that is never populated.

### Changed
- **`sibyl memory search` explains a zero instead of just reporting one** (branch
  `lang-core-verdict`, 2026-08-31). It used to print `(no matches for 'x')` and
  stop — the terminal-shaped version of the defect this stage closes, and the
  reader is usually a new user deciding whether the product works. Saying "no
  matches" against a store that is simply EMPTY was the single most misleading
  thing this command could do. It now prints the canonical cause in plain
  language plus a machine-readable `cause: <code>` line, and pays one count on
  the zero path so `empty_store` is reported as what it is: a fixable state, not
  a failed search. The success path is unchanged and prints no extra noise.
- The cause vocabulary is imported from `sibyl_memory_client.verdicts`; the CLI
  prints causes, it does not own them. A test asserts it declares none.

## [0.3.23] - 2026-08-22

### Changed
- Require `sibyl-memory-client>=0.7.0` so `sibyl update` / a fresh install pulls
  the multi-language search part-4 fixes (N4/N5/N1'-diagnostics: a nonzero-df
  function word no longer anchors/pollutes ranking, a dropped negation word
  abstains instead of silently answering the opposite). No CLI code change.
  See `sibyl-memory-client` 0.7.0 CHANGELOG for the full N4/N3'/N5/N1' detail.

## [0.3.22] - 2026-08-16

### Changed
- Require `sibyl-memory-client>=0.6.1` so `sibyl update` pulls the multi-language search part-3 fixes (N1/N2/N3: the agent-default MCP path now reaches PL/EN recall parity on question-shaped queries). No CLI code change.

## [0.3.21] - 2026-08-12

Multi-language search, part 2 (Polish-language evaluation) — CLI leg.

### Fixed
- **F1 — `sibyl memory list/search/recall` read the wrong tenant on an
  activated account.** `cmd_memory` opened the store at the hard-coded
  `DEFAULT_TENANT` while the MCP server writes under the account's real tenant,
  so an activated account saw `(no entities)` / `(no matches)` for a perfectly
  healthy store — the surface that made the Polish eval first look like a recall
  failure before the linguistic gap was even reached. `cmd_memory` now resolves
  the tenant from `credentials.json` via the same Contract-T ladder the server
  uses (`tenant_id -> account_id -> DEFAULT_TENANT`, matching `server.py`), like
  `status`/`health` already did, with a call-time `--creds` default fallback so
  direct-`Namespace` unit invocations still resolve. Read-only; no write path
  touched.
- **Free-tier cap display corrected: 2 MB to 5 MB.** The `sibyl upgrade`
  subtitle still showed the pre-2026-08-06 cap; the free-tier local cap was
  raised to 5 MiB in the 0.5.0 release. Display-only, no logic change.

### Changed
- **Dependency floor raised to `sibyl-memory-client>=0.6.0`.** `sibyl memory
  search` / `sibyl recall` now inherit the F2 unconditional-shadow append and
  the D2L coverage-gated stem rescue, taking Polish recall to parity with
  English on the eval battery (see `sibyl-memory-client` 0.6.0). No CLI code
  change for this leg.

## [0.3.20] - 2026-08-06

### Changed
- **Dependency floor raised to `sibyl-memory-client>=0.5.0`** for multi-language
  search (schema v4). `sibyl search` / `sibyl recall` now resolve non-ASCII,
  non-Latin, CJK, Thai, and compound-token queries that previously returned
  nothing (100-language sweep: 21/100 → 100/100). No CLI code change. See
  `sibyl-memory-client` 0.5.0.

## [0.3.19] - 2026-07-05

Super-patch: recovery + adjudication of the remaining Fable 10-lens audit
findings (`plugin-hardening-superpatch-plan-2026-07-05.md`).

### Fixed
- **`sibyl logout` never revoked the server-side bearer (Real #4).** The
  account bearer has no server-side expiry, so a logout that only unlinked
  local credentials left the token valid forever. `logout` now revokes
  THIS device's bearer first, reusing the exact endpoint + auth shape
  `sibyl devices revoke` already uses (`GET /api/plugin/devices` to
  identify this device via `is_this_device`, then
  `POST /api/plugin/devices {"bearer_id": ...}`) — no new endpoint. Fully
  best-effort: any network/HTTP failure, or an inability to identify this
  device's server record, prints the same offline caveat
  (`sibyl devices revoke` from another device) instead of blocking local
  logout, which still proceeds unconditionally.
- **`credentials.json` dropped the server-issued `tenant_id` (Contract
  T).** `cmd_init`'s field allowlist (`_CRED_FIELDS`) omitted `tenant_id`,
  so every activated account silently fell back to the shared
  `DEFAULT_TENANT` on every surface (client/mcp/hermes/langgraph) instead
  of resolving to its own tenant. `tenant_id` is now persisted alongside
  `account_id`, so all surfaces resolve the SAME tenant from one
  credentials file via the canonical ladder
  `tenant_id -> account_id -> DEFAULT_TENANT`.

### Changed
- Packaging: added the `Repository` URL
  `https://github.com/Sibyl-Labs/Sibyl-Memory` to `[project.urls]`
  (previously omitted) (R27), and bounded the third-party `pyyaml`
  dependency to `>=6.0,<7` (R29).

## [0.3.18] - 2026-06-30

Post-launch quality fixes from external audit reports (#13, #19, #15). All LOW
severity; diagnostic/robustness only — no behavior change to wiring or the cap
gate itself.

### Fixed
- **`status` DB size now matches the cap gate (#13, B001).** `sibyl status`
  reported the raw `memory.db` file size (`Path.stat().st_size`), but the
  free-tier cap is enforced against the WAL-inclusive logical size
  (`sibyl_memory_client.storage.db_size_bytes`). During a write burst the raw
  file under-reports (committed pages still in `memory.db-wal`), so the displayed
  size/percentage disagreed with what the gate actually checks. `status` now uses
  the same `db_size_bytes()` measure.
- **`migrate` SQLite connections always close (#19, B001).** `verify_new_entries`
  (and the sibling `db_baseline`) closed the connection only on the success path;
  a `sqlite3.Error` mid-query leaked the open connection. Both now wrap
  connect+query in `try/finally`.
- **`migrate._tree_size` tolerates un-statable entries (#19, B005).** A
  permission-denied file or broken symlink under the scanned tree raised `OSError`
  and aborted the whole size estimate. Each entry is now guarded independently and
  the offending one is skipped.
- **`setup` config backups are timestamped (#19, B005).** Hermes/Claude/Codex
  config backups used a fixed `.bak` suffix, so a second `sibyl init` overwrote
  the first run's backup. Backups now carry a UTC timestamp
  (`<file>.<YYYYMMDDThhmmssZ>.bak`) via a shared `_timestamped_backup` helper, so
  every run keeps its own copy. (The timestamp is defined before use — the
  external report's own patch referenced an undefined variable and raised
  NameError; that bug is not reproduced.)
- **`setup` MCP smoke-test no longer cross-class dispatches (#19, B005).**
  `CodexWirer.verify_mcp_starts` called `ClaudeCodeWirer.verify_mcp_starts(self)`.
  Extracted a standalone `_verify_mcp_starts(binary)` module helper used by both
  wirers.
- **`status` store discovery survives a restricted profiles dir (#15 hygiene).**
  `_discover_stores` iterated the Hermes `profiles/` directory without guarding
  `iterdir()`, which raises `PermissionError` on a restricted directory. The sweep
  now skips gracefully instead of crashing `sibyl status`.

## [0.3.17] - 2026-06-25

Pre-launch security audit hardening.

### Fixed
- `read_credentials()` and the `tier_cache.json` parse no longer crash on a
  corrupt/non-JSON file (graceful "Not activated" instead of a traceback).
- Atomic credentials write uses a unique temp name + `fsync` + `os.replace`
  (no fixed-`.tmp` concurrent-writer race).
- `status` and `migrate` validate that a `--db` target is a real SQLite file
  instead of silently treating garbage as an empty database.
- `devices revoke` guards malformed/hostile server data and rejects negative
  indexes. `health` expands `~` and wraps provider errors. Version compare
  normalized (1.2 == 1.2.0, rc tags). `init` persists an allowlisted
  credentials dict. `setup` installs the plugin only after the overwrite
  confirm, surfaces pip output on failure, and respects PEP-668. `migrate`
  backup is fsync'd and re-verified on disk before any debloat.

## [0.3.16] - 2026-06-19

### Fixed

- **Bearer token printed to the terminal (red-team F3, 2026-06-17).** `sibyl upgrade`
  and `sibyl dashboard` printed the full session bearer inside the opened URL to
  stdout (terminal scrollback / tmux / CI logs / screen-shares), violating this
  package's own "session_token is never printed in full" rule. They now print only
  the bare base URL; the token still rides the opened browser URL (moving that
  handoff to a one-time, server-issued exchange code is a tracked server-side
  follow-up).

## [0.3.15] - 2026-06-16

### Added

- **Read-only `sibyl memory list | search | recall`** (beta reports,
  2026-06). Inspect what is actually stored without writing through an
  agent: `list [category]`, full-text `search <query>` across all tiers, and
  `recall <category> <name>`. Opens the resolved DB read-only via the SDK (never
  writes) and respects `--db`, so you can inspect any split-brain store that
  `sibyl status` surfaces. (big-patch PKG-4) Regression test:
  `tests/test_memory_cmd_2026_06_16.py` (6 cases).

## [0.3.14] — 2026-06-11

### Added

- **`sibyl status` now lists every memory store on the machine + warns on
  split-brain divergence** (beta report, 2026-06-11). A new read-only
  `_discover_stores()` enumerates the SDK/CLI/MCP default
  (`~/.sibyl-memory/memory.db`), the Hermes adapter
  (`$HERMES_HOME/sibyl/memory.db`), per-profile DBs
  (`$HERMES_HOME/sibyl/profiles/<p>/memory.db`), and any `SIBYL_MEMORY_DB`
  override, listing each with path + size. When more than one store holds data,
  a warning explains that memory is not shared across entry points and how to
  point them at one path. Discovery creates and moves nothing — path
  unification is a separate, migration-gated change. Regression test:
  `tests/test_status_stores_2026_06_11.py` (4 cases). (big-patch PKG-2/PKG-3)

## [0.3.13] — 2026-06-11

### Fixed

- **Corrupt `~/.claude/settings.json` was silently replaced with a sibyl-only
  file (data loss).** `_write_settings_with_sibyl` swallowed JSON parse errors
  (`except Exception: cfg = {}`) and the subsequent atomic replace destroyed the
  user's other `mcpServers`, `permissions`, `hooks`, and `env`. Setup now fails
  fast with a clear error (file untouched, backup path reported) on invalid JSON
  or a non-object top level; an empty file is still treated as legitimately
  empty. The same fail-fast applies to a valid-but-non-mapping Hermes YAML
  config. `current_state()` now surfaces `settings_parse_error` instead of
  swallowing it. (bugflow)
- **`sibyl status` crashed with a TypeError whenever a tier cache existed.**
  `cmd_status` sliced `checked_at` with `[:19]`, but `_capcheck` writes
  `checked_at` as epoch seconds (float), so every `sibyl status` run with a
  populated `~/.sibyl-memory/tier_cache.json` raised
  `TypeError: 'float' object is not subscriptable`. The value is now rendered
  via `time.strftime` when numeric and passed through (truncated to 19 chars)
  when it is already an ISO string; missing or null renders as `?`. Validated
  against float, ISO-string, missing, null, and zero inputs. (bugflow)

## [0.3.12] — 2026-06-05

### Fixed

- **`sibyl migrate --force` (onboarding dead-end).** When a detected harness
  already had a non-sibyl memory provider, the wirer refused with
  "Use --force to overwrite." but `run_guided_setup` called `wire()` with no
  `force`, and `sibyl migrate` had no `--force` flag to pass, leaving the user
  with no way forward. Added `--force` to `sibyl migrate`, threaded through
  `cmd_migrate` → `run_guided_setup(force=)` → `wire(force=force)`, so migration
  can overwrite an existing provider when the user explicitly opts in.
  Regression coverage: `tests/test_migrate_force_2026_06_05.py`. (bugflow)

## [0.3.11] — 2026-06-01

### Fixed

- **`sibyl init` left a pre-existing `~/.sibyl-memory` world-readable.**
  `mkdir(mode=0o700)` is a no-op when the directory already exists, so a dir
  created earlier at 0755 kept loose permissions on the credentials directory.
  `os.chmod(~/.sibyl-memory, 0o700)` is now applied explicitly after mkdir.
  (security; beta report dor_alpha)
- **Claude Code MCP registration could report false success.**
  `claude mcp add --scope user` returning exit 0 did not guarantee the server
  showed up in `claude mcp list`. The wirer now verifies via `claude mcp get`
  after adding; if the server is absent it returns an error with concrete
  remediation instead of a false "wired". (beta report)

## [0.3.10] — 2026-06-01

### Fixed

- **`sibyl setup hermes` raised a TypeError on every first-run wiring.**
  `HermesWirer._install_plugin()` called `install()` with only `hermes_home`
  (as a `str`), but `sibyl_memory_hermes.install_plugin.install()` requires
  `(hermes_home: Path, force: bool, dry_run: bool)` with no defaults, so it
  raised `TypeError` before the plugin could install. Now calls
  `install(hermes_home=Path(self.hermes_home), force=False, dry_run=False)`.

## [0.3.9] — 2026-05-31

Guided migration plus first-class Codex support and the real fix for Claude
Code MCP discovery.

### Added

- **`sibyl migrate` — guided onboarding.** One command takes a user from
  "memory scattered across CLAUDE.md / AGENTS.md / config files" to "memory in
  Sibyl." It (1) backs up every existing memory/agent file FIRST to a
  timestamped, byte-verified folder with a collision-free layout (a home file
  and a same-named project file never clobber each other), (2) auto-wires Sibyl
  into every detected harness, (3) hands the semantic extraction to the user's
  OWN agent — the agent reads only from the backup and writes via the
  `sibyl-memory` MCP tool, so Sibyl Labs never sees the user's files or memory
  (local/private by construction), (4) verifies what actually landed in the DB,
  and (5) optionally trims the originals — only on an explicit confirm and only
  because a verified backup exists. New `migrate.py` module + orchestrator.
- **Codex is now a first-class wiring target.** New `CodexWirer` edits
  `~/.codex/config.toml` (`[mcp_servers.sibyl_memory]`) atomically with a `.bak`
  backup and an idempotent guard, writing the RESOLVED absolute binary path
  (matching codex's own `codex mcp add` behavior). `sibyl setup codex` now
  works — previously the parser offered `codex` but `ALL_WIRERS` lacked it, so
  it errored.

### Fixed

- **Claude Code MCP registration.** Wiring now goes through
  `claude mcp add --scope user` — where Claude Code actually discovers MCP
  servers — instead of writing `~/.claude/settings.json`, which Claude Code does
  NOT read for MCP discovery. This was the root cause of "configured but never
  connects." Detection now uses `claude mcp get`; the settings.json path remains
  a fallback only for environments without the `claude` CLI.
- **Absolute-path registration (both Claude and Codex).** User-scope / config
  servers are launched from the harness's own environment, not the interactive
  shell, so a bare `sibyl-memory-mcp` could fail to resolve (Claude showed
  "✗ Failed to connect"; Codex would not spawn). Both wirers now register the
  resolved absolute path.

### Tested

- 82 tests including adversarial inputs and a 60-iteration fuzz of the migration
  flow. Live MCP connection verified end-to-end against Claude Code
  (`--scope user` → ✓ Connected) and Codex (initialize handshake + ListTools),
  and a real headless agent extraction wrote structured entities into an
  isolated DB.

## [0.3.8] — 2026-05-24

Fix: `sibyl setup claude-code` wired the MCP config but never ensured the
`sibyl-memory-mcp` binary existed, causing ENOENT on every Claude Code
reconnect. Same defect in the Codex wirer path.

### Fixed

- `ClaudeCodeWirer.wire()` now calls `shutil.which("sibyl-memory-mcp")`
  before writing config. If the binary is absent, it auto-installs
  `sibyl-memory-mcp` via `pip install` (mirroring `HermesWirer._install_plugin`).
  If auto-install fails, the outcome downgrades to `error` with the exact
  `pip install sibyl-memory-mcp` command instead of false success.
- `current_state()` now includes `mcp_binary_found`. The `wired_with_sibyl`
  flag is only True when both the config block matches AND the binary is on
  PATH. Previously, re-running `sibyl setup` reported "already wired" even
  when the binary was missing, hiding the problem on every retry.
- Config-present-but-binary-missing is now a distinct path in `wire()`:
  it installs the binary without re-writing the config block, then reports
  `wired` (not `already`).

### Added

- **Post-wire MCP verification.** After wiring (or confirming "already"),
  `cmd_setup` spawns `sibyl-memory-mcp` briefly and confirms it doesn't
  crash on startup (catches ImportError, missing deps, bad credentials).
  Reports `✓ MCP server verified` on success or `✗ Server crashed on
  startup (exit N): <stderr>` on failure, with a non-zero exit code so
  CI and scripts can detect the problem.
- Claude Code reconnect instructions print after verification: tells
  the user to type `/mcp` and reconnect `sibyl-memory`, or restart.
- `[mcp]` optional extra in pyproject.toml: `pip install "sibyl-memory-cli[mcp]"`
  now pulls in `sibyl-memory-mcp>=0.1.2` transitively.

### Root cause

`sibyl-memory-mcp` ships in a separate opt-in PyPI package. `sibyl setup`
wrote a config entry pointing at the binary without checking it existed.
The Hermes wirer had self-heal (`_install_plugin()`); the Claude Code and
Codex wirers did not.

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
  helpers stay in `cli.py` (the legacy `print_status` path they served
  was removed later, in 0.5.0). New `_aesthetic.a.*`
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

External-tester remediation release. Family-wide alignment with the
v0.4.0 client + v0.3.2 hermes (external-test fixes: exception export
path, db file perms, identifier validation, FTS5 error surfacing). No CLI
code changes in this release.

### Changed

- `sibyl-memory-client` pin: `>=0.3.3` → `>=0.4.0`.
- `sibyl-memory-hermes` pin: `>=0.3.1` → `>=0.3.2`.

### Notes

- `sibyl init / upgrade / status / health` surface is unchanged from
  v0.1.2. The external tester's fixes flow through transparently via the dependency
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
