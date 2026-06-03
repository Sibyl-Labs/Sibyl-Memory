"""SEC-14 regression: a type-invalid tool argument must not echo its raw value.

The MCP SDK's Tool.run wraps a pydantic ValidationError as a ToolError whose
message includes the caller's raw input_value. If a secret is fat-fingered into a
typed argument, it would be reflected back as an error result. The server guards
this by wrapping the LOWLEVEL CallToolRequest handler — the real dispatch path.

These tests exercise mcp._mcp_server.request_handlers[CallToolRequest] directly
(NOT mcp.call_tool, which FastMCP binds at construction and which a naive test
would pass while production still leaked).
"""
from __future__ import annotations

import asyncio

from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import MemoryClient


def _wire(tmp_path, monkeypatch):
    client = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    monkeypatch.setattr(server, "_open_client", lambda: client)
    mcp = server.build_server()
    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    return client, handler


def _call(handler, name, arguments):
    req = CallToolRequest(
        method="tools/call",
        params={"name": name, "arguments": arguments},
    )
    return asyncio.run(handler(req))


def test_arg_validation_does_not_leak_input_value(tmp_path, monkeypatch):
    _, handler = _wire(tmp_path, monkeypatch)
    secret = "sk-live-SECRETVALUE-9999"
    result = _call(handler, "memory_search", {"query": "x", "limit": secret})
    blob = result.model_dump_json()
    # the caller-supplied secret must NOT be reflected back
    assert secret not in blob
    # it must be an error (not a silent pass) carrying the generic scrub message
    assert '"isError":true' in blob.replace(" ", "")
    assert ("not echoed back" in blob) or ("failed validation" in blob)


def test_valid_call_is_not_over_scrubbed(tmp_path, monkeypatch):
    client, handler = _wire(tmp_path, monkeypatch)
    client.set_entity("projects", "atlas", {"note": "budget planning"})
    result = _call(handler, "memory_search", {"query": "budget", "limit": 5})
    blob = result.model_dump_json()
    # a valid call returns normally; the guard must not touch non-error results
    assert '"isError":true' not in blob.replace(" ", "")
    assert "atlas" in blob
