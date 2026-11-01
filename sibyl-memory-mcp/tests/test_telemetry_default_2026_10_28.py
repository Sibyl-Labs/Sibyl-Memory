"""0.4.0: the MCP server keeps the usage-count opt-out default (Hermes turned
its own off in 0.6.0): on unless SIBYL_MEMORY_TELEMETRY=0."""
from __future__ import annotations

from pathlib import Path

import sibyl_memory_mcp.server as server
from sibyl_memory_client._heartbeat import HeartbeatReporter


def _build(monkeypatch, tmp_path):
    d = Path.home() / ".sibyl-memory"
    monkeypatch.setattr(server, "DEFAULT_DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(server, "DEFAULT_CRED_PATH", d / "credentials.json")
    return server._build_client()


def test_mcp_client_reports_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SIBYL_MEMORY_TELEMETRY", raising=False)
    hb = _build(monkeypatch, tmp_path)._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is True


def test_mcp_client_opt_out(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    hb = _build(monkeypatch, tmp_path)._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is False
