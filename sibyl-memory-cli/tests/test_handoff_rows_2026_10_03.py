"""Agent handoff rows (spec agent-handoff-messages 2026-10-03, section 2, test plan 4.3).

Every scenario below drives one stop of a `sibyl` command, non-interactive
(capsys: no TTY) unless it says otherwise, against a routing fake of
`cli.http_request`, and asserts the row's `next:` value and the prefix of its
`ask your human:` sentence (prefix only, so wording can be tuned). Every
scenario also passes the convention lint (`assert_convention`, 4.2).

`SCENARIOS` is imported by test_handoff_convention_2026_10_03.py for the sweep.
"""
from __future__ import annotations

import builtins
import json
import time as _time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import assert_convention, handoff
from sibyl_memory_cli import _account_gate as G
from sibyl_memory_cli import cli
from sibyl_memory_client import _entitlement as E

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "22222222-3333-4444-8555-666666666666"
ORIGIN = "https://account.sibyllabs.org"
CS = "cs_test_a1B2c3D4e5F6g7H8"
FEATURES_ON = {"device_approval": False, "email_signin": True, "cli_email_verify": True,
               "web_checkout": False, "web_dashboard_url": ORIGIN, "cli_login": False}
ACCESS = {"ok": True, "account_id": ACCT, "tier": "free", "source": "free", "cap_bytes": 5242880}


# ---- fakes -----------------------------------------------------------------

class FakeServer:
    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []

    def on(self, method, prefix, *responses):
        self.routes[(method, prefix)] = list(responses)
        return self

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        self.calls.append({"method": method, "path": path, "body": body})
        for (m, prefix), queue in self.routes.items():
            if m == method and path.startswith(prefix):
                resp = queue.pop(0) if len(queue) > 1 else queue[0]
                if callable(resp) and not isinstance(resp, cli.HttpError):
                    resp = resp(body)
                if isinstance(resp, cli.HttpError):
                    raise resp
                return json.loads(json.dumps(resp))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")


def err(status, body=None, headers=None):
    return cli.HttpError(status, body if body is not None else {}, "https://api/x", headers or {})


class Clock:
    """cli.time stand-in whose sleep advances the clock (time-outs in tests)."""

    def __init__(self):
        self.t = _time.time()

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s or 0), 1.0)

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture()
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    return s


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
    monkeypatch.setattr(cli, "POLL_INTERVAL_SEC", 0)
    monkeypatch.delenv("SIBYL_NONINTERACTIVE", raising=False)

    def no_input(*a, **k):
        raise EOFError
    monkeypatch.setattr(builtins, "input", no_input)


@pytest.fixture()
def refresh(monkeypatch):
    state = {"outcome": E.RefreshOutcome("transient", code="offline")}

    def fn(account_id, session_token):
        out = state["outcome"]
        return out(account_id, session_token) if callable(out) else out
    monkeypatch.setattr(E, "http_refresh", fn)
    return state


def write_creds(path: Path, **over) -> Path:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "buyer@example.com",
         "wallet": None, "issued_at": "2026-10-03T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    c.update(over)
    c = {k: v for k, v in c.items() if v is not ...}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))
    return path


def bound(email=None, account=None, entitlement=None):
    body = {"bound": True, "credentials": {
        "account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": email, "wallet": None,
        "issued_at": "2026-10-03T00:00:00Z", "bearer_token": BEARER}}
    if account is not None:
        body["account"] = account
    if entitlement is not None:
        body["entitlement"] = entitlement
    return body


# ---- the scenario table ------------------------------------------------------
# Each entry: id -> (setup(ctx) -> argv tail, expected next, human prefix or None,
# expected later list or None (None: not checked)).

SCENARIOS: dict = {}


def scenario(sid, nxt, human=None, later=None):
    def deco(fn):
        SCENARIOS[sid] = (fn, nxt, human, later)
        return fn
    return deco


def _init(ctx, *extra):
    return ["init", "--force", *extra]


# init ------------------------------------------------------------------------

@scenario("I1 native windows", "done", "install Sibyl Memory inside WSL2")
def _(ctx):
    ctx.mp.setattr(cli, "_platform_info", lambda: ("windows", "Windows 11", False))
    ctx.mp.setattr(cli, "NATIVE_WINDOWS_POLICY", "block")
    ctx.mp.delenv(cli.NATIVE_WINDOWS_ESCAPE_ENV, raising=False)
    return _init(ctx)


@scenario("I2 already activated", "sibyl status", None, ["sibyl init --force"])
def _(ctx):
    write_creds(ctx.cred)
    return ["init"]


@scenario("I3 computer limit", "sibyl init", "your account is at its computer limit", ["sibyl upgrade"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", err(409, {
        "code": "device_limit", "limit": 1, "tier": "free",
        "devices": [{"label": "laptop", "os_family": "linux", "last_seen_at": "2026-10-01T00:00:00Z"}],
        "manage_url": f"{ORIGIN}/security"}))
    return _init(ctx)


@scenario("I8 unexpected check error", "sibyl init --force")
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", err(500, {"error": "boom"}))
    return _init(ctx)


@scenario("I9 malformed credentials", "sibyl init --force")
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", {"bound": True, "credentials": "garbage"})
    return _init(ctx)


@scenario("I10 session token mismatch", "sibyl init --force")
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", {"bound": True, "credentials": {
        "account_id": ACCT, "session_token": "not-this-session"}})
    return _init(ctx)


@scenario("I11 old api", "sibyl setup")
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    return _init(ctx)


@scenario("I11 email required, none", "sibyl email add <address>", "an email address for the account",
          ["sibyl setup"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I12 email not required, none", "sibyl setup", "an email address for the account",
          ["sibyl email add <address>"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(account={"email_verified": False, "email_required": False}))
    return _init(ctx)


@scenario("I11 email on file unverified, required", "sibyl email verify", "the six digit code we will email",
          ["sibyl setup"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I15 email on file unverified, not required", "sibyl setup", None, ["sibyl email verify"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": False, "email_required": False}))
    return _init(ctx)


@scenario("I12 not open, none", "sibyl setup", "add and confirm your email at", ["sibyl email"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60,
                  "features": dict(FEATURES_ON, cli_email_verify=False)})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I11 not open, unverified", "sibyl setup", "add and confirm your email at", ["sibyl email"])
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": False, "email_required": True}))
    return _init(ctx)


# F7 (onboarding friction 2026-10-08): the installer wires the agent before
# `sibyl init`, so with a harness already wired the step after activation is
# `sibyl status` and `sibyl setup` is never queued.

def _wire_claude_code():
    (Path.home() / ".claude.json").write_text(json.dumps({"mcpServers": {"sibyl-memory": {}}}))


@scenario("I11 F7 old api, harness wired", "sibyl status", None, [])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    return _init(ctx)


@scenario("I11 F7 email verified, harness wired", "sibyl status", None, [])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": True, "email_required": True}))
    return _init(ctx)


@scenario("I11 F7 email required, none, harness wired", "sibyl email add <address>",
          "an email address for the account", [])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I11 F7 unverified, required, harness wired", "sibyl email verify",
          "the six digit code we will email", [])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I15 F7 unverified, not required, harness wired", "sibyl status", None, ["sibyl email verify"])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(email="u@example.com", account={"email_verified": False, "email_required": False}))
    return _init(ctx)


@scenario("I12 F7 not open, harness wired", "sibyl status", "add and confirm your email at", ["sibyl email"])
def _(ctx):
    _wire_claude_code()
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60,
                  "features": dict(FEATURES_ON, cli_email_verify=False)})
    ctx.server.on("GET", "/api/plugin/check",
                  bound(account={"email_verified": False, "email_required": True}))
    return _init(ctx)


@scenario("I11 F7 codex wired", "sibyl status", None, [])
def _(ctx):
    from sibyl_memory_cli.setup import CodexWirer
    w = CodexWirer()
    w.config_path.parent.mkdir(parents=True, exist_ok=True)
    w.config_path.write_text(w._block_text())
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    return _init(ctx)


@scenario("I16 approval declined", "done", "this computer was not approved")
def _(ctx):
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    ctx.server.on("GET", "/api/plugin/check", {"bound": False, "approval": {
        "required": True, "approved": False, "declined": True}})
    return _init(ctx)


@scenario("I18 timed out", "sibyl init --force", "did the browser show Activation successful?")
def _(ctx):
    ctx.mp.setattr(cli, "time", Clock())
    ctx.mp.setattr(cli, "POLL_INTERVAL_SEC", 3)
    ctx.server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 6})
    ctx.server.on("GET", "/api/plugin/check", {"bound": False})
    return _init(ctx)


@scenario("I19 corrupt credentials (status)", "sibyl init --force")
def _(ctx):
    ctx.cred.write_text("{not json")
    return ["status"]


# email -----------------------------------------------------------------------

@scenario("E1 not activated", "sibyl init", None, ["sibyl email add a@example.com"])
def _(ctx):
    return ["email", "add", "a@example.com"]


@scenario("E2 state 401", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", err(401))
    return ["email"]


@scenario("E3 state unreachable", "retry", "connect this computer to the internet")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", err(0))
    return ["email"]


@scenario("E3 state server error", "retry", None)
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", err(503))
    return ["email"]


@scenario("E4 old api", "done")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    return ["email"]


@scenario("E5 confirmed", "done")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", dict(ACCESS, account={"email_verified": True}))
    return ["email"]


@scenario("E5 not confirmed", "sibyl email verify", "the six digit code we will email to b***@e***.com")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access",
                  dict(ACCESS, account={"email_verified": False}, features=FEATURES_ON))
    return ["email"]


@scenario("E5 none", "sibyl email add <address>", "an email address for the account")
def _(ctx):
    write_creds(ctx.cred, email=None)
    ctx.server.on("POST", "/api/plugin/access",
                  dict(ACCESS, account={"email_verified": False}, features=FEATURES_ON))
    return ["email"]


# E11 from the state branch: the api does not take confirmation from the
# terminal, so `sibyl email add` / `verify` would only fail with not_open and
# the agent would ask for an address again (the not_open loop).

@scenario("E5 not open, not confirmed", "done", "add and confirm your email at")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", dict(ACCESS, account={"email_verified": False}))
    return ["email"]


@scenario("E5 not open, none", "done", "add and confirm your email at")
def _(ctx):
    write_creds(ctx.cred, email=None)
    ctx.server.on("POST", "/api/plugin/access", dict(
        ACCESS, account={"email_verified": False},
        features=dict(FEATURES_ON, cli_email_verify=False)))
    return ["email"]


@scenario("E6 bad address", "sibyl email add <address>", "a valid email address")
def _(ctx):
    write_creds(ctx.cred)
    return ["email", "add", "nope"]


@scenario("E7 code sent, no terminal", "sibyl email confirm <code>", "check n***@e***.com for a six digit code")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/email-verify/request",
                  {"to_masked": "n***@e***.com", "expires_in_seconds": 900})
    return ["email", "add", "name@example.com"]


@scenario("E10 not six digits", "sibyl email confirm <code>", "the six digit code from the email")
def _(ctx):
    write_creds(ctx.cred)
    return ["email", "confirm", "12"]


def _confirm_error(ctx, e):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/email-verify/confirm", e)
    return ["email", "confirm", "123456"]


@scenario("E11 not open", "sibyl email", "add and confirm your email at")
def _(ctx):
    return _confirm_error(ctx, err(403, {"code": "not_open"}))


@scenario("E12 wrong code", "sibyl email confirm <code>", "re-read the six digit code (2 attempts left)")
def _(ctx):
    return _confirm_error(ctx, err(400, {"code": "wrong_code", "attempts_remaining": 2}))


@scenario("E13 expired, server text", "sibyl email verify", "a new code will be sent to b***@e***.com")
def _(ctx):
    return _confirm_error(ctx, err(400, {"code": "expired", "error": "that code has expired."}))


@scenario("E13 expired, no server text", "sibyl email verify", "a new code will be sent")
def _(ctx):
    return _confirm_error(ctx, err(400, {"code": "expired"}))


@scenario("E14 already verified", "sibyl status")
def _(ctx):
    return _confirm_error(ctx, err(409, {"code": "already_verified", "error": "already confirmed."}))


@scenario("E15 email taken", "sibyl email add <address>", "that address belongs to another account")
def _(ctx):
    return _confirm_error(ctx, err(409, {"code": "email_taken", "error": "taken."}))


@scenario("E16 mail unavailable", "retry in 5 minutes")
def _(ctx):
    return _confirm_error(ctx, err(503, {"code": "mail_unavailable", "error": "mail is down."}))


@scenario("E17 identity frozen", "done", "this account's identity is frozen")
def _(ctx):
    return _confirm_error(ctx, err(403, {"code": "identity_frozen", "error": "frozen."}))


@scenario("E18 401", "sibyl init")
def _(ctx):
    return _confirm_error(ctx, err(401))


@scenario("E19 under review", "done", "your account is under review")
def _(ctx):
    return _confirm_error(ctx, err(403, {"reason": "tamper_flag_active"}))


@scenario("E20 old api 404", "sibyl email", "add and confirm your email at")
def _(ctx):
    return _confirm_error(ctx, err(404, {"error": "not found"}))


@scenario("E21 rate limited", "retry in 2 minutes")
def _(ctx):
    return _confirm_error(ctx, err(429, {"error": "slow"}, {"Retry-After": "120"}))


@scenario("E22 unreachable", "retry", "connect this computer to the internet")
def _(ctx):
    return _confirm_error(ctx, err(0))


@scenario("E23 gate: sign-in file damaged", "sibyl init --force")
def _(ctx):
    return _confirm_error(ctx, err(403, {"code": "entitlement_invalid"}))


@scenario("E24 confirmed, nothing wired", "sibyl setup")
def _(ctx):
    return _confirm_error(ctx, {"ok": True})


@scenario("E24 confirmed, a harness wired", "sibyl status")
def _(ctx):
    (Path.home() / ".claude.json").write_text(json.dumps({"mcpServers": {"sibyl-memory": {}}}))
    return _confirm_error(ctx, {"ok": True})


@scenario("E25 confirmed, credentials not updated", "sibyl init --force")
def _(ctx):
    def boom(*a, **k):
        raise OSError("read-only")
    ctx.mp.setattr(cli, "write_credentials_atomic", boom)
    return _confirm_error(ctx, {"ok": True, "credentials": {
        "account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "buyer@example.com",
        "bearer_token": BEARER}})


# upgrade / claim ---------------------------------------------------------------

@scenario("U1 not activated", "sibyl init", None, ["sibyl upgrade"])
def _(ctx):
    return ["upgrade"]


@scenario("U2 credentials missing fields", "sibyl init --force")
def _(ctx):
    write_creds(ctx.cred, session_token=..., bearer_token=...)
    return ["upgrade"]


@scenario("U3 session expired", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", err(401))
    return ["upgrade"]


@scenario("U6 could not start", "retry")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    ctx.server.on("POST", "/api/plugin/upgrade-nonce", err(500, {"error": "x"}))
    return ["upgrade"]


@scenario("U7 upgraded", "sibyl refresh")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access", ACCESS, dict(ACCESS, tier="pro", source="subscription"))
    ctx.server.on("POST", "/api/plugin/upgrade-nonce", {"exchange_url": f"{ORIGIN}/x"})
    return ["upgrade"]


@scenario("U8 timed out", "sibyl refresh", "did the payment complete?", ["sibyl claim <cs_code>"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.mp.setattr(cli, "time", Clock())
    ctx.mp.setattr(cli, "POLL_INTERVAL_SEC", 900)
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    ctx.server.on("POST", "/api/plugin/upgrade-nonce", {"exchange_url": f"{ORIGIN}/x"})
    return ["upgrade"]


@scenario("K1 bad code", "sibyl claim <cs_code>", "the whole cs_ code")
def _(ctx):
    return ["claim", "nope"]


@scenario("K2 not activated", "sibyl init", None, [f"sibyl claim {CS}"])
def _(ctx):
    return ["claim", CS]


@scenario("K3 gate line", "sibyl init --force", None, [f"sibyl claim {CS}"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(403, {"code": "entitlement_invalid"}))
    return ["claim", CS]


@scenario("K4 unclaimed, email on file", "sibyl email verify", "the six digit code we will email",
          [f"sibyl claim {CS}"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "unclaimed_account", "error": "x"}))
    return ["claim", CS]


@scenario("K4 unclaimed, no email", "sibyl email add <address>", "the email address you used to buy",
          [f"sibyl claim {CS}"])
def _(ctx):
    write_creds(ctx.cred, email=None)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "unclaimed_account", "error": "x"}))
    return ["claim", CS]


@scenario("K5 email required", "sibyl email verify", "the six digit code", [f"sibyl claim {CS}"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(409, {"code": "email_required", "error": "x"}))
    return ["claim", CS]


@scenario("K6 403 other", "sibyl init", None, [f"sibyl claim {CS}"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(403, {"error": "mismatch"}))
    return ["claim", CS]


@scenario("K7 unreachable", "retry", "connect this computer to the internet")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(0))
    return ["claim", CS]


@scenario("K8 other code", "sibyl claim <cs_code>", "the purchase could not be linked")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", err(404, {"error": "no claimable purchase."}))
    return ["claim", CS]


@scenario("K9 linked", "sibyl refresh")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/stripe-claim", {"ok": True, "tier": "pro"})
    return ["claim", CS]


# status / dashboard / whoami / logout / health ------------------------------------

@scenario("S1 not activated", "sibyl init")
def _(ctx):
    return ["status"]


@scenario("S5 signed out", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = E.RefreshOutcome("denied", code="signed_out")
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    return ["status"]


@scenario("S7 under review", "done", "your account is under review")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = E.RefreshOutcome("denied", code="under_review")
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    return ["status"]


@scenario("S3 fresh", "done")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=ctx.mint())
    ctx.server.on("POST", "/api/plugin/access", ACCESS)
    return ["status"]


@scenario("S9 tier drift", "sibyl refresh")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=ctx.mint())
    ctx.server.on("POST", "/api/plugin/access", dict(ACCESS, tier="pro"))
    return ["status"]


@scenario("S11 server unreachable", "sibyl refresh", "connect this computer to the internet")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=ctx.mint())
    ctx.server.on("POST", "/api/plugin/access", err(0))
    return ["status"]


@scenario("D1 dashboard opened", "done", "sign in on the account page")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("POST", "/api/plugin/access",
                  dict(ACCESS, features=FEATURES_ON, account={"email_verified": False}))
    return ["dashboard"]


@scenario("W1 whoami not activated", "sibyl init")
def _(ctx):
    return ["whoami"]


@scenario("W whoami", "done")
def _(ctx):
    write_creds(ctx.cred)
    return ["whoami"]


@scenario("O1 nothing to remove", "done", None, ["sibyl init"])
def _(ctx):
    return ["logout"]


@scenario("O2 logged out", "done", None, ["sibyl init"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [{"is_this_device": True, "bearer_id": "b1"}]})
    ctx.server.on("POST", "/api/plugin/devices", {"revoked": True})
    return ["logout"]


@scenario("O3 revoke unconfirmed", "done", "this computer's sign-in may still be active", ["sibyl init"])
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", err(0))
    return ["logout"]


@scenario("H3c health", "done")
def _(ctx):
    write_creds(ctx.cred)
    from sibyl_memory_client.testing import write_entitlement
    write_entitlement(ctx.tmp, ctx.mint())
    return ["health"]


# devices -----------------------------------------------------------------------

@scenario("V1 not activated", "sibyl init")
def _(ctx):
    return ["devices"]


@scenario("V1 malformed", "sibyl init --force")
def _(ctx):
    write_creds(ctx.cred, session_token=...)
    return ["devices"]


@scenario("V2 bad index", "sibyl devices")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [{"bearer_id": "b0"}]})
    return ["devices", "revoke", "5"]


@scenario("V3 own device", "sibyl logout", "that is this computer")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [{"bearer_id": "b0", "is_this_device": True}]})
    return ["devices", "revoke", "0"]


@scenario("V4 list unreachable", "retry", "connect this computer to the internet")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", err(0))
    return ["devices"]


@scenario("V5 revoked", "sibyl devices")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [{"bearer_id": "b0", "device_label": "old"}]})
    ctx.server.on("POST", "/api/plugin/devices", {"revoked": True})
    return ["devices", "revoke", "0"]


@scenario("V6 list 401", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", err(401))
    return ["devices"]


@scenario("V7 listed, under the limit", "done")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [
        {"bearer_id": "b0", "is_this_device": True, "last_seen_at": "2026-10-03T00:00:00Z"}]})
    return ["devices"]


@scenario("V7 listed, over the limit", "sibyl devices revoke <index>", "which computer to sign out",
          ["sibyl upgrade"])
def _(ctx):
    write_creds(ctx.cred)
    from sibyl_memory_client.testing import write_entitlement
    write_entitlement(ctx.tmp, ctx.mint(acct={"devices": {"n": 2, "limit": 1, "over": True}}))
    ctx.server.on("GET", "/api/plugin/devices", {"devices": [
        {"bearer_id": "b0", "is_this_device": True, "last_seen_at": "2026-10-03T00:00:00Z"},
        {"bearer_id": "b1", "last_seen_at": "2026-10-01T00:00:00Z"}]})
    return ["devices"]


@scenario("V8 no active devices", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.server.on("GET", "/api/plugin/devices", {"devices": []})
    return ["devices"]


# update --------------------------------------------------------------------------

def _versions(ctx, latest, method="venv"):
    ctx.mp.setattr(cli, "_installed_version", lambda pkg: "0.1.0")
    ctx.mp.setattr(cli, "_pypi_latest", lambda pkg, timeout=4.0: latest)
    ctx.mp.setattr(cli, "_detect_install_method", lambda: method)


@scenario("Z2 outdated, pip", "sibyl update --apply")
def _(ctx):
    _versions(ctx, "0.2.0")
    return ["update"]


@scenario("Z2 outdated, pipx", "pipx upgrade sibyl-memory-cli sibyl-memory-hermes sibyl-memory-client "
          "sibyl-memory-mcp sibyl-memory-langgraph")
def _(ctx):
    _versions(ctx, "0.2.0", "pipx")
    return ["update"]


@scenario("Z2 outdated, PEP 668", "sibyl update --apply", "this Python is externally managed")
def _(ctx):
    _versions(ctx, "0.2.0", "pep668")
    return ["update"]


@scenario("Z5 all current", "done")
def _(ctx):
    _versions(ctx, "0.1.0")
    return ["update"]


@scenario("Z5 PyPI unreachable", "retry")
def _(ctx):
    _versions(ctx, None)
    return ["update"]


# memory / adopt -----------------------------------------------------------------

@scenario("A1 no store", "sibyl status")
def _(ctx):
    return ["memory", "list"]


@scenario("A2 list without an account", "done", None, ["sibyl init"])
def _(ctx):
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    c = MemoryClient.local(str(ctx.tmp / "memory.db"), tenant_id=DEFAULT_TENANT)
    c.set_entity("a", "b", {"x": 1})
    c.storage.close()
    return ["memory", "list"]


@scenario("A5 adopt without sign-in", "sibyl init", None, ["sibyl memory adopt"])
def _(ctx):
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    c = MemoryClient.local(str(ctx.tmp / "memory.db"), tenant_id=DEFAULT_TENANT)
    c.set_entity("a", "b", {"x": 1})
    c.storage.close()
    return ["memory", "adopt"]


# save / debloat / skills / autosave / refresh / reminders ---------------------------

@scenario("F1 empty fact", 'sibyl save --fact "<fact>"', "the fact to save")
def _(ctx):
    return ["save", "--fact", "  "]


@scenario("F2 not signed in", "sibyl init", None, ["sibyl save --fact 'the sky is blue'"])
def _(ctx):
    return ["save", "--fact", "the sky is blue"]


@scenario("F4 capsule on free", "sibyl save", None, ["sibyl upgrade"])
def _(ctx):
    return ["save", "--capsule"]


@scenario("F6 the three ways", "done")
def _(ctx):
    return ["save"]


@scenario("G1 trim without a terminal", "sibyl debloat apply AGENTS.md --yes", "Move ")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "apply", "AGENTS.md"]


@scenario("G3 trim applied", "done", None, ["sibyl debloat undo AGENTS.md"])
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "apply", "AGENTS.md", "--yes"]


@scenario("G3 trim refused", "sibyl debloat plan", None, [])
def _(ctx):
    _bloated_claude_md(ctx, fresh=True)
    return ["debloat", "apply", "AGENTS.md", "--yes"]


@scenario("G5 plan shown", "sibyl debloat apply <file>", "which file to trim")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "plan"]


@scenario("debloat measure", "done")
def _(ctx):
    ctx.mp.chdir(ctx.tmp)
    return ["debloat"]


@scenario("T1 skills not installed", "sibyl skills install")
def _(ctx):
    import shutil
    shutil.rmtree(Path.home() / ".sibyl-memory" / "skills", ignore_errors=True)
    return ["skills"]


@scenario("P1 proposals not activated", "sibyl init")
def _(ctx):
    return ["skills", "proposals"]


@scenario("Y1 autosave on free", "done", None, ["sibyl upgrade"])
def _(ctx):
    return ["autosave"]


@scenario("R1 refresh not activated", "sibyl init")
def _(ctx):
    return ["refresh"]


@scenario("R3 refresh fresh", "done")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=ctx.mint())
    return ["refresh"]


@scenario("R4 refresh denied", "sibyl init")
def _(ctx):
    write_creds(ctx.cred)
    ctx.refresh["outcome"] = E.RefreshOutcome("denied", code="signed_out")
    return ["refresh"]


@scenario("R5 refresh unreachable", "retry", "connect this computer to the internet once")
def _(ctx):
    write_creds(ctx.cred)
    return ["refresh"]


@scenario("reminders", "done")
def _(ctx):
    return ["reminders"]


# setup / migrate ----------------------------------------------------------------

@scenario("X3 nothing detected", "sibyl status", "which agent you use", ["sibyl setup <target>"])
def _(ctx):
    empty = ctx.tmp / "empty-bin"
    empty.mkdir()
    ctx.mp.setenv("PATH", str(empty))
    return ["setup"]


@scenario("N2 no memory files", "sibyl setup", "where your CLAUDE.md")
def _(ctx):
    ctx.mp.chdir(ctx.tmp)
    return ["migrate"]


# ---- sweep extension (handoff check 2026-10-06, SHOULD-FIX 5) -----------------------
# Every command path the first sweep did not reach: memory search / recall,
# adopt A3/A4/A6/A7, debloat auto / manual / undo, skills install, update
# --apply, setup X6 to X16 through the real wirers (only the subprocess, the
# MCP binary probe and the server smoke test are faked), migrate N3 to N10
# through cmd_migrate, health H1c / H2c.

def _seed_store(ctx):
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    c = MemoryClient.local(str(ctx.tmp / "memory.db"), tenant_id=DEFAULT_TENANT)
    c.set_entity("people", "ada", {"note": "likes tea"})
    c.storage.close()


def _signed_in(ctx):
    write_creds(ctx.cred)
    from sibyl_memory_client.testing import write_entitlement
    write_entitlement(ctx.tmp, ctx.mint())


def _answers(ctx, *replies):
    """builtins.input answering in order; end of input after the last."""
    q = list(replies)

    def fake(*a, **k):
        if not q:
            raise EOFError
        return q.pop(0)
    ctx.mp.setattr(builtins, "input", fake)


class _Tty:
    def isatty(self):
        return True

    def readline(self, *a):
        return ""


# memory search / recall ----------------------------------------------------------

@scenario("A2 search, a match", "done", None, ["sibyl init"])
def _(ctx):
    _seed_store(ctx)
    return ["memory", "search", "tea"]


@scenario("A2 search, no match", "done", None, ["sibyl init"])
def _(ctx):
    _seed_store(ctx)
    return ["memory", "search", "zebra"]


@scenario("A2 recall, found", "done", None, ["sibyl init"])
def _(ctx):
    _seed_store(ctx)
    return ["memory", "recall", "people", "ada"]


@scenario("A2 recall, missing", "sibyl memory list", None, ["sibyl init"])
def _(ctx):
    _seed_store(ctx)
    return ["memory", "recall", "people", "nobody"]


@scenario("A search signed in", "done", None, [])
def _(ctx):
    _signed_in(ctx)
    _seed_store(ctx)
    return ["memory", "search", "tea"]


# adopt ------------------------------------------------------------------------------

def _adopt_fake(ctx, fn):
    _seed_store(ctx)
    ctx.mp.setattr(G, "adopt_pre_signin", lambda db, cred, trigger, confirm_other_accounts: fn(
        confirm_other_accounts))
    return ["memory", "adopt"]


def _other_account(confirm):
    from sibyl_memory_client._adopt import AdoptionRefused, AdoptionResult
    if not confirm:
        raise AdoptionRefused("other_account", "this store also holds another account")
    return AdoptionResult("moved", ACCT, counts={"entities": 1})


@scenario("A3 other account, no terminal", "sibyl memory adopt --yes",
          "this store also holds another account's memory")
def _(ctx):
    return _adopt_fake(ctx, _other_account)


@scenario("A4 other account, declined", "done")
def _(ctx):
    import sys
    ctx.mp.setattr(sys, "stdin", _Tty())
    _answers(ctx, "n")
    return _adopt_fake(ctx, _other_account)


@scenario("A4 other account, confirmed", "done")
def _(ctx):
    import sys
    ctx.mp.setattr(sys, "stdin", _Tty())
    _answers(ctx, "y")
    return _adopt_fake(ctx, _other_account)


@scenario("A6 adoption refused", "sibyl status")
def _(ctx):
    from sibyl_memory_client._adopt import AdoptionRefused

    def refuse(confirm):
        raise AdoptionRefused("tenant_mismatch", "the signed-in account does not match this store")
    return _adopt_fake(ctx, refuse)


@scenario("A7 nothing to move", "done")
def _(ctx):
    from sibyl_memory_client._adopt import AdoptionResult
    return _adopt_fake(ctx, lambda confirm: AdoptionResult("nothing_to_move", ACCT))


@scenario("A7 moved", "done")
def _(ctx):
    from sibyl_memory_client._adopt import AdoptionResult
    return _adopt_fake(ctx, lambda confirm: AdoptionResult(
        "moved", ACCT, counts={"entities": 2}, renamed=[{"from": "a", "to": "a (before sign-in)"}]))


# debloat auto / manual / undo -------------------------------------------------------

@scenario("G auto, confirmed", "done")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "auto", "AGENTS.md", "--yes"]


@scenario("G1 auto without a terminal", "sibyl debloat auto AGENTS.md --yes", "Let Sibyl trim this file")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "auto", "AGENTS.md"]


@scenario("G auto, not trimmable", "sibyl debloat plan")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "auto", "README.md"]


@scenario("G auto, no file", "sibyl debloat plan")
def _(ctx):
    ctx.mp.chdir(ctx.tmp)
    return ["debloat", "auto"]


@scenario("G manual, was not on auto", "done")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "manual", "AGENTS.md"]


@scenario("G manual, back from auto", "done")
def _(ctx):
    _bloated_claude_md(ctx)
    cli.main(["debloat", "auto", "AGENTS.md", "--yes"])
    return ["debloat", "manual", "AGENTS.md"]


@scenario("G undo, nothing trimmed", "done")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "undo"]


@scenario("G1 undo without a terminal", "sibyl debloat undo AGENTS.md --yes", "Restore AGENTS.md")
def _(ctx):
    _bloated_claude_md(ctx)
    cli.main(["debloat", "apply", "AGENTS.md", "--yes"])
    return ["debloat", "undo"]


@scenario("G undo, restored", "done")
def _(ctx):
    _bloated_claude_md(ctx)
    cli.main(["debloat", "apply", "AGENTS.md", "--yes"])
    return ["debloat", "undo", "AGENTS.md", "--yes"]


@scenario("G1 undo, a label with a space", "sibyl debloat undo 'my notes.md' --yes", "Restore my notes.md")
def _(ctx):
    _bloated_claude_md(ctx)
    return ["debloat", "undo", "my notes.md"]


# skills install ------------------------------------------------------------------------

@scenario("T skills install", "done")
def _(ctx):
    return ["skills", "install"]


# update --apply ---------------------------------------------------------------------

@scenario("Z3 apply, pip failed", "done", "pip exited 1 upgrading the packages")
def _(ctx):
    import subprocess
    _versions(ctx, "0.2.0")
    ctx.mp.setattr(subprocess, "call", lambda *a, **k: 1)
    return ["update", "--apply"]


@scenario("Z3 apply, pip missing", "done", "pip is missing for")
def _(ctx):
    import subprocess
    _versions(ctx, "0.2.0")

    def missing(*a, **k):
        raise FileNotFoundError("pip")
    ctx.mp.setattr(subprocess, "call", missing)
    return ["update", "--apply"]


@scenario("Z3 apply, pipx", "pipx upgrade sibyl-memory-cli sibyl-memory-hermes sibyl-memory-client "
          "sibyl-memory-mcp sibyl-memory-langgraph")
def _(ctx):
    _versions(ctx, "0.2.0", "pipx")
    return ["update", "--apply"]


@scenario("Z4 apply, upgraded", "sibyl update", "restart any running agent")
def _(ctx):
    import subprocess
    _versions(ctx, "0.2.0")
    ctx.mp.setattr(subprocess, "call", lambda *a, **k: 0)
    return ["update", "--apply"]


# setup X6 to X16 (real wirers) ---------------------------------------------------------

def _hermes_config(text):
    import os
    hh = Path(os.environ["HERMES_HOME"])
    hh.mkdir(parents=True, exist_ok=True)
    (hh / "config.yaml").write_text(text)
    return hh


def _codex(ctx, *, verify=(True, "MCP server starts (stdio handshake ok)"), config=None):
    from sibyl_memory_cli import setup as S
    ctx.mp.setattr(S.CodexWirer, "_mcp_binary_found", lambda self: True)
    ctx.mp.setattr(S.CodexWirer, "verify_mcp_starts", lambda self: verify)
    if config is not None:
        p = Path.home() / ".codex" / "config.toml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(config)


@scenario("X6 hermes has another provider", "sibyl setup hermes --force", "Hermes already has memory provider")
def _(ctx):
    _hermes_config("memory:\n  provider: mem0\n")
    return ["setup", "hermes", "--yes"]


@scenario("X7 overwrite declined at the prompt", "done")
def _(ctx):
    _hermes_config("memory:\n  provider: mem0\n")
    _answers(ctx, "n")
    return ["setup", "hermes"]


@scenario("X8 PyYAML missing", "pip install pyyaml", None, ["sibyl setup hermes"])
def _(ctx):
    from sibyl_memory_cli import setup as S
    _hermes_config("memory:\n  provider: mem0\n")
    ctx.mp.setattr(S, "_import_yaml", lambda: None)
    return ["setup", "hermes", "--yes"]


@scenario("X9 MCP binary missing", "pip install sibyl-memory-mcp", None, ["sibyl setup codex"])
def _(ctx):
    from sibyl_memory_cli import setup as S
    ctx.mp.setattr(S.CodexWirer, "_mcp_binary_found", lambda self: False)
    ctx.mp.setattr(S, "_install_pkg_or_instruct", lambda pkg: f"pip install {pkg} failed (exit 1).")
    ctx.mp.setattr(cli, "_detect_install_method", lambda: "venv")
    return ["setup", "codex", "--yes"]


def _claude_cli(ctx, add_rc):
    from sibyl_memory_cli import setup as S
    ctx.mp.setattr(S.ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: "/opt/fake/claude"))
    ctx.mp.setattr(S.ClaudeCodeWirer, "_mcp_binary_found", lambda self: True)

    def run(cmd, timeout=None, **k):
        if cmd[:3] == ["claude", "mcp", "add"]:
            return (add_rc, "", "add refused" if add_rc else "")
        return (1, "", "")              # `claude mcp get`: never listed
    ctx.mp.setattr(S, "_run", run)
    return ["setup", "claude-code", "--yes"]


@scenario("X10 claude mcp add failed", "claude mcp add --scope user sibyl-memory -- sibyl-memory-mcp")
def _(ctx):
    ctx.mp.setenv("PATH", str(ctx.tmp / "no-bin"))
    return _claude_cli(ctx, 1)


@scenario("X11 added but not listed", "claude mcp list", "restart Claude Code")
def _(ctx):
    return _claude_cli(ctx, 0)


@scenario("X12 server did not start", "done", "run sibyl-memory-mcp in a terminal")
def _(ctx):
    _codex(ctx, verify=(False, "server exited: Traceback (most recent call last): ImportError"))
    return ["setup", "codex", "--yes"]


@scenario("X12 server reports a missing credential", "sibyl status",
          "the memory server reports a missing credential")
def _(ctx):
    _codex(ctx, verify=(False, "server exited: missing credential file"))
    return ["setup", "codex", "--yes"]


@scenario("X14 codex wired", "sibyl status", "restart Codex so it loads")
def _(ctx):
    _codex(ctx)
    return ["setup", "codex", "--yes"]


@scenario("X14 hermes wired", "sibyl status", "restart Hermes so it loads")
def _(ctx):
    _hermes_config("model: x\n")
    return ["setup", "hermes", "--yes"]


@scenario("X14 codex already wired, no restart", "sibyl status")
def _(ctx):
    _codex(ctx, config='[mcp_servers.sibyl_memory]\ncommand = "sibyl-memory-mcp"\n')
    return ["setup", "codex", "--yes"]


@scenario("X15 codex config write failed", "done", "OpenAI Codex could not be wired (config write failed")
def _(ctx):
    from sibyl_memory_cli import setup as S
    _codex(ctx)

    def denied(self):
        raise PermissionError("permission denied")
    ctx.mp.setattr(S.CodexWirer, "_append_block", denied)
    return ["setup", "codex", "--yes"]


@scenario("X15 hermes plugin install failed", "done", "Hermes could not be wired (install-plugin failed")
def _(ctx):
    from sibyl_memory_cli import setup as S
    _hermes_config("model: x\n")

    def boom(self):
        raise OSError("read-only file system")
    ctx.mp.setattr(S.HermesWirer, "_install_plugin", boom)
    return ["setup", "hermes", "--yes"]


@scenario("X15 hermes config write failed", "done", "Hermes could not be wired (config write failed")
def _(ctx):
    from sibyl_memory_cli import setup as S
    _hermes_config("model: x\n")

    def boom(self, yaml):
        raise PermissionError("permission denied")
    ctx.mp.setattr(S.HermesWirer, "_write_config_with_sibyl", boom)
    return ["setup", "hermes", "--yes"]


@scenario("X16 fresh add declined", "done")
def _(ctx):
    _codex(ctx)
    _answers(ctx, "n")
    return ["setup", "codex"]


@scenario("X4 picker: none", "done")
def _(ctx):
    _codex(ctx, config="")
    _hermes_config("model: x\n")
    _answers(ctx, "n")
    return ["setup"]


# migrate N3 to N10 through cmd_migrate --------------------------------------------------

def _project(ctx, text):
    """A project with one CLAUDE.md; PATH emptied so no harness binary is
    detected (only a HERMES_HOME the scenario creates is)."""
    proj = ctx.tmp / "proj"
    proj.mkdir(exist_ok=True)
    ctx.mp.chdir(proj)
    (proj / "CLAUDE.md").write_text(text)
    empty = ctx.tmp / "empty-bin"
    empty.mkdir(exist_ok=True)
    ctx.mp.setenv("PATH", str(empty))
    return proj


def _extraction(ctx, *, terminal):
    """input() for cmd_migrate: the pause before verify stands in for the
    person's agent running the extraction prompt (it writes one entry); with
    no terminal every prompt then meets end of input, with one every prompt
    is answered yes."""
    def fake(prompt="", *a, **k):
        if "press Enter to verify" in prompt:
            _seed_store(ctx)
        if not terminal:
            raise EOFError
        return "" if "press Enter" in prompt else "y"
    ctx.mp.setattr(builtins, "input", fake)


@scenario("N3 declined", "done")
def _(ctx):
    _project(ctx, CURRENT + HIST)
    _answers(ctx, "n")
    return ["migrate"]


@scenario("N9 no terminal: proceeds, trim not taken", "sibyl status", "this computer is not signed in yet",
          ["sibyl init", "sibyl debloat plan"])
def _(ctx):
    _project(ctx, CURRENT + HIST)
    _extraction(ctx, terminal=False)
    return ["migrate"]


@scenario("N7 signed in, done", "sibyl status", "restart your agent if it was open", ["sibyl debloat plan"])
def _(ctx):
    _signed_in(ctx)
    _project(ctx, CURRENT + HIST)
    _extraction(ctx, terminal=False)
    return ["migrate", "--yes"]


@scenario("N10 no safe trim", "sibyl status", "this computer is not signed in yet",
          ["sibyl init", "sibyl debloat plan"])
def _(ctx):
    _project(ctx, CURRENT)
    _extraction(ctx, terminal=False)
    return ["migrate", "--yes"]


@scenario("N10 backup could not be re-verified", "sibyl status", "this computer is not signed in yet",
          ["sibyl init", "sibyl debloat plan"])
def _(ctx):
    from sibyl_memory_cli import migrate as M
    _project(ctx, CURRENT + HIST)
    ctx.mp.setattr(M, "verify_backup_of", lambda *a, **k: False)
    _extraction(ctx, terminal=True)
    return ["migrate"]


@scenario("N4 backup failed", "sibyl migrate --backup-dir <dir>", "the backup folder")
def _(ctx):
    _project(ctx, CURRENT)
    blocker = ctx.tmp / "not-a-dir"
    blocker.write_text("x")
    return ["migrate", "--yes", "--backup-dir", str(blocker / "sub")]


@scenario("N5 auto-wire incomplete", "sibyl status", "this computer is not signed in yet",
          ["sibyl init", "sibyl setup hermes --force"])
def _(ctx):
    _project(ctx, CURRENT)
    _hermes_config("memory:\n  provider: mem0\n")
    return ["migrate", "--yes", "--no-debloat"]


@scenario("N8 database unreadable", "sibyl status", "/", ["sibyl init"])
def _(ctx):
    _project(ctx, CURRENT)
    (ctx.tmp / "memory.db").write_bytes(b"this is not sqlite" * 64)
    return ["migrate", "--yes"]


# health H1c / H2c -----------------------------------------------------------------------

@scenario("H1c hermes package missing", "pip install sibyl-memory-hermes")
def _(ctx):
    import sys
    ctx.mp.setitem(sys.modules, "sibyl_memory_hermes", None)
    return ["health"]


@scenario("H2c health failed", "sibyl status")
def _(ctx):
    import sibyl_memory_hermes as SH

    class Broken:
        def __init__(self, **k):
            raise RuntimeError("database is locked")
    ctx.mp.setattr(SH, "SibylMemoryProvider", Broken)
    return ["health"]


@scenario("H2c health unexpected result", "sibyl status")
def _(ctx):
    import sibyl_memory_hermes as SH

    class Odd:
        def __init__(self, **k):
            pass

        def health(self):
            return "fine"
    ctx.mp.setattr(SH, "SibylMemoryProvider", Odd)
    return ["health"]


# Each extension scenario reaches the path it is named for (a line only that
# path prints), so a fixture drift cannot turn it into a copy of another row.
REACHES = {
    "A3 other account, no terminal": "Not a terminal: re-run with --yes to move them.",
    "A4 other account, declined": "Nothing moved.",
    "A6 adoption refused": "does not match this store",
    "A7 nothing to move": "Nothing to move:",
    "A7 moved": "Moved 2 rows",
    "A2 search, no match": "(no matches for 'zebra')",
    "G1 undo without a terminal": "Not a terminal: re-run with --yes to confirm.",
    "G manual, back from auto": "is back to manual",
    "Z3 apply, pip failed": "Running: ",
    "X6 hermes has another provider": "Memory provider overwrite declined.",
    "X8 PyYAML missing": "PyYAML not installed.",
    "X9 MCP binary missing": "not on PATH",
    "X10 claude mcp add failed": "`claude mcp add` failed (exit 1)",
    "X11 added but not listed": "is not in `claude mcp list`",
    "X12 server did not start": "MCP server could not start.",
    "X14 codex already wired, no restart": "already has the sibyl-memory MCP server",
    "X15 codex config write failed": "config write failed: PermissionError",
    "X15 hermes plugin install failed": "install-plugin failed: OSError",
    "X15 hermes config write failed": "config write failed: PermissionError",
    "X16 fresh add declined": "Declined by user.",
    "N3 declined": "Aborted. Nothing was changed.",
    "N9 no terminal: proceeds, trim not taken": "no terminal: CLAUDE.md not trimmed (default no)",
    "N10 no safe trim": "CLAUDE.md: no safe trim found; left as is.",
    "N10 backup could not be re-verified": "could not be re-verified; skipping trim.",
    "N4 backup failed": "Backup failed:",
    "N5 auto-wire incomplete": "hermes: auto-wire incomplete",
    "N8 database unreadable": "exists but is unreadable",
    "H1c hermes package missing": "sibyl-memory-hermes not installed.",
    "H2c health failed": "Health check failed: RuntimeError",
    "H2c health unexpected result": "Health check returned an unexpected result.",
}


@pytest.mark.parametrize("sid", list(REACHES))
def test_sweep_extension_reaches_its_path(sid, ctx, capsys) -> None:
    cap, *_ = run_scenario(sid, ctx, capsys)
    assert REACHES[sid] in cap.out, (sid, cap.out[-1500:])


CURRENT = "# Agent rules\n\nRun the tests first.\nUse the venv.\nKeep answers short.\nNo secrets.\n\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))


def _bloated_claude_md(ctx, *, fresh=False):
    """The lifecycle suite's trimmable instruction file (a History section),
    last modified an hour ago unless ``fresh`` (a file edited minutes ago is
    refused)."""
    import os
    proj = ctx.tmp / "proj"
    proj.mkdir(exist_ok=True)
    ctx.mp.chdir(proj)
    f = proj / "AGENTS.md"
    f.write_text(CURRENT + HIST)
    if not fresh:
        old = _time.time() - 3600
        os.utime(f, (old, old))


# ---- runner -------------------------------------------------------------------

@pytest.fixture()
def ctx(tmp_path, server, refresh, monkeypatch, test_signer):
    def mint(**kw):
        return test_signer.mint(account_id=ACCT, session_token=BEARER, **kw)
    return SimpleNamespace(tmp=tmp_path, cred=tmp_path / "credentials.json", server=server,
                           refresh=refresh, mp=monkeypatch, mint=mint)


def run_scenario(sid, ctx, capsys):
    fn, nxt, human, later = SCENARIOS[sid]
    tail = fn(ctx)
    cli.main(["--credentials", str(ctx.cred), "--db", str(ctx.tmp / "memory.db"),
              "--tier-cache", str(ctx.tmp / "tier_cache.json")] + tail)
    cap = capsys.readouterr()
    return cap, nxt, human, later


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_row(sid, ctx, capsys) -> None:
    cap, nxt, human, later = run_scenario(sid, ctx, capsys)
    err = cap.err
    if sid.startswith(("debloat", "G")):
        # the one known stderr line: a debloat config warning (spec 4.2)
        err = "\n".join(ln for ln in err.splitlines() if "(defaults used)" not in ln)
    h = assert_convention(cap.out, err)
    assert h["next"] == nxt, (sid, cap.out[-1500:])
    if human is None:
        assert h["human"] is None, (sid, h["block"])
    else:
        assert h["human"] and h["human"].startswith(human), (sid, h["block"])
    if later is not None:
        assert h["later"] == later, (sid, h["block"])


def test_x1_unknown_target(capsys) -> None:
    """argparse refuses an unknown target on the command line; a direct call
    (setup driven programmatically) still gets the block."""
    import argparse
    from sibyl_memory_cli import setup as S
    rc = S.cmd_setup(argparse.Namespace(target="nonsense", yes=False, force=False, dry_run=False))
    h = assert_convention(capsys.readouterr().out)
    assert rc == 1 and h["next"] == "sibyl setup <hermes|claude-code|codex>"
    assert h["human"].startswith("which agent you use")


# ---- waits (asserted before the bound response is served) ------------------------

def test_init_wait_lines_print_before_the_bind(tmp_path, server, capsys) -> None:
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    server.on("GET", "/api/plugin/check", {"bound": False}, bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    wait = next(i for i, ln in enumerate(lines) if ln.startswith("waiting: for the bind at auth.sibyllabs.org"))
    ask = next(i for i, ln in enumerate(lines) if ln.startswith("ask your human: open "))
    assert ask == wait - 1
    assert wait < next(i for i, ln in enumerate(lines) if "Activated." in ln)
    assert "(up to 1:00)" in lines[wait]
    assert "with email plus the code" in lines[ask]
    assert handoff(out)["next"] == "sibyl setup"


def test_init_wait_names_device_approval_when_the_feature_is_on(tmp_path, server, capsys) -> None:
    server.on("POST", "/api/plugin/session-init",
              {"pairing_ttl_seconds": 60, "features": dict(FEATURES_ON, device_approval=True)})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    ask = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ask your human: "))
    # F12: the approve path comes first, the activation page second.
    assert ask.startswith(f"ask your human: if you already have a Sibyl Memory account, "
                          f"approve this computer at {ORIGIN}/activate?r=")
    assert "; otherwise open https://auth.sibyllabs.org/" in ask
    assert ask.index("/activate?r=") < ask.index("auth.sibyllabs.org/")


_STALE_WIRING = ("~/.hermes/config.yaml", "pip install sibyl-memory-mcp", "provider: sibyl",
                 "SibylMemoryProvider", "install-plugin")


def test_init_wiring_line_points_at_setup_when_nothing_is_wired(tmp_path, server, capsys) -> None:
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    out = capsys.readouterr().out
    after = out[out.index("Activated."):]
    assert after.count("wire it into your agent: sibyl setup (wires Claude Code, Codex and Hermes)") == 1
    for stale in _STALE_WIRING:
        assert stale not in after, stale
    assert handoff(out)["next"] == "sibyl setup"


def test_init_prints_no_wiring_block_when_an_agent_is_wired(tmp_path, server, capsys) -> None:
    _wire_claude_code()
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    out = capsys.readouterr().out
    after = out[out.index("Activated."):]
    assert "wire it into your agent" not in after
    for stale in _STALE_WIRING:
        assert stale not in after, stale
    h = handoff(out)
    assert h["next"] == "sibyl status" and "sibyl setup" not in h["later"]


# session-init's USDC-send bind fields (null while USDC sign-in is off).
USDC_BIND_ON = {"bind_amount_micros": 123456, "bind_bus_wallet": "0x5ab927893db6f35083a82bd69c2c091fbe488f32"}


def test_init_wait_f12_keeps_the_four_way_sentence_when_approval_is_off(tmp_path, server, capsys) -> None:
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON,
                                                   **USDC_BIND_ON})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    out = capsys.readouterr().out
    ask = next(ln for ln in out.splitlines() if ln.startswith("ask your human: "))
    assert ask.startswith("ask your human: open https://auth.sibyllabs.org/")
    assert "approve" not in ask and "/activate?r=" not in out
    assert ask.endswith("or by sending USDC from a mobile wallet; I pick it up here")


def test_init_wait_f12_one_decision_first(tmp_path, server, capsys) -> None:
    server.on("POST", "/api/plugin/session-init",
              {"pairing_ttl_seconds": 60, "features": dict(FEATURES_ON, device_approval=True),
               **USDC_BIND_ON})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("ask your human: "))
    ask = lines[i]
    assert lines[i + 1].startswith("waiting: for the bind at auth.sibyllabs.org")
    import re
    first, _, second = ask.partition("; otherwise ")
    m = re.search(r"/activate\?r=[0-9a-f]{64} with the code (\d{3} \d{3})$", first)
    assert m, first
    code_row = next(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines if ln.lstrip().startswith("Code"))
    assert m.group(1) in code_row
    assert second.startswith("open https://auth.sibyllabs.org/")
    assert "email plus that code, a desktop wallet, or USDC from a mobile wallet; I pick it up here" in second
    assert ask.count("?r=") == 1 and "\u2014" not in ask


def test_init_approval_wait(tmp_path, server, capsys) -> None:
    url = f"{ORIGIN}/activate?r=" + "0123456789abcdef" * 4
    waiting = {"bound": False, "approval": {"required": True, "approved": False, "declined": False,
                                            "approve_url": url}}
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    server.on("GET", "/api/plugin/check", waiting, waiting, bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    out = capsys.readouterr().out
    assert out.count("waiting: for approval at account.sibyllabs.org") == 1
    assert f"ask your human: on a computer already signed in, open {url}" in out


def test_init_device_limit_interactive_wait(tmp_path, server, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "y")
    lim = {"code": "device_limit", "limit": 1, "tier": "free", "devices": [{"label": "laptop"}],
           "manage_url": f"{ORIGIN}/security"}
    server.on("POST", "/api/plugin/session-init", err(409, lim))
    server.on("GET", "/api/plugin/check", bound(email="u@example.com"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force", "--no-email"])
    out = capsys.readouterr().out
    assert "ask your human: on the account page choose Sign out next to laptop" in out
    assert "waiting: for the bind (up to " in out


def test_upgrade_wait_lines(tmp_path, server, capsys) -> None:
    cred = write_creds(tmp_path / "c.json")
    server.on("POST", "/api/plugin/access",
              dict(ACCESS, account={"purchase_blocked": True}), dict(ACCESS, tier="pro", source="staker"))
    server.on("POST", "/api/plugin/upgrade-nonce", {"exchange_url": f"{ORIGIN}/x"})
    cli.main(["--credentials", str(cred), "upgrade"])
    out = capsys.readouterr().out
    assert "waiting: for the plan change (up to 30:00)" in out
    ask = next(ln for ln in out.splitlines() if ln.startswith("ask your human: Pro is a purchase"))
    assert "if the page refuses, stop and I will run sibyl email add" in ask
    assert handoff(out)["next"] == "sibyl refresh"


# ---- interactive rows ---------------------------------------------------------------

def test_e9_three_wrong_codes(tmp_path, server, capsys, monkeypatch) -> None:
    cred = write_creds(tmp_path / "c.json")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "111111")
    server.on("POST", "/api/plugin/email-verify/request", {"to_masked": "b***@e***.com"})
    server.on("POST", "/api/plugin/email-verify/confirm", err(400, {"code": "wrong_code", "attempts_remaining": 1}))
    cli.main(["--credentials", str(cred), "email", "verify"])
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "sibyl email verify" and h["human"].startswith("the code was wrong three times")


def test_e8_skipped_at_the_prompt(tmp_path, server, capsys, monkeypatch) -> None:
    cred = write_creds(tmp_path / "c.json")
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    server.on("POST", "/api/plugin/email-verify/request", {"to_masked": "b***@e***.com",
                                                            "expires_in_seconds": 600})
    cli.main(["--credentials", str(cred), "email", "verify"])
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "sibyl email confirm <code>"
    assert h["human"] == ("check b***@e***.com for a six digit code from Sibyl Labs (valid 10 minutes) "
                          "and tell me the code")


def test_i11_interactive_confirm_hands_on_to_setup(tmp_path, server, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    answers = iter(["name@example.com", "123456"])
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(answers))
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60, "features": FEATURES_ON})
    server.on("GET", "/api/plugin/check", bound(account={"email_verified": False, "email_required": True}))
    server.on("POST", "/api/plugin/email-verify/request", {"to_masked": "n***@e***.com"})
    server.on("POST", "/api/plugin/email-verify/confirm", {"ok": True})
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "sibyl setup" and h["human"] is None


def test_i14_rows_left_for_another_account(tmp_path, server, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_adopt_after_sign_in", lambda *a, **k: True)
    monkeypatch.setattr(cli._gate, "write_entitlement_atomic", lambda *a, **k: tmp_path / "e.json")
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 60})
    server.on("GET", "/api/plugin/check", bound(email="u@example.com", entitlement="SE1.a.b"))
    cli.main(["--credentials", str(tmp_path / "c.json"), "init", "--force"])
    h = assert_convention(capsys.readouterr().out)
    assert h["later"] == ["sibyl memory adopt"] and h["next"] == "sibyl setup"


def test_g2_declined_at_the_prompt(tmp_path, capsys, monkeypatch) -> None:
    from sibyl_memory_cli import lifecycle_cli as LC
    monkeypatch.setattr(LC.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "n")
    monkeypatch.chdir(tmp_path)
    cli.main(["skills", "uninstall"])
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "done" and h["human"] is None


def test_g1_skills_uninstall_without_a_terminal(capsys) -> None:
    cli.main(["skills", "uninstall"])
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "sibyl skills uninstall --yes" and h["human"].startswith("Remove the save/debloat")


# ---- --json documents (no text block) ---------------------------------------------

def test_z1_update_json_carries_the_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_installed_version", lambda pkg: "0.1.0")
    monkeypatch.setattr(cli, "_pypi_latest", lambda pkg, timeout=4.0: "0.2.0")
    monkeypatch.setattr(cli, "_detect_install_method", lambda: "pep668")
    cli.main(["update", "--json"])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["next"] == "sibyl update --apply"
    assert doc["human_action"].startswith("this Python is externally managed")
    assert doc["any_outdated"] is True and "packages" in doc
    assert "next: " not in out


def test_g4_debloat_json_carries_the_keys(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    cli.main(["debloat", "--json"])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["next"] == "done" and doc["human_action"] is None
    assert "next: " not in out


# ---- access states (one table, section 1.7) ------------------------------------------

@pytest.mark.parametrize("info,nxt,human", [
    ({"state": "fresh"}, "done", None),
    ({"state": "fresh", "mem": "read_only"}, "sibyl email add <address>", "an email address to confirm"),
    ({"state": "fresh", "mem": "read_only", "acct": {"devices": {"n": 2, "limit": 1, "over": True}}},
     "sibyl devices", "more computers are signed in"),
    ({"state": "stale", "grace_left_seconds": 3 * 86400}, "sibyl refresh",
     "connect this computer to the internet once; memory keeps working 3 more days"),
    ({"state": "bootstrap", "grace_left_seconds": 86400}, "sibyl refresh", "connect this computer"),
    ({"state": "no_account"}, "sibyl init", None),
    ({"state": "locked", "code": "signed_out"}, "sibyl init", None),
    ({"state": "locked", "code": "account_deleted"}, "sibyl init", "this account was deleted"),
    ({"state": "locked", "code": "invalid_entitlement"}, "sibyl init --force", None),
    ({"state": "locked", "code": "wrong_device"}, "sibyl init --force", None),
    ({"state": "locked", "code": "expired_offline"}, "sibyl refresh", "connect this computer"),
    ({"state": "locked", "code": "setup_incomplete"}, "sibyl refresh", "connect this computer"),
    ({"state": "locked", "code": "under_review"}, "done", "your account is under review"),
    ({"state": "locked", "code": "client_too_old"},
     "pip install -U sibyl-memory-client sibyl-memory-mcp sibyl-memory-hermes sibyl-memory-cli", None),
    ({"state": "locked", "code": "policy_none"}, "sibyl email add <address>", "an email address to confirm"),
    ({"state": "fresh", "tier_known": False, "tier": "platinum"}, "sibyl update", None),
    ({"state": "fresh", "refresh_code": "tier_unknown"}, "done", None),
])
def test_access_handoff_table(info, nxt, human) -> None:
    got_next, got_human = G.access_handoff(info)
    assert got_next == nxt
    if human is None:
        assert got_human is None
    else:
        assert got_human.startswith(human)


# ---- 5.3 findings -----------------------------------------------------------------------

def test_finding_1_setup_picker_survives_end_of_input(tmp_path, monkeypatch, capsys) -> None:
    from sibyl_memory_cli import setup as S

    class W:
        def __init__(self, name, initial):
            self.name, self.initial, self.display_name = name, initial, name.title()

        def is_present(self):
            return True

        def current_state(self):
            return {"wired_with_sibyl": True, "settings_path": "/x"}

        def wire(self, **k):
            return S.WireOutcome(self.name, "wired", "wired")

    monkeypatch.setattr(S, "ALL_WIRERS", {"hermes": lambda **k: W("hermes", "h"),
                                          "codex": lambda **k: W("codex", "c")})
    rc = cli.main(["setup"])
    out = capsys.readouterr().out
    assert "no terminal: wiring all detected (same as --yes)" in out
    h = assert_convention(out)
    assert h["next"] == "sibyl status" and h["human"].startswith("restart Hermes / Codex")
    assert rc == 0


def test_finding_2_save_fact_over_the_cap_is_not_a_traceback(tmp_path, monkeypatch, capsys) -> None:
    from sibyl_memory_cli import lifecycle_cli as LC
    from sibyl_memory_client.exceptions import CapExceededError

    class C:
        def set_entity(self, *a, **k):
            raise CapExceededError("You're past the 5 MB free-tier cap.", current_size=1, cap=1)
    monkeypatch.setattr(LC, "_client", lambda args: C())
    assert cli.main(["save", "--fact", "x"]) == 1
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "done" and h["later"] == ["sibyl upgrade"]


def test_finding_3_migrate_does_not_say_done_with_nothing_extracted(tmp_path, monkeypatch, capsys) -> None:
    from sibyl_memory_cli import migrate as M
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    monkeypatch.setattr(M, "run_guided_setup", lambda **k: {
        "ok": True, "later": [], "phases": {"backup": {"ok": True, "dir": str(tmp_path / "bk"), "files": 1},
                                            "wire": {}, "verify": {"new_total": 0, "by_category": {}}}})
    cli.main(["migrate", "--yes"])
    out = capsys.readouterr().out
    assert "Backed up and wired. Extraction has not run yet." in out
    assert "Done. Your memory now lives in Sibyl" not in out
    assert handoff(out)["next"] == "sibyl status"


def test_finding_4_expired_both_paths(capsys) -> None:
    with_msg = err(400, {"code": "expired", "error": "server says expired"})
    without = err(400, {"code": "expired"})
    assert cli._email_error_message(with_msg) == "server says expired"
    assert cli._email_error_message(without) == "that code expired. run sibyl email verify for a new one."
    assert cli._email_error_handoff(with_msg)[0] == cli._email_error_handoff(without)[0] == "sibyl email verify"


# ---- migrate GuidedIO no-terminal lines (N6, N9) ----------------------------------------

def test_n6_n9_guided_io_end_of_input_lines(tmp_path, monkeypatch) -> None:
    from sibyl_memory_cli import migrate as M
    home = tmp_path / "home"
    home.mkdir()
    hist = "\n".join(f"## 2025-01-1{i % 9} session {i}\n\n" + ("Did a thing. " * 40) + "\n" for i in range(30))
    (home / "CLAUDE.md").write_text("# Project\n\nRules.\n\n## History\n\n" + hist)
    io = M.GuidedIO()
    rep = M.run_guided_setup(home=home, cwd=None, db_path=tmp_path / "m.db", backup_parent=tmp_path,
                             io=io, wirers={})
    text = "\n".join(io.lines)
    assert "no terminal to wait on: verify runs now and will count 0" in text
    assert "ask your human: nothing; I run the extraction prompt above myself in this session" in text
    trims = [ln for ln in io.lines if ln.startswith("no terminal: ") and "not trimmed (default no)" in ln]
    if rep["phases"].get("debloat"):
        assert trims and "later: sibyl debloat apply" in trims[0]
        assert "sibyl debloat plan" in rep["later"]
