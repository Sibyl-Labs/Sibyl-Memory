# sibyl-memory-cli

Command-line interface for the **Sibyl Memory Plugin**.

```bash
pip install sibyl-memory-cli
```

Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is not supported.

This pulls in `sibyl-memory-client` (the local SDK) and `sibyl-memory-hermes` (the Hermes provider) automatically.

## Commands

```
sibyl --version               Print the installed versions: cli, client, mcp, hermes, langgraph.
sibyl init                    Open the browser activation page. Writes ~/.sibyl-memory/credentials.json.
                              Afterwards it offers to add or confirm your email (terminal only).
sibyl init --no-email         Activate without the email prompt.
sibyl init --replace          Re-activate this computer in place of its earlier sign-in (re-activation
                              never uses a second computer slot). At the account's computer limit
                              (Free: one computer) init lists the computers signed in and offers to
                              sign one out on the account page.
sibyl refresh                 Check your account now (an upgrade, a staking unlock or a plan change
                              lands at once), refresh the procedures, shims and hooks, and say what
                              changed. Exits 0 with no account (it says what to do), 2 when offline.
sibyl autosave [on|off|every N]
                              Pro automatic save: every N minutes of active work (30 to 240, default
                              60). On the free plan the setting is kept for the day you upgrade.
sibyl email                   Show your account email: confirmed, not confirmed, or none.
sibyl email add <address>     Send a code to a new address, then confirm it.
sibyl email verify            Send a code to the address you activated with, then confirm it.
sibyl email confirm <code>    Confirm a code from the email (for scripts and non-interactive shells).
sibyl claim <cs_...>          Link a card purchase from sibyllabs.org/pro to this account.
sibyl migrate                 Guided onboarding: back up your existing memory/agent files, wire Sibyl
                              into every detected harness, populate Sibyl Memory from the backup, and
                              optionally slim the instruction files it found (CLAUDE.md, AGENTS.md,
                              .cursorrules, ...). Backup-first, never destructive.
sibyl setup [target]          Wire Sibyl as the memory provider for Hermes, Claude Code, and/or Codex,
                              and install the save and debloat procedures for each.
                              target is one of: hermes | claude-code | codex (default: detect all).
sibyl save                    Save: --fact "<text>" writes one fact; --status shows the last save; bare
                              prints how to run the save procedure in your agent. --capsule on Pro.
sibyl debloat                 Measure the instruction files your agent loads every start (--fyi, --json);
                              plan | apply [<file>] | auto <file> | manual <file> | undo [<file>].
sibyl skills                  install | status | reset <SAVE.md|DEBLOAT.md> | uninstall (run it before pip uninstall).
sibyl reminders               on | off | quiet (save and debloat reminders; quiet silences Pro reports).
sibyl status                  Show local credentials, DB size, this computer's access, and the server's
                              view of your tier.
sibyl whoami                  One-line account summary (masked by default).
sibyl devices                 List the devices (active tokens) bound to your account, with the
                              account's computer limit when the api sends it (Free 1, Pro up to 10).
sibyl devices revoke N        Revoke a device by index (run `sibyl devices` to see the indexes).
sibyl dashboard               Open the account page at account.sibyllabs.org (no token in the URL; you
                              sign in there). Shows `sibyl status` while the page is not open yet.
sibyl upgrade                 Open the tier/billing flow: the account billing page when the server offers
                              it, otherwise the stake / USDC page through a one-time link.
sibyl update                  Check PyPI for newer releases of the installed Sibyl packages: cli, client,
                              hermes, the MCP server, and langgraph when installed.
sibyl update --apply          Update the installed Sibyl packages in place (pip install -U), the MCP server
                              included; never installs one you do not have. This is the
                              canonical way to update the plugin, distinct from `sibyl upgrade` (which is
                              the tier/billing flow, not a package update).
sibyl health                  Run the SibylMemoryProvider self-check (schema version, DB path, tenant,
                              entitlement state, pinned key ids).
sibyl logout                  Sign this computer out: revoke it on the server, remove credentials.json and
                              entitlement.json (your memory.db is left untouched).
sibyl memory list [category]  List stored entities, optionally filtered by category (--limit N).
                              `sibyl memory` never needs an account: your local data is always yours to read.
sibyl memory search <query>   Full-text search across entities, state, reference, and journal (--limit N).
sibyl memory recall <cat> <name>
                              Recall a single entity by category + name.
sibyl memory adopt [--yes]    Move memory written on this computer before sign-in into your account
                              (local, one transaction, nothing deleted; asks first if the store has
                              held another account). `sibyl init` does this on the first sign-in.
```

## Migrate (guided onboarding)

```bash
$ sibyl migrate
```

`sibyl migrate` moves your accumulated agent memory into Sibyl without risking
your existing files:

1. **Back up first.** Every memory/agent file it finds (`CLAUDE.md`,
   `AGENTS.md`, `.codex/config.toml`, `.hermes/*`, and similar) is copied to a
   timestamped backup folder and byte-verified before anything else happens.
2. **Wire Sibyl** into every detected harness — Claude Code (via
   `claude mcp add --scope user`), Codex (via `~/.codex/config.toml`), Hermes.
3. **Extract** — it prints a prompt you run in your own agent. The agent reads
   only from the backup and writes structured memory through the `sibyl-memory`
   tool. The extraction runs locally on your machine; Sibyl Labs never sees your
   files or memory.
4. **Verify** the new entries that landed in your local DB.
5. **Optionally trim** the instruction files it found (`CLAUDE.md`, `AGENTS.md`,
   `.cursorrules`, ...), one question per file, only if you confirm and only because
   a verified backup of that file exists. Configuration files are never trimmed. Your
   full pre-migration files are always preserved.

Flags: `--backup-dir PATH` (default: home), `--no-debloat` (skip the trim
step), `--yes` (skip the initial confirm; the trim step still asks separately).

> No warranty. Keep your backup until you've confirmed everything migrated.
> Sibyl Labs is not responsible for data loss.

## Activation

```bash
$ sibyl init

─ activation ───────────────────────────────────────────────
two paths · pick whichever fits your device

  Session          a1b2c3d4…e5f6
  Code             437 480  (use this in the email panel)
  Opening          https://auth.sibyllabs.org/a1b2c3d4-…

  desktop wallet · or email + code
  this terminal will pick up automatically when you bind.

  ⠙ watching the network for your bind … 9:59 left
```

The browser opens the sign-in page. Sign in with your email and the code shown in the terminal, or with a desktop wallet (when the server offers it, a third path sends a small USDC amount from a mobile wallet). If you already have an account, `sibyl init` also prints an approve link on `account.sibyllabs.org`: open it on a computer that is already signed in and type the same code. The terminal picks up the moment the binding lands. Credentials are written to `~/.sibyl-memory/credentials.json` at mode 0600, and this computer's signed access file to `~/.sibyl-memory/entitlement.json` (0600).

An account is required for agents to use memory (MCP, Hermes, LangGraph, the SDK). `sibyl init` is the sign-in; there is no separate login command. The access pass is re-checked about once a day (and at the end of a paid plan) and keeps working offline for up to 14 days. Memory written on this computer before you signed in moves into your account at that first sign-in (on this computer only); if the store has held another account, `sibyl init` leaves it and `sibyl memory adopt` asks first.

## Email

Every account needs a confirmed email for receipts, security notices, purchases and recovery.
Your local memory works either way. Wallet activation stays available.

```bash
$ sibyl email add you@example.com
  code sent to y***@e***.com. it expires in 10 minutes.
  code from the email: 123456
  ✓ Email confirmed. Your account is complete.
```

`sibyl email verify` confirms the address you activated with. Confirming from the terminal keeps
this computer signed in and signs out any other computer that was set up with the address before
you confirmed it.

## Upgrade

Free is 5 MB of local memory. Pro removes the cap: $12/month or $108/year (plus applicable tax by card), bought at
`sibyllabs.org/checkout` or through `sibyl upgrade`. Holding 100,000 $SIBYL staked also gives Pro.

```bash
$ sibyl upgrade
```

`sibyl upgrade` shows your account and current tier, then opens the purchase page in your
browser: the account billing page (`https://account.sibyllabs.org/billing`; sign in there and buy
Pro) when the server offers it, otherwise a one-time exchange link. No token ever rides the URL.
It then waits for the plan change. A card purchase made elsewhere can be linked with
`sibyl claim <cs_...>`.

When the tier changes, the CLI rewrites `credentials.json` and clears `tier_cache.json` so your next write picks up the new entitlement without delay.

## Status

```bash
$ sibyl status

P L U G I N
  Version          cli 0.5.0 · client 0.9.0
L O C A L
  Credentials      ~/.sibyl-memory/credentials.json
  Account          a1b2c3d4-e5f6-4789-8abc-def012345678
  Tier             Free
  Wallet           0xa1b2c3d4e5f60718293a4b5c6d7e8f9012345678
  Email            you@example.com
  Issued           2026-05-16 18:12 UTC
  Platform         Linux (x86_64)
  DB path          ~/.sibyl-memory/memory.db
  DB size          1,247,300 bytes (1.19 MB · 23.8% of free cap)

A C C E S S
  Access           active · re-checks in 24h
  Tier cache       free (checked 2026-05-16 18:12 UTC)

S E R V E R
  Tier             Free
  Source           free
  Cap bytes        5,242,880
  Email            not confirmed · run sibyl email verify
  Dashboard        https://account.sibyllabs.org

L I F E C Y C L E
  save             last never · 0 writes since · manual (automatic on Pro)
  context          no always-load files found here
  skills           not installed · sibyl skills install
  reminders        on
```

`$SIBYL held`, `Threshold` and `Qualified` rows appear under `SERVER` when the server reports staking details.

`Email`, `Account` and `Dashboard` rows appear when the server reports them. On a paid plan a `Plan` row under `Access` shows the billing date when the server sends it: `Pro · renews 8 Oct 2027` for a renewing card plan, `Pro · ends 8 Oct 2027` when the plan is set to end (a cancellation, or a fixed term). The `Tier` rows always show the tier of this computer's current access pass.

`sibyl status` runs one access check-in when online (the check-in is the status). The `Access` row reads one of:

| Row | Meaning | What to do |
|---|---|---|
| `active · re-checks in <h>h` | this computer is signed in; the access pass is checked again in about <h> hours (not a billing date) | nothing |
| `offline · <n> days of grace left · connect once to renew` | not reached the server lately | connect to the internet once |
| `paused · offline too long · connect to the internet, or run sibyl init` | offline more than 14 days | connect; if it stays paused, `sibyl init` |
| `setup not finished · connect to the internet once to finish setup` | credentials from before 0.9.0 and no check-in yet | connect once |
| `signed out · run sibyl init` | signed out, revoked, or the sign-in expired | `sibyl init` |
| `under review · email sibyl@sibyllabs.org` | the account is under review | email us |

When the server cannot be reached, the account rows come from the last check-in, labeled `as of <date>`. With no credentials at all, `sibyl status` says so and that agents cannot use memory until you run `sibyl init`; your local data is kept.

If `LOCAL` and `SERVER` tiers diverge, run `sibyl refresh` to pick up the server plan (or `sibyl init --force` to re-activate).

## Environment overrides

For internal testing only:

```bash
SIBYL_API_BASE=https://staging.example.internal sibyl init
SIBYL_ACTIVATE_BASE=https://staging.example.internal/plugin/activate sibyl init
SIBYL_UPGRADE_BASE=https://staging.example.internal/plugin/upgrade sibyl upgrade
SIBYL_ACCOUNT_BASE=https://account.staging.example.internal sibyl dashboard
SIBYL_NONINTERACTIVE=1 sibyl init     # never prompt (same as a non-TTY shell)
```

## Security

- `credentials.json` and `entitlement.json` are written atomically at mode 0600.
- The access file is verified offline against a public key pinned in `sibyl-memory-client`; it is never printed.
- `session_token` is never printed in full: only a short slice.
- No token rides a browser URL: `sibyl dashboard` opens the account page bare, and `sibyl upgrade`
  uses a one-time exchange link.
- No memory content ever transits these endpoints. `sibyl status` reads only the size of `memory.db`; `sibyl memory list|search|recall|adopt` read your memory locally and send none of it anywhere.
- Wallet operations happen in the browser. The CLI sees only the resulting tier change.

## Save and debloat

**Save.** At the end of a session, or after a big block of work, your agent writes what happened
into Sibyl Memory through the plugin's own tools: the decisions made and why (journal), the
people, projects and products touched (entities), what to pick up next time and the open
warnings (session state), the ranked priorities, and it closes what died (archive). A delta
marker means a second save in the same day captures only what changed. It may offer to turn a
repeated pattern into a reusable skill; that is offered, never forced. Nothing in a save leaves
your machine.

**Debloat.** Sibyl measures the files your agent loads on every session start (`CLAUDE.md`,
`AGENTS.md`, `.cursorrules` and the rest of the scan set), deterministically (`chars/4`,
labeled `MEASURED~`), and when they are heavy, trims the accreted history out of them without
losing a byte: the history moves out and a one-line pointer stays. On the free plan the history
moves to a sibling `*-history` file. On Pro it is stored in Sibyl Memory as a reference document,
so it stays searchable, and the measurement and trim run by themselves under a policy you set
once.

| | Free (activated account) | Pro |
|---|---|---|
| The procedures in `~/.sibyl-memory/skills/` (`SAVE.md`, `DEBLOAT.md`), `/sibyl-save` and `/sibyl-debloat` in Claude Code, skills in Hermes and Codex, a block in Codex's `AGENTS.md`, the `save` and `debloat` MCP prompts | yes | yes |
| `sibyl save`, `sibyl debloat` (measure, plan, apply, undo) | yes | yes |
| Reminders (one quiet line; `sibyl reminders off`) | yes | replaced by a one-line report of what ran |
| Where trimmed history goes | a sibling `*-history` file | Sibyl Memory, plus a byte-verified backup |
| Automatic debloat | no | yes: `propose` by default; a file is trimmed by itself only after you run `sibyl debloat auto <file>` (that file at its path, never the same name elsewhere) |
| Automatic save by active time: once early (after 5 memory writes or a compaction), then about every 60 minutes of active work, only when there is something to save, at most 8 a session and 12 a day (`sibyl autosave every <30-240>` or `sibyl autosave off`). Claude Code: the Stop hook asks your agent to run the save procedure headless (a scripted `claude -p` run is never asked); Cursor, Codex and other MCP hosts: a `notice` on the next tool result; Hermes: a note in the next prefetch. A session capsule (counts only) at compaction and session end. Each automatic save is one extra model turn that reads and writes memory | no | yes |
| Skill proposals: Sibyl finds repeated work in your journal (locally, no network), names up to 3 at a session start at most once a day, and creates nothing without a yes (`/sibyl-save curate`, or `sibyl skills proposals accept <id>`, which writes the skill for Claude Code, Codex and Hermes) | no (`/sibyl-save` still offers skills when you run it) | yes |

The procedures are vendored, with attribution, from the public MIT repos
[sibyl-save](https://github.com/Sibyl-Labs/sibyl-save) and
[debloat](https://github.com/Sibyl-Labs/debloat); both stay standalone and account-free.

Uninstalling: run `sibyl skills uninstall` first. It removes the Claude Code hooks in
`~/.claude/settings.json`, the skill shims and the Codex block (files you edited are kept); then
`pip uninstall` the packages. The hooks call this package's `sibyl` command: if the package goes
first they exit quietly, but they stay in your settings until you reinstall and run
`sibyl skills uninstall`, or delete the five `sibyl hook` entries by hand.

Privacy: the lifecycle reads your instruction files to measure and trim them locally; no file
content and no conversation content ever leaves your machine.

Forks: a gate-stripped fork can run the lifecycle code locally (MIT allows it). It gains nothing
hosted, and its trims land in a store the official path cannot recognize as Pro. No file, flag
or environment variable turns the automation on for a free account.

Commands: `sibyl save` (`--fact`, `--status`, `--capsule` on Pro), `sibyl debloat` (`--fyi`,
`--json`, `plan`, `apply [<file>]`, `auto <file>`, `manual <file>`, `undo [<file>]`),
`sibyl skills install|status|reset|uninstall`, `sibyl skills proposals [show|accept|reject|mute|unmute]`
(Pro), `sibyl autosave [on|off|every <minutes>]`, `sibyl reminders on|off|quiet`. `sibyl setup`
installs the procedures and wires them: Claude Code gets the two skills plus five hooks in
`~/.claude/settings.json` (`sibyl hook <event>`, installed by setup, not for manual use; any
other hook you have is left alone; each hook exits 0 quietly if the `sibyl` it names is gone),
Codex gets a marked block in `~/.codex/AGENTS.md` and two skills in `~/.codex/skills/`, Hermes
gets two skills in `$HERMES_HOME/skills/`. `sibyl debloat auto <file>` authorizes that one file
at its path. On Pro the Stop hook asks the agent to save once early (after 5 memory writes or a
compaction), then about every hour of active work (`sibyl autosave`). `sibyl migrate` now slims the instruction files it
found (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, ...), one question per file.

## License

MIT.
