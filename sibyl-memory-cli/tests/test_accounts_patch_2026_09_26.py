"""cli 0.5.0: email-mandatory accounts + the one coordinated plugin patch (2026-09-26).

Spec: email-mandatory-plugin-patch-spec-2026-09-26.md sections 4.1-4.11, tests 14-21.

Every server response below is a RECORDED SHAPE, copied from the api handlers:
  OLD_API_*  the api at sandbox HEAD 09ff953 (phase 3-5 + fix batches, before this
             spec): `features` has four keys, no `account`, approve_url is `?s=`.
  OLDER_*    an api from before phase 3: no `features` at all.
  NEW_API_*  the contract this spec adds (3.2 email-verify, 3.4 account/features,
             3.5 `?r=` handle).
Nothing here touches the network: `cli.http_request` is replaced by a fake that
routes by method + path and records every call.
"""
from __future__ import annotations

import builtins
import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

from sibyl_memory_cli import cli


ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "22222222-3333-4444-8555-666666666666"
ACCOUNT_ORIGIN = "https://account.sibyllabs.org"

# ---- recorded server shapes ----------------------------------------------

OLD_API_FEATURES = {"device_approval": False, "email_signin": False,
                    "web_dashboard_url": None, "cli_login": False}
NEW_API_FEATURES_ALL_ON = {"device_approval": True, "email_signin": True, "cli_email_verify": True,
                           "web_checkout": True, "web_dashboard_url": ACCOUNT_ORIGIN, "cli_login": False}
NEW_API_FEATURES_OFF = {"device_approval": False, "email_signin": False, "cli_email_verify": False,
                        "web_checkout": False, "web_dashboard_url": None, "cli_login": False}

ACCESS_BASE = {"ok": True, "account_id": ACCT, "wallet": None, "session_verified": True,
               "tier": "free", "source": "free", "cap_bytes": 5242880,
               "latest_versions": {"cli": "0.4.1"}}
OLDER_ACCESS = dict(ACCESS_BASE)                                   # no features
OLD_API_ACCESS = dict(ACCESS_BASE, features=OLD_API_FEATURES)       # features, no account


def new_access(features=None, **account):
    acc = {"claimed": False, "email_verified": False, "complete": False, "email_required": False,
           "grace_until": None, "enforced": False, "email_masked": None}
    acc.update(account)
    return dict(ACCESS_BASE, features=features or NEW_API_FEATURES_ALL_ON, account=acc)


def bound_check(email=None, wallet=None, account=None):
    body = {"bound": True, "credentials": {
        "account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": email, "wallet": wallet,
        "issued_at": "2026-09-26T00:00:00Z", "bearer_token": BEARER,
        "signature": "sig", "signed_at": "2026-09-26T00:00:00Z"}}
    if account is not None:
        body["account"] = account
    return body


# ---- fakes ---------------------------------------------------------------

class FakeServer:
    """Routes (METHOD, path-prefix) -> a list of responses consumed in order
    (the last one repeats). A response that is an HttpError is raised."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []

    def on(self, method, prefix, *responses):
        self.routes[(method, prefix)] = list(responses)
        return self

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers or {}})
        for (m, prefix), queue in self.routes.items():
            if m == method and path.startswith(prefix):
                resp = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(resp, cli.HttpError):
                    raise resp
                return json.loads(json.dumps(resp))
        raise cli.HttpError(404, {"error": "unparseable response body"}, f"https://api{path}")

    def paths(self):
        return [(c["method"], c["path"].split("?")[0]) for c in self.calls]

    def calls_to(self, prefix):
        return [c for c in self.calls if c["path"].startswith(prefix)]


def err(status, body=None, headers=None):
    return cli.HttpError(status, body if body is not None else {}, "https://api/x", headers)


@pytest.fixture()
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    return s


@pytest.fixture()
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: urls.append(url) or True)
    return urls


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    monkeypatch.delenv("SIBYL_NONINTERACTIVE", raising=False)


@pytest.fixture()
def no_input(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("input() must not be called here")
    monkeypatch.setattr(builtins, "input", boom)


def feed_input(monkeypatch, *answers):
    q = list(answers)
    prompts: list[str] = []

    def fake(prompt=""):
        prompts.append(prompt)
        if not q:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return q.pop(0)
    monkeypatch.setattr(builtins, "input", fake)
    return prompts


def write_creds(path: Path, **over):
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "buyer@example.com",
         "wallet": None, "issued_at": "2026-09-26T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    c.update(over)
    path.write_text(json.dumps(c))
    return path


@pytest.fixture()
def creds(tmp_path):
    return write_creds(tmp_path / "credentials.json")


def run(argv_tail, cred_path, tmp_path=None):
    base = ["--credentials", str(cred_path)]
    if tmp_path is not None:
        base += ["--db", str(tmp_path / "memory.db"), "--tier-cache", str(tmp_path / "tier_cache.json")]
    return cli.main(base + argv_tail)


def init(tmp_path, *extra):
    cred = tmp_path / "credentials.json"
    return cli.main(["--credentials", str(cred), "init", "--force", *extra]), cred


def new_rid():
    return "0123456789abcdef" * 4


# =========================================================================
# 4.1 plumbing
# =========================================================================

def test_approve_url_ok_only_account_origin_with_r_handle():
    assert cli._approve_url_ok(f"{ACCOUNT_ORIGIN}/activate?r={new_rid()}")
    assert not cli._approve_url_ok(f"{ACCOUNT_ORIGIN}/activate?s=11111111-2222-4333-8444-555555555555")
    assert not cli._approve_url_ok(f"https://evil.example/activate?r={new_rid()}")
    assert not cli._approve_url_ok(f"{ACCOUNT_ORIGIN}.evil.example/activate?r={new_rid()}")
    assert not cli._approve_url_ok(f"{ACCOUNT_ORIGIN}/activate?r={new_rid()}&next=https://evil")
    assert not cli._approve_url_ok(f"{ACCOUNT_ORIGIN}/activate?r={new_rid().upper()}")
    assert not cli._approve_url_ok(None)


def test_on_account_origin_rejects_lookalike_hosts():
    assert cli._on_account_origin(ACCOUNT_ORIGIN)
    assert cli._on_account_origin(ACCOUNT_ORIGIN + "/billing")
    assert not cli._on_account_origin(ACCOUNT_ORIGIN + ".evil.example")
    assert not cli._on_account_origin("http://account.sibyllabs.org")
    assert not cli._on_account_origin(None)


def test_session_ref_matches_server_sessionref():
    sid = "AbCdEf01-2222-4333-8444-555555555555"
    assert cli._session_ref(sid) == hashlib.sha256(sid.lower().encode()).hexdigest()


def test_is_interactive_respects_tty_and_env(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    assert cli._is_interactive() is True
    monkeypatch.setenv("SIBYL_NONINTERACTIVE", "1")
    assert cli._is_interactive() is False
    monkeypatch.delenv("SIBYL_NONINTERACTIVE")
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    assert cli._is_interactive() is False


def test_fetch_access_sends_bearer_and_degrades(creds, server):
    server.on("POST", "/api/plugin/access", err(503, {"error": "session store unavailable"}))
    assert cli._fetch_access(json.loads(creds.read_text())) is None
    call = server.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {BEARER}"
    assert call["body"] == {"account_id": ACCT, "session_token": BEARER}


def test_http_request_carries_retry_after_header(monkeypatch):
    import io
    import urllib.error

    def raise_429(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "120"},
                                     io.BytesIO(b'{"error":"slow down"}'))
    monkeypatch.setattr(cli.urllib.request, "urlopen", raise_429)
    with pytest.raises(cli.HttpError) as ei:
        cli.http_request("POST", "/api/plugin/email-verify/request", body={})
    assert ei.value.status == 429
    assert ei.value.headers.get("Retry-After") == "120"
    assert cli._email_error_message(ei.value) == "too many requests. try again in 2 minutes."


# =========================================================================
# 14. init
# =========================================================================

def test_init_features_absent_no_hint_no_prompt_same_credentials(tmp_path, server, opened, no_input, capsys):
    """OLDER api: no features, no account. Output and file as 0.4.1."""
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(email="u@example.com"))
    rc, cred = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "approve this" not in out and "sibyl email" not in out and "?r=" not in out
    persisted = json.loads(cred.read_text())
    assert persisted == {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
                         "wallet": None, "issued_at": "2026-09-26T00:00:00Z", "bearer_token": BEARER,
                         "session_token": BEARER}
    assert [p for p in server.paths()] == [("POST", "/api/plugin/session-init"), ("GET", "/api/plugin/check")]


def test_init_old_api_features_wallet_bind_prints_nothing_new(tmp_path, server, opened, no_input, capsys):
    """OLD api (features with device_approval false, no account): the wallet nag
    must not name `sibyl email add`, which that api does not have."""
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": OLD_API_FEATURES})
    server.on("GET", "/api/plugin/check", bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678"))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "add an email" not in out and "sibyl email" not in out


def test_init_device_approval_hint_uses_sha256_handle(tmp_path, server, opened, no_input, capsys, monkeypatch):
    fixed = "abcdef01-2222-4333-8444-555555555555"
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: fixed)
    server.on("POST", "/api/plugin/session-init",
              {"pairing_ttl_seconds": 300, "features": dict(OLD_API_FEATURES, device_approval=True)})
    server.on("GET", "/api/plugin/check", bound_check(email="u@example.com"))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    ref = hashlib.sha256(fixed.encode()).hexdigest()
    assert f"{ACCOUNT_ORIGIN}/activate?r={ref}" in out
    # P1 (web-first spec 9.4): the account may exist only on the website
    assert "already have a Sibyl Memory account (website or another computer)? approve this one at" in out
    assert "already set up with this email on another computer?" not in out
    assert f"?s={fixed}" not in out
    # the hint line carries the handle, never the session id (the only place
    # the id appears is the pre-existing auth.sibyllabs.org/<id> Opening row)
    hint = next(ln for ln in out.splitlines() if "already have a Sibyl Memory account" in ln)
    assert fixed not in hint


def test_init_approval_sequence_prints_url_once_and_switches_label(tmp_path, server, opened, no_input, capsys):
    url = f"{ACCOUNT_ORIGIN}/activate?r={new_rid()}"
    waiting = {"bound": False, "expires_at": "x",
               "approval": {"required": True, "approved": False, "declined": False, "approve_url": url}}
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_OFF})
    server.on("GET", "/api/plugin/check",
              {"bound": False, "expires_at": "x"}, waiting, waiting,
              bound_check(email="u@example.com", account={"claimed": True, "email_verified": True,
                                                         "complete": True, "email_required": False}))
    rc, cred = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count(f"approve this computer at {url}") == 1
    assert "waiting for approval at account.sibyllabs.org" in out
    assert "watching the network for your bind" in out
    assert json.loads(cred.read_text())["session_token"] == BEARER
    # complete account: no nag
    assert "sibyl email" not in out


def test_init_declined_exits_2_and_writes_nothing(tmp_path, server, opened, no_input, capsys):
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_OFF})
    server.on("GET", "/api/plugin/check", {"bound": False, "approval": {
        "required": True, "approved": False, "declined": True, "approve_url": f"{ACCOUNT_ORIGIN}/activate?r={new_rid()}"}})
    rc, cred = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "This computer was not approved (Not me was chosen)." in out
    assert "sibyl init --force" in out
    assert not cred.exists()


@pytest.mark.parametrize("bad_url", [
    "https://evil.example/activate?r=" + "0" * 64,
    f"{ACCOUNT_ORIGIN}/activate?s=11111111-2222-4333-8444-555555555555",   # OLD api shape (?s=)
    f"{ACCOUNT_ORIGIN}.evil.example/activate?r=" + "0" * 64,
])
def test_init_never_prints_foreign_or_legacy_approve_url(tmp_path, server, opened, no_input, capsys, bad_url):
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check",
              {"bound": False, "approval": {"required": True, "approved": False, "declined": False,
                                            "approve_url": bad_url}},
              bound_check(email="u@example.com"))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert bad_url not in out
    assert "approve this computer at" not in out
    assert all(u.startswith("https://auth.sibyllabs.org/") for u in opened)


def test_init_wallet_bind_interactive_adds_email(tmp_path, server, opened, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    prompts = feed_input(monkeypatch, "me@example.com", "123 456")
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678",
                          account={"claimed": True, "email_verified": False, "complete": False, "email_required": True}))
    server.on("POST", "/api/plugin/email-verify/request",
              {"ok": True, "to_masked": "m***@e***.com", "expires_in_seconds": 600})
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "me@example.com", "email_verified": True, "claimed": True,
               "sessions_revoked": 0, "claims_attached": 1, "account": {},
               "credentials": signed_creds(email="me@example.com", wallet="0x1234567890abcdef1234567890abcdef12345678")})
    rc, cred = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "add an email to finish your account" in out
    assert "receipts, security notices and purchases need one. local memory works either way." in out
    assert prompts[0] == "  add an email now? (address, or Enter to skip): "
    req = server.calls_to("/api/plugin/email-verify/request")[0]
    assert req["body"] == {"account_id": ACCT, "email": "me@example.com"}
    assert req["headers"]["Authorization"] == f"Bearer {BEARER}"
    conf = server.calls_to("/api/plugin/email-verify/confirm")[0]
    assert conf["body"] == {"code": "123456"}
    assert "Email confirmed. Your account is complete." in out
    assert "1 purchase(s) attached" in out
    assert json.loads(cred.read_text())["email"] == "me@example.com"
    # the wiring line (nothing wired) still prints after the email step
    assert out.index("Email confirmed") < out.index("wire it into your agent: sibyl setup")


def test_init_non_tty_no_prompt(tmp_path, server, opened, no_input, capsys):
    """capsys stdout is not a TTY: the real _is_interactive() answers False."""
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678",
                          account={"claimed": True, "email_verified": False}))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "sibyl email add you@example.com" in out
    assert not server.calls_to("/api/plugin/email-verify")


def test_init_no_email_flag_skips_prompt(tmp_path, server, opened, no_input, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678",
                          account={"claimed": True, "email_verified": False}))
    rc, _ = init(tmp_path, "--no-email")
    assert rc == 0
    assert "add an email to finish your account" in capsys.readouterr().out
    assert not server.calls_to("/api/plugin/email-verify")


def test_init_email_bind_unverified_sends_code_without_asking(tmp_path, server, opened, capsys, monkeypatch):
    # F-1a (C2): no "confirm it now?" question; the code goes out and the
    # only prompt is for the code.
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    prompts = feed_input(monkeypatch, "654321")
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(email="label@example.com", account={"claimed": False, "email_verified": False}))
    server.on("POST", "/api/plugin/email-verify/request",
              {"ok": True, "to_masked": "l***@e***.com", "expires_in_seconds": 600})
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "label@example.com", "email_verified": True, "claimed": False,
               "sessions_revoked": 2, "claims_attached": 0})
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "confirm your email to finish your account: sibyl email verify" in out
    assert prompts == ["  code from the email: "]
    assert not any("confirm it now?" in p for p in prompts)
    assert server.calls_to("/api/plugin/email-verify/request")[0]["body"] == {"account_id": ACCT}
    assert "2 other computer(s) signed out" in out
    assert "signing in on the account page signs this computer out" not in out


def test_init_email_bind_enter_at_code_prompt_skips_and_warns(tmp_path, server, opened, capsys, monkeypatch):
    # F-1a (C2): Enter at the code prompt skips; the consequence is said.
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "")
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(email="label@example.com", account={"email_verified": False}))
    server.on("POST", "/api/plugin/email-verify/request",
              {"ok": True, "to_masked": "l***@e***.com", "expires_in_seconds": 600})
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert len(server.calls_to("/api/plugin/email-verify/request")) == 1
    assert not server.calls_to("/api/plugin/email-verify/confirm")
    assert ("until you confirm your email here, signing in on the account page signs this computer "
            "out. confirm later with: sibyl email verify") in out


def test_init_email_bind_non_interactive_sends_nothing_and_warns(tmp_path, server, opened, no_input,
                                                                capsys, monkeypatch):
    # F-1a (C2): no terminal (an agent-driven run): no code is mailed
    # unasked, and the consequence of leaving the email unconfirmed is said.
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(email="label@example.com", account={"email_verified": False}))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert not server.calls_to("/api/plugin/email-verify")
    assert ("until you confirm your email here, signing in on the account page signs this computer "
            "out. confirm later with: sibyl email verify") in out


def test_init_email_flag_off_warns_too(tmp_path, server, opened, no_input, capsys, monkeypatch):
    # F-1a (C2): cli_email_verify off is a skip branch as well.
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_OFF})
    server.on("GET", "/api/plugin/check",
              bound_check(email="label@example.com", account={"email_verified": False}))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "signing in on the account page signs this computer out" in out


def test_init_email_verified_says_nothing_about_sign_out(tmp_path, server, opened, no_input, capsys):
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(email="label@example.com", account={"email_verified": True}))
    rc, _ = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and "signs this computer out" not in out


def test_init_prompt_failure_not_open_prints_web_instruction_and_exits_0(tmp_path, server, opened, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "me@example.com")
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678", account={"email_verified": False}))
    server.on("POST", "/api/plugin/email-verify/request",
              err(503, {"code": "not_open", "error": "email confirmation from the terminal is not open yet. add your email at https://account.sibyllabs.org instead."}))
    rc, cred = init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("email confirmation from the terminal is not open yet. add your email at "
            "https://account.sibyllabs.org (sign in with the email or wallet you used here).") in out
    assert cred.exists()
    assert "wire it into your agent: sibyl setup" in out


def test_init_cli_email_verify_off_nags_without_prompt(tmp_path, server, opened, no_input, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_OFF})
    server.on("GET", "/api/plugin/check",
              bound_check(wallet="0x1234567890abcdef1234567890abcdef12345678", account={"email_verified": False}))
    rc, _ = init(tmp_path)
    assert rc == 0
    assert "add an email to finish your account" in capsys.readouterr().out


def test_init_account_never_persisted_into_credentials(tmp_path, server, opened, no_input):
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300, "features": NEW_API_FEATURES_ALL_ON})
    server.on("GET", "/api/plugin/check",
              bound_check(email="u@example.com", account={"email_verified": True, "claimed": True}))
    rc, cred = init(tmp_path)
    assert rc == 0
    persisted = json.loads(cred.read_text())
    assert "account" not in persisted and "signature" not in persisted


# =========================================================================
# 15. sibyl email
# =========================================================================

def test_email_state_confirmed(creds, server, capsys):
    server.on("POST", "/api/plugin/access", new_access(email_verified=True, claimed=True, email_masked="b***@e***.com"))
    assert run(["email"], creds) == 0
    out = capsys.readouterr().out
    assert "confirmed b***@e***.com" in out


def test_email_state_not_confirmed(creds, server, capsys):
    server.on("POST", "/api/plugin/access", new_access(email_verified=False, email_masked="b***@e***.com"))
    assert run(["email"], creds) == 0
    out = capsys.readouterr().out
    assert "not confirmed b***@e***.com" in out and "run sibyl email verify" in out


def test_email_state_none(tmp_path, server, capsys):
    cred = write_creds(tmp_path / "c.json", email=None, wallet="0x1234567890abcdef1234567890abcdef12345678")
    server.on("POST", "/api/plugin/access", new_access(email_verified=False, claimed=True))
    assert run(["email"], cred) == 0
    out = capsys.readouterr().out
    assert "none" in out and "run sibyl email add <address>" in out


@pytest.mark.parametrize("body", [OLD_API_ACCESS, OLDER_ACCESS])
def test_email_state_old_server(creds, server, capsys, body):
    server.on("POST", "/api/plugin/access", body)
    assert run(["email"], creds) == 0
    assert "this server does not report email state yet." in capsys.readouterr().out


def test_email_no_creds_exit_0_for_state_1_for_actions(tmp_path, capsys):
    missing = tmp_path / "none.json"
    assert run(["email"], missing) == 0
    assert run(["email", "verify"], missing) == 1
    assert "Not activated." in capsys.readouterr().out


def test_email_add_non_interactive_requests_and_explains(creds, server, no_input, capsys):
    server.on("POST", "/api/plugin/email-verify/request",
              {"ok": True, "to_masked": "n***@e***.com", "expires_in_seconds": 900})
    assert run(["email", "add", "new@example.com"], creds) == 0
    out = capsys.readouterr().out
    call = server.calls[0]
    assert call["path"] == "/api/plugin/email-verify/request"
    assert call["body"] == {"account_id": ACCT, "email": "new@example.com"}
    # GATE-15: _bearer_headers refreshes first when no usable token is held
    # (the conftest routes that refresh to the test signer), so the header set
    # is the bearer plus, at most, the freshly issued entitlement.
    assert call["headers"]["Authorization"] == f"Bearer {BEARER}"
    assert set(call["headers"]) <= {"Authorization", "X-Sibyl-Entitlement"}
    assert "code sent to n***@e***.com. it expires in 15 minutes." in out
    assert "then run: sibyl email confirm <code>" in out


def test_email_add_rejects_non_address(creds, server, capsys):
    assert run(["email", "add", "not-an-address"], creds) == 1
    assert not server.calls


def test_email_verify_omits_email_key(creds, server, no_input):
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True, "to_masked": "b***@e***.com"})
    assert run(["email", "verify"], creds) == 0
    assert server.calls[0]["body"] == {"account_id": ACCT}


def signed_creds(**over):
    """The credentials POST /email-verify/confirm answers (api RB-F1): re-signed
    by the /check builder, no session_token / bearer_token."""
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "confirmed@example.com",
         "wallet": None, "issued_at": "2026-09-26T12:00:00.000Z", "schema_version": 3,
         "signature": "b" * 64, "signed_at": "2026-09-26T12:00:00.000Z"}
    c.update(over)
    return c


def test_email_confirm_success_writes_server_credentials_and_clears_cache(creds, server, capsys, monkeypatch):
    """Release-blocker F1: the file is rebuilt from the server's re-signed
    credentials through the CLI-15 allowlist; this computer's bearer is kept;
    an old signature and unknown keys do not survive."""
    write_creds(creds, signature="a" * 64, signed_at="2026-05-01T00:00:00Z", junk="x")
    cleared = []
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: cleared.append(True))
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "confirmed@example.com", "email_verified": True, "claimed": True,
               "sessions_revoked": 3, "claims_attached": 2, "account": {"complete": True},
               "credentials": signed_creds(tier="pro")})
    assert run(["email", "confirm", "123456"], creds) == 0
    out = capsys.readouterr().out
    call = server.calls[0]
    assert call["body"] == {"code": "123456"}
    # GATE-15: _bearer_headers refreshes first when no usable token is held
    # (the conftest routes that refresh to the test signer), so the header set
    # is the bearer plus, at most, the freshly issued entitlement.
    assert call["headers"]["Authorization"] == f"Bearer {BEARER}"
    assert set(call["headers"]) <= {"Authorization", "X-Sibyl-Entitlement"}
    assert "Email confirmed. Your account is complete." in out
    assert "2 purchase(s) attached" in out and "3 other computer(s) signed out" in out
    persisted = json.loads(creds.read_text())
    assert persisted == {"account_id": ACCT, "tenant_id": ACCT, "tier": "pro", "email": "confirmed@example.com",
                         "wallet": None, "issued_at": "2026-09-26T12:00:00.000Z",
                         "session_token": BEARER, "bearer_token": BEARER}
    assert cleared == [True]


def test_email_confirm_without_server_credentials_leaves_signed_file_untouched(creds, server, capsys):
    """Release-blocker F1: an api that answers no credentials (older api) must
    not make the CLI edit email under the old signature (adapters send it with
    the claim; check-write would log credentials_tamper_suspected)."""
    write_creds(creds, signature="a" * 64, signed_at="2026-05-01T00:00:00Z")
    before = creds.read_bytes()
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "confirmed@example.com", "email_verified": True, "claimed": True,
               "sessions_revoked": 0, "claims_attached": 0})
    assert run(["email", "confirm", "123456"], creds) == 0
    assert "Email confirmed. Your account is complete." in capsys.readouterr().out
    assert creds.read_bytes() == before


def test_email_confirm_credentials_for_another_account_are_ignored(creds, server):
    before = creds.read_bytes()
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "x@example.com", "credentials": signed_creds(account_id="99999999-2222-4333-8444-555555555555")})
    assert run(["email", "confirm", "123456"], creds) == 0
    assert creds.read_bytes() == before


def test_email_confirm_zero_counts_print_no_extra_lines(creds, server, capsys):
    server.on("POST", "/api/plugin/email-verify/confirm",
              {"ok": True, "email": "buyer@example.com", "sessions_revoked": 0, "claims_attached": 0})
    assert run(["email", "confirm", "123456"], creds) == 0
    out = capsys.readouterr().out
    assert "attached" not in out and "signed out" not in out


def test_email_confirm_bad_format_no_call(creds, server, capsys):
    assert run(["email", "confirm", "12ab"], creds) == 1
    assert not server.calls


ORIGIN_ERR = "https://account.sibyllabs.org"


@pytest.mark.parametrize("status,body,headers,expected,rc", [
    (503, {"code": "not_open", "error": "x"}, None,
     "email confirmation from the terminal is not open yet. add your email at https://account.sibyllabs.org (sign in with the email or wallet you used here).", 2),
    (409, {"code": "already_verified", "error": "this account already has a confirmed email. to add another, run sibyl email add <address>."}, None,
     "this account already has a confirmed email. to add another, run sibyl email add <address>.", 1),
    (409, {"code": "email_taken", "error": f"that email already belongs to another Sibyl Memory account. sign in at {ORIGIN_ERR} with it, or use a different address."}, None,
     f"that email already belongs to another Sibyl Memory account. sign in at {ORIGIN_ERR} with it, or use a different address.", 1),
    (503, {"code": "mail_unavailable", "error": "we could not send the code right now. try again in a few minutes."}, None,
     "we could not send the code right now. try again in a few minutes.", 2),
    (400, {"code": "email_required_in_body", "error": "give the address: sibyl email add you@example.com"}, None,
     "give the address: sibyl email add you@example.com", 1),
    (423, {"code": "identity_frozen", "error": "changes to sign-in methods are paused on this account while we review a recent security report. email sibyl@sibyllabs.org."}, None,
     "changes to sign-in methods are paused on this account while we review a recent security report. email sibyl@sibyllabs.org.", 1),
    (401, {"error": "session not found, revoked, or expired"}, None, "session expired. re-run sibyl init.", 1),
    (403, {"error": "account under review", "reason": "tamper_flag_active", "contact": "sibyl@sibyllabs.org"}, None,
     "this account is under review. email sibyl@sibyllabs.org.", 1),
    (429, {"error": "rate limited"}, {"Retry-After": "300"}, "too many requests. try again in 5 minutes.", 1),
    (429, {"error": "rate limited"}, None, "too many requests. try again in a few minutes.", 1),
    (0, {"error": "timed out"}, None, "could not reach the server.", 2),
    (404, {"error": "unparseable response body"}, None,
     "email confirmation is not available on this server yet. add your email at https://account.sibyllabs.org instead.", 2),
    (500, {"error": "boom"}, None, "server error: 500", 2),
])
def test_email_request_error_map(creds, server, capsys, status, body, headers, expected, rc):
    server.on("POST", "/api/plugin/email-verify/request", err(status, body, headers))
    assert run(["email", "verify"], creds) == rc
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("status,body,expected", [
    (401, {"code": "wrong_code", "error": "that code is not right.", "attempts_remaining": 3},
     "that code is not right (3 attempts left)."),
    (410, {"code": "expired", "error": "that code expired. run sibyl email verify for a new one."},
     "that code expired. run sibyl email verify for a new one."),
])
def test_email_confirm_error_map(creds, server, capsys, status, body, expected):
    server.on("POST", "/api/plugin/email-verify/confirm", err(status, body))
    assert run(["email", "confirm", "123456"], creds) == 1
    assert expected in capsys.readouterr().out
    assert json.loads(creds.read_text())["email"] == "buyer@example.com"   # unchanged


def test_email_confirm_prompt_retries_on_wrong_code(creds, server, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "111111", "222222")
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True, "to_masked": "b***@e***.com"})
    server.on("POST", "/api/plugin/email-verify/confirm",
              err(401, {"code": "wrong_code", "error": "that code is not right.", "attempts_remaining": 4}),
              {"ok": True, "email": "buyer@example.com", "sessions_revoked": 0, "claims_attached": 0})
    assert run(["email", "verify"], creds) == 0
    out = capsys.readouterr().out
    assert "that code is not right (4 attempts left)." in out
    assert "Email confirmed." in out
    assert len(server.calls_to("/api/plugin/email-verify/confirm")) == 2


def test_email_confirm_prompt_stops_after_three_wrong(creds, server, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "111111", "222222", "333333")
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True, "to_masked": "b***@e***.com"})
    server.on("POST", "/api/plugin/email-verify/confirm",
              err(401, {"code": "wrong_code", "error": "x", "attempts_remaining": 1}))
    assert run(["email", "verify"], creds) == 1
    assert len(server.calls_to("/api/plugin/email-verify/confirm")) == 3
    assert "run sibyl email verify for a new code." in capsys.readouterr().out


def test_email_confirm_prompt_expired_stops_immediately(creds, server, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "111111")
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True})
    server.on("POST", "/api/plugin/email-verify/confirm",
              err(410, {"code": "expired", "error": "that code expired. run sibyl email verify for a new one."}))
    assert run(["email", "verify"], creds) == 1
    assert len(server.calls_to("/api/plugin/email-verify/confirm")) == 1


def test_email_confirm_prompt_enter_skips(creds, server, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "")
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True})
    assert run(["email", "verify"], creds) == 1
    assert not server.calls_to("/api/plugin/email-verify/confirm")
    assert "sibyl email confirm <code>" in capsys.readouterr().out


# =========================================================================
# 16. claim
# =========================================================================

CS = "cs_test_a1B2c3D4e5F6g7H8"
UNCLAIMED_ERR = ("this account is not confirmed yet. sign in once at https://account.sibyllabs.org with the email "
                 "(or wallet) you used for `sibyl init`, then run `sibyl claim` again. your purchase is safe and "
                 "still waiting for you.")
EMAIL_REQ_ERR = ("add and confirm an email before buying: run `sibyl email add you@example.com`, or add it at "
                 "https://account.sibyllabs.org/complete. your purchase is safe.")


_HANDOFF_KEYS = ("later: ", "ask your human: ", "next: ", "waiting: ")


def _claim_lines(out):
    """The claim copy, without the agent handoff block (asserted on its own in
    test_handoff_rows_2026_10_03.py)."""
    return [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith(_HANDOFF_KEYS)]


def test_claim_unclaimed_account(creds, server, capsys):
    server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "unclaimed_account", "error": UNCLAIMED_ERR}))
    assert run(["claim", CS], creds) == 1
    lines = _claim_lines(capsys.readouterr().out)
    assert lines[0].endswith("Claim failed (409).")
    assert lines[1] == UNCLAIMED_ERR
    assert lines[2] == "fastest path: sibyl email verify (confirms the email you used here and keeps"
    assert lines[3] == f"this computer signed in), then sibyl claim {CS} again."
    assert len(lines) == 4


def test_claim_unclaimed_account_without_email_suggests_email_add(tmp_path, server, capsys):
    """Release-blocker F7: nothing to verify without an email on file."""
    cred = write_creds(tmp_path / "credentials.json", email=None, wallet="0x1234567890abcdef1234567890abcdef12345678")
    server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "unclaimed_account", "error": UNCLAIMED_ERR}))
    assert run(["claim", CS], cred) == 1
    lines = _claim_lines(capsys.readouterr().out)
    assert lines[2] == "fastest path: sibyl email add you@example.com (confirms an email from this"
    assert lines[3] == f"computer and keeps it signed in), then sibyl claim {CS} again."
    assert "sibyl email verify" not in "\n".join(lines)


def _assert_adapters_send_no_claim(persisted: dict):
    """The adapters (client _capcheck, hermes provider, mcp server) attach
    credentials_claim/credentials_signature only when the file carries a
    truthy `signature`; an unsigned file sends no claim."""
    assert not persisted.get("signature")
    assert "signed_at" not in persisted


def test_claim_success_on_a_signed_file_drops_the_signature_and_writes_the_tier(creds, server, capsys):
    """Release-blocker F1 (same class) + hardening L2: tier is a signed field.
    The tier hint is written (no stale tier next to the new server tier) and
    the old signature goes with the edit, so no adapter sends a claim that
    would read as tampering. Other fields are kept."""
    write_creds(creds, signature="a" * 64, signed_at="2026-05-01T00:00:00Z")
    server.on("POST", "/api/plugin/stripe-claim", {"ok": True, "tier": "pro", "expires_at": "2026-10-29"})
    assert run(["claim", CS], creds) == 0
    assert "Purchase linked. Tier: PRO" in capsys.readouterr().out
    persisted = json.loads(creds.read_text())
    assert persisted["tier"] == "pro"
    _assert_adapters_send_no_claim(persisted)
    for k in ("account_id", "tenant_id", "email", "issued_at", "session_token", "bearer_token"):
        assert k in persisted


def test_claim_success_updates_an_unsigned_file(creds, server):
    server.on("POST", "/api/plugin/stripe-claim", {"ok": True, "tier": "pro"})
    assert run(["claim", CS], creds) == 0
    assert json.loads(creds.read_text())["tier"] == "pro"


def test_claim_email_required(creds, server, capsys):
    server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "email_required", "error": EMAIL_REQ_ERR}))
    assert run(["claim", CS], creds) == 1
    lines = _claim_lines(capsys.readouterr().out)
    assert lines[1] == EMAIL_REQ_ERR
    assert lines[2] == f"then run sibyl claim {CS} again."
    assert len(lines) == 3


def test_claim_other_409_verbatim_only(creds, server, capsys):
    msg = "this purchase was already claimed to a different account. if that was not you, email sibyl@sibyllabs.org immediately."
    server.on("POST", "/api/plugin/stripe-claim", err(409, {"error": msg}))
    assert run(["claim", CS], creds) == 1
    lines = _claim_lines(capsys.readouterr().out)
    assert lines[1:] == [msg]


def test_claim_403_and_0_hints_unchanged(creds, server, capsys):
    server.on("POST", "/api/plugin/stripe-claim", err(403, {"error": "session does not match the account"}))
    assert run(["claim", CS], creds) == 1
    assert "This session does not match the account. Run `sibyl init`, then retry." in capsys.readouterr().out
    server.on("POST", "/api/plugin/stripe-claim", err(0, {"error": "timed out"}))
    assert run(["claim", CS], creds) == 1
    out = capsys.readouterr().out
    assert "Claim failed (network)." in out and "Could not reach the server." in out


def test_claim_404(creds, server, capsys):
    server.on("POST", "/api/plugin/stripe-claim", err(404, {"error": "no claimable purchase for this checkout."}))
    assert run(["claim", CS], creds) == 1
    lines = _claim_lines(capsys.readouterr().out)
    assert lines[0].endswith("Claim failed (404).")
    assert lines[1:] == ["no claimable purchase for this checkout."]


# =========================================================================
# 17. status
# =========================================================================

def _status_out(creds, server, tmp_path, capsys, body):
    server.on("POST", "/api/plugin/access", body)
    assert run(["status"], creds, tmp_path) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("body", [OLDER_ACCESS, OLD_API_ACCESS])
def test_status_old_api_byte_identical(creds, server, tmp_path, capsys, monkeypatch, body):
    new = _status_out(creds, server, tmp_path, capsys, body)
    monkeypatch.setattr(cli, "_print_account_rows", lambda *a, **k: None)
    old = _status_out(creds, server, tmp_path, capsys, body)
    assert new == old
    assert "Dashboard" not in new and "incomplete" not in new
    assert new.count("Email") == 1          # the local row only


def test_status_email_rows(creds, server, tmp_path, capsys):
    out = _status_out(creds, server, tmp_path, capsys,
                      new_access(email_verified=True, claimed=True, complete=True, email_masked="b***@e***.com"))
    assert "confirmed · b***@e***.com" in out
    assert "incomplete" not in out
    out = _status_out(creds, server, tmp_path, capsys, new_access(email_verified=False, email_masked="b***@e***.com"))
    assert "not confirmed · run sibyl email verify" in out
    nomail = write_creds(tmp_path / "w.json", email=None)
    out = _status_out(nomail, server, tmp_path, capsys, new_access(email_verified=False))
    assert "none · run sibyl email add you@example.com" in out


def test_status_account_row_grace_and_enforced(creds, server, tmp_path, capsys):
    out = _status_out(creds, server, tmp_path, capsys,
                      new_access(email_required=True, grace_until="2026-10-10T00:00:00.000Z"))
    assert "incomplete · email required by 2026-10-10" in out
    assert "purchases need" not in out
    out = _status_out(creds, server, tmp_path, capsys, new_access(email_required=True))
    assert re.search(r"Account\s+.*incomplete\s*$", out, re.M)
    out = _status_out(creds, server, tmp_path, capsys,
                      new_access(email_required=True, enforced=True, grace_until="2026-10-10T00:00:00Z"))
    assert ("incomplete · email required by 2026-10-10 · purchases need a confirmed email "
            "(local memory unaffected)") in out


def test_status_dashboard_row_only_for_account_origin(creds, server, tmp_path, capsys):
    out = _status_out(creds, server, tmp_path, capsys, new_access())
    assert re.search(r"Dashboard\s+.*https://account\.sibyllabs\.org", out)
    evil = dict(NEW_API_FEATURES_ALL_ON, web_dashboard_url="https://account.sibyllabs.org.evil.example")
    out = _status_out(creds, server, tmp_path, capsys, new_access(features=evil))
    assert "Dashboard" not in out
    out = _status_out(creds, server, tmp_path, capsys, new_access(features=NEW_API_FEATURES_OFF))
    assert "Dashboard" not in out


# =========================================================================
# 18. dashboard
# =========================================================================

def test_dashboard_opens_url_without_query(creds, server, opened, capsys):
    server.on("POST", "/api/plugin/access", new_access(email_verified=True))
    assert run(["dashboard"], creds) == 0
    assert opened == [ACCOUNT_ORIGIN]
    out = capsys.readouterr().out
    assert "sign in with your confirmed email (or your wallet)." in out
    assert BEARER not in out and all(BEARER not in u and "?" not in u for u in opened)


def test_dashboard_unverified_adds_hint(creds, server, opened, capsys):
    server.on("POST", "/api/plugin/access", new_access(email_verified=False))
    assert run(["dashboard"], creds) == 0
    out = capsys.readouterr().out
    assert "your account has no confirmed email yet: sign in with your wallet and add one there," in out


@pytest.mark.parametrize("body", [OLDER_ACCESS, OLD_API_ACCESS, new_access(features=NEW_API_FEATURES_OFF)])
def test_dashboard_falls_through_to_status(creds, server, opened, tmp_path, capsys, body, monkeypatch):
    monkeypatch.setenv("SIBYL_DASHBOARD_BASE", "https://account.sibyllabs.org")   # ignored now
    server.on("POST", "/api/plugin/access", body)
    assert run(["dashboard"], creds, tmp_path) == 0
    out = capsys.readouterr().out
    assert opened == []
    assert "the account page is not open yet." in out
    assert "Cap bytes" in out and "Tier cache" in out       # status ran


def test_dashboard_source_has_no_session_query():
    src = inspect.getsource(cli.cmd_dashboard)
    assert "?session=" not in src
    assert 'environ.get("SIBYL_DASHBOARD_BASE")' not in inspect.getsource(cli)


def test_session_query_only_in_upgrade_fallback_and_legacy_activate():
    """Source guard: every `?session=` in cli.py is either the 404 upgrade
    fallback (bearer, very old server only), the legacy SIBYL_ACTIVATE_BASE
    activation link, or the /check poll (both the pairing id, pre-existing)."""
    lines = [ln.strip() for ln in inspect.getsource(cli).splitlines() if "?session=" in ln]
    allowed = {
        'return f"{UPGRADE_BASE}?session={session_token}"',
        'activate_url = f"{ACTIVATE_BASE}?session={session_id}"',
        'resp = http_request("GET", f"/api/plugin/check?session={urllib.parse.quote(session_id)}", timeout=10.0)',
    }
    code_lines = [ln for ln in lines if not ln.startswith("#") and "`" not in ln]
    assert set(code_lines) <= allowed, code_lines
    assert inspect.getsource(cli._legacy_upgrade_url).count("?session=") == 1
    assert "?session=" not in inspect.getsource(cli.cmd_upgrade)


# =========================================================================
# 19. upgrade
# =========================================================================

def _stop_after_first_poll(monkeypatch):
    """Make the /access poll loop run exactly once, then time out."""
    t = {"n": 0}
    real = cli.time.time

    def fake_time():
        t["n"] += 1
        return real() + (0 if t["n"] < 4 else cli.UPGRADE_TIMEOUT_SEC + 10)
    monkeypatch.setattr(cli.time, "time", fake_time)


def test_upgrade_web_checkout_opens_billing(creds, server, opened, capsys, monkeypatch):
    server.on("POST", "/api/plugin/access", new_access(email_verified=True),
              dict(new_access(email_verified=True), tier="pro", source="subscription"))
    assert run(["upgrade"], creds) == 0
    assert opened == [f"{ACCOUNT_ORIGIN}/billing"]
    out = capsys.readouterr().out
    assert "buy Pro on the account page (sign in first; card, or USDC from the plugin page)." in out
    assert "this terminal keeps watching for the tier change." in out
    assert not server.calls_to("/api/plugin/upgrade-nonce")
    assert "Upgraded to PRO via subscription." in out


def test_upgrade_legacy_uses_nonce_exchange(creds, server, opened, capsys, monkeypatch):
    _stop_after_first_poll(monkeypatch)
    exch = "https://sibyllabs.org/plugin/upgrade?upgrade_nonce=ABCDEF0123456789ABCDEF0123456789ABCDEF"
    server.on("POST", "/api/plugin/access", OLD_API_ACCESS)
    server.on("POST", "/api/plugin/upgrade-nonce",
              {"ok": True, "upgrade_nonce": "ABCDEF", "expires_at": "x", "exchange_url": exch, "ttl_seconds": 900})
    assert run(["upgrade"], creds) == 1        # poll timed out, tier unchanged
    assert opened == [exch]
    call = server.calls_to("/api/plugin/upgrade-nonce")[0]
    assert call["body"] == {"account_id": ACCT, "session_token": BEARER}
    # GATE-15: _bearer_headers refreshes first when no usable token is held
    # (the conftest routes that refresh to the test signer), so the header set
    # is the bearer plus, at most, the freshly issued entitlement.
    assert call["headers"]["Authorization"] == f"Bearer {BEARER}"
    assert set(call["headers"]) <= {"Authorization", "X-Sibyl-Entitlement"}
    out = capsys.readouterr().out
    assert BEARER not in out and "upgrade_nonce" not in out and all(BEARER not in u for u in opened)


def test_upgrade_legacy_page_lines_state_the_current_offer(creds, server, opened, capsys, monkeypatch):
    """The exchange-link path names the current offer (2026-10-14): Pro $12/month
    or $108/year, USDC for one fixed period, staking 100,000 $SIBYL (staked,
    not held), card at sibyllabs.org/checkout. Never the retired quarterly plan."""
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access", OLD_API_ACCESS)
    server.on("POST", "/api/plugin/upgrade-nonce",
              {"ok": True, "exchange_url": "https://sibyllabs.org/plugin/upgrade?upgrade_nonce=X"})
    run(["upgrade"], creds)
    out = capsys.readouterr().out
    assert "1. Pro in USDC: $12 for one month or $108 for one year (a fixed period)" in out
    assert "2. stake 100,000 $SIBYL on Base: Pro while it stays staked (holding does not count)" in out
    assert "by card: $12/month or $108/year plus applicable tax at sibyllabs.org/checkout, then sibyl claim <cs_code>" in out
    assert "quarterly" not in out and "subscribe in USDC" not in out


def test_upgrade_nonce_404_falls_back_to_legacy_link(creds, server, opened, monkeypatch):
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access", OLDER_ACCESS)
    server.on("POST", "/api/plugin/upgrade-nonce", err(404, {"error": "unparseable response body"}))
    run(["upgrade"], creds)
    assert opened == [f"{cli.UPGRADE_BASE}?session={BEARER}"]


@pytest.mark.parametrize("failure", [
    err(503, {"error": "nonce store unavailable"}),
    err(0, {"error": "timed out"}),
    err(404, {"error": "account not found"}),
    err(429, {"error": "too many upgrade-nonce requests"}),
])
def test_upgrade_other_nonce_failure_exits_2(creds, server, opened, capsys, failure):
    server.on("POST", "/api/plugin/access", OLD_API_ACCESS)
    server.on("POST", "/api/plugin/upgrade-nonce", failure)
    assert run(["upgrade"], creds) == 2
    assert opened == []
    assert "could not start the upgrade. try again." in capsys.readouterr().out


def test_upgrade_enforced_prints_email_nag_and_still_opens(creds, server, opened, capsys, monkeypatch):
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access",
              new_access(email_required=True, enforced=True, grace_until="2026-10-01T00:00:00Z"))
    run(["upgrade"], creds)
    out = capsys.readouterr().out
    assert "a confirmed email is required before buying: sibyl email add you@example.com" in out
    assert opened == [f"{ACCOUNT_ORIGIN}/billing"]


def test_upgrade_purchase_blocked_in_nag_warns(creds, server, opened, capsys, monkeypatch):
    """Release-blocker F7: nag + purchase gate refuses purchases before grace
    ends (enforced false); the api says purchase_blocked and the CLI warns."""
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access",
              new_access(email_required=True, enforced=False, purchase_blocked=True, grace_until="2026-10-10T00:00:00Z"))
    run(["upgrade"], creds)
    assert "a confirmed email is required before buying: sibyl email add you@example.com" in capsys.readouterr().out


def test_upgrade_not_blocked_does_not_warn(creds, server, opened, capsys, monkeypatch):
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access", new_access(email_required=True, enforced=False, purchase_blocked=False))
    run(["upgrade"], creds)
    assert "a confirmed email is required before buying" not in capsys.readouterr().out


@pytest.mark.parametrize("where", ["access", "nonce"])
def test_upgrade_expired_session_says_reinit(creds, server, opened, capsys, where):
    """Release-blocker F7: a 401 means re-run sibyl init, not "try again"."""
    if where == "access":
        server.on("POST", "/api/plugin/access", err(401, {"error": "session not found, revoked, or expired"}))
    else:
        server.on("POST", "/api/plugin/access", OLD_API_ACCESS)
        server.on("POST", "/api/plugin/upgrade-nonce", err(401, {"error": "session not found, revoked, or expired"}))
    assert run(["upgrade"], creds) == 1
    out = capsys.readouterr().out
    assert "session expired. re-run sibyl init." in out
    assert "try again" not in out
    assert opened == []


def test_upgrade_success_on_a_signed_file_drops_the_signature_and_writes_the_tier(creds, server, opened, monkeypatch):
    """Release-blocker F1 (same class) + hardening L2: tier and wallet are
    signed fields. The new tier (and the staker wallet) are written and the
    old signature and signed_at are dropped with the edit."""
    wallet = "0x1234567890abcdef1234567890abcdef12345678"
    write_creds(creds, signature="a" * 64, signed_at="2026-05-01T00:00:00Z")
    server.on("POST", "/api/plugin/access", new_access(email_verified=True, complete=True),
              dict(ACCESS_BASE, tier="stake", source="staker", cap_bytes=None,
                   staker={"wallet": wallet, "qualified": True}))
    assert run(["upgrade"], creds) == 0
    persisted = json.loads(creds.read_text())
    assert persisted["tier"] == "stake"
    assert persisted["wallet"] == wallet
    _assert_adapters_send_no_claim(persisted)
    assert persisted["account_id"] == ACCT and persisted["session_token"] == BEARER


def test_upgrade_access_unreachable_uses_nonce(creds, server, opened, monkeypatch):
    _stop_after_first_poll(monkeypatch)
    server.on("POST", "/api/plugin/access", err(0, {"error": "down"}))
    server.on("POST", "/api/plugin/upgrade-nonce", {"ok": True, "exchange_url": "https://sibyllabs.org/plugin/upgrade?upgrade_nonce=X"})
    run(["upgrade"], creds)
    assert opened == ["https://sibyllabs.org/plugin/upgrade?upgrade_nonce=X"]


# =========================================================================
# 20. devices / logout
# =========================================================================

TAMPER_403 = {"error": "account under review", "reason": "tamper_flag_active", "contact": "sibyl@sibyllabs.org"}


def test_devices_list_403_tamper_copy(creds, server, capsys):
    server.on("GET", "/api/plugin/devices", err(403, TAMPER_403))
    assert run(["devices"], creds) == 2
    out = capsys.readouterr().out
    assert "this account is under review. email sibyl@sibyllabs.org." in out
    assert "tamper_flag_active" not in out


def test_devices_revoke_403_tamper_copy(creds, server, capsys):
    server.on("GET", "/api/plugin/devices", {"ok": True, "devices": [
        {"bearer_id": "b0", "is_this_device": True}, {"bearer_id": "b1", "device_label": "old laptop"}]})
    server.on("POST", "/api/plugin/devices", err(403, TAMPER_403))
    assert run(["devices", "revoke", "1"], creds) == 2
    assert "this account is under review. email sibyl@sibyllabs.org." in capsys.readouterr().out


def test_devices_revoke_already_revoked(creds, server, capsys):
    server.on("GET", "/api/plugin/devices", {"ok": True, "devices": [
        {"bearer_id": "b0", "is_this_device": True},
        {"bearer_id": "b1", "device_label": "approved-from-web"}]})
    server.on("POST", "/api/plugin/devices", {"ok": True, "revoked": True, "bearer_id": "b1",
                                             "already_revoked": True, "sessions_revoked": 0, "self_revoked": False})
    assert run(["devices", "revoke", "1"], creds) == 0
    out = capsys.readouterr().out
    assert "approved-from-web: already revoked" in out
    assert "Revoked device" not in out


def test_devices_revoke_fresh(creds, server, capsys):
    server.on("GET", "/api/plugin/devices", {"ok": True, "devices": [
        {"bearer_id": "b0", "is_this_device": True},
        {"bearer_id": "b1", "device_label": "mobile-usdc-send"}]})
    server.on("POST", "/api/plugin/devices", {"ok": True, "revoked": True, "bearer_id": "b1",
                                             "already_revoked": False, "sessions_revoked": 1, "self_revoked": False})
    assert run(["devices", "revoke", "1"], creds) == 0
    assert "Revoked device mobile-usdc-send" in capsys.readouterr().out


def test_devices_list_renders_new_labels(creds, server, capsys):
    server.on("GET", "/api/plugin/devices", {"ok": True, "devices": [
        {"bearer_id": "b0", "is_this_device": True, "device_label": "approved-from-web",
         "install_method": "cli", "last_seen_at": "2026-09-26T00:00:00Z"},
        {"bearer_id": "b1", "device_label": "mobile-usdc-send", "last_seen_at": "2026-09-25T00:00:00Z"}]})
    assert run(["devices"], creds) == 0
    out = capsys.readouterr().out
    assert "approved-from-web" in out and "mobile-usdc-send" in out


def test_logout_confirmed_revoke_says_so(tmp_path, server, capsys):
    cred = write_creds(tmp_path / "credentials.json")
    server.on("GET", "/api/plugin/devices", {"devices": [{"bearer_id": "b0", "is_this_device": True}]})
    server.on("POST", "/api/plugin/devices", {"ok": True, "revoked": True, "already_revoked": False})
    assert run(["logout"], cred, tmp_path) == 0
    out = capsys.readouterr().out
    assert "signed out on the server too." in out
    assert "remote session may still be active" not in out


def test_logout_unconfirmed_keeps_caveat(tmp_path, server, capsys):
    cred = write_creds(tmp_path / "credentials.json")
    server.on("GET", "/api/plugin/devices", err(403, TAMPER_403))
    assert run(["logout"], cred, tmp_path) == 0
    out = capsys.readouterr().out
    assert "signed out on the server too." not in out
    assert "remote session may still be active" in out


def test_logout_revoke_nothing_to_revoke_has_no_caveat():
    """Nothing to revoke: no caveat (the contract of the removed 0.4.x
    `_logout_revoke_bearer` wrapper, now asserted on the helper itself)."""
    assert cli._logout_revoke_status({}) == ("nothing", None)


# =========================================================================
# 21. never print a token
# =========================================================================

def test_no_print_of_token_values_in_source():
    src = inspect.getsource(cli)
    offenders = []
    for ln in src.splitlines():
        s = ln.strip()
        if "print(" not in s:
            continue
        if re.search(r"\{\s*(session_token|bearer|token|bearer_token)\s*\}", s) or \
           re.search(r"creds\[\s*['\"](session_token|bearer_token)['\"]\s*\]", s) or \
           re.search(r"\{\s*session_id\s*\}", s):
            offenders.append(s)
    assert offenders == [], offenders


def test_full_email_flow_never_prints_bearer(creds, server, capsys, monkeypatch, opened, tmp_path):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    feed_input(monkeypatch, "123456")
    server.on("POST", "/api/plugin/access", new_access())
    server.on("POST", "/api/plugin/email-verify/request", {"ok": True, "to_masked": "b***@e***.com"})
    server.on("POST", "/api/plugin/email-verify/confirm", {"ok": True, "email": "buyer@example.com"})
    for argv in (["email"], ["email", "verify"], ["status"], ["dashboard"]):
        run(argv, creds, tmp_path)
    out = capsys.readouterr().out
    assert BEARER not in out


# =========================================================================
# 4.10 version
# =========================================================================

def test_package_version_is_0_5_0():
    from sibyl_memory_cli import __version__
    assert __version__ == "0.5.0"
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'version = "0.5.0"' in pyproject.read_text()
