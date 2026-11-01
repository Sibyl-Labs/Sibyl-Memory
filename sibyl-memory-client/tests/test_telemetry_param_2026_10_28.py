"""MemoryClient(telemetry=) (0.10.0): False sends no usage count from this
client whatever the environment says; None (default) and True follow
SIBYL_MEMORY_TELEMETRY (on unless "0")."""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client._heartbeat import HeartbeatReporter, _NullHeartbeat
from sibyl_memory_client.testing import NullEntitlementGate


def _c(tmp_path, **kw) -> MemoryClient:
    return MemoryClient.local(str(tmp_path / "m.db"), account_id="acct-1", session_token="tok",
                              entitlement_gate=NullEntitlementGate(), **kw)


@pytest.mark.parametrize("env", [None, "1", "0"])
def test_telemetry_false_is_the_null_heartbeat(tmp_path, monkeypatch, env) -> None:
    if env is None:
        monkeypatch.delenv("SIBYL_MEMORY_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", env)
    assert isinstance(_c(tmp_path, telemetry=False)._heartbeat, _NullHeartbeat)


@pytest.mark.parametrize("telemetry", [None, True])
def test_default_follows_the_environment(tmp_path, monkeypatch, telemetry) -> None:
    monkeypatch.delenv("SIBYL_MEMORY_TELEMETRY", raising=False)
    hb = _c(tmp_path, telemetry=telemetry)._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is True
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    hb = _c(tmp_path, telemetry=telemetry)._heartbeat
    assert isinstance(hb, HeartbeatReporter) and hb._enabled is False
