"""sibyl-memory-mcp. MCP server for Sibyl Memory Plugin.

Wraps the local SQLite + FTS5 memory engine (sibyl-memory-client) and
exposes it to any MCP-compatible agent: Claude Code, Codex CLI, Cursor,
Continue, etc.

Usage (Claude Code):
    sibyl setup claude-code
    or by hand:
        claude mcp add --scope user sibyl-memory -- sibyl-memory-mcp

Usage (Codex CLI):
    sibyl setup codex
    or by hand, in ~/.codex/config.toml:
        [mcp_servers.sibyl_memory]
        command = "/absolute/path/to/sibyl-memory-mcp"

Both require `sibyl init` first (0.3.0): the memory tools need an activated
account holding a valid, server-signed entitlement in ~/.sibyl-memory/. Until
then every tool answers an error whose JSON carries code "ACCOUNT_REQUIRED".
"""

from .server import build_server, run_stdio

# Single-sourced from installed metadata so the wheel + code never drift
# (v0.1.3: the hardcoded "0.1.0" had drifted from the 0.1.2 published wheel;
# mirrors sibyl-memory-client's dynamic-version pattern). Source-tree fallback
# for editable installs that haven't been pip-installed yet.
from importlib.metadata import PackageNotFoundError, version as _pkg_version
try:
    __version__ = _pkg_version("sibyl-memory-mcp")
except PackageNotFoundError:  # pragma: no cover - source-tree dev only
    __version__ = "0.0.0+source"

__all__ = ["build_server", "run_stdio", "__version__"]
