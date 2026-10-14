"""Tier convention, CLI side (spec tier-convention-spec-2026-10-05, 5.2 and 5.4).

`sibyl status` prints Free / Pro / Elite; an unrecognised signed label prints
the `tier_unknown` access row; a 503 `tier_unknown` from the server keeps the
token and prints the renewal-paused row. The hooks keep a stdlib-only literal
mirror of the client's paid set; the source guard at the bottom is the CLI's
grep gate.
"""
from __future__ import annotations

import ast
import builtins
import json
import re
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import hooks as H
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client import tiers
from sibyl_memory_client.testing import write_entitlement

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCT_OBJ = {"complete": True, "email_verified": True, "email_required": False,
            "grace_until": None, "enforced": False, "purchase_blocked": False}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")


@pytest.fixture()
def access(monkeypatch):
    """Route `/api/plugin/access` (the status server view); everything else 404."""
    state = {"body": {"ok": True, "tier": "free", "source": "free", "cap_bytes": 5242880}}

    def fake(method, path, *, body=None, timeout=15.0, headers=None):
        if method == "POST" and path.startswith("/api/plugin/access"):
            return json.loads(json.dumps(state["body"]))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")
    monkeypatch.setattr(cli, "http_request", fake)
    return state


@pytest.fixture()
def refresh(monkeypatch):
    state = {"outcome": E.RefreshOutcome("transient", code="offline")}

    def fn(account_id, session_token):
        out = state["outcome"]
        return out(account_id, session_token) if callable(out) else out
    monkeypatch.setattr(E, "http_refresh", fn)
    return state


def _creds(path: Path, tier: str) -> Path:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": tier, "email": "u@example.com",
         "wallet": None, "issued_at": "2026-10-05T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))
    return path


def _mint(signer, **kw):
    return signer.mint(account_id=ACCT, session_token=BEARER, **kw)


def _status(tmp_path, capsys) -> tuple[int, str]:
    rc = cli.main(["--credentials", str(tmp_path / "credentials.json"),
                   "--db", str(tmp_path / "memory.db"),
                   "--tier-cache", str(tmp_path / "tier_cache.json"), "status"])
    return rc, capsys.readouterr().out


# ---- hooks mirror ---------------------------------------------------------

def test_hooks_mirror_includes_elite():
    assert H.PAID_TIERS == tiers.PAID_TIERS
    assert "elite" in H.PAID_TIERS and "free" not in H.PAID_TIERS


def _claim_file(sm: Path, signer, tier: str) -> Path:
    write_entitlement(sm, signer.mint(tier=tier))
    return sm / "credentials.json"


@pytest.mark.parametrize("label,paid", [("elite", True), (" Elite ", True), ("PRO", True),
                                        ("stake", True), ("gold", False), ("free", False)])
def test_claims_paid_normalises_the_claim(entitled_home, test_signer, label, paid):
    cred = _claim_file(entitled_home, test_signer, label)
    assert H.claims_paid(cred) is paid
    assert H.claim_label(cred) == label.strip().lower()


def test_hook_logs_tier_unknown_for_an_unrecognised_label(tmp_path, entitled_home, test_signer):
    home = entitled_home.parent
    _claim_file(entitled_home, test_signer, "gold")
    H.run_hook("session-start", json.dumps({"session_id": "s1", "cwd": str(tmp_path)}), home=home)
    logs = list((home / ".sibyl-memory").rglob("lifecycle.log"))
    assert logs, "no lifecycle.log written"
    text = "".join(p.read_text() for p in logs)
    assert "hook session-start: tier_unknown" in text


def test_hook_logs_nothing_for_a_free_label(tmp_path, entitled_home, test_signer):
    home = entitled_home.parent
    _claim_file(entitled_home, test_signer, "free")
    H.run_hook("session-start", json.dumps({"session_id": "s1", "cwd": str(tmp_path)}), home=home)
    text = "".join(p.read_text() for p in (home / ".sibyl-memory").rglob("lifecycle.log"))
    assert "tier_unknown" not in text


# ---- sibyl status ---------------------------------------------------------

def test_status_prints_elite(tmp_path, access, refresh, test_signer, capsys):
    _creds(tmp_path / "credentials.json", "elite")
    acct = dict(ACCT_OBJ, plan={"tier": "elite", "src": "comp"},
                devices={"n": 1, "limit": 10, "over": False})
    refresh["outcome"] = lambda a, s: E.RefreshOutcome(
        "ok", token=_mint(test_signer, tier="elite", cap=None, src="subscription", acct=acct))
    access["body"] = {"ok": True, "tier": "elite", "source": "subscription", "cap_bytes": None}
    rc, out = _status(tmp_path, capsys)
    assert rc == 0
    assert len(re.findall(r"Tier\s+Elite\b", out)) == 2          # local and server view
    assert "ELITE" not in out
    assert "1 of 10 (Elite)" in out and "Pro: up to" not in out
    assert "active · re-checks in" in out
    assert "differs from server tier" not in out


def test_status_legacy_local_label_is_not_drift(tmp_path, access, refresh, test_signer, capsys):
    _creds(tmp_path / "credentials.json", "stake")
    refresh["outcome"] = lambda a, s: E.RefreshOutcome(
        "ok", token=_mint(test_signer, tier="pro", cap=None, src="staker"))
    access["body"] = {"ok": True, "tier": "pro", "source": "staker", "cap_bytes": None}
    rc, out = _status(tmp_path, capsys)
    assert rc == 0
    assert len(re.findall(r"Tier\s+Pro\b", out)) == 2
    assert "STAKE" not in out and "differs from server tier" not in out


def test_status_unknown_label_prints_the_tier_unknown_row(tmp_path, access, refresh, test_signer, capsys):
    _creds(tmp_path / "credentials.json", "gold")
    refresh["outcome"] = lambda a, s: E.RefreshOutcome(
        "ok", token=_mint(test_signer, tier="gold", cap=None, src="subscription"))
    rc, out = _status(tmp_path, capsys)
    assert rc == 0
    assert ('active · access label "gold" is not recognised by this version, treated as Free '
            "(tier_unknown) · run sibyl update") in out
    assert re.search(r"Tier\s+Free\b", out)
    assert "% of free cap" in out or "not created" in out


def test_status_server_503_tier_unknown_keeps_the_token(tmp_path, access, refresh, test_signer, capsys):
    _creds(tmp_path / "credentials.json", "pro")
    tok = _mint(test_signer, tier="pro", cap=None, src="subscription")
    write_entitlement(tmp_path, tok)
    refresh["outcome"] = E.RefreshOutcome("transient", code="tier_unknown")
    rc, out = _status(tmp_path, capsys)
    assert rc == 0
    assert ("renewal paused · access label check (tier_unknown) · keep working; "
            "it retries on its own") in out
    assert json.loads((tmp_path / "entitlement.json").read_text())["token"] == tok


def test_unknown_rows_do_not_hide_locked_states():
    from sibyl_memory_cli import _account_gate as G
    assert G.access_row({"state": "locked", "code": "signed_out", "tier_known": False,
                         "refresh_code": "tier_unknown"}) == "signed out · run sibyl init"
    assert "re-checks in" in G.access_row({"state": "fresh", "tier_known": True, "renews_in_seconds": 3600})


# ---- source guard (the CLI's grep gate) -------------------------------------

def test_no_legacy_tier_literal_outside_the_hooks_mirror():
    """No legacy tier name is spelled in the CLI package except the single
    hooks.py mirror line, and that line parses to the client's paid set."""
    pkg = Path(cli.__file__).parent
    legacy = re.compile(r'"(sync|team|lifetime|stake|enterprise)"')
    paid_set = re.compile(r'frozenset\(\{\s*"pro"')
    hits, mirror = [], []
    for p in sorted(pkg.rglob("*.py")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if p.name == "hooks.py" and line.startswith("PAID_TIERS = frozenset("):
                mirror.append(line)
                continue
            if legacy.search(line) or paid_set.search(line):
                hits.append(f"{p.relative_to(pkg)}:{n}: {line.strip()}")
    assert hits == []
    assert len(mirror) == 1
    literal = ast.literal_eval(mirror[0].split("frozenset(", 1)[1].rsplit(")", 1)[0])
    assert frozenset(literal) == tiers.PAID_TIERS

