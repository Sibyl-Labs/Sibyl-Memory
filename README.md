# Sibyl Labs

Agentic memory infrastructure. Local-first, SQLite-backed, structured-tier memory for AI agents — plus the CLI, MCP server, and Hermes plugin that ship on top of it.

This is the official source repository for the Sibyl Memory Plugin family. All packages are published to PyPI under the MIT license.

---

## Packages

| Package | PyPI | Description |
|---|---|---|
| [`sibyl-memory-client`](./sibyl-memory-client) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-client)](https://pypi.org/project/sibyl-memory-client/) | Local-first agentic memory SDK. SQLite-backed five-tier hierarchical schema, FTS5 search, multi-tenant, with self-learning skill detection and local memory linter. Foundation of the plugin family. |
| [`sibyl-memory-cli`](./sibyl-memory-cli) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-cli)](https://pypi.org/project/sibyl-memory-cli/) | Command-line interface. `sibyl init` activates, `sibyl upgrade` runs the staker / subscription flow, `sibyl status` shows current tier and DB stats, `sibyl whoami`, `sibyl devices`. |
| [`sibyl-memory-hermes`](./sibyl-memory-hermes) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-hermes)](https://pypi.org/project/sibyl-memory-hermes/) | Bundled memory payload for Hermes Agent v0.13+ (and any other Python orchestration that wants direct SDK access). |
| [`sibyl-memory-mcp`](./sibyl-memory-mcp) | [![PyPI](https://img.shields.io/pypi/v/sibyl-memory-mcp)](https://pypi.org/project/sibyl-memory-mcp/) | MCP server. Wraps the local SQLite + FTS5 memory engine and exposes it to MCP-compatible agents (Claude Code, Codex, Cursor, Continue, anything that speaks MCP). |
| [`sibyl-plugin-schema`](./sibyl-plugin-schema) | (internal) | SQL migrations for the activation / account / subscription database. Not on PyPI — kept here as immutable record. |

---

## Install

```bash
pip install sibyl-memory-cli
sibyl init
```

`sibyl init` opens a browser to activate your account at https://sibyllabs.org/plugin/activate, binds your wallet (SIWE) or email, and writes credentials to `~/.sibyl-memory/credentials.json`. Free tier is the default; staker and subscription tiers unlock additional capacity.

For direct SDK use:

```bash
pip install sibyl-memory-client
```

For Hermes integration:

```bash
pip install sibyl-memory-hermes
```

For MCP:

```bash
pip install sibyl-memory-mcp
# Then point your MCP client at the server entry point.
```

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

Each package has its own `README.md` and `CHANGELOG.md` for details.

---

## License

MIT. See [LICENSE](./LICENSE).

## About

Built by SIBYL, the autonomous agent operating at Sibyl Labs LLC. Follow the work on X at [@sibylcap](https://x.com/sibylcap), or at [sibyllabs.org](https://sibyllabs.org).

Copyright (c) 2026 Sibyl Labs LLC.
