"""C2 fixes (2026-09-04): full account id on every display surface + `sibyl claim`.

Panel finding (three seats independently): the buy-before-install claim flow
requires the full 36-char account id, but every CLI surface printed it through
short() as `12345678…abcd`, making the documented flow unusable. account_id is
a routing identifier (rides URL querystrings and subscribe calls), not a
bearer secret; the F3 never-print-the-bearer invariant covers tokens.
"""

import json
from pathlib import Path

import pytest

from sibyl_memory_cli import cli


ACCT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture()
def creds_file(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({
        "account_id": ACCT,
        "tenant_id": ACCT,
        "tier": "free",
        "email": "buyer@example.com",
        "wallet": None,
        "issued_at": "2026-09-04T00:00:00Z",
        "schema_version": 3,
        "session_token": "22222222-3333-4444-8555-666666666666",
        "bearer_token": "22222222-3333-4444-8555-666666666666",
    }))
    return p


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_whoami_prints_full_account_id(creds_file, capsys):
    rc = cli.cmd_whoami(_Args(credentials=str(creds_file), full=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert ACCT in out                       # full 36-char id, verbatim
    assert "…" not in out.split("account")[1].split("\n")[0]  # no ellipsis on the account line


def test_claim_code_validation():
    assert cli._looks_like_checkout_code("cs_test_a1B2c3D4e5F6g7H8")
    assert cli._looks_like_checkout_code("cs_live_" + "a" * 24)
    assert cli._looks_like_checkout_code("cs_" + "z" * 12)
    assert not cli._looks_like_checkout_code("cs_test_short")       # < 10 after marker
    assert not cli._looks_like_checkout_code("cs_test_abc123…")     # pasted ellipsis
    assert not cli._looks_like_checkout_code("pi_123456789012")     # wrong prefix
    assert not cli._looks_like_checkout_code("")


def test_claim_happy_path_posts_and_syncs(creds_file, capsys, monkeypatch):
    calls = {}

    def fake_http(method, path, *, body=None, **kw):
        calls["method"], calls["path"], calls["body"] = method, path, body
        return {"ok": True, "tier": "pro", "subscription_id": "s1", "expires_at": "2026-10-07T00:00:00Z"}

    monkeypatch.setattr(cli, "http_request", fake_http)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: calls.setdefault("cache_cleared", True))

    rc = cli.cmd_claim(_Args(credentials=str(creds_file), checkout_code="cs_test_a1B2c3D4e5F6g7H8"))
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["method"] == "POST" and calls["path"] == "/api/plugin/stripe-claim"
    assert calls["body"]["cs"] == "cs_test_a1B2c3D4e5F6g7H8"
    assert calls["body"]["account_id"] == ACCT
    assert calls["body"]["session_token"] == "22222222-3333-4444-8555-666666666666"  # bearer-bound
    assert "PRO" in out
    assert calls.get("cache_cleared") is True
    # local tier hint synced through the atomic writer
    assert json.loads(creds_file.read_text())["tier"] == "pro"


def test_claim_maps_server_errors(creds_file, capsys, monkeypatch):
    def fake_http(method, path, *, body=None, **kw):
        raise cli.HttpError(409, {"error": "this purchase was already claimed to a different account."}, "u")

    monkeypatch.setattr(cli, "http_request", fake_http)
    rc = cli.cmd_claim(_Args(credentials=str(creds_file), checkout_code="cs_test_a1B2c3D4e5F6g7H8"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "different account" in out


def test_claim_requires_activation(tmp_path, capsys):
    rc = cli.cmd_claim(_Args(credentials=str(tmp_path / "missing.json"), checkout_code="cs_test_a1B2c3D4e5F6g7H8"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "sibyl init" in out
