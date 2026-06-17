"""Usage heartbeat (2026-06-16): the local-first plugin had no usage signal, so
account request counts under-reported real memory use. The client now reports
aggregate op COUNTS (no content/PII) to /heartbeat, debounced + fire-and-forget.
These tests pin the safety contract: opt-out, no-account no-op, debounce, and
that it never raises into a memory operation."""
from __future__ import annotations

from pathlib import Path

from sibyl_memory_client._heartbeat import HeartbeatReporter
from sibyl_memory_client.client import MemoryClient


def _capture(flush_every=3, flush_interval_s=10_000):
    r = HeartbeatReporter("acct-123", "sess", flush_every=flush_every, flush_interval_s=flush_interval_s)
    sent: list[int] = []
    r._fire = lambda ops, sync=False: sent.append(ops)  # capture; no thread, no network
    return r, sent


def test_disabled_without_account_id():
    r = HeartbeatReporter(None)
    assert r._enabled is False
    r.record()  # must be a silent no-op


def test_opt_out_env(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    r = HeartbeatReporter("acct-123")
    assert r._enabled is False


def test_debounce_flushes_every_n():
    r, sent = _capture(flush_every=3)
    r.record(); r.record()
    assert sent == []            # below threshold: nothing sent
    r.record()                   # 3rd op -> flush
    assert sent == [3]
    r.record(); r.record(); r.record()
    assert sent == [3, 3]        # next batch of 3


def test_final_flush_sends_remainder():
    r, sent = _capture(flush_every=100)
    r.record(); r.record()
    r._flush_final()             # process-exit path
    assert sent == [2]


def test_record_never_raises():
    r, sent = _capture(flush_every=1)
    def boom(*a, **k):
        raise RuntimeError("network down")
    r._fire = boom
    r.record()                   # must swallow; no exception escapes


def test_only_counts_no_content_in_payload(monkeypatch):
    # The wire payload must carry ONLY account_id + event_type + an integer count.
    captured = {}
    r = HeartbeatReporter("acct-123", "sess", flush_every=1)
    import urllib.request
    class _Resp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        import json
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = dict(req.header_items())
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r.record()  # flush_every=1 -> fires; daemon thread
    import time as _t
    for _ in range(50):
        if "body" in captured: break
        _t.sleep(0.02)
    assert captured["body"]["account_id"] == "acct-123"
    assert captured["body"]["event_type"] == "heartbeat"
    assert isinstance(captured["body"]["heartbeat_count"], int)
    assert set(captured["body"].keys()) == {"account_id", "event_type", "heartbeat_count"}


def test_client_records_on_ops(tmp_path: Path):
    c = MemoryClient.local(path=tmp_path / "m.db")
    calls: list[str] = []
    class _Cap:
        def record(self, kind="op"):
            calls.append(kind)
    c._heartbeat = _Cap()
    c.set_entity("partner", "x", {"a": 1})
    c.search("x")
    c.list_entities()
    assert "set_entity" in calls
    assert "search" in calls
    assert "list_entities" in calls
