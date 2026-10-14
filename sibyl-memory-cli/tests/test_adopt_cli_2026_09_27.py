"""cli 0.5.0 gate fix: memory from before sign-in is readable after sign-in
(defect 1), and the setup-incomplete access row (defect 2).

`sibyl init` moves the store's DEFAULT_TENANT rows into the account on the first
sign-in (client _adopt, one local transaction); `sibyl memory adopt` is the
explicit command, and asks first when the store has held another account.
"""
from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import _account_gate as G
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.testing import NullEntitlementGate, write_entitlement

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "99999999-8888-4777-8666-555555555555"


class FakeServer:
    def __init__(self):
        self.routes: dict = {}

    def on(self, method, prefix, resp):
        self.routes[(method, prefix)] = resp
        return self

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        for (m, prefix), resp in self.routes.items():
            if m == method and path.startswith(prefix):
                if isinstance(resp, cli.HttpError):
                    raise resp
                return json.loads(json.dumps(resp))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")


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


def mint(signer, **kw):
    return signer.mint(account_id=ACCT, session_token=BEARER, **kw)


def write_creds(path: Path, **over) -> Path:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
         "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    c.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))
    return path


def run(tail, cred, tmp_path):
    return cli.main(["--credentials", str(cred), "--db", str(tmp_path / "memory.db"),
                     "--tier-cache", str(tmp_path / "tier_cache.json")] + tail)


def seed(db: Path, tenant: str = DEFAULT_TENANT, name: str = "Blocktronics") -> None:
    c = MemoryClient.local(str(db), tenant_id=tenant, entitlement_gate=NullEntitlementGate())
    c.set_entity("partner", name, {"note": "token forensics suite"})
    c.set_state("session", {"focus": "before sign-in"})
    c.storage.close()


def count(db: Path, tenant: str) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM entities WHERE tenant_id = ?",
                           (tenant,)).fetchone()[0]
    finally:
        con.close()


def bound_check(entitlement):
    return {"bound": True, "entitlement": entitlement, "credentials": {
        "account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
        "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "bearer_token": BEARER}}


# ----------------------------------------------------------------------
# sibyl init: the first sign-in adopts
# ----------------------------------------------------------------------

def test_init_moves_pre_signin_memory_into_the_account(tmp_path, server, test_signer, capsys) -> None:
    db = tmp_path / "memory.db"
    seed(db)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(mint(test_signer)))
    cred = tmp_path / "credentials.json"
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "init", "--force", "--no-email"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 rows from before sign-in moved into your account" in out
    assert "forensics" not in out                     # counts only, never content
    assert count(db, DEFAULT_TENANT) == 0 and count(db, ACCT) == 1
    # ...and `sibyl memory list` (the account tenant) shows it.
    assert run(["memory", "list"], cred, tmp_path) == 0
    assert "Blocktronics" in capsys.readouterr().out


def test_init_leaves_another_accounts_store_alone(tmp_path, server, test_signer, capsys) -> None:
    db = tmp_path / "memory.db"
    seed(db, OTHER, "theirs")
    seed(db)
    server.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    server.on("GET", "/api/plugin/check", bound_check(mint(test_signer)))
    cred = tmp_path / "credentials.json"
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "init", "--force", "--no-email"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "also holds another account's memory" in out and "sibyl memory adopt" in out
    assert count(db, DEFAULT_TENANT) == 1 and count(db, OTHER) == 1 and count(db, ACCT) == 0


# ----------------------------------------------------------------------
# sibyl memory adopt
# ----------------------------------------------------------------------

@pytest.fixture()
def signed_in(tmp_path, test_signer):
    cred = write_creds(tmp_path / "credentials.json")
    write_entitlement(tmp_path, mint(test_signer))
    return cred


def test_memory_adopt_moves_and_is_idempotent(tmp_path, signed_in, capsys) -> None:
    db = tmp_path / "memory.db"
    seed(db)
    assert run(["memory", "adopt"], signed_in, tmp_path) == 0
    out = capsys.readouterr().out
    assert "Moved 2 rows from before sign-in" in out
    assert count(db, DEFAULT_TENANT) == 0 and count(db, ACCT) == 1
    assert run(["memory", "adopt"], signed_in, tmp_path) == 0
    assert "Nothing to move" in capsys.readouterr().out
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT kind FROM tenant_adoptions").fetchall() == [("command",)]
    con.close()


def test_memory_adopt_asks_when_another_account_used_the_store(tmp_path, signed_in, monkeypatch,
                                                                capsys) -> None:
    db = tmp_path / "memory.db"
    seed(db, OTHER, "theirs")
    seed(db)
    # not a terminal, no --yes: refused, nothing moved
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert run(["memory", "adopt"], signed_in, tmp_path) == 1
    assert "--yes" in capsys.readouterr().out
    assert count(db, DEFAULT_TENANT) == 1
    # a terminal, answer "n": nothing moved
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "n")
    assert run(["memory", "adopt"], signed_in, tmp_path) == 1
    assert count(db, DEFAULT_TENANT) == 1
    # a terminal, answer "y": moved; the other account's rows untouched
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "y")
    assert run(["memory", "adopt"], signed_in, tmp_path) == 0
    assert count(db, DEFAULT_TENANT) == 0 and count(db, ACCT) == 1 and count(db, OTHER) == 1


def test_memory_adopt_yes_flag(tmp_path, signed_in) -> None:
    db = tmp_path / "memory.db"
    seed(db, OTHER, "theirs")
    seed(db)
    assert run(["memory", "adopt", "--yes"], signed_in, tmp_path) == 0
    assert count(db, ACCT) == 1


def test_memory_adopt_needs_a_verified_sign_in(tmp_path, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise AssertionError("no network call is allowed here")
    monkeypatch.setattr(cli, "http_request", boom)
    db = tmp_path / "memory.db"
    seed(db)
    assert run(["memory", "adopt"], tmp_path / "absent.json", tmp_path) == 1
    out = capsys.readouterr().out
    assert "sibyl init" in out and "Nothing moved" in out
    assert count(db, DEFAULT_TENANT) == 1


def test_memory_adopt_offline_legacy_credentials_moves_nothing(tmp_path, monkeypatch, capsys) -> None:
    """Credentials, no entitlement, api unreachable: setup_incomplete, nothing moved."""
    cred = write_creds(tmp_path / "credentials.json")
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="x"))
    db = tmp_path / "memory.db"
    seed(db)
    assert run(["memory", "adopt"], cred, tmp_path) == 1
    assert "Nothing moved" in capsys.readouterr().out
    assert count(db, DEFAULT_TENANT) == 1


# ----------------------------------------------------------------------
# sibyl status
# ----------------------------------------------------------------------

def test_status_hint_points_at_adopt(tmp_path, server, signed_in, monkeypatch, capsys) -> None:
    db = tmp_path / "memory.db"
    seed(db, OTHER, "theirs")     # keeps the rows under DEFAULT_TENANT (no auto move)
    seed(db)
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="x"))
    server.on("POST", "/api/plugin/access", {"ok": True, "tier": "free", "source": "free",
                                              "cap_bytes": 5242880})
    assert run(["status"], signed_in, tmp_path) == 0
    out = capsys.readouterr().out
    assert "1 entities from before sign-in" in out and "sibyl memory adopt" in out


def test_status_setup_incomplete_row(tmp_path, server, monkeypatch, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json")
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="x"))
    server.on("POST", "/api/plugin/access", cli.HttpError(0, {"error": "unreachable"}, "x", {}))
    assert run(["status"], cred, tmp_path) == 0
    assert "setup not finished · connect to the internet once to finish setup" in \
        capsys.readouterr().out
    assert G.has_account_access(cred) is False
