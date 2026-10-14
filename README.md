<div align="center">

```
███████╗██╗██████╗ ██╗   ██╗██╗     
██╔════╝██║██╔══██╗╚██╗ ██╔╝██║     
███████╗██║██████╔╝ ╚████╔╝ ██║     
╚════██║██║██╔══██╗  ╚██╔╝  ██║     
███████║██║██████╔╝   ██║   ███████╗
╚══════╝╚═╝╚═════╝    ╚═╝   ╚══════╝

         M  E  M  O  R  Y
```

**agentic memory infrastructure · file-based · zero embeddings**

[![PyPI · client](https://img.shields.io/pypi/v/sibyl-memory-client?label=client&color=8a6a2a)](https://pypi.org/project/sibyl-memory-client/)
[![PyPI · cli](https://img.shields.io/pypi/v/sibyl-memory-cli?label=cli&color=8a6a2a)](https://pypi.org/project/sibyl-memory-cli/)
[![PyPI · hermes](https://img.shields.io/pypi/v/sibyl-memory-hermes?label=hermes&color=8a6a2a)](https://pypi.org/project/sibyl-memory-hermes/)
[![PyPI · mcp](https://img.shields.io/pypi/v/sibyl-memory-mcp?label=mcp&color=8a6a2a)](https://pypi.org/project/sibyl-memory-mcp/)
[![PyPI · langgraph](https://img.shields.io/pypi/v/sibyl-memory-langgraph?label=langgraph&color=8a6a2a)](https://pypi.org/project/sibyl-memory-langgraph/)
[![License: MIT](https://img.shields.io/badge/license-MIT-15110a.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-15110a.svg)](https://www.python.org/downloads/)
[![LongMemEval](https://img.shields.io/badge/LongMemEval-95.6%25%20%23%E2%80%832-2e6b3a.svg)](https://blog.sibylcap.com/longmemeval-v2)

<sub><i>built by an autonomous agent · sibyl labs llc</i></sub>

</div>

---

## What this is

Five PyPI packages, one schema family, one architecture.

**Runs on** Linux, macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is not supported.

`sibyl-memory-client` is a local-first agentic memory SDK. SQLite-backed, five-tier hierarchical schema, FTS5 search, multi-tenant by design. No vector database. No embedding model. No external retrieval service. The memory lives on the agent's machine; the substrate is a single file on disk.

> **Privacy disclosure.** Your memory content never leaves the machine. The plugin talks to `api.sibyllabs.org` for these things only: activation (`sibyl init`); a daily account check-in that renews this computer's access (account id, this computer's sign-in token, plugin version, OS family; no memory content); a size check when a free account nears its cap (account id, sign-in token, the database's byte size and the proposed delta); and, unless you opt out with `SIBYL_MEMORY_TELEMETRY=0`, an aggregate count of memory operations. The size check and the count also carry this computer's signed access token (account id, tier, cap, device hash, account flags; no personal data). An account is required to use the memory tools; your local data is always readable with `sibyl memory`. Verified against the source in `sibyl-memory-client/src/sibyl_memory_client/` (`_entitlement.py`, `_capcheck.py`, `_heartbeat.py`).

The other four packages ride on top: `sibyl-memory-cli` for activation and tier management, `sibyl-memory-hermes` for Hermes Agent integration, `sibyl-memory-mcp` for any MCP-compatible client (Claude Code, Codex, Cursor, Continue), and `sibyl-memory-langgraph` as a LangGraph `BaseStore` adapter.

The architecture was benchmarked publicly on [LongMemEval Oracle](https://blog.sibylcap.com/longmemeval-v2) (ICLR 2025, University of Michigan, 500 questions) and placed **#2 overall at 95.6%**, tied with Chronos (PwC), beating Mastra, MemMachine, Hindsight, Mem0, Supermemory, Zep, and the Oracle baseline. It is the only file-based system in the top tier: running on a single 4 vCPU / 16 GB box, no vector infrastructure, no embedding fees.

This is the entire stack as it ships to production agents today.

---

## Packages

| Package | PyPI | Description |
|---|---|---|
| [`sibyl-memory-client`](./sibyl-memory-client) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-client)](https://pypi.org/project/sibyl-memory-client/) | Local-first agentic memory SDK. SQLite-backed five-tier hierarchical schema, FTS5 search, multi-tenant, with self-learning skill detection and local memory linter. Foundation of the plugin family. |
| [`sibyl-memory-cli`](./sibyl-memory-cli) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-cli)](https://pypi.org/project/sibyl-memory-cli/) | Command-line interface. `sibyl init` activates, `sibyl upgrade` opens the Pro / Elite upgrade flow, `sibyl status` shows current tier and DB stats, `sibyl whoami`, `sibyl devices`. |
| [`sibyl-memory-hermes`](./sibyl-memory-hermes) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-hermes)](https://pypi.org/project/sibyl-memory-hermes/) | Bundled memory payload for Hermes Agent v0.13+ (and any other Python orchestration that wants direct SDK access). |
| [`sibyl-memory-mcp`](./sibyl-memory-mcp) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-mcp)](https://pypi.org/project/sibyl-memory-mcp/) | MCP server. Wraps the local SQLite + FTS5 memory engine and exposes it to MCP-compatible agents (Claude Code, Codex, Cursor, Continue, anything that speaks MCP). |
| [`sibyl-memory-langgraph`](./sibyl-memory-langgraph) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-langgraph)](https://pypi.org/project/sibyl-memory-langgraph/) | LangGraph `BaseStore` adapter. Long-term, cross-thread store backed by the same SQLite + FTS5 engine — durable agent memory with no vector database and no embeddings. |

---

## Install

```bash
pip install sibyl-memory-cli
sibyl init
```

`sibyl init` opens a browser to activate your account, binds your wallet or email, and writes credentials to `~/.sibyl-memory/credentials.json` plus this computer's signed access file, `entitlement.json`. Free tier is the default; Pro and Elite unlock self-learning, the memory linter, and remove the local cap.

**An account is required.** Since client 0.9.0 every memory tool (MCP, Hermes, LangGraph, the SDK) needs an activated account holding a valid, server-signed entitlement. Without one the tools answer with a clear "run `sibyl init`" error instead of storing anything. The entitlement renews itself in the background about once a day and keeps working offline for up to 14 days. Your local memory is never deleted by any of this, and `sibyl memory list|search|recall` always reads it without an account. Memory written on this computer before you signed in moves into your account, on this computer, the first time you sign in (one local step, nothing uploaded); `sibyl memory adopt` does it on request, and asks first if the store has held another account. Upgrading needs one connection to finish setup: until this computer has checked in once, the tools answer "connect to the internet once to finish setup".

### Open code, hosted account

The code in this repository is MIT licensed, including the whole local engine, so a fork can remove the account check. We say that plainly. Here is what such a fork keeps and what it cannot have:

| A fork keeps (local, always public under MIT) | A fork loses (held by the server; needs the entitlement or the live sign-in it comes from) |
|---|---|
| The SQLite schema, storage, FTS5 search, the verdict contract, the write-time normalizer, a local cap check it can edit away, unlimited local stores | Anything above the free cap on the official path (from the client cutoff, `/check-write` refuses without a live sign-in and a matching entitlement) |
| Reading and writing its own local database with any agent it wires up itself | Paid recognition anywhere: learner and linter authorization keyed to the entitlement tier, purchase claims, the billing portal, USDC subscriptions |
| The MIT right to redistribute its modified copy under its own name | Device management (`sibyl devices`, revoke, web approval), the account page, email verification, recovery, receipts and security notices |
| | Updates, support, the beta channel, and every hosted feature to come |
| | The name: the official packages, docs and installers point at accounts |

For direct SDK use:

```bash
pip install sibyl-memory-client
```

For Hermes integration:

```bash
pip install sibyl-memory-cli
sibyl setup hermes    # installs the plugin and sets memory.provider: sibyl
```

For MCP (Claude Code, Codex, Cursor, Continue, ...):

```bash
pip install sibyl-memory-mcp sibyl-memory-cli
sibyl setup claude-code    # or: sibyl setup codex
# other MCP clients: register a server named sibyl-memory with command sibyl-memory-mcp
```

Full documentation at [docs.sibyllabs.org/memory](https://docs.sibyllabs.org/memory/).

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  sibyl-memory-cli               sibyl-memory-mcp        │
│  ┌──────────────┐               ┌──────────────┐        │
│  │ sibyl init   │               │ MCP server   │        │
│  │ sibyl status │               │ (stdio)      │        │
│  │ sibyl whoami │               └──────┬───────┘        │
│  └──────┬───────┘                      │                │
│         └────────────┬─────────────────┘                │
│  sibyl-memory-hermes │     sibyl-memory-langgraph       │
│  ┌──────────────┐    │     ┌────────────────┐           │
│  │ Hermes hook  │    │     │ LangGraph      │           │
│  └──────┬───────┘    │     │ BaseStore      │           │
│         │            │     └───────┬────────┘           │
│         │            │             │                    │
│         │            │             │                    │
│         └────────────┼─────────────┘                    │
│                      ▼                                  │
│         sibyl-memory-client (SDK)                       │
│         ┌────────────────────────┐                      │
│         │ SQLite + FTS5          │                      │
│         │ 5-tier schema          │                      │
│         │ self-learning skills   │                      │
│         │ multi-tenant           │                      │
│         └────────────────────────┘                      │
└─────────────────────────────────────────────────────────┘
```

Each package has its own `README.md` and `CHANGELOG.md` for the detail.

The five tiers, in case you're curious:

```
  HOT        state/        live working state, rewritten in place
  WARM       entities/     single source of truth per (category, name)
  COLD       journal/      append-only event log
  REFERENCE  reference/    static knowledge, rarely changes
  ARCHIVE    archive/      retired entities, kept for audit
```

Rule 43 (single source of truth per entity) is enforced at the schema level via a `UNIQUE (tenant_id, category, name)` constraint, not just a convention in the application code. Drift is impossible by construction.

---

## Provenance

Built by [SIBYL](https://x.com/sibylcap), the autonomous agent operating at [Sibyl Labs LLC](https://sibyllabs.org).

The agent has been operating in production since February 2026, ships code daily, holds an on-chain identity on Base (ERC-8004 agent ID 20880), runs an autonomous trading engine, an on-chain messaging protocol, an x402 payment rail, a token-gated chat demo, an advisory dashboard, and this memory product family. Everything verifiable on-chain.

Memory architecture is the proven core. Sibyl Labs LLC owns the IP, signs contracts, and holds the legal wrapper around the agent's work. The work itself is shipped by the agent, in sessions, through the operator (`@tradingtulips`). The PyPI releases, the CLI, the SDK, the CLI banner above: all of it is autonomous agent output.

The on-chain record is the resume. This repository is one chapter of it.

---

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

## License

MIT. See [LICENSE](./LICENSE).

Copyright (c) 2026 Sibyl Labs LLC.
