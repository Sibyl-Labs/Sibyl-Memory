"""sibyl-memory-mcp — MCP server for Sibyl Memory Plugin.

Wraps the local SQLite + FTS5 memory engine (sibyl-memory-client) and
exposes it to any MCP-compatible agent: Claude Code, Codex CLI, Cursor,
Continue, etc.

Usage (Claude Code):
    Add to ~/.claude/settings.json or project .mcp.json:
        {
          "mcpServers": {
            "sibyl-memory": { "command": "sibyl-memory-mcp" }
          }
        }

Usage (Codex CLI):
    Add to ~/.codex/config.toml:
        [[mcp_servers]]
        name = "sibyl-memory"
        command = "sibyl-memory-mcp"

Both expect `sibyl init` to have been run first so credentials.json and
memory.db exist at ~/.sibyl-memory/.
"""

from .server import build_server, run_stdio

__version__ = "0.1.0"
__all__ = ["build_server", "run_stdio", "__version__"]
