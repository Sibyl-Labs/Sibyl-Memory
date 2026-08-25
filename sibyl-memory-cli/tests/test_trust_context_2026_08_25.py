"""Darwin trust-store fix (2026-08-25), CLI leg: `sibyl init` on the macOS
python.org framework build failed every server call with
`Warning: session-init failed (0)` (CERTIFICATE_VERIFY_FAILED surfacing as
HttpError status 0). Both CLI network paths — http_request (all
api.sibyllabs.org calls) and _pypi_latest (update check) — now pass the shared
certifi-backed context from sibyl_memory_client._trust, with a stdlib-default
fallback when the client package is absent. These tests pin the wiring at both
call sites and the graceful fallback."""
from __future__ import annotations

import ssl
import sys
import urllib.request

from sibyl_memory_cli import cli


class _Resp:
    def __init__(self, body: bytes = b"{}"):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _shared_context():
    from sibyl_memory_client import _trust
    return _trust.https_context()


def test_http_request_passes_trust_context(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _Resp(b"{\"ok\": true}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = cli.http_request("GET", "/api/plugin/whoami")
    assert out == {"ok": True}
    assert captured["context"] is _shared_context()


def test_pypi_latest_passes_trust_context(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _Resp(b"{\"info\": {\"version\": \"9.9.9\"}}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert cli._pypi_latest("sibyl-memory-cli") == "9.9.9"
    assert captured["context"] is _shared_context()


def test_fallback_without_client_package(monkeypatch):
    # sys.modules[name] = None makes the from-import raise ImportError.
    monkeypatch.setitem(sys.modules, "sibyl_memory_client._trust", None)
    ctx = cli._https_context()            # must not raise
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_fallback_still_serves_http_request(monkeypatch):
    monkeypatch.setitem(sys.modules, "sibyl_memory_client._trust", None)
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["context"] = context
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert cli.http_request("GET", "/api/plugin/whoami") == {}
    assert isinstance(captured["context"], ssl.SSLContext)
