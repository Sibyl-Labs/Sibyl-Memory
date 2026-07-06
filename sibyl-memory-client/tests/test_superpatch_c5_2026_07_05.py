"""Super-patch Unit C5 (2026-07-05) — Hardening #12.

The heartbeat URL is env-overridable (SIBYL_MEMORY_HEARTBEAT_URL) and the
account bearer was previously attached to EVERY heartbeat, so an attacker who
injected any scheme/host via that env var would still receive the long-lived
bearer — a token-exfil channel.

The server's soft cap-gate genuinely requires the bearer (a heartbeat with no
session token is a hard 401, not a soft pass), so the fix GATES the header
rather than removing it: the bearer is attached ONLY when the resolved URL is
https AND its host is an allowlisted sibyllabs domain. Every other case — a
non-https override, a foreign host, a host-that-merely-contains-sibyllabs, a
userinfo trick — gets NO Authorization header.

These tests capture the outgoing urllib request and assert the header policy.
Hermetic: monkeypatches urllib.request.urlopen; no network, reuses conftest's
home isolation.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from sibyl_memory_client._heartbeat import (
    _DEFAULT_URL,
    _auth_allowed_for_url,
    HeartbeatReporter,
)


class _Resp:
    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _send_and_capture(monkeypatch, *, url=None, session_token="11111111-1111-1111-1111-111111111111"):
    """Fire one synchronous heartbeat and return the outgoing request's headers.

    Uses a high flush_every so ``record()`` does NOT trip a debounced (threaded)
    flush; the send is driven entirely by ``_flush_final`` (sync=True → inline
    ``_send``), so there is no daemon thread to race against.
    """
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    r = HeartbeatReporter("acct-123", session_token, url=url, flush_every=100)
    r.record()          # accumulate one op (below threshold: no threaded flush)
    r._flush_final()    # synchronous send path, inline (no thread)
    assert "headers" in captured, "heartbeat did not fire"
    return captured


# --------------------------------------------------------------------------
# The core regression: bearer gated to the allowlisted https default host.
# --------------------------------------------------------------------------

def test_default_https_sibyllabs_host_gets_bearer(monkeypatch):
    """The legitimate default endpoint (https + api.sibyllabs.org) still
    carries the bearer — the server's soft cap-gate needs it."""
    cap = _send_and_capture(monkeypatch, url=None)  # resolves to _DEFAULT_URL
    assert cap["url"] == _DEFAULT_URL
    assert cap["headers"].get("authorization") == "Bearer 11111111-1111-1111-1111-111111111111"


def test_env_override_https_sibyllabs_still_allowed(monkeypatch):
    """An https sibyllabs subdomain override is still trusted."""
    cap = _send_and_capture(monkeypatch, url="https://api.staging.sibyllabs.org/api/plugin/heartbeat")
    assert cap["headers"].get("authorization", "").startswith("Bearer ")


def test_http_override_gets_no_bearer(monkeypatch):
    """A non-https override URL never receives the bearer (exfil over
    cleartext / MITM channel is blocked)."""
    cap = _send_and_capture(monkeypatch, url="http://api.sibyllabs.org/api/plugin/heartbeat")
    assert "authorization" not in cap["headers"]
    # The heartbeat body is still sent (telemetry is not auth-scoped locally).
    assert cap["body"]["account_id"] == "acct-123"


def test_foreign_host_override_gets_no_bearer(monkeypatch):
    """The canonical exfil attempt: env points the URL at an attacker host.
    The account bearer must NOT be attached."""
    cap = _send_and_capture(monkeypatch, url="https://evil.example.com/collect")
    assert "authorization" not in cap["headers"]


def test_env_var_override_is_gated(monkeypatch):
    """Same, driven through the real SIBYL_MEMORY_HEARTBEAT_URL env var
    (url=None so the reporter reads the env override)."""
    monkeypatch.setenv("SIBYL_MEMORY_HEARTBEAT_URL", "https://evil.example.com/collect")
    cap = _send_and_capture(monkeypatch, url=None)
    assert cap["url"] == "https://evil.example.com/collect"
    assert "authorization" not in cap["headers"]


def test_lookalike_host_gets_no_bearer(monkeypatch):
    """A host that merely ends with the brand but is NOT a sibyllabs subdomain
    (no dot boundary) must be rejected."""
    cap = _send_and_capture(monkeypatch, url="https://notsibyllabs.org/collect")
    assert "authorization" not in cap["headers"]


def test_suffix_trick_host_gets_no_bearer(monkeypatch):
    """`sibyllabs.org.evil.com` must be rejected — the real host is evil.com."""
    cap = _send_and_capture(monkeypatch, url="https://sibyllabs.org.evil.com/collect")
    assert "authorization" not in cap["headers"]


def test_userinfo_trick_host_gets_no_bearer(monkeypatch):
    """`https://api.sibyllabs.org@evil.com/` resolves to host evil.com; the
    bearer must not be attached (uses urlparse.hostname, not string match)."""
    cap = _send_and_capture(monkeypatch, url="https://api.sibyllabs.org@evil.com/collect")
    assert "authorization" not in cap["headers"]


# --------------------------------------------------------------------------
# Unit-level coverage of the allowlist predicate.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    _DEFAULT_URL,
    "https://sibyllabs.org/api/plugin/heartbeat",
    "https://api.sibyllabs.org/x",
    "https://deep.sub.sibyllabs.org/x",
    "HTTPS://API.SIBYLLABS.ORG/x",  # scheme + host case-insensitive
])
def test_auth_allowed_true(url):
    assert _auth_allowed_for_url(url) is True


@pytest.mark.parametrize("url", [
    None,
    "",
    "not a url",
    "http://api.sibyllabs.org/x",          # non-https
    "ftp://api.sibyllabs.org/x",           # non-https
    "https://evil.example.com/x",          # foreign host
    "https://notsibyllabs.org/x",          # no dot boundary
    "https://sibyllabs.org.evil.com/x",    # suffix trick
    "https://api.sibyllabs.org@evil.com/x",  # userinfo trick
    "https://sibyllabs.org.evil/x",
])
def test_auth_allowed_false(url):
    assert _auth_allowed_for_url(url) is False


def test_gate_flag_set_on_init():
    """The reporter caches the gate decision from the resolved URL."""
    good = HeartbeatReporter("a", "t", url=_DEFAULT_URL)
    bad = HeartbeatReporter("a", "t", url="https://evil.example.com/collect")
    assert good._attach_auth is True
    assert bad._attach_auth is False
