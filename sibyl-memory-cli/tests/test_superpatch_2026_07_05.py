"""Regression tests for the 2026-07-05 super-patch (Unit CLI).

  Real #4     `sibyl logout` must best-effort REVOKE this device's server bearer
              before unlinking local credentials (the bearer has no server-side
              expiry), reusing the same /api/plugin/devices endpoint + Bearer
              auth as `sibyl devices revoke`. A network failure is swallowed but
              REPORTED as an offline caveat.
  Contract T  `sibyl init` must PERSIST the server-issued tenant_id into
              credentials.json (it was dropped from the _CRED_FIELDS allowlist),
              so every surface resolves the same tenant.
"""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_cli import cli


# ----------------------------------------------------------------------
# Real #4 — logout revokes this device's server bearer
# ----------------------------------------------------------------------

def _write_creds(path: Path, **extra) -> None:
    creds = {"account_id": "acct-1", "session_token": "tok-abc", "tier": "paid"}
    creds.update(extra)
    path.write_text(json.dumps(creds))


def test_logout_issues_revoke_when_online(tmp_path, monkeypatch, capsys):
    cred = tmp_path / "credentials.json"
    tc = tmp_path / "tier_cache.json"
    _write_creds(cred)
    tc.write_text("{}")

    calls: list[tuple] = []

    def fake_http(method, path, *, body=None, timeout=15.0, headers=None):
        calls.append((method, path, body, headers))
        if method == "GET" and path.startswith("/api/plugin/devices"):
            return {"devices": [
                {"is_this_device": False, "bearer_id": "other", "device_label": "phone"},
                {"is_this_device": True, "bearer_id": "bid-9", "device_label": "thislaptop"},
            ]}
        if method == "POST" and path == "/api/plugin/devices":
            return {"revoked": True}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "http_request", fake_http)

    rc = cli.main(["--credentials", str(cred), "--tier-cache", str(tc), "logout"])
    assert rc == 0

    # A POST revoke was issued for THIS device's bearer_id, with the bearer auth
    # shape reused from `sibyl devices revoke`.
    posts = [c for c in calls if c[0] == "POST" and c[1] == "/api/plugin/devices"]
    assert posts, "logout did not issue the server-side revoke POST"
    assert posts[0][2] == {"bearer_id": "bid-9"}
    assert posts[0][3].get("Authorization") == "Bearer tok-abc"

    # Local logout still happened; no offline caveat on the happy path.
    out = capsys.readouterr().out
    assert not cred.exists()
    assert "remote session may still be active" not in out


def test_logout_prints_offline_caveat_on_network_failure(tmp_path, monkeypatch, capsys):
    cred = tmp_path / "credentials.json"
    tc = tmp_path / "tier_cache.json"
    _write_creds(cred)

    def fake_http_fail(method, path, *, body=None, timeout=15.0, headers=None):
        # Simulate the CLI's network-failure envelope (URLError -> HttpError 0).
        raise cli.HttpError(0, {"error": "network unreachable"}, f"http://x{path}")

    monkeypatch.setattr(cli, "http_request", fake_http_fail)

    rc = cli.main(["--credentials", str(cred), "--tier-cache", str(tc), "logout"])
    out = capsys.readouterr().out

    # Failure is swallowed (logout succeeds locally) but the caveat is reported.
    assert rc == 0
    assert not cred.exists()
    assert "remote session may still be active" in out
    assert "sibyl devices revoke" in out


def test_logout_without_bearer_id_reports_caveat(tmp_path, monkeypatch, capsys):
    """Server lists devices but can't identify this one -> can't confirm revoke."""
    cred = tmp_path / "credentials.json"
    tc = tmp_path / "tier_cache.json"
    _write_creds(cred)

    def fake_http(method, path, *, body=None, timeout=15.0, headers=None):
        if method == "GET" and path.startswith("/api/plugin/devices"):
            return {"devices": [{"is_this_device": False, "bearer_id": "other"}]}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "http_request", fake_http)

    rc = cli.main(["--credentials", str(cred), "--tier-cache", str(tc), "logout"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not cred.exists()
    assert "remote session may still be active" in out


# ----------------------------------------------------------------------
# Contract T — init persists the server-issued tenant_id
# ----------------------------------------------------------------------

def test_init_persists_server_tenant_id(tmp_path, monkeypatch):
    cred = tmp_path / "credentials.json"  # absent -> fresh activation

    def fake_http(method, path, *, body=None, timeout=15.0, headers=None):
        if path.startswith("/api/plugin/session-init"):
            return {"pairing_ttl_seconds": 300}
        if path.startswith("/api/plugin/check"):
            return {"bound": True, "credentials": {
                "account_id": "acct-1",
                "tenant_id": "tid-server-issued",
                "tier": "paid",
                "bearer_token": "btok-123",
            }}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "http_request", fake_http)
    # Don't spawn a browser in CI.
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)

    rc = cli.main(["--credentials", str(cred), "init"])
    assert rc == 0

    persisted = json.loads(cred.read_text())
    # THE regression: tenant_id must survive activation (was dropped pre-fix).
    assert persisted.get("tenant_id") == "tid-server-issued"
    assert persisted.get("account_id") == "acct-1"
    assert persisted.get("session_token") == "btok-123"  # bearer persisted
