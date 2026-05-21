# sibyl-memory-mcp

MCP server for [Sibyl Memory Plugin](https://sibyllabs.org/memory). Exposes the local SQLite + FTS5 memory engine to any MCP-compatible agent: **Claude Code, Codex CLI, Cursor, Continue**, anything that speaks Model Context Protocol.

## Install

```bash
pip install sibyl-memory-mcp
```

You also need an activated Sibyl Memory account. If you haven't already:

```bash
sibyl init
```

This creates `~/.sibyl-memory/credentials.json` (server-issued, HMAC-signed) and a local SQLite database at `~/.sibyl-memory/memory.db`. The MCP server reads both automatically.

## Add to Claude Code

Edit `~/.claude/settings.json` (global) or `.mcp.json` (project-local):

```json
{
  "mcpServers": {
    "sibyl-memory": {
      "command": "sibyl-memory-mcp"
    }
  }
}
```

Restart Claude Code. The 8 memory tools (prefixed `memory_*`) become available immediately.

## Add to Codex CLI

Edit `~/.codex/config.toml`:

```toml
[[mcp_servers]]
name = "sibyl-memory"
command = "sibyl-memory-mcp"
```

Restart Codex.

## Tools exposed

| Tool | What it does |
|------|--------------|
| `memory_remember` | Store an entity by (category, name) |
| `memory_recall` | Read an entity by exact key |
| `memory_search` | FTS5 search across all entities |
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

- **Free tier**: 8 tools work. Hard-capped at 2 MB of local storage. Writes that would push past the cap return `CAP_EXCEEDED` with an `upgrade_url`. Self-learning and memory-check-up tools are not exposed on free tier.
- **Paid tiers** (Sync / Stake / Lifetime / Enterprise): cap removed. All tools enabled.

The cap-gate runs against the **server-authoritative** tier (verified via HMAC-signed credentials): the MCP server can't bypass it by editing the local file.

## License

MIT: same as the rest of the `sibyl-memory-*` family.
