"""cli 0.6.0: the agent-equivalent client the CLI builds (`sibyl save --fact`)
keeps the usage-count opt-out default (Hermes turned its own off in 0.6.0)."""
from __future__ import annotations

import argparse
from pathlib import Path

from sibyl_memory_cli import lifecycle_cli as L
from sibyl_memory_client._heartbeat import HeartbeatReporter


def _args(tmp_path):
    return argparse.Namespace(credentials=str(Path.home() / ".sibyl-memory" / "credentials.json"),
                              db=str(tmp_path / "m.db"))


def test_cli_client_reports_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SIBYL_MEMORY_TELEMETRY", raising=False)
    hb = L._client(_args(tmp_path))._heartbeat
    assert isinstance(hb, HeartbeatReporter)
    assert hb._enabled is bool(hb._account_id)


def test_cli_client_opt_out(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    hb = L._client(_args(tmp_path))._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is False
