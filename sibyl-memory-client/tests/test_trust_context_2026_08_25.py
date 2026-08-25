"""Darwin trust-store fix (2026-08-25): the python.org macOS framework build
ships no CA bundle in Python's own OpenSSL, so the stdlib default SSL context
failed verification against api.sibyllabs.org. sibyl_memory_client._trust now
builds one shared context = stdlib defaults PLUS certifi's bundle (additive,
never weakened), and both SDK transports (heartbeat, check-write) pass it
explicitly. These tests pin: verification stays fully on in every branch,
certifi's bundle is loaded when present, a missing certifi degrades to the
stdlib default instead of crashing, the context is built once, and both real
call sites actually pass it to urlopen (defined-but-unwired is a fail).
"""
from __future__ import annotations

import ssl
import sys
import urllib.request

import pytest

from sibyl_memory_client import _trust


@pytest.fixture(autouse=True)
def _fresh_trust_cache(monkeypatch):
    """Each test builds its own context; never leak one test's cache."""
    monkeypatch.setattr(_trust, "_CACHED", None)


def test_context_verifies_by_default():
    ctx = _trust.https_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_certifi_bundle_loaded_when_present(monkeypatch):
    certifi = pytest.importorskip("certifi")
    loaded: list = []
    orig = ssl.SSLContext.load_verify_locations

    def spy(self, cafile=None, capath=None, cadata=None):
        loaded.append(cafile)
        return orig(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", spy)
    ctx = _trust.https_context()
    assert certifi.where() in loaded
    # additive, never weakened
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_missing_certifi_degrades_to_stdlib_default(monkeypatch):
    # sys.modules[name] = None makes `import certifi` raise ImportError.
    monkeypatch.setitem(sys.modules, "certifi", None)
    ctx = _trust.https_context()          # must not raise
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_context_is_built_once():
    assert _trust.https_context() is _trust.https_context()


class _Resp:
    def read(self):
        return b"{}"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_heartbeat_send_passes_trust_context(monkeypatch):
    from sibyl_memory_client._heartbeat import HeartbeatReporter
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    r = HeartbeatReporter("acct-123", "sess", flush_every=100)
    r._send(3)  # sync path: no daemon-thread polling needed
    assert captured["context"] is _trust.https_context()


def test_capcheck_transport_passes_trust_context(monkeypatch):
    from sibyl_memory_client._capcheck import _default_check_write_fn
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = _default_check_write_fn(
        "https://api.sibyllabs.org/api/plugin/check-write",
        {"account_id": "acc-1"},
        timeout=1.0,
    )
    assert out == {}
    assert captured["context"] is _trust.https_context()
