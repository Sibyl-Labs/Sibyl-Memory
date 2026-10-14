"""R1 (plan plugin-paid-tier-2026-09-29, 2.3 / 3.4): Hermes.

* every turn (sync_turn) and tool call counts active time; on Pro the next
  prefetch carries the automatic-save line when it is due, once per interval,
  outside the memory fence; never on the free tier;
* at shutdown, on Pro only, one local learner pass (skill proposals).

On 3e13ca4 these fail: the adapter kept no active time (``_notices`` had no
``note_activity``), prefetch had no save line, and shutdown ran no learner.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)


def _adapter(tmp_path, sid="r1-sess") -> SibylAdapter:
    a = SibylAdapter()
    a.initialize(sid, hermes_home=str(tmp_path / "hh"))
    return a


def _bump(a: SibylAdapter, n: int) -> None:
    tid = a._lifecycle_client().get_tenant()

    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
        t["tier_seen"] = t.get("tier_seen") or "paid"
    ST.LifecycleState().update(_u)


def _save_lines(a: SibylAdapter, n: int = 3) -> list[str]:
    out = []
    for i in range(n):
        block = a.prefetch(f"what about the atlas project item {i}")
        if "Sibyl Memory Pro:" in block:
            out.append(block.split("## Sibyl Memory: note\n", 1)[1])
    return out


def test_turns_count_active_time_and_prefetch_asks_on_pro(entitled_test_home, test_signer, tmp_path):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    a = _adapter(tmp_path)
    n = a._notices
    a.sync_turn("hello", "hi there")
    t0 = n._last_event
    assert t0 is not None
    a.handle_tool_call("sibyl_list", {})
    assert n._last_event >= t0
    _bump(a, 2)
    assert not _save_lines(a)                         # not an hour yet
    n._active += 3601
    lines = _save_lines(a)
    assert len(lines) == 1
    assert lines[0].startswith("Sibyl Memory Pro: about 60 minutes of work since the last save.")
    assert "~/.sibyl-memory/skills/SAVE.md" in lines[0]
    n._active += 1200
    assert not _save_lines(a)                         # once per interval
    a.shutdown()


def test_free_prefetch_never_asks(entitled_test_home, tmp_path):
    a = _adapter(tmp_path)
    _bump(a, 3)
    a._notices._active += 5 * 3600
    assert not _save_lines(a, 4)
    a.shutdown()


def test_shutdown_runs_the_learner_on_pro_only(entitled_test_home, test_signer, tmp_path, monkeypatch):
    from sibyl_memory_client.lifecycle import notices as N
    from sibyl_memory_client.lifecycle import proposals as PR
    calls: list = []
    monkeypatch.setattr(PR, "run_learner", lambda client, **k: calls.append(client) or None)
    # only the shutdown pass is counted here (the process-start pass runs in
    # its own thread and is the client's ProcessNotices test)
    monkeypatch.setattr(N.ProcessNotices, "start", lambda self, *a, **k: None)
    a = _adapter(tmp_path)
    a.shutdown()
    assert calls == []
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    b = _adapter(tmp_path, "r1-pro")
    b.shutdown()
    assert len(calls) == 1
