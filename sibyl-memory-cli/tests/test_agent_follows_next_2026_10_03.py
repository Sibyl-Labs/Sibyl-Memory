"""End to end: an agent that follows only `next:` lines (spec agent-handoff-messages
2026-10-03, 4.4).

No TTY (capsys) and no monkeypatching of `_is_interactive`, so the real
non-interactive paths run. The stub api is a routing fake over
`cli.http_request`; the gate's /entitlement transport is the stub's too. The
driver is the agent: it runs a command, reads the captured stdout, takes the
final `next:` line, substitutes `<address>` with an address it was given, and
for `<code>` asks its human (reads the stub mailbox) only after checking an
`ask your human:` line is present. It stops at `next: done` or after eight steps.
A second run against an old api (no `features`, no `account`, no
`entitlement`) must still complete.
"""
from __future__ import annotations

import json

import pytest

from conftest import assert_convention
from sibyl_memory_cli import cli
from sibyl_memory_client import _entitlement as E

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "22222222-3333-4444-8555-666666666666"
ADDRESS = "name@example.com"


class StubApi:
    """The five endpoints of the flow. Records the code it 'emails'."""

    def __init__(self, signer, *, old_api: bool = False):
        self.signer = signer
        self.old = old_api
        self.checks = 0
        self.mailbox: list[str] = []
        self.email = None
        self.verified = False

    def mint(self, **acct):
        if not acct:
            return self.signer.mint(account_id=ACCT, session_token=BEARER)
        return self.signer.mint(account_id=ACCT, session_token=BEARER, acct=acct)

    def creds(self):
        return {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": self.email,
                "wallet": None, "issued_at": "2026-10-03T00:00:00Z", "bearer_token": BEARER}

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        if method == "POST" and path.startswith("/api/plugin/session-init"):
            if self.old:
                return {"pairing_ttl_seconds": 60}
            return {"pairing_ttl_seconds": 60, "features": {"cli_email_verify": True}}
        if method == "GET" and path.startswith("/api/plugin/check"):
            self.checks += 1
            if self.checks <= 2:
                return {"bound": False}
            resp = {"bound": True, "credentials": self.creds()}
            if not self.old:
                resp["entitlement"] = self.mint(email_required=True, email_verified=False)
                resp["account"] = {"email_verified": False, "email_required": True}
            return resp
        if method == "POST" and path.startswith("/api/plugin/email-verify/request"):
            self.email = (body or {}).get("email") or self.email
            self.mailbox.append("123456")
            return {"to_masked": "n***@e***.com", "expires_in_seconds": 900}
        if method == "POST" and path.startswith("/api/plugin/email-verify/confirm"):
            if not self.mailbox or (body or {}).get("code") != self.mailbox[-1]:
                raise cli.HttpError(400, {"code": "wrong_code", "attempts_remaining": 2}, "https://api/x", {})
            self.verified = True
            return {"ok": True, "credentials": self.creds(),
                    "entitlement": self.mint(email_required=False, email_verified=True)}
        if method == "POST" and path.startswith("/api/plugin/access"):
            resp = {"ok": True, "account_id": ACCT, "tier": "free", "source": "free", "cap_bytes": 5242880}
            if not self.old:
                resp["account"] = {"email_verified": self.verified, "email_required": not self.verified}
            return resp
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")

    def refresh(self, account_id, session_token):
        """The gate's /entitlement: a fresh token for the current account."""
        acct = {} if self.old else {"email_required": not self.verified, "email_verified": self.verified}
        return E.RefreshOutcome("ok", token=self.mint(**acct))


def drive(tmp_path, capsys, api: StubApi) -> list[tuple[str, str]]:
    base = ["--credentials", str(tmp_path / "credentials.json"), "--db", str(tmp_path / "memory.db"),
            "--tier-cache", str(tmp_path / "tier_cache.json")]
    trace: list[tuple[str, str]] = []
    command = "sibyl init"
    for _ in range(8):
        argv = command.split()[1:]
        cli.main(base + argv)
        cap = capsys.readouterr()
        out = cap.out
        if command == "sibyl init":
            # A wait announces itself before the bind is served.
            lines = out.splitlines()
            w = next(i for i, ln in enumerate(lines) if ln.startswith("waiting: "))
            done_at = next(i for i, ln in enumerate(lines) if "Activated." in ln)
            assert w < done_at
        h = assert_convention(out, "\n".join(ln for ln in cap.err.splitlines() if "(defaults used)" not in ln))
        nxt = h["next"]
        trace.append((command, nxt))
        if nxt == "done":
            break
        if "<address>" in nxt:
            assert h["human"], "an address placeholder needs an ask line"
            nxt = nxt.replace("<address>", ADDRESS)
        if "<code>" in nxt:
            assert h["human"] and h["human"].startswith("check "), h["block"]
            nxt = nxt.replace("<code>", api.mailbox[-1])          # the human reads the mailbox
        assert "<" not in nxt, f"unresolvable placeholder: {nxt}"
        assert nxt.startswith("sibyl "), f"the agent only runs sibyl commands here: {nxt}"
        command = nxt
    return trace


@pytest.fixture()
def wired(monkeypatch, tmp_path, test_signer):
    def make(old_api=False):
        api = StubApi(test_signer, old_api=old_api)
        monkeypatch.setattr(cli, "http_request", api)
        monkeypatch.setattr(E, "http_refresh", api.refresh)
        monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
        monkeypatch.setattr(cli, "POLL_INTERVAL_SEC", 0)
        monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
        monkeypatch.delenv("SIBYL_NONINTERACTIVE", raising=False)
        # An isolated machine: no agent harness on PATH (this box has real ones).
        empty = tmp_path / "empty-bin"
        empty.mkdir(exist_ok=True)
        monkeypatch.setenv("PATH", str(empty))
        return api
    return make


def test_agent_follows_only_next_lines(tmp_path, capsys, wired) -> None:
    api = wired()
    trace = drive(tmp_path, capsys, api)
    assert trace == [
        ("sibyl init", "sibyl email add <address>"),
        (f"sibyl email add {ADDRESS}", "sibyl email confirm <code>"),
        ("sibyl email confirm 123456", "sibyl setup"),
        ("sibyl setup", "sibyl status"),
        ("sibyl status", "done"),
    ]
    assert api.verified and json.loads((tmp_path / "credentials.json").read_text())["email"] == ADDRESS


def test_agent_follows_only_next_lines_on_an_old_api(tmp_path, capsys, wired) -> None:
    api = wired(old_api=True)
    trace = drive(tmp_path, capsys, api)
    assert trace == [
        ("sibyl init", "sibyl setup"),
        ("sibyl setup", "sibyl status"),
        ("sibyl status", "done"),
    ]
