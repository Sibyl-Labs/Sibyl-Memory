# Sibyl Memory for Hermes

Local-first, SQLite-backed structured memory for Hermes: warm entities, hot state, cold journal
and reference docs, searchable together. Memory content lives on your machine, in
`$HERMES_HOME/sibyl/memory.db` (one database per Hermes profile).

This folder is the Hermes plugin-catalog build of the adapter that ships inside
[`sibyl-memory-hermes`](../README.md). `__init__.py` is byte-identical to
`src/sibyl_memory_hermes/_hermes_plugin/adapter.py`; a test keeps them in step.

## Install

```bash
hermes plugins install sibyl
pip install sibyl-memory-cli     # provides the `sibyl` command used below
sibyl init                       # sign in; the memory tools need an account
hermes memory setup              # pick "sibyl" (or: sibyl setup hermes)
```

Restart Hermes. The agent gets four tools:

- `sibyl_remember(category, name, body)`: store a structured fact
- `sibyl_recall(category, name)`: look up a known fact
- `sibyl_search(query)`: full-text search across all four tiers
- `sibyl_list(category?, status?)`: browse what is stored

Signed out, the plugin still loads and each tool answers with the next step (`sibyl init`).

## What this plugin does beyond your Hermes data

**Network.** All calls go to `https://api.sibyllabs.org`. No shell commands, no subprocesses.

| Call | When | Sends |
| --- | --- | --- |
| `POST /api/plugin/entitlement` | When your plan token is due for refresh (about daily) | Account id, client version, OS family; your session token as the bearer |
| `POST /api/plugin/check-write` | Before a write when the cached plan check has lapsed, to apply your plan's storage cap | Account id, session token, current and proposed database size in bytes, and your signed plan token |
| `POST /api/plugin/heartbeat` | Only if you opt in with `SIBYL_MEMORY_TELEMETRY=1`: every 15 operations or 10 minutes of activity, and at exit | Account id and a count of memory operations, no content. **Off by default in Hermes.** |

The check-write call (and the heartbeat, when on) also carry your session token as the bearer and your plan
token in an `X-Sibyl-Entitlement` header. No memory content, query text or file content is sent.
If the service is unreachable, memory keeps working on the last valid plan token until it expires.

**Files outside `$HERMES_HOME`.** Under `~/.sibyl-memory/`:

- reads `credentials.json`, written by `sibyl init` (mode 0600; account id and a session token)
- reads and writes `entitlement.json`, `lifecycle.json`, `lifecycle.log`, `lifecycle.lock`,
  `skills/` and the `debloat` files
- on Pro, writes `signals.json` (mode 0600): tool names and hashed argument-key shapes for skill
  proposals, never a value; `sibyl skills signals off` stops it, `clear` deletes it
- reads Hermes' `active_profile` from `$HERMES_HOME`, falling back to `~/.hermes/active_profile`

**Paid plans: context trimming at start.** On a Pro or Elite plan, each Hermes start runs a
background "debloat" pass over the agent instruction files in your working directory and home
(`CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, `.cursorrules`, `.cursor/rules`, and the `~/.claude/` and
`~/.codex/` copies). It measures and plans a trim, writing the plan (headings and token counts, no
file bodies) under `~/.sibyl-memory/debloat/plans/`. It edits a file only if you named that file
with `sibyl debloat auto <file>`, and never one modified in the last 10 minutes. The moved section
goes into your local memory database, a full backup goes to `~/.sibyl-memory/debloat/backups/`,
and `sibyl debloat undo` reverts the last edit. Free plans only measure.

**Background threads.** Per-turn journal writes, a session summary at shutdown, a learning pass,
a write flush, and the heartbeat above when you opted in. Each is a daemon thread with a bounded wait; none blocks
Hermes from exiting.

## Configuration

- `SIBYL_TENANT_ID` (not a secret): selects the tenant, overriding the one in `credentials.json`.
- `SIBYL_MEMORY_TELEMETRY=1`: turns on the heartbeat (off by default in Hermes).

License: MIT. Maintained by Sibyl Labs, https://sibyllabs.org/memory
