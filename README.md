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
[![License: MIT](https://img.shields.io/badge/license-MIT-15110a.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-15110a.svg)](https://www.python.org/downloads/)
[![LongMemEval](https://img.shields.io/badge/LongMemEval-95.6%25%20%23%E2%80%832-2e6b3a.svg)](https://blog.sibylcap.com/longmemeval-v2)

<sub><i>built by an autonomous agent · sibyl labs llc</i></sub>

</div>

---

## What this is

Four PyPI packages, one schema family, one architecture.

`sibyl-memory-client` is a local-first agentic memory SDK. SQLite-backed, five-tier hierarchical schema, FTS5 search, multi-tenant by design. No vector database. No embedding model. No external retrieval service. The memory lives on the agent's machine; the substrate is a single file on disk.

> **Privacy disclosure.** Your memory content never leaves the machine. The only outbound network call is tier verification: when an activated account writes past its tier cap, the client calls `api.sibyllabs.org/api/plugin/check-write` with account metadata only (account id, session token, and the database's byte size and proposed delta) — never the contents of your memory. Verified against the source in `sibyl-memory-client/src/sibyl_memory_client/_capcheck.py`. Free, unactivated use makes no network calls at all.

The other three packages ride on top: `sibyl-memory-cli` for activation and tier management, `sibyl-memory-hermes` for Hermes Agent integration, and `sibyl-memory-mcp` for any MCP-compatible client (Claude Code, Codex, Cursor, Continue).

The architecture was benchmarked publicly on [LongMemEval Oracle](https://blog.sibylcap.com/longmemeval-v2) (ICLR 2025, University of Michigan, 500 questions) and placed **#2 overall at 95.6%**, tied with Chronos (PwC), beating Mastra, MemMachine, Hindsight, Mem0, Supermemory, Zep, and the Oracle baseline. It is the only file-based system in the top tier: running on a single 4 vCPU / 16 GB box, no vector infrastructure, no embedding fees.

This is the entire stack as it ships to production agents today.

---

## Packages

| Package                                        | PyPI                                                                                                        | Description                                                                                                                                                                                          |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`sibyl-memory-client`](./sibyl-memory-client) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-client)](https://pypi.org/project/sibyl-memory-client/) | Local-first agentic memory SDK. SQLite-backed five-tier hierarchical schema, FTS5 search, multi-tenant, with self-learning skill detection and local memory linter. Foundation of the plugin family. |
| [`sibyl-memory-cli`](./sibyl-memory-cli)       | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-cli)](https://pypi.org/project/sibyl-memory-cli/)       | Command-line interface. `sibyl init` activates, `sibyl upgrade` runs the staker / subscription flow, `sibyl status` shows current tier and DB stats, `sibyl whoami`, `sibyl devices`.                |
| [`sibyl-memory-hermes`](./sibyl-memory-hermes) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-hermes)](https://pypi.org/project/sibyl-memory-hermes/) | Bundled memory payload for Hermes Agent v0.13+ (and any other Python orchestration that wants direct SDK access).                                                                                    |
| [`sibyl-memory-mcp`](./sibyl-memory-mcp)       | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-mcp)](https://pypi.org/project/sibyl-memory-mcp/)       | MCP server. Wraps the local SQLite + FTS5 memory engine and exposes it to MCP-compatible agents (Claude Code, Codex, Cursor, Continue, anything that speaks MCP).                                    |

---

## Install

```bash
pip install sibyl-memory-cli
sibyl init
```

## Docker integration

## Docker

A Docker configuration is included for running the `sibyl-memory-mcp` server locally.

Build the image:

```bash
docker compose build
```

Start the server:

```bash
docker compose up -d
```

The container uses the project's existing stdio MCP entrypoint (`python -m sibyl_memory_mcp`) and stores persistent data in the mounted `docker-data/` directory.

`sibyl init` opens a browser to activate your account, binds your wallet or email, and writes credentials to `~/.sibyl-memory/credentials.json`. Free tier is the default; staker and subscription tiers unlock self-learning, the memory linter, and remove the local cap.

For direct SDK use:

```bash
pip install sibyl-memory-client
```

For Hermes integration:

```bash
pip install sibyl-memory-hermes
sibyl-memory-hermes install-plugin
# then edit ~/.hermes/config.yaml:
#   memory:
#     provider: sibyl
```

For MCP (Claude Code, Codex, Cursor, Continue, ...):

```bash
pip install sibyl-memory-mcp
# then point your MCP client at the server entry point.
```

Full documentation at [docs.sibyllabs.org/memory](https://docs.sibyllabs.org/memory/).

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  sibyl-memory-cli       sibyl-memory-mcp                │
│  ┌──────────────┐       ┌──────────────┐                │
│  │ sibyl init   │       │ MCP server   │                │
│  │ sibyl status │       │ (stdio)      │                │
│  │ sibyl whoami │       └──────┬───────┘                │
│  └──────┬───────┘              │                        │
│         │     sibyl-memory-hermes                       │
│         │     ┌──────────────┐                          │
│         │     │ Hermes hook  │                          │
│         │     └──────┬───────┘                          │
│         │            │                                  │
│         └──────┬─────┘                                  │
│                ▼                                        │
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

## License

MIT. See [LICENSE](./LICENSE).

Copyright (c) 2026 Sibyl Labs LLC.
