"""cli 0.5.0 account gate (spec account-gated-plugin 2026-09-27, 9.5 items 24-29, and 5.6).

`cli.http_request` is replaced by a routing fake; the gate's own refresh
transport (`sibyl_memory_client._entitlement.http_refresh`) is replaced per
test. Nothing here touches the network (the conftest also refuses non-loopback
sockets).
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import stat
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import _account_gate as G
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import write_entitlement

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeServer:
    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []

    def on(self, method, prefix, *responses):
        self.routes[(method, prefix)] = list(responses)
        return self

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        self.calls.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})
        for (m, prefix), queue in self.routes.items():
            if m == method and path.startswith(prefix):
                resp = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(resp, cli.HttpError):
                    raise resp
                return json.loads(json.dumps(resp))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")

    def calls_to(self, prefix):
        return [c for c in self.calls if c["path"].startswith(prefix)]


def err(status, body=None):
    return cli.HttpError(status, body if body is not None else {}, "https://api/x", {})


@pytest.fixture()
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    return s


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)


@pytest.fixture()
def refresh(monkeypatch):
    """Script the gate's /entitlement transport; records calls."""
    state = {"outcome": E.RefreshOutcome("transient", code="offline"), "calls": 0}

    def fn(account_id, session_token):
        state["calls"] += 1
        out = state["outcome"]
        return out(account_id, session_token) if callable(out) else out
    monkeypatch.setattr(E, "http_refresh", fn)
    return state


def write_creds(path: Path, **over) -> Path:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
         "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    c.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))
    return path


def bound_check(entitlement=None):
    body = {"bound": True, "credentials": {
        "account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
        "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "bearer_token": BEARER}}
    if entitlement is not None:
        body["entitlement"] = entitlement
    return body


def init(tmp_path):
    cred = tmp_path / "credentials.json"
    return cli.main(["--credentials", str(cred), "init", "--force", "--no-email"]), cred


def run(tail, cred, tmp_path):
    return cli.main(["--credentials", str(cred), "--db", str(tmp_path / "memory.db"),
                     "--tier-cache", str(tmp_path / "tier_cache.json")] + tail)


def mint(signer, **kw):
    return signer.mint(account_id=ACCT, session_token=BEARER, **kw)


# 24 init ------------------------------------------------------------------

def test_init_writes_entitlement_after_credentials(tmp_path, server, test_signer, capsys) -> None:
    tok = mint(test_signer)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(entitlement=tok))
    rc, cred = init(tmp_path)
    assert rc == 0
    ent = tmp_path / "entitlement.json"
    assert json.loads(ent.read_text())["token"] == tok
    assert stat.S_IMODE(os.stat(ent).st_mode) == 0o600
    assert os.stat(ent).st_mtime_ns >= os.stat(cred).st_mtime_ns
    assert "entitlement" not in json.loads(cred.read_text())   # never inside credentials
    out = capsys.readouterr().out
    assert "Access" in out and tok not in out


def test_init_without_entitlement_writes_nothing_extra_and_drops_the_old(tmp_path, server, test_signer) -> None:
    write_entitlement(tmp_path, mint(test_signer))             # from a previous sign-in
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check())
    rc, _ = init(tmp_path)
    assert rc == 0 and not (tmp_path / "entitlement.json").exists()


@pytest.mark.parametrize("bad", [123, ["SE1.a.b"], "SE1." + "A" * 4100 + ".B", "not-a-token",
                                 "SE1.a.bé"])
def test_init_ignores_a_malformed_entitlement(bad, tmp_path, server) -> None:
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(entitlement=bad))
    rc, cred = init(tmp_path)
    assert rc == 0 and cred.exists()
    assert not (tmp_path / "entitlement.json").exists()


# 25 email confirm ---------------------------------------------------------

def test_email_confirm_rewrites_the_entitlement(tmp_path, server, test_signer) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    write_entitlement(tmp_path, mint(test_signer))
    new = mint(test_signer, acct={"complete": True, "email_verified": True, "email_required": False,
                                  "grace_until": None, "enforced": False, "purchase_blocked": False,
                                  "claimed": True, "frozen": False})
    server.on("POST", "/api/plugin/email-verify/confirm", {"ok": True, "entitlement": new})
    assert run(["email", "confirm", "123456"], cred, tmp_path) == 0
    assert json.loads((tmp_path / "entitlement.json").read_text())["token"] == new


def test_email_confirm_without_entitlement_keeps_the_old(tmp_path, server, test_signer) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    old = mint(test_signer)
    write_entitlement(tmp_path, old)
    server.on("POST", "/api/plugin/email-verify/confirm", {"ok": True})
    assert run(["email", "confirm", "123456"], cred, tmp_path) == 0
    assert json.loads((tmp_path / "entitlement.json").read_text())["token"] == old


# 26 status ------------------------------------------------------------------

ACCESS = {"ok": True, "tier": "free", "source": "free", "cap_bytes": 5242880}


def _status(tmp_path, server, capsys, access=ACCESS):
    server.on("POST", "/api/plugin/access", access)
    rc = run(["status"], tmp_path / "credentials.json", tmp_path)
    return rc, capsys.readouterr().out


def test_status_active(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=mint(test_signer))
    rc, out = _status(tmp_path, server, capsys)
    assert rc == 0 and "active · re-checks in 24h" in out
    assert refresh["calls"] == 1                              # the refresh IS the check


def test_status_offline_grace(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    past = time.time() - 9 * 86400
    write_entitlement(tmp_path, mint(test_signer, now=past), received_at=past)
    rc, out = _status(tmp_path, server, capsys)
    assert "offline · 5 days of grace left · connect once to renew" in out


def test_status_signed_out(tmp_path, server, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    refresh["outcome"] = E.RefreshOutcome("denied", code="signed_out")
    rc, out = _status(tmp_path, server, capsys)
    assert "signed out · run sibyl init" in out


def test_status_under_review(tmp_path, server, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    refresh["outcome"] = E.RefreshOutcome("denied", code="under_review")
    rc, out = _status(tmp_path, server, capsys)
    assert "under review · email sibyl@sibyllabs.org" in out


def test_status_paused_offline_too_long(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    past = time.time() - 20 * 86400
    write_entitlement(tmp_path, mint(test_signer, now=past), received_at=past)
    rc, out = _status(tmp_path, server, capsys)
    assert "paused · offline too long" in out


def test_status_offline_prints_last_known_account_state(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json")
    acct = {"complete": False, "email_verified": False, "email_required": True,
            "grace_until": "2026-10-11T00:00:00Z", "enforced": False, "purchase_blocked": False,
            "claimed": False, "frozen": False}
    write_entitlement(tmp_path, mint(test_signer, acct=acct))
    rc, out = _status(tmp_path, server, capsys, access=err(0, {"error": "unreachable"}))
    assert "as of " in out and "incomplete" in out


def test_status_without_credentials(tmp_path, server, capsys) -> None:
    rc, out = _status(tmp_path, server, capsys)
    assert rc == 0
    assert "Not activated." in out
    assert "agents cannot use memory until you do. your local data is kept." in out


def test_status_hints_default_tenant_rows(tmp_path, server, refresh, capsys) -> None:
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    c = MemoryClient.local(str(tmp_path / "memory.db"), tenant_id=DEFAULT_TENANT)
    c.set_entity("a", "b", {"secret": "never printed"})
    c.storage.close()
    write_creds(tmp_path / "credentials.json")
    rc, out = _status(tmp_path, server, capsys)
    assert "1 entities from before sign-in" in out and "never printed" not in out


# 27 logout / health ---------------------------------------------------------

def test_logout_removes_the_entitlement(tmp_path, server, test_signer, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    write_entitlement(tmp_path, mint(test_signer))
    server.on("GET", "/api/plugin/devices", {"devices": [{"is_this_device": True, "bearer_id": "b1"}]})
    server.on("POST", "/api/plugin/devices", {"revoked": True})
    assert run(["logout"], cred, tmp_path) == 0
    out = capsys.readouterr().out
    assert not (tmp_path / "entitlement.json").exists()
    assert "removed" in out and str(tmp_path / "entitlement.json") in out


def test_health_reports_state_and_kids(tmp_path, test_signer, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    write_entitlement(tmp_path, mint(test_signer))
    run(["health"], cred, tmp_path)
    out = capsys.readouterr().out
    assert "entitlement" in out and "fresh" in out
    assert test_signer.kid in out


# 28 escape hatch ------------------------------------------------------------

@pytest.fixture()
def seeded(tmp_path):
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    db = tmp_path / "memory.db"
    c = MemoryClient.local(str(db), tenant_id=DEFAULT_TENANT)
    c.set_entity("partner", "Blocktronics", {"note": "token forensics suite"})
    c.storage.close()
    return db


@pytest.mark.parametrize("tail,needle", [
    (["memory", "list"], "Blocktronics"),
    (["memory", "search", "forensics"], "Blocktronics"),
    (["memory", "recall", "partner", "Blocktronics"], "forensics"),
])
def test_memory_reads_without_an_account_and_no_network(tail, needle, seeded, tmp_path, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise AssertionError("no network call is allowed here")
    monkeypatch.setattr(cli, "http_request", boom)
    monkeypatch.setattr(E, "http_refresh", boom)
    # no credentials anywhere this command looks, and none in the SDK home either
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    rc = run(tail, tmp_path / "absent-credentials.json", tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and needle in out
    assert G.NO_ACCOUNT_MEMORY_LINE in out


def test_memory_with_an_account_prints_no_line(seeded, tmp_path, test_signer, capsys) -> None:
    from sibyl_memory_client import DEFAULT_TENANT
    cred = write_creds(tmp_path / "credentials.json", tenant_id=DEFAULT_TENANT)
    write_entitlement(tmp_path, mint(test_signer, tenant_id=DEFAULT_TENANT))
    assert run(["memory", "list"], cred, tmp_path) == 0
    assert G.NO_ACCOUNT_MEMORY_LINE not in capsys.readouterr().out


def test_migrate_runs_without_an_account(tmp_path, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise AssertionError("no network call is allowed here")
    monkeypatch.setattr(cli, "http_request", boom)
    monkeypatch.setattr(E, "http_refresh", boom)
    monkeypatch.chdir(tmp_path)
    rc = run(["migrate", "--yes"], tmp_path / "absent.json", tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and G.NO_ACCOUNT_MEMORY_LINE in out


# 5.6: the header on session-authenticated calls ------------------------------

def test_session_calls_carry_the_entitlement_header(tmp_path, server, test_signer) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    tok = mint(test_signer)
    write_entitlement(tmp_path, tok)
    server.on("GET", "/api/plugin/devices", {"devices": []})
    run(["devices"], cred, tmp_path)
    call = server.calls_to("/api/plugin/devices")[-1]
    assert call["headers"]["X-Sibyl-Entitlement"] == tok
    assert call["headers"]["Authorization"] == f"Bearer {BEARER}"


def test_bearer_headers_never_send_a_token_past_grace(tmp_path, test_signer, monkeypatch, refresh) -> None:
    # GATE-15: the refresh-first attempt fails (offline), so nothing is sent.
    cred = write_creds(tmp_path / "credentials.json")
    past = time.time() - 20 * 86400
    write_entitlement(tmp_path, mint(test_signer, now=past), received_at=past)
    monkeypatch.setattr(cli, "_ENT_CRED_PATH", cred)
    h = cli._bearer_headers(json.loads(cred.read_text()))
    assert "X-Sibyl-Entitlement" not in h and h["Authorization"] == f"Bearer {BEARER}"


def test_logout_refreshes_first_when_the_token_is_expired(tmp_path, server, test_signer, refresh) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    past = time.time() - 20 * 86400
    write_entitlement(tmp_path, mint(test_signer, now=past), received_at=past)
    fresh = mint(test_signer)
    refresh["outcome"] = E.RefreshOutcome("ok", token=fresh)
    server.on("GET", "/api/plugin/devices", {"devices": [{"is_this_device": True, "bearer_id": "b1"}]})
    server.on("POST", "/api/plugin/devices", {"revoked": True})
    assert run(["logout"], cred, tmp_path) == 0
    assert refresh["calls"] == 1
    assert server.calls_to("/api/plugin/devices")[0]["headers"]["X-Sibyl-Entitlement"] == fresh


def test_revoke_proceeds_without_the_header_when_refresh_fails(tmp_path, server, test_signer, refresh, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    past = time.time() - 20 * 86400
    write_entitlement(tmp_path, mint(test_signer, now=past), received_at=past)
    refresh["outcome"] = E.RefreshOutcome("transient", code="offline")
    server.on("GET", "/api/plugin/devices", {"devices": [
        {"is_this_device": True, "bearer_id": "me"}, {"is_this_device": False, "bearer_id": "other"}]})
    server.on("POST", "/api/plugin/devices", {"revoked": True})
    assert run(["devices", "revoke", "1"], cred, tmp_path) == 0
    posts = [c for c in server.calls_to("/api/plugin/devices") if c["method"] == "POST"]
    assert posts and "X-Sibyl-Entitlement" not in posts[0]["headers"]


def test_403_entitlement_invalid_retries_once_without_the_header(tmp_path, server, test_signer) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    write_entitlement(tmp_path, mint(test_signer))
    server.on("GET", "/api/plugin/devices",
              err(403, {"code": "entitlement_invalid", "error": "x"}), {"devices": []})
    assert run(["devices"], cred, tmp_path) == 0
    first, second = server.calls_to("/api/plugin/devices")
    assert "X-Sibyl-Entitlement" in first["headers"]
    assert "X-Sibyl-Entitlement" not in second["headers"]


@pytest.mark.parametrize("code,needle", [("entitlement_invalid", "run sibyl init"),
                                         ("client_too_old", "pip install -U")])
def test_gate_403s_print_one_action(code, needle, tmp_path, server, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    server.on("POST", "/api/plugin/email-verify/confirm", err(403, {"code": code, "error": "x"}))
    run(["email", "confirm", "123456"], cred, tmp_path)
    assert needle in capsys.readouterr().out


# 29 source guard: no token value is ever printed ---------------------------

def test_no_token_value_reaches_the_terminal(tmp_path, server, test_signer, refresh, capsys) -> None:
    tok = mint(test_signer)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(entitlement=tok))
    server.on("POST", "/api/plugin/access", ACCESS)
    server.on("GET", "/api/plugin/devices", {"devices": [{"is_this_device": True, "bearer_id": "b1"}]})
    server.on("POST", "/api/plugin/devices", {"revoked": True})
    refresh["outcome"] = E.RefreshOutcome("ok", token=tok)
    rc, cred = init(tmp_path)
    for tail in (["status"], ["health"], ["devices"], ["logout"]):
        run(tail, cred, tmp_path)
    out = capsys.readouterr().out
    assert tok not in out and tok.split(".")[1] not in out and tok.split(".")[2] not in out
    assert BEARER not in out


def test_account_gate_module_prints_nothing() -> None:
    src = Path(G.__file__).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert calls == []
