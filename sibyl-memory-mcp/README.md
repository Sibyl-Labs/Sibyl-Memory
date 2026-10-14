# sibyl-memory-mcp

MCP server for [Sibyl Memory Plugin](https://sibyllabs.org/memory). Exposes the local SQLite + FTS5 memory engine to any MCP-compatible agent: **Claude Code, Codex CLI, Cursor, Continue**, anything that speaks Model Context Protocol.

## Install

```bash
pip install sibyl-memory-mcp
```

Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is not supported.

You also need an activated Sibyl Memory account (required since 0.3.0). If you haven't already:

```bash
sibyl init
```

This creates `~/.sibyl-memory/credentials.json`, this computer's signed access file `~/.sibyl-memory/entitlement.json`, and a local SQLite database at `~/.sibyl-memory/memory.db`. The MCP server reads them automatically, and picks up a `sibyl init` run while it is already running on the next call.

### Without an account

The server still starts and lists its 8 tools, so your agent can see them and learn why they refuse. Every tool call answers an error result (`isError: true`) whose text is JSON:

```json
{"ok": false, "error": "EntitlementError", "code": "ACCOUNT_REQUIRED", "reason": "no_account",
 "message": "Sibyl Memory needs an account before it can store or recall anything. Open a terminal and run: sibyl init ...",
 "recovery": "Run sibyl init in a terminal, then retry.", "account_url": "https://account.sibyllabs.org"}
```

`reason` says why: `no_account`, `signed_out` (this computer was signed out or the sign-in expired), `expired_offline` (offline more than 14 days), `setup_incomplete` (credentials from before 0.9.0 and no check-in yet: connect to the internet once), `under_review`, `account_deleted`, `invalid_entitlement` / `wrong_device` (the access file is damaged or from another computer), `policy_read_only` / `policy_none` (email policy), `client_too_old`. The `message` is written for a person and can be relayed as is. Your local memory is kept in every case, and `sibyl memory list|search|recall` reads it without an account.

Successful results can carry a one-line `notice` (for example "Sibyl Memory has not reached the server for 8 days ...", or a reminder to confirm your email), at most once a day per message.

> **Privacy disclosure.** Your memory content never leaves the machine. The plugin talks to `api.sibyllabs.org` for these things only: activation (`sibyl init`); a daily account check-in that renews this computer's access (account id, this computer's sign-in token, plugin version, OS family; no memory content); a size check when a free account nears its cap (account id, sign-in token, the database's byte size and the proposed delta); and, unless you opt out with `SIBYL_MEMORY_TELEMETRY=0`, an aggregate count of memory operations. The size check and the count also carry this computer's signed access token (account id, tier, cap, device hash, account flags; no personal data). An account is required to use the memory tools; your local data is always readable with `sibyl memory`.

## Add to Claude Code

The one command (from `sibyl-memory-cli`):

```bash
sibyl setup claude-code
```

It registers the server with `claude mcp add --scope user` and checks that Claude Code lists it. To do the same by hand:

```bash
claude mcp add --scope user sibyl-memory -- sibyl-memory-mcp
```

Use the absolute path to `sibyl-memory-mcp` (`which sibyl-memory-mcp`) if it lives in a virtualenv. For a single project, a `.mcp.json` with `{"mcpServers": {"sibyl-memory": {"command": "sibyl-memory-mcp"}}}` also works. Editing `~/.claude/settings.json` does not register an MCP server.

Restart Claude Code. The 8 memory tools (prefixed `memory_*`) become available immediately.

## Add to Codex CLI

The one command:

```bash
sibyl setup codex
```

It adds this table to `~/.codex/config.toml`. To do the same by hand:

```toml
[mcp_servers.sibyl_memory]
command = "/absolute/path/to/sibyl-memory-mcp"
```

Use the absolute path (`which sibyl-memory-mcp`): Codex starts the server from its own environment, so a bare command name may not resolve. Restart Codex.

## Run with Docker

A first-party image is provided at the repo root (`Dockerfile`,
`docker-compose.yml`, `.dockerignore`). The image is non-root, runs on a
pinned slim Python base, and bakes in no secrets. Memory lives on a mounted
volume so it survives container recreation.

The MCP server speaks stdio, not HTTP. It is not a daemon you leave running.
Run it attached to an MCP client's stdin, or via `docker compose run`.

Build:

```bash
docker build -t sibyl-memory-mcp:local .
```

First, activate on the host so the mounted volume carries your credentials:

```bash
sibyl init
```

Run attached, mounting your host `~/.sibyl-memory` for `memory.db`,
`credentials.json` and `entitlement.json` (the mount must be writable: the
access file renews itself in place):

```bash
docker run -i --rm \
  -v "$HOME/.sibyl-memory:/home/app/.sibyl-memory" \
  sibyl-memory-mcp:local
```

Note the `-i` (keep STDIN open) and the absence of `-t` (no TTY): the MCP
client drives stdin programmatically.

Wire it into Claude Code by pointing the command at the container (for
example in a project `.mcp.json`):

```json
{
  "mcpServers": {
    "sibyl-memory": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/absolute/path/to/your/.sibyl-memory:/home/app/.sibyl-memory",
        "sibyl-memory-mcp:local"
      ]
    }
  }
}
```

Using Compose (note `run`, not `up`, because it is a stdio server):

```bash
docker compose run --rm sibyl-memory-mcp
```

Environment overrides (`SIBYL_MEMORY_DB`, `SIBYL_CREDENTIALS`) are passed
through when set on the host. The server takes its tenant from
`credentials.json`. No secret is ever
written into the image or the compose file. Credentials arrive only through
the mounted volume.

## Tools exposed

| Tool | What it does |
|------|--------------|
| `memory_remember` | Store an entity by (category, name) |
| `memory_recall` | Read an entity by exact key |
| `memory_search` | FTS5 search across entities, state, reference docs and the journal |
| `memory_list` | List entities in a category |
| `memory_forget` | Archive an entity (recoverable) |
| `memory_set_state` | Write a HOT-tier state doc |
| `memory_get_state` | Read a HOT-tier state doc |
| `memory_record_event` | Append a COLD-tier journal event |

Full docs at [docs.sibyllabs.org/memory/integrations](https://docs.sibyllabs.org/memory/integrations).

## Environment overrides

| Var | Default | What it overrides |
|-----|---------|--------------------|
| `SIBYL_MEMORY_DB` | `~/.sibyl-memory/memory.db` | Local SQLite path |
| `SIBYL_CREDENTIALS` | `~/.sibyl-memory/credentials.json` | Credentials file path |

## Tier behavior

- **Free tier**: the 8 tools work. Hard-capped at 5 MB of local storage. Writes that would push past the cap return `CAP_EXCEEDED` with an `upgrade_url`.
- **Paid tiers** (`pro`, `elite`): the same 8 tools, cap removed.

The cap-gate runs against the **server-authoritative** tier: the local hint is the tier inside the server-signed entitlement (verified offline against a pinned public key), and the cap boundary is decided by the server. Editing a local file does not change either.

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

In this server: the prompts `save` (mode `run`, `preview`, `setup`, `headless`, `curate`; in
Claude Code `/mcp__sibyl-memory__save`) and `debloat` (mode `measure`, `trim`), and the resources
`sibyl://skills/SAVE.md`, `sibyl://skills/DEBLOAT.md`, `sibyl://skills/manifest`,
`sibyl://context/weight`. Prompts and resources work without an account; the memory tools they
call do not. A successful tool result may carry one quiet lifecycle `notice` (account notices
come first). On Pro, each server process runs the debloat plan once at start.

## License

MIT: same as the rest of the `sibyl-memory-*` family.
