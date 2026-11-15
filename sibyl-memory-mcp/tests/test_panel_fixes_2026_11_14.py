"""Regression tests for the 2026-11-14 review panel fixes in sibyl-memory-mcp
(PANEL-FINDINGS.md finding 29: the declared mcp floor)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
SERVER = Path(__file__).resolve().parents[1] / "src" / "sibyl_memory_mcp" / "server.py"


def _mcp_floor() -> tuple:
    m = re.search(r'"mcp>=([0-9.]+)', PYPROJECT.read_text())
    assert m, "mcp floor not found"
    return tuple(int(x) for x in m.group(1).split("."))


def test_mcp_floor_admits_no_release_without_tool_meta():
    # server.py passes meta= to @mcp.tool; FastMCP.tool() first takes it in
    # mcp 1.19.0 (1.18.0 raises TypeError at startup, measured 2026-11-14).
    assert "@mcp.tool(meta=" in SERVER.read_text()
    assert _mcp_floor() >= (1, 19, 0)


def test_installed_fastmcp_has_every_argument_the_server_passes():
    from mcp.server.fastmcp import FastMCP
    init = inspect.signature(FastMCP.__init__).parameters
    assert "instructions" in init and "log_level" in init
    assert "meta" in inspect.signature(FastMCP.tool).parameters
    assert "title" in inspect.signature(FastMCP.prompt).parameters
    assert "title" in inspect.signature(FastMCP.resource).parameters
