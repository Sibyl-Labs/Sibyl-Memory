"""Hermes 0.6.0: the provider's client sends no usage count unless the person
opts in with SIBYL_MEMORY_TELEMETRY=1 (the MCP server and the CLI keep the
opt-out default)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sibyl_memory_client._heartbeat import HeartbeatReporter, _NullHeartbeat
from sibyl_memory_hermes import SibylMemoryProvider


def _provider(tmp_path) -> SibylMemoryProvider:
    return SibylMemoryProvider(db_path=str(tmp_path / "m.db"),
                               credentials_path=str(Path.home() / ".sibyl-memory" / "credentials.json"))


@pytest.mark.parametrize("env", [None, "0", "true", "yes"])
def test_off_unless_opted_in(tmp_path, monkeypatch, env) -> None:
    if env is None:
        monkeypatch.delenv("SIBYL_MEMORY_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", env)
    assert isinstance(_provider(tmp_path).client._heartbeat, _NullHeartbeat)


def test_opt_in_with_one(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "1")
    hb = _provider(tmp_path).client._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is True
