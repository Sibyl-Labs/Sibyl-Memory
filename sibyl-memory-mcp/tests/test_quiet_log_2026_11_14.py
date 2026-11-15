"""The MCP SDK's per-request INFO line ("Processing request of type
ListToolsRequest") is not written to stderr at the default level: harnesses
such as DeepSeek Harness show the server's stderr on their terminal (sandbox
M8). FASTMCP_LOG_LEVEL, the SDK's own setting, raises it again."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import sibyl_memory_mcp.server as server


def _list_tools_stderr(tmp_path, extra_env=None) -> tuple:
    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIBYL_MEMORY_TELEMETRY": "0",
        "SIBYL_API_BASE": "http://127.0.0.1:9",
        "SIBYL_MEMORY_DB": str(tmp_path / "memory.db"),
        "SIBYL_CREDENTIALS": str(tmp_path / "credentials.json"),
    }
    env.update(extra_env or {})
    proc = subprocess.Popen([sys.executable, "-m", "sibyl_memory_mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    tools = None
    try:
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "quiet-log-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        for m in msgs:
            proc.stdin.write((json.dumps(m) + "\n").encode())
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                tools = msg["result"]["tools"]
                break
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    return tools, proc.stderr.read().decode(errors="replace")


def test_default_level_prints_no_request_line(tmp_path):
    tools, err = _list_tools_stderr(tmp_path)
    assert tools and any(t["name"] == "memory_search" for t in tools)
    assert "Processing request" not in err


def test_fastmcp_log_level_raises_it(tmp_path):
    tools, err = _list_tools_stderr(tmp_path, {"FASTMCP_LOG_LEVEL": "info"})
    assert tools
    assert "Processing request of type ListToolsRequest" in err


def test_log_level_parsing(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    assert server._log_level() == "WARNING"
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", " debug ")
    assert server._log_level() == "DEBUG"
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "loud")
    assert server._log_level() == "WARNING"


def test_env_name_survives_deepseek_harness_scrub(monkeypatch):
    # DeepSeek Harness drops parent variables matching this pattern and its
    # own DSH_ prefix before spawning a stdio server. Panel fix 32
    # (2026-11-14): the names checked are the ones server._log_level actually
    # reads, recorded from its environment lookups, not a string literal.
    read = []

    class Env(dict):
        def get(self, key, default=None):
            read.append(key)
            return super().get(key, default)

    monkeypatch.setattr(server.os, "environ", Env(os.environ, FASTMCP_LOG_LEVEL="error"))
    assert server._log_level() == "ERROR"
    assert read
    for name in read:
        assert not re.search(r"KEY|PASSWORD|SECRET|TOKEN", name, re.I), name
        assert not name.startswith("DSH_"), name
