"""QA findings F1 and F2 (2026-10-08).

F1: every user-facing tier line shows the tier of the current verified pass
(the saved credentials tier only without a valid pass), and a refresh or a
background re-check rewrites the saved tier so every reader agrees. One tier
per screen.

F2: `sibyl status` shows the billing date from the pass's acct.plan
(`Plan  Pro · ends 8 Oct 2027` / `Pro · renews 8 Oct 2027`), and the access
row says `re-checks in Xh` (the pass re-check), never `renews in`.

`cli.http_request` is a routing fake and the gate's refresh transport is
scripted per test; nothing reaches the network (the conftest refuses it).
"""
from __future__ import annotations

import json
import os
import re
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
PRO_ACCESS = {"ok": True, "tier": "pro", "source": "subscription", "cap_bytes": None}
FREE_ACCESS = {"ok": True, "tier": "free", "source": "free", "cap_bytes": 5242880}
ACCT_BASE = {"complete": True, "email_verified": True, "email_required": False,
             "grace_until": None, "enforced": False, "purchase_blocked": False}


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


@pytest.fixture()
def refresh(monkeypatch):
    state = {"outcome": E.RefreshOutcome("transient", code="offline"), "calls": 0}

    def fn(account_id, session_token):
        state["calls"] += 1
        out = state["outcome"]
        return out(account_id, session_token) if callable(out) else out
    monkeypatch.setattr(E, "http_refresh", fn)
    return state


def write_creds(path: Path, mode: int = 0o600, **over) -> Path:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
         "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    c.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c))
    os.chmod(path, mode)
    return path


def mint(signer, **kw):
    return signer.mint(account_id=ACCT, session_token=BEARER, **kw)


def pro_token(signer, plan=None, **kw):
    acct = dict(ACCT_BASE, plan=dict({"tier": "pro", "src": "card"}, **(plan or {})))
    return mint(signer, tier="pro", cap=None, src="subscription", acct=acct, **kw)


def free_token(signer, **kw):
    acct = dict(ACCT_BASE, plan={"tier": "free", "src": "none"})
    return mint(signer, tier="free", acct=acct, **kw)


def run(tail, tmp_path):
    return cli.main(["--credentials", str(tmp_path / "credentials.json"), "--db", str(tmp_path / "memory.db"),
                     "--tier-cache", str(tmp_path / "tier_cache.json")] + tail)


def status(tmp_path, server, capsys, access):
    server.on("POST", "/api/plugin/access", access)
    rc = run(["status"], tmp_path)
    return rc, capsys.readouterr().out


def tier_rows(out: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^\s+Tier\s{2,}(\S+)", out, re.M)]


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "credentials.json").read_text())


# ---- F1: status after a pass with a different tier than the saved one ------

def test_signed_in_free_then_bought_pro_status_shows_pro_everywhere(tmp_path, server, test_signer,
                                                                    refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="free", signature="sig", signed_at="2026-09-27")
    refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=pro_token(test_signer))
    rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
    assert rc == 0
    assert tier_rows(out) == ["Pro", "Pro"], out           # LOCAL and SERVER agree
    assert "differs from server tier" not in out
    assert "uncapped" in out or "not created" in out          # cap follows the shown tier
    c = saved(tmp_path)
    assert c["tier"] == "pro"                                 # the refresh rewrote the saved tier
    assert "signature" not in c and "signed_at" not in c      # a signed field never changes under an old signature
    assert c["session_token"] == BEARER and c["email"] == "u@example.com"


def test_pro_ended_status_shows_free_even_offline(tmp_path, server, test_signer, refresh, capsys) -> None:
    # saved Pro, the last verified pass says Free, this check-in fails (offline)
    write_creds(tmp_path / "credentials.json", tier="pro")
    write_entitlement(tmp_path, free_token(test_signer))
    rc, out = status(tmp_path, server, capsys, FREE_ACCESS)
    assert tier_rows(out) == ["Free", "Free"], out
    assert saved(tmp_path)["tier"] == "pro"                   # no check-in happened: nothing rewritten
    assert "differs from server tier" not in out


def test_no_valid_pass_falls_back_to_the_saved_tier(tmp_path, server, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="pro")      # no entitlement, refresh fails
    rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
    assert tier_rows(out)[0] == "Pro"
    assert saved(tmp_path)["tier"] == "pro"


def test_stale_tier_cache_is_not_shown_as_a_second_tier(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="pro")
    write_entitlement(tmp_path, pro_token(test_signer))
    (tmp_path / "tier_cache.json").write_text(json.dumps({"tier": "free", "checked_at": time.time()}))
    rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
    line = [ln for ln in out.splitlines() if "Tier cache" in ln][0]
    assert "superseded by the access pass" in line and "free" not in line


def test_refresh_rewrites_the_saved_tier_atomically_keeping_the_mode(tmp_path, server, test_signer,
                                                                     refresh, capsys) -> None:
    cred = write_creds(tmp_path / "credentials.json", mode=0o640, tier="free")
    ino_before = os.stat(cred).st_ino
    (tmp_path / "tier_cache.json").write_text(json.dumps({"tier": "free", "checked_at": time.time()}))
    refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=pro_token(test_signer))
    assert run(["refresh"], tmp_path) == 0
    out = capsys.readouterr().out
    assert "-> pro" in out
    assert saved(tmp_path)["tier"] == "pro"
    assert stat.S_IMODE(os.stat(cred).st_mode) == 0o640       # same file mode
    assert os.stat(cred).st_ino != ino_before                 # replaced, not edited in place
    assert not list(tmp_path.glob("credentials.json.*.tmp"))  # no temp left behind
    assert not (tmp_path / "tier_cache.json").exists()        # the old cached answer goes with it
    # and back to Free when the plan ends
    refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=free_token(test_signer))
    assert run(["refresh"], tmp_path) == 0
    capsys.readouterr()
    assert saved(tmp_path)["tier"] == "free"


def test_whoami_and_init_already_activated_show_the_pass_tier(tmp_path, test_signer, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="free")
    write_entitlement(tmp_path, pro_token(test_signer))
    assert run(["whoami"], tmp_path) == 0
    assert "PRO" in capsys.readouterr().out
    assert run(["init"], tmp_path) == 0
    out = capsys.readouterr().out
    assert re.search(r"Tier\s+PRO", out), out


def test_init_already_activated_without_a_pass_shows_the_saved_tier(tmp_path, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="pro")
    assert run(["init"], tmp_path) == 0
    assert re.search(r"Tier\s+PRO", capsys.readouterr().out)
    assert not (tmp_path / "entitlement.json").exists()       # looking left no file behind


def test_display_tier_rules() -> None:
    assert G.display_tier(None, "pro") == "pro"
    assert G.display_tier({"state": "locked", "tier": "pro"}, "free") == "free"
    assert G.display_tier({"state": "fresh", "tier": "pro", "tier_effective": "pro"}, "free") == "pro"
    assert G.display_tier({"state": "stale", "tier": "free", "tier_effective": "free"}, "pro") == "free"
    # an unrecognised label is shown as it is enforced: Free
    assert G.display_tier({"state": "fresh", "tier": "platinum", "tier_effective": "free"}, "pro") == "free"


# ---- F2: Plan row and the re-check label ----------------------------------

def test_status_plan_ends_on_a_scheduled_cancel(tmp_path, server, test_signer, refresh, capsys,
                                                monkeypatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        write_creds(tmp_path / "credentials.json", tier="pro")
        refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=pro_token(
            test_signer, plan={"ends_at": "2027-10-08T12:00:00.000Z"}))
        rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
        assert re.search(r"^\s+Plan\s+Pro · ends 8 Oct 2027$", out, re.M), out
        assert "renews 8 Oct" not in out
        assert "active · re-checks in" in out and "renews in" not in out
        plan_line = [ln for ln in out.splitlines() if re.match(r"^\s+Plan\s", ln)][0]
        assert "\u2014" not in plan_line                       # no em dash
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_status_plan_renews_for_a_renewing_card_plan(tmp_path, server, test_signer, refresh, capsys,
                                                     monkeypatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        write_creds(tmp_path / "credentials.json", tier="pro")
        refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=pro_token(
            test_signer, plan={"renews_at": "2027-10-08T12:00:00Z"}))
        rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
        assert re.search(r"^\s+Plan\s+Pro · renews 8 Oct 2027$", out, re.M), out
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_status_has_no_plan_row_without_a_date(tmp_path, server, test_signer, refresh, capsys) -> None:
    write_creds(tmp_path / "credentials.json", tier="pro")
    refresh["outcome"] = lambda a, s: E.RefreshOutcome("ok", token=pro_token(test_signer))
    rc, out = status(tmp_path, server, capsys, PRO_ACCESS)
    assert not re.search(r"^\s+Plan\s", out, re.M)


def test_plan_row_rules() -> None:
    fresh = {"state": "fresh", "tier": "pro", "tier_effective": "pro"}
    assert G.plan_row(None) is None
    assert G.plan_row(dict(fresh)) is None
    assert G.plan_row(dict(fresh, plan={"tier": "pro", "src": "card"})) is None
    assert G.plan_row(dict(fresh, plan={"tier": "pro", "src": "card", "ends_at": "garbage"})) is None
    assert G.plan_row({"state": "locked", "plan": {"ends_at": "2027-10-08T12:00:00Z"}}) is None
    row = G.plan_row(dict(fresh, plan={"tier": "pro", "src": "card", "ends_at": "2027-10-08T12:00:00Z"}))
    assert row.startswith("Pro · ends ") and "Oct 2027" in row
    # the tier word is the shown tier, never a second label from the plan
    row = G.plan_row(dict(fresh, plan={"tier": "elite", "src": "comp", "ends_at": "2027-10-08T12:00:00Z"}))
    assert row.startswith("Pro ")


def test_access_row_says_re_checks_not_renews() -> None:
    assert G.access_row({"state": "fresh", "tier_known": True, "renews_in_seconds": 7200}) == \
        "active · re-checks in 2h"
    ro = G.access_row({"state": "fresh", "tier_known": True, "renews_in_seconds": 3600, "mem": "read_only"})
    assert ro.endswith("re-checks in 1h") and "renews" not in ro
