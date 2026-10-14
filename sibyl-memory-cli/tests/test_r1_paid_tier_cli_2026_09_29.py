"""R1 of the paid tier (plan plugin-paid-tier-2026-09-29, sections 2, 3.3, 4, 5.2,
9.4): CLI side.

* the Claude Code hooks: automatic save by ACTIVE time (a Stop after 61 active
  minutes with one write continues, with none it does not, a second request
  inside the same active hour does not, the numbered request and its
  transcript guard, the `claude -p` heuristic, idle and turn caps, a save made
  elsewhere restarting the clock), the tier-change and skill lines at start;
* `sibyl autosave`, `sibyl refresh`, `sibyl skills proposals`;
* the computer limit: `sibyl init` sends replace on re-activation, prints the
  computers and the replace prompt (terminal) or the instructions (no
  terminal) on the recorded 409 / `/check` fixtures of the plan's contract,
  keeps waiting after a yes; `sibyl devices` and `sibyl status` show the limit;
  the read-only access row names the computer limit.

On 3e13ca4 every test here fails: `autosave`, `refresh` and `skills proposals`
are not commands (argparse exits 2), `init --replace` is not an option, the
hook continued at most once per session (the second-hour and numbered-request
tests), took no notice of `claude -p` runs, had no `stop_reason`, and init
treated a 409 on /check as "Unexpected error" and sent no replace.
"""
from __future__ import annotations

import builtins
import json
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import hooks as H
from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills as SK
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

FIX = json.loads((Path(__file__).parent / "fixtures" / "device_limit_r1.json").read_text())


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def _offline_server(monkeypatch):
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})

    def offline(*a, **k):
        raise cli.HttpError(0, {}, "offline")
    monkeypatch.setattr(cli, "http_request", offline)


@pytest.fixture
def home():
    return Path.home()


@pytest.fixture
def sm(home):
    return home / ".sibyl-memory"


@pytest.fixture
def pro(sm, test_signer):
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="pro"))
    return sm


def _acct(sm: Path) -> list[str]:
    return ["--credentials", str(sm / "credentials.json"), "--db", str(sm / "memory.db")]


def _tenant(sm: Path) -> str:
    c = json.loads((sm / "credentials.json").read_text())
    return c.get("tenant_id") or c["account_id"]


def _client(sm: Path) -> MemoryClient:
    return MemoryClient.local(str(sm / "memory.db"), tenant_id=_tenant(sm),
                              credentials_path=sm / "credentials.json")


def _hook(event: str, payload: dict, home: Path, now: float) -> str:
    return H.run_hook(event, json.dumps(payload), home=home, now=now)


def _bump(home: Path, tid: str, n: int) -> None:
    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_total"] = int(t.get("writes_total") or 0) + n
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
    ST.LifecycleState(home).update(_u)


class _Session:
    """A Claude Code session driven through the hooks: turns of the model
    (prompt -> stop) with the person's pauses between them."""

    def __init__(self, home: Path, tmp: Path, sid: str, t0: float) -> None:
        self.home, self.sid, self.t = home, sid, t0
        self.tr = tmp / f"{sid}.jsonl"
        self.tr.write_text("{}\n")
        _hook("session-start", {"session_id": sid, "cwd": str(tmp)}, home, self.t)

    def turn(self, work_min: float, pause_min: float = 1.0) -> str:
        self.t += pause_min * 60
        _hook("prompt", {"session_id": self.sid}, self.home, self.t)
        self.t += work_min * 60
        return self.stop()

    def stop(self, **extra) -> str:
        payload = {"session_id": self.sid, "stop_hook_active": False, "transcript_path": str(self.tr)}
        payload.update(extra)
        return _hook("stop", payload, self.home, self.t)

    @property
    def state(self) -> dict:
        return ST.LifecycleState(self.home).load()["sessions"][ST.session_key(self.sid)]


# ----------------------------------------------------------------------
# Hooks: active-time automatic save (plan 2.2, 2.3, 2.5, 9.4)
# ----------------------------------------------------------------------

def test_stop_after_61_active_minutes_with_one_write_continues_then_hourly(home, pro, tmp_path):
    tid = _tenant(pro)
    s = _Session(home, tmp_path, "h1", time.time() - 6 * 3600)
    assert s.turn(1) == ""                       # a first turn
    _bump(home, tid, 1)
    assert s.turn(25) == "" and s.turn(25) == ""
    assert s.state["active_seconds"] < 3600
    out = s.turn(9)                              # past 60 active minutes
    assert json.loads(out) == {"decision": "block", "reason": H.stop_reason(1)}
    assert "(save 1 of this session). Then stop." in out
    assert s.turn(20) == ""                      # same active hour: not again
    assert s.stop(stop_hook_active=True) == ""
    assert s.turn(25) == "" and s.turn(10) == ""
    out = s.turn(6)
    assert json.loads(out)["reason"] == H.stop_reason(2)
    st = ST.LifecycleState(home).load()
    assert st["tenants"][tid]["nudges_today"] == 2 and len(s.state["nudges"]) == 2


def test_stop_with_no_writes_never_continues(home, pro, tmp_path):
    s = _Session(home, tmp_path, "h0", time.time() - 6 * 3600)
    for _ in range(6):
        assert s.turn(25) == ""
    assert s.state["active_seconds"] >= 2 * 3600


def test_first_save_rule_still_carries_medium_sessions(home, pro, tmp_path):
    s = _Session(home, tmp_path, "hm", time.time() - 3600)
    s.turn(2)
    _bump(home, _tenant(pro), 5)
    assert json.loads(s.turn(3))["reason"] == H.stop_reason(1)
    _bump(home, _tenant(pro), 5)
    assert s.turn(3) == ""                       # the first rule is once; the hour rule is next


def test_idle_pause_counts_five_minutes_and_a_turn_at_most_thirty(home, pro, tmp_path):
    s = _Session(home, tmp_path, "hi", time.time() - 12 * 3600)
    s.turn(1, pause_min=0)
    a0 = s.state["active_seconds"]
    s.turn(2, pause_min=180)                     # three hours away, then a 2-minute turn
    assert s.state["active_seconds"] - a0 == pytest.approx(5 * 60 + 2 * 60)
    a1 = s.state["active_seconds"]
    s.turn(300, pause_min=0)                     # a turn left waiting for five hours
    assert s.state["active_seconds"] - a1 == pytest.approx(ST.TURN_CAP_SECONDS)


def test_scripted_runs_are_never_continued(home, pro, tmp_path):
    tid = _tenant(pro)
    t = time.time() - 4 * 3600
    _hook("session-start", {"session_id": "p1", "cwd": str(tmp_path)}, home, t)
    _hook("prompt", {"session_id": "p1"}, home, t + 1)            # `claude -p`: one prompt
    _bump(home, tid, 40)
    tr = tmp_path / "p1.jsonl"
    tr.write_text("{}\n")
    for i in range(1, 5):
        assert _hook("stop", {"session_id": "p1", "stop_hook_active": False, "transcript_path": str(tr)},
                     home, t + i * 1800) == ""
    s = _Session(home, tmp_path, "p2", time.time() - 4 * 3600)
    s.turn(1)
    s.turn(1)
    s.t += 3 * 3600
    assert s.stop(transcript_path="") == ""                        # no transcript: scripted
    log = (pro / "lifecycle.log").read_text()
    assert "hook stop: pass" in log


def test_numbered_request_guard_survives_a_rewritten_state(home, pro, tmp_path):
    tid = _tenant(pro)
    s = _Session(home, tmp_path, "hg", time.time() - 8 * 3600)
    s.turn(1)
    _bump(home, tid, 2)
    out = ""
    for _ in range(5):
        out = s.turn(25)
        if out:
            break
    reason = json.loads(out)["reason"]
    assert reason == H.stop_reason(1)
    s.tr.write_text(s.tr.read_text() + json.dumps({"type": "user", "content": reason}) + "\n")
    for _ in range(3):
        s.turn(25)
    second = H.stop_reason(len(s.state["nudges"]) + 1)
    # Claude Code already recorded that numbered request (the state lost it).
    s.tr.write_text(s.tr.read_text() + json.dumps({"type": "user", "content": second}) + "\n")
    assert s.turn(25) == ""


def test_a_save_elsewhere_restarts_the_hook_clock(home, pro, tmp_path):
    tid = _tenant(pro)
    s = _Session(home, tmp_path, "hs", time.time() - 8 * 3600)
    s.turn(1)
    for _ in range(2):
        s.turn(25)
    # another agent (Cursor, a terminal) saves: the DB marker is shared
    c = _client(pro)
    c.enable_lifecycle_counting(home)          # as the MCP server's client does
    c.set_state("sibyl_save:last_save", {"ts": "2026-09-29T00:00:00Z"})
    ST.flush_all()
    _bump(home, tid, 3)                        # new work after that save
    assert s.turn(25) == ""                     # 76 active minutes, but 25 since that save
    assert s.turn(25) == ""
    assert json.loads(s.turn(15))["reason"] == H.stop_reason(1)


def test_free_stop_and_prompt_never_automate(home, sm, tmp_path):
    s = _Session(home, tmp_path, "hf", time.time() - 8 * 3600)
    _bump(home, _tenant(sm), 50)
    for _ in range(6):
        assert s.turn(25) == ""
        assert s.stop() == ""


def test_autosave_off_stops_the_hook(home, pro, tmp_path, capsys):
    assert cli.main(["autosave", "off"]) == 0
    s = _Session(home, tmp_path, "ho", time.time() - 8 * 3600)
    s.turn(1)
    _bump(home, _tenant(pro), 9)
    for _ in range(5):
        assert s.turn(25) == ""


def test_session_start_tier_change_and_skill_lines(home, sm, test_signer, tmp_path):
    t = time.time() - 7200
    _hook("session-start", {"session_id": "t1", "cwd": str(tmp_path)}, home, t)   # first sight: free
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="stake"))
    c = _client(sm)
    now = time.time()
    ev = [{"event_id": str(i), "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(x)), "snippet": "x"}
          for i, x in enumerate((now - 4 * 86400, now - 2 * 86400, now - 1800))]
    with c.storage.transaction() as con:
        for i in range(4):
            con.execute("INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, "
                        "proposed_title, proposed_body, evidence, confidence, summarizer) VALUES "
                        "(?, ?, 'repeated_action', ?, NULL, 'b', ?, ?, 'local-deterministic')",
                        (f"id{i}", _tenant(sm), f"fix-lockfile-{i}", json.dumps(ev), 0.9 - i / 100))
    out = json.loads(_hook("session-start", {"session_id": "t2", "cwd": str(tmp_path)}, home, now))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "this computer is now on Pro" in ctx
    assert ("Sibyl Memory: 4 candidate skills from your recent sessions (fix-lockfile-0, fix-lockfile-1, "
            "fix-lockfile-2 and 1 more). Say /sibyl-save curate to review, or run sibyl skills proposals.") in ctx
    again = _hook("session-start", {"session_id": "t3", "cwd": str(tmp_path)}, home, now + 60)
    assert "candidate skill" not in again                          # 24 h throttle


# ----------------------------------------------------------------------
# sibyl autosave (plan 2.4)
# ----------------------------------------------------------------------

def test_autosave_status_every_bounds_and_free_copy(home, sm, pro, capsys):
    assert cli.main(_acct(sm) + ["autosave"]) == 0
    out = capsys.readouterr().out
    assert "every 60 min of active work" in out and "at most 8 a session and 12 a day" in out
    assert cli.main(_acct(sm) + ["autosave", "every", "45"]) == 0
    assert "every 45 min of active work" in capsys.readouterr().out
    assert ST.LifecycleState(home).load()["autosave"]["interval_seconds"] == 2700
    for bad in ("29", "241"):
        assert cli.main(_acct(sm) + ["autosave", "every", bad]) == 1
        assert "30 to 240" in capsys.readouterr().out
    assert cli.main(_acct(sm) + ["autosave", "every"]) == 1
    assert cli.main(_acct(sm) + ["autosave", "off"]) == 0
    assert "off · sibyl autosave on" in capsys.readouterr().out
    assert ST.LifecycleState(home).load()["autosave"]["enabled"] is False
    assert cli.main(_acct(sm) + ["autosave", "on"]) == 0
    capsys.readouterr()


def test_autosave_on_free_says_pro_and_keeps_the_setting(home, sm, capsys):
    assert cli.main(_acct(sm) + ["autosave", "every", "90"]) == 0
    out = capsys.readouterr().out
    assert "automatic save is on Pro · this setting applies the day you upgrade" in out
    assert ST.LifecycleState(home).load()["autosave"]["interval_seconds"] == 5400


# ----------------------------------------------------------------------
# sibyl refresh (plan 5.2)
# ----------------------------------------------------------------------

def test_refresh_without_an_account_exits_0(tmp_path, capsys):
    assert cli.main(["--credentials", str(tmp_path / "none.json"), "refresh"]) == 0
    out = capsys.readouterr().out
    assert "Not activated. Run `sibyl init`." in out


def test_refresh_reports_the_upgrade_and_what_it_refreshed(home, sm, test_signer, monkeypatch, capsys):
    from sibyl_memory_client import _entitlement as E
    (home / ".claude").mkdir()
    acct = {"complete": True, "email_verified": True, "email_required": False,
            "devices": FIX["entitlement_acct_devices_pro"]}
    monkeypatch.setattr(E, "http_refresh", test_signer.refresh_fn(tier="pro", cap=None, src="stake",
                                                                   acct=acct))
    assert cli.main(_acct(sm) + ["refresh"]) == 0
    out = capsys.readouterr().out
    assert "free -> pro (stake)" in out
    assert "5 MB -> none" in out
    assert "computers" in out and "2 of 10" in out
    assert "procedures" in out and (home / ".claude" / "skills" / "sibyl-save" / "SKILL.md").exists()
    assert "Hooks and shims are the same on every plan" in out
    assert "Running agents pick this up on their next call." in out
    assert "automatic · every 60 min of active work" in out
    assert "0 skill proposals pending" in out
    # nothing changed the second time
    assert cli.main(_acct(sm) + ["refresh"]) == 0
    assert "no change · pro" in capsys.readouterr().out


def test_refresh_offline_exits_2_and_shows_the_last_check_in(sm, monkeypatch, capsys):
    from sibyl_memory_client import _entitlement as E
    monkeypatch.setattr(E, "http_refresh", lambda a, t: E.RefreshOutcome("transient", code="offline"))
    assert cli.main(_acct(sm) + ["refresh"]) == 2
    assert "could not reach the server" in capsys.readouterr().out


# ----------------------------------------------------------------------
# sibyl init: the computer limit (plan 4.2 / 4.4), recorded fixtures
# ----------------------------------------------------------------------

ACCT, BEARER = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FakeServer:
    def __init__(self):
        self.routes: dict = {}
        self.calls: list = []

    def on(self, method, prefix, *resps):
        self.routes[(method, prefix)] = list(resps)

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        self.calls.append((method, path, json.loads(json.dumps(body)) if body is not None else None))
        for (m, prefix), resps in self.routes.items():
            if m == method and path.startswith(prefix):
                r = resps.pop(0) if len(resps) > 1 else resps[0]
                if isinstance(r, cli.HttpError):
                    raise r
                return json.loads(json.dumps(r))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")


def _bound(signer):
    return {"bound": True, "entitlement": signer.mint(account_id=ACCT, session_token=BEARER),
            "credentials": {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
                            "wallet": None, "issued_at": "2026-09-29T00:00:00Z", "bearer_token": BEARER}}


@pytest.fixture
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    opened: list = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    s.opened = opened
    s.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    return s


def _init(tmp_path, *extra):
    cred = tmp_path / "c" / "credentials.json"
    return cred, cli.main(["--credentials", str(cred), "--db", str(tmp_path / "c" / "m.db"),
                           "init", "--force", "--no-email", *extra])


def test_409_at_session_init_without_a_terminal_prints_the_computers_and_what_to_do(
        tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: pytest.fail("no prompt without a terminal"))
    server.on("POST", "/api/plugin/session-init", cli.HttpError(409, FIX["bind_409_body"], "u"))
    cred, rc = _init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2 and not cred.exists()
    assert "This account already has 1 computer signed in; Free is one computer." in out
    assert "work-laptop" in out and "macos" in out and "last seen 2026-09-28 17:02 UTC" in out
    assert "https://account.sibyllabs.org/security" in out and "sibyl init" in out
    assert "Pro works on every computer you use: https://sibyllabs.org/pro" in out     # R1B-4: no constant
    assert "up to 10" not in out
    assert BEARER not in out and "token" not in out.lower()


def test_check_limit_in_a_terminal_asks_to_replace_and_no_is_the_default(tmp_path, server, monkeypatch,
                                                                          capsys, test_signer):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    asked: list = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or "")
    server.on("GET", "/api/plugin/check", FIX["check_at_limit"])
    cred, rc = _init(tmp_path)
    assert rc == 2 and not cred.exists()
    assert asked == ["Open the account page to sign work-laptop out? [y/N]: "]
    assert server.opened[-1].startswith("https://auth") or server.opened[-1].startswith("http")
    assert "https://account.sibyllabs.org/security" not in server.opened


def test_check_limit_yes_opens_the_security_page_and_keeps_waiting(tmp_path, server, monkeypatch,
                                                                    capsys, test_signer):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    server.on("GET", "/api/plugin/check", FIX["check_at_limit"], FIX["check_at_limit"], _bound(test_signer))
    cred, rc = _init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and cred.exists()
    assert "https://account.sibyllabs.org/security" in server.opened
    assert out.count("already has 1 computer signed in") == 1          # shown once
    assert "This terminal keeps waiting." in out and "Activated." in out


def test_a_409_on_check_is_the_limit_not_an_unexpected_error(tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    server.on("GET", "/api/plugin/check", cli.HttpError(409, FIX["bind_409_body"], "u"))
    _cred, rc = _init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2 and "Unexpected error" not in out and "work-laptop" in out


def test_reactivation_sends_replace_with_the_sign_in_this_computer_holds(tmp_path, server, test_signer,
                                                                          monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    server.on("GET", "/api/plugin/check", _bound(test_signer))
    cred = tmp_path / "c" / "credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text(json.dumps({"account_id": ACCT, "tenant_id": ACCT, "tier": "free",
                                "session_token": "old-bearer-0000"}))
    _cred, rc = _init(tmp_path, "--replace")
    assert rc == 0
    body = [b for m, p, b in server.calls if p == "/api/plugin/session-init"][0]
    assert body["replace"] == {"session_token": "old-bearer-0000"}
    # a first activation sends no replace
    server.calls.clear()
    other = tmp_path / "fresh"
    assert cli.main(["--credentials", str(other / "credentials.json"), "--db", str(other / "m.db"),
                     "init", "--no-email"]) == 0
    body = [b for m, p, b in server.calls if p == "/api/plugin/session-init"][0]
    assert "replace" not in body


def test_an_old_api_without_the_limit_is_unchanged(tmp_path, server, test_signer, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    server.on("GET", "/api/plugin/check", {"bound": False}, {"bound": False, "limit": 3}, _bound(test_signer))
    _cred, rc = _init(tmp_path)
    assert rc == 0 and "computer signed in" not in capsys.readouterr().out


# ----------------------------------------------------------------------
# devices, status, the read-only row
# ----------------------------------------------------------------------

def _with_devices(sm, signer, devices, monkeypatch, **kw):
    """The entitlement on disk and every refresh (sibyl status refreshes) carry
    acct.devices, as the api does once it counts computers."""
    from sibyl_memory_client import _entitlement as E
    acct = {"complete": True, "email_verified": True, "email_required": False, "devices": devices}
    write_entitlement(sm, signer.mint(acct=acct, **kw))
    monkeypatch.setattr(E, "http_refresh", signer.refresh_fn(acct=acct, **kw))


def test_devices_and_status_show_the_limit(sm, test_signer, monkeypatch, capsys):
    _with_devices(sm, test_signer, FIX["entitlement_acct_devices_ok"], monkeypatch)
    monkeypatch.setattr(cli, "_security_request", lambda *a, **k: {"devices": [
        {"device_label": "this-one", "is_this_device": True, "install_method": "cli",
         "last_seen_at": "2026-09-29T10:00:00Z", "bearer_id": "b1"}]})
    assert cli.main(_acct(sm) + ["devices"]) == 0
    out = capsys.readouterr().out
    assert "1 active" in out and "1 of 1 (Free) · Pro: up to 10" in out
    assert cli.main(_acct(sm) + ["status"]) == 0
    out = capsys.readouterr().out
    assert "Computers" in out and "1 of 1 (Free) · Pro: up to 10" in out


def test_over_the_limit_reads_as_read_only_for_that_reason(sm, test_signer, monkeypatch, capsys):
    _with_devices(sm, test_signer, FIX["entitlement_acct_devices_over"], monkeypatch, mem="read_only")
    assert cli.main(_acct(sm) + ["status"]) == 0
    out = capsys.readouterr().out
    assert "read only on this computer · more computers signed in than the plan allows" in out
    assert "confirm your email" not in out
    assert "3 of 1 (Free)" in out and "over the limit" in out


# ----------------------------------------------------------------------
# sibyl skills proposals (plan 3.3)
# ----------------------------------------------------------------------

def test_proposals_without_an_account_and_on_free(tmp_path, sm, capsys):
    assert cli.main(["--credentials", str(tmp_path / "none.json"), "skills", "proposals"]) == 0
    assert "Not activated" in capsys.readouterr().out
    assert cli.main(_acct(sm) + ["skills", "proposals"]) == 0
    out = capsys.readouterr().out
    assert "on Pro" in out and "candidate" not in out


def _seed(sm: Path, verb: str = "deploy preview check") -> MemoryClient:
    c = _client(sm)
    start = time.time() - 3 * 86400
    for i in range(4):
        c.write_event(acted={"kind": verb},
                      ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start + i * 20 * 3600)))
    c.learn()
    return c


def test_proposals_list_show_accept_writes_valid_skills_per_harness(home, pro, capsys):
    assert cli.main(_acct(pro) + ["skills", "proposals"]) == 0
    out = capsys.readouterr().out
    assert "pending" in out and "none" in out
    for d in (".claude", ".codex", ".hermes"):
        (home / d).mkdir(exist_ok=True)
    c = _seed(pro)
    p = [x for x in c.learner().list_proposals() if x.pattern_kind == "repeated_action"][0]
    assert cli.main(_acct(pro) + ["skills", "proposals"]) == 0
    out = capsys.readouterr().out
    assert p.id[:8] in out and "repeated_action" in out and "confidence" in out
    assert cli.main(_acct(pro) + ["skills", "proposals", "show", p.id[:8]]) == 0
    assert p.proposed_body.splitlines()[0] in capsys.readouterr().out
    assert cli.main(_acct(pro) + ["skills", "proposals", "accept", p.id[:8]]) == 0
    out = capsys.readouterr().out
    name = SK.skill_name(p.proposed_slug)
    for d in (home / ".claude" / "skills", home / ".codex" / "skills", home / ".hermes" / "skills"):
        f = d / name / "SKILL.md"
        assert f.exists(), f
        assert SK.validate_skill_text(f.read_text(), name) == []
    assert f"reference copy in memory as skill/{p.proposed_slug}" in out
    assert c.learner().get_proposal(p.id).status == "accepted"
    # accepting again rewrites nothing and says so
    assert cli.main(_acct(pro) + ["skills", "proposals", "accept", p.id]) == 0
    assert "already there" in capsys.readouterr().out


def test_proposals_accept_to_a_folder_and_reject_mute_unmute(home, pro, tmp_path, capsys):
    c = _seed(pro, "rotate the logs")
    ps = [x for x in c.learner().list_proposals()]
    assert len(ps) >= 2
    dest = tmp_path / "skills-out"
    assert cli.main(_acct(pro) + ["skills", "proposals", "accept", ps[0].id, "--to", str(dest)]) == 0
    assert (dest / SK.skill_name(ps[0].proposed_slug) / "SKILL.md").exists()
    capsys.readouterr()
    assert cli.main(_acct(pro) + ["skills", "proposals", "reject", ps[1].id, "--note", "noise"]) == 0
    out = capsys.readouterr().out
    assert "not proposed again for 30 days" in out
    assert c.learner().get_proposal(ps[1].id).review_note == "noise"
    assert cli.main(_acct(pro) + ["skills", "proposals", "reject", ps[1].id]) == 1
    assert cli.main(_acct(pro) + ["skills", "proposals", "mute", "co_occurrence", "--days", "7"]) == 0
    st = ST.LifecycleState(home).load()
    assert st["skills"]["muted_kinds"]["co_occurrence"] > time.time() + 6 * 86400
    assert cli.main(_acct(pro) + ["skills", "proposals", "mute", "nonsense"]) == 1
    assert cli.main(_acct(pro) + ["skills", "proposals", "unmute", "co_occurrence"]) == 0
    assert "co_occurrence" not in ST.LifecycleState(home).load()["skills"]["muted_kinds"]
    assert cli.main(_acct(pro) + ["skills", "proposals", "unmute", ps[1].proposed_slug]) == 0
    assert ps[1].proposed_slug in ST.LifecycleState(home).load()["skills"]["unmuted"]
    assert cli.main(_acct(pro) + ["skills", "proposals", "show", "zz"]) == 1


def test_no_new_environment_switch_and_no_token_in_outputs(home, pro, tmp_path, capsys):
    import re
    for mod in ("lifecycle_cli.py", "hooks.py"):
        src = (Path(cli.__file__).parent / mod).read_text()
        assert not re.search(r"SIBYL_[A-Z_]*(AUTO|SAVE|SKILL|DEVICE)", src)
    tok = json.loads((pro / "entitlement.json").read_text())["token"]
    s = _Session(home, tmp_path, "tk", time.time() - 3 * 3600)
    s.turn(1)
    _bump(home, _tenant(pro), 6)
    outs = [s.turn(25) for _ in range(4)]
    cli.main(_acct(pro) + ["autosave"])
    cli.main(_acct(pro) + ["skills", "proposals"])
    text = "".join(outs) + capsys.readouterr().out + (pro / "lifecycle.log").read_text() \
        + (pro / "lifecycle.json").read_text()
    assert tok not in text and "session_token" not in text


# ----------------------------------------------------------------------
# R1B-4 (independent check of a7084df). On a7084df these fail: the status row
# said "the most recently used computer" (F5); the prompt offered "Sign out
# <label> and use this computer instead?" and the upgrade line always said
# "up to 10" (F8); a Stop request suppressed by the transcript guard was
# still recorded (caps, interval) (F10a); one session's compaction made
# another session due (F10b).
# ----------------------------------------------------------------------

def test_r1b4_status_over_the_limit_names_the_newest_sign_in(sm, test_signer, monkeypatch, capsys):
    _with_devices(sm, test_signer, FIX["entitlement_acct_devices_over"], monkeypatch, mem="read_only")
    assert cli.main(_acct(sm) + ["status"]) == 0
    out = capsys.readouterr().out
    assert "over the limit: new memories save only on the computer that signed in most recently" in out
    assert "recently used" not in out


def test_r1b4_the_prompt_says_the_terminal_cannot_sign_out_and_asks_to_open_the_page(
        tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    seen: list = []

    def ask(prompt=""):
        seen.append((capsys.readouterr().out, prompt))
        return ""
    monkeypatch.setattr(builtins, "input", ask)
    server.on("GET", "/api/plugin/check", FIX["check_at_limit"])
    _cred, rc = _init(tmp_path)
    assert rc == 2
    before, prompt = seen[0]
    assert "This terminal cannot sign another computer out; that is done on the account page." in before
    assert prompt == "Open the account page to sign work-laptop out? [y/N]: "
    assert "use this computer instead" not in prompt


def test_r1b4_yes_opens_the_page_and_says_what_to_do_next(tmp_path, server, monkeypatch, capsys, test_signer):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    server.on("GET", "/api/plugin/check", FIX["check_at_limit"], _bound(test_signer))
    _cred, rc = _init(tmp_path)
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0 and "https://account.sibyllabs.org/security" in server.opened
    assert ("Next: choose Sign out next to work-laptop there, then finish the sign-in on the activation "
            "page in your browser. This terminal keeps waiting.") in out


def test_r1b4_upgrade_line_uses_the_server_pro_limit_when_sent(tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    server.on("POST", "/api/plugin/session-init", cli.HttpError(409, FIX["bind_409_body_with_pro_limit"], "u"))
    _cred, rc = _init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2 and "Pro works on every computer you use (up to 10): https://sibyllabs.org/pro" in out
    for bad in (True, "10", -1, 0):
        server.on("POST", "/api/plugin/session-init",
                  cli.HttpError(409, dict(FIX["bind_409_body"], pro_limit=bad), "u"))
        _init(tmp_path)
        out = capsys.readouterr().out
        assert "Pro works on every computer you use: https://sibyllabs.org/pro" in out, bad


def test_r1b4_a_paid_account_at_its_limit_gets_no_upgrade_line(tmp_path, server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    body = dict(FIX["bind_409_body"], tier="pro", limit=10)
    server.on("POST", "/api/plugin/session-init", cli.HttpError(409, body, "u"))
    _cred, rc = _init(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2 and "its plan allows 10" in out and "Pro works" not in out


def test_r1b4_a_request_suppressed_by_the_transcript_guard_costs_nothing(home, pro, tmp_path):
    tid = _tenant(pro)
    s = _Session(home, tmp_path, "hx", time.time() - 8 * 3600)
    s.turn(1)
    _bump(home, tid, 2)
    for _ in range(2):
        assert s.turn(25) == ""
    # The state lost request 1, which the transcript already holds.
    s.tr.write_text(s.tr.read_text() + json.dumps({"type": "user", "content": H.stop_reason(1)}) + "\n")
    assert s.turn(15) == ""                                  # due, but suppressed
    st = ST.LifecycleState(home).load()
    assert s.state["nudges"] == [] and s.state["last_nudge_active"] is None
    assert ST.nudges_today(st["tenants"][tid], s.t) == 0 and s.state["seq_floor"] == 1
    assert "already in the transcript" in (pro / "lifecycle.log").read_text()
    out = s.turn(1)                                          # still due: the next request, numbered past it
    assert json.loads(out)["reason"] == H.stop_reason(2)
    st = ST.LifecycleState(home).load()
    assert len(s.state["nudges"]) == 1 and ST.nudges_today(st["tenants"][tid], s.t) == 1


def test_r1b4_suppressed_requests_do_not_eat_the_daily_cap(home, pro, tmp_path):
    tid = _tenant(pro)
    s = _Session(home, tmp_path, "hd", time.time() - 8 * 3600)
    s.turn(1)
    _bump(home, tid, 2)
    ST.LifecycleState(home).update(lambda st: ST.tenant(st, tid).update(
        nudges_day=time.strftime("%Y-%m-%d", time.localtime(s.t)), nudges_today=11))
    s.turn(25)
    s.turn(25)
    s.tr.write_text(s.tr.read_text() + json.dumps({"type": "user", "content": H.stop_reason(1)}) + "\n")
    assert s.turn(15) == ""
    assert json.loads(s.turn(1))["reason"] == H.stop_reason(2)   # the 12th of the day is still available
    assert s.turn(1) == ""                                       # and then the cap holds


def test_r1b4_one_sessions_compaction_does_not_make_another_due(home, pro, tmp_path):
    tid = _tenant(pro)
    t0 = time.time() - 4 * 3600
    a = _Session(home, tmp_path, "ca", t0)
    b = _Session(home, tmp_path, "cb", t0)
    for x in (a, b):
        x.turn(1)
    _bump(home, tid, 1)
    _hook("pre-compact", {"session_id": "ca", "trigger": "auto"}, home, a.t + 30)
    assert b.turn(2) == ""                                   # B was not compacted
    a.t += 60
    assert json.loads(a.turn(2))["reason"] == H.stop_reason(1)
    assert b.state["compaction_seen"] is False and a.state["compaction_seen"] is True


def test_r1b4_free_compaction_reminder_stays_in_its_session(home, sm, tmp_path):
    tid = _tenant(sm)
    now = time.time()
    _hook("session-start", {"session_id": "fa", "cwd": str(tmp_path)}, home, now)
    _hook("session-start", {"session_id": "fb", "cwd": str(tmp_path)}, home, now)
    _bump(home, tid, 1)
    _hook("pre-compact", {"session_id": "fa", "trigger": "auto"}, home, now + 5)
    assert _hook("prompt", {"session_id": "fb"}, home, now + 10) == ""
    out = json.loads(_hook("prompt", {"session_id": "fa"}, home, now + 20))
    assert out["hookSpecificOutput"]["additionalContext"].startswith("Sibyl Memory: 1 writes since the last save.")
