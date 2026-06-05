"""Regression (bugflow 2026-06-05): tool errors must set the MCP `isError` flag.

`_err()` used to return a plain dict, which FastMCP delivered as a *successful*
tool result (`isError: false`) with the error nested inside the payload — an
agent keying off the protocol-level `isError` flag could not detect the failure
at all. `_err()` now raises `ToolError` carrying the same structured payload as
JSON, so callers see `isError: true` AND can still parse error/code/recovery.
"""
from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import sibyl_memory_mcp.server as server
from sibyl_memory_client.exceptions import (
    CapExceededError,
    NotFoundError,
    TierGateError,
    ValidationError,
)


def test_err_raises_toolerror_not_returns_dict():
    # The whole point of the fix: _err raises rather than returns.
    with pytest.raises(ToolError):
        server._err(NotFoundError("entity not found"))


@pytest.mark.parametrize(
    "exc, code",
    [
        (CapExceededError("over the 2 MB free-tier cap", current_size=3_000_000, cap=2_000_000), "CAP_EXCEEDED"),
        (TierGateError("paid feature", feature="self_learning"), "TIER_GATED"),
        (NotFoundError("missing"), "NOT_FOUND"),
        (ValidationError("bad body"), "VALIDATION_ERROR"),
    ],
)
def test_err_toolerror_preserves_structured_payload(exc, code):
    with pytest.raises(ToolError) as ei:
        server._err(exc)
    payload = json.loads(str(ei.value))
    assert payload["code"] == code
    assert payload["error"] == type(exc).__name__
    assert payload["message"]  # non-empty human message survives


def test_cap_exceeded_keeps_recovery_and_upgrade_url():
    with pytest.raises(ToolError) as ei:
        server._err(CapExceededError("cap", current_size=3_000_000, cap=2_000_000))
    payload = json.loads(str(ei.value))
    assert "Run `sibyl upgrade`" in payload["recovery"]
    assert payload["upgrade_url"].startswith("https://")
