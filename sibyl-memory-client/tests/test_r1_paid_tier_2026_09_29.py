"""R1 of the paid tier (plan plugin-paid-tier-2026-09-29, sections 2, 3, 4, 9.4):
client side.

* active-time automatic save: accounting (idle cap, per-session isolation, the
  shared DB marker restarting another session's clock), bounds on the
  settings, the per-day and per-session caps, the due rule;
* the per-process notice channel (MCP hosts, Hermes): once per interval, free
  never, tier-change line, precedence save > skills;
* skill proposals: relevance filter, 3 named, once per session and 24 h,
  rejected slugs cool off 30 days, twice rejected muted until unmuted, muted
  kinds, free tier refused (TierGateError), the learner's own writes are not
  counted, the Codex quick_validate rules, accepted files survive uninstall;
* the computer-limit read-only copy (acct.devices.over);
* SAVE.md: the Codex row and the headless cadence, hashes consistent.

On 3e13ca4 every test here fails: the names they import (state.note_activity,
reminders.autosave_due, lifecycle.proposals, skills.write_skill,
exceptions.devices_read_only_message, Learner.run(exclude_kinds=...)) did not
exist, and the behavioural equivalents (a second automatic save in a long
session, a rejected slug proposed again the next run, the email copy on a
device-limit read-only token, the Codex prompts row) are shown failing in the
release notes Part D.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills as SK
from sibyl_memory_client.exceptions import EntitlementError, TierGateError
from sibyl_memory_client.lifecycle import proposals as PR
from sibyl_memory_client.lifecycle import reminders as R
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.lifecycle.notices import ProcessNotices
from sibyl_memory_client.testing import write_entitlement


@pytest.fixture(autouse=True)
def _no_heartbeat(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")


def _client(tmp_path: Path, name: str = "m.db") -> MemoryClient:
    return MemoryClient.local(tmp_path / name)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _bump(home: Path, tid: str, n: int) -> None:
    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_total"] = int(t.get("writes_total") or 0) + n
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
    ST.LifecycleState(home).update(_u)


def _save_marker(client: MemoryClient) -> None:
    client.set_state("sibyl_save:last_save", {"ts": _iso(time.time())})


# ----------------------------------------------------------------------
# Active-time accounting (plan 2.1)
# ----------------------------------------------------------------------

def test_idle_gap_counts_at_most_five_minutes():
    st = ST.default_state()
    t0 = 1_000_000.0
    ST.note_activity(st, "s", t0, tenant_id="t", harness="x")
    ST.note_activity(st, "s", t0 + 60, tenant_id="t", harness="x")
    ST.note_activity(st, "s", t0 + 60 + 3 * 3600, tenant_id="t", harness="x")   # overnight gap
    assert st["sessions"]["s"]["active_seconds"] == 60 + ST.IDLE_CAP_SECONDS
    # a clock that went backwards adds nothing and does not move the stamp
    ST.note_activity(st, "s", t0, tenant_id="t", harness="x")
    assert st["sessions"]["s"]["active_seconds"] == 60 + ST.IDLE_CAP_SECONDS


def test_sessions_are_accounted_separately():
    st = ST.default_state()
    for i in range(13):
        ST.note_activity(st, "a", 1000.0 + i * 300, tenant_id="t", harness="x")
    ST.note_activity(st, "b", 1000.0, tenant_id="t", harness="x")
    ST.note_activity(st, "b", 1100.0, tenant_id="t", harness="x")
    assert st["sessions"]["a"]["active_seconds"] == 3600
    assert st["sessions"]["b"]["active_seconds"] == 100


def test_settings_are_bounded_on_read(tmp_path):
    home = Path.home()
    p = home / ".sibyl-memory" / "lifecycle.json"
    p.write_text(json.dumps({"autosave": {"interval_seconds": 5, "max_per_day": 999,
                                          "max_per_session": -3}}))
    cfg = ST.autosave_settings(ST.LifecycleState(home).load())
    assert cfg["interval_seconds"] == ST.AUTOSAVE_INTERVAL_MIN == 1800
    assert cfg["max_per_day"] == 12 and cfg["max_per_session"] == 0
    p.write_text(json.dumps({"autosave": {"interval_seconds": 10 ** 9}}))
    assert ST.autosave_settings(ST.LifecycleState(home).load())["interval_seconds"] == 14400
    p.write_text(json.dumps({"autosave": {"enabled": "yes please"}}))
    assert ST.autosave_settings(ST.LifecycleState(home).load())["enabled"] is True   # only False turns it off
    p.write_text("{not json")
    assert ST.autosave_settings(ST.LifecycleState(home).load()) == ST.default_autosave()


def _session(st, active, *, tid="t", sk="s", writes=1, **kw):
    s = ST.ensure_session(st, sk, tenant_id=tid, harness="x", now=0.0)
    s["active_seconds"] = float(active)
    s.update(kw)
    ST.tenant(st, tid)["writes_since_save"] = writes
    return s


def test_due_rule_interval_writes_and_repeat():
    now = 10_000_000.0
    st = ST.default_state()
    _session(st, 3599, writes=1)
    assert R.autosave_due(st, "t", "s", now) is None          # 59:59 active
    _session(st, 3661, writes=1)
    assert R.autosave_due(st, "t", "s", now) == "interval"    # 61 active minutes, 1 write
    _session(st, 3661, writes=0)
    assert R.autosave_due(st, "t", "s", now) is None          # nothing to save
    _session(st, 3661, writes=1)
    ST.record_nudge(st, "t", "s", now)
    assert R.autosave_due(st, "t", "s", now) is None          # same active hour
    _session(st, 3661 + 3599, writes=1)
    assert R.autosave_due(st, "t", "s", now) is None
    _session(st, 3661 + 3600, writes=1)
    assert R.autosave_due(st, "t", "s", now) == "interval"


def test_due_rule_first_save_and_compaction():
    now = 10_000_000.0
    st = ST.default_state()
    _session(st, 60, writes=4)
    assert R.autosave_due(st, "t", "s", now) is None
    _session(st, 60, writes=5)
    assert R.autosave_due(st, "t", "s", now) == "first"
    ST.record_nudge(st, "t", "s", now)
    _session(st, 3000, writes=9)
    assert R.autosave_due(st, "t", "s", now) is None          # the first rule fires once
    st2 = ST.default_state()
    _session(st2, 10, writes=1, compaction_seen=True)
    assert R.autosave_due(st2, "t", "s", now) == "compaction"
    _session(st2, 10, writes=0)
    assert R.autosave_due(st2, "t", "s", now) is None          # compaction alone, nothing written


def test_caps_per_session_and_per_day_and_off_switches():
    now = time.time()
    st = ST.default_state()
    for i in range(8):
        _session(st, 3600 * (i + 1) + 1, writes=1)
        assert R.autosave_due(st, "t", "s", now) == "interval", i
        ST.record_nudge(st, "t", "s", now)
    _session(st, 3600 * 20, writes=1)
    assert R.autosave_due(st, "t", "s", now) is None           # 8 per session
    # 12 per day across sessions
    st = ST.default_state()
    n = 0
    for sess in range(3):
        for i in range(5):
            _session(st, 3600 * (i + 1) + 1, sk=f"s{sess}", writes=1)
            if R.autosave_due(st, "t", f"s{sess}", now):
                ST.record_nudge(st, "t", f"s{sess}", now)
                n += 1
    assert n == 12
    assert ST.nudges_today(ST.tenant(st, "t"), now + 86400 * 2) == 0   # a new day starts over
    st = ST.default_state()
    _session(st, 7200, writes=3)
    st["autosave"]["enabled"] = False
    assert R.autosave_due(st, "t", "s", now) is None
    st["autosave"]["enabled"] = True
    st["reminders"] = False
    assert R.autosave_due(st, "t", "s", now) is None


def test_a_save_anywhere_restarts_every_open_session_clock():
    st = ST.default_state()
    a = _session(st, 5000, sk="a", writes=3)
    b = _session(st, 4000, sk="b", writes=3)
    a["started_at"] = b["started_at"] = 100.0
    ST.observe_marker(st, "t", 200.0)            # another agent's save (the shared DB marker)
    assert a["active_at_save"] == 5000 and b["active_at_save"] == 4000
    assert R.autosave_due(st, "t", "a", 300.0) is None
    ST.tenant(st, "t")["writes_since_save"] = 2
    a["active_seconds"] = 5000 + 3600
    assert R.autosave_due(st, "t", "a", 300.0) == "interval"


# ----------------------------------------------------------------------
# The per-process notice channel (plan 2.3: MCP hosts, Hermes)
# ----------------------------------------------------------------------

def _drive(n: ProcessNotices, minutes: int, start: float) -> float:
    t = start
    for _ in range(minutes):
        n.note_activity(t)
        t += 60
    return t


def test_process_notice_once_per_interval_and_free_never(tmp_path, entitle_as):
    home = Path.home()
    client = _client(tmp_path)
    client.enable_lifecycle_counting(home)
    tid = client.get_tenant()
    now = time.time()
    n = ProcessNotices("mcp")
    n.started_at = now - 7200
    _bump(home, tid, 2)
    t = _drive(n, 30, now - 7200)
    assert n.next_notice(client, home=home, cwd=tmp_path, now=t) != R.TIER_UP_LINE
    # free: no save request whatever the active time
    t = _drive(n, 45, t)
    for _ in range(3):
        line = n.next_notice(client, home=home, cwd=tmp_path, now=t)
        assert line is None or "Pro" not in line
    entitle_as("pro")
    n2 = ProcessNotices("mcp")
    n2.started_at = now - 7200
    t = _drive(n2, 62, now - 7200)
    got = [n2.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(4)]
    saves = [g for g in got if g and g.startswith("Sibyl Memory Pro:")]
    assert len(saves) == 1
    assert "about 60 minutes of work since the last save" in saves[0]
    assert "headless" in saves[0] and "the `save` prompt" in saves[0]
    t = _drive(n2, 30, t)
    assert not [g for g in (n2.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3))
                if g and g.startswith("Sibyl Memory Pro:")]
    t = _drive(n2, 31, t)
    assert any((g or "").startswith("Sibyl Memory Pro:")
               for g in (n2.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3)))
    st = ST.LifecycleState(home).load()
    assert st["tenants"][tid]["nudges_today"] == 2
    assert len(st["sessions"][n2.session_key]["nudges"]) == 2


def test_process_notice_is_withheld_when_another_channel_carries_it(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = _client(tmp_path)
    _bump(home, client.get_tenant(), 3)
    n = ProcessNotices("mcp")
    n.started_at = time.time() - 7200
    t = _drive(n, 70, n.started_at)
    lines = [n.next_notice(client, home=home, cwd=tmp_path, now=t, allow_save=False) for _ in range(4)]
    assert not [x for x in lines if x and x.startswith("Sibyl Memory Pro:")]


def test_a_save_in_another_agent_resets_this_process_clock(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = _client(tmp_path)
    tid = client.get_tenant()
    n = ProcessNotices("mcp")
    n.started_at = time.time() - 7200
    t = _drive(n, 70, n.started_at)
    other = _client(tmp_path)       # another agent on the same store writes the marker
    _save_marker(other)
    lines = [n.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3)]
    assert not [x for x in lines if x and x.startswith("Sibyl Memory Pro:")]
    _bump(home, tid, 2)             # new work after that save
    t = _drive(n, 30, t)            # 100 active minutes in this process, 30 since the save
    lines = [n.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3)]
    assert not [x for x in lines if x and x.startswith("Sibyl Memory Pro:")]
    t = _drive(n, 31, t)
    assert any((x or "").startswith("Sibyl Memory Pro:")
               for x in (n.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3)))


def test_hermes_notice_names_the_file(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = _client(tmp_path)
    _bump(home, client.get_tenant(), 6)
    n = ProcessNotices("hermes", session_id="h1")
    n.started_at = time.time() - 600
    t = _drive(n, 3, n.started_at)
    lines = [n.next_notice(client, home=home, cwd=tmp_path, now=t) for _ in range(3)]
    save = [x for x in lines if x and x.startswith("Sibyl Memory Pro:")]
    assert save and "6 memory writes since the last save" in save[0]
    assert "~/.sibyl-memory/skills/SAVE.md" in save[0] and "`save` prompt" not in save[0]


def test_tier_change_line_once_each_way(tmp_path, entitle_as):
    home = Path.home()
    client = _client(tmp_path)
    assert ProcessNotices("mcp").next_notice(client, home=home, cwd=tmp_path) != R.TIER_UP_LINE
    entitle_as("pro")
    lines = [ProcessNotices("mcp").next_notice(client, home=home, cwd=tmp_path) for _ in range(2)]
    assert lines[0] == R.TIER_UP_LINE and lines[1] != R.TIER_UP_LINE
    entitle_as("free")
    assert ProcessNotices("mcp").next_notice(client, home=home, cwd=tmp_path) == R.TIER_DOWN_LINE
    assert "nothing was deleted" in R.TIER_DOWN_LINE


# ----------------------------------------------------------------------
# Skill proposals (plan section 3, the four existing detectors)
# ----------------------------------------------------------------------

def _row(ts_list, *, conf=0.8, kind="repeated_action", status="pending"):
    return {"status": status, "pattern_kind": kind, "confidence": conf,
            "evidence": [{"event_id": str(i), "ts": _iso(t), "snippet": "x"} for i, t in enumerate(ts_list)]}


def test_relevance_filter():
    now = time.time()
    anchor = now - 3 * 86400
    good = [now - 10 * 86400, now - 4 * 86400, now - 3600]
    assert PR.is_relevant(_row(good), now, anchor=anchor)
    assert not PR.is_relevant(_row(good, conf=0.59), now, anchor=anchor)             # confidence floor
    assert not PR.is_relevant(_row(good[:2]), now, anchor=anchor)                    # 3 events minimum
    assert not PR.is_relevant(_row([now - 20 * 86400, now - 19 * 86400, now - 3600]), now, anchor=anchor)
    assert not PR.is_relevant(_row([now - 5 * 86400, now - 4.9 * 86400, now - 4.8 * 86400]), now,
                              anchor=anchor)                                         # not in the last session
    assert not PR.is_relevant(_row([now - 600, now - 400, now - 200]), now, anchor=anchor)  # one session only
    assert not PR.is_relevant(_row(good), now, anchor=anchor, muted={"repeated_action"})
    assert not PR.is_relevant(_row(good, status="rejected"), now, anchor=anchor)


def test_three_named_once_per_session_and_24h_throttle():
    st = ST.default_state()
    ST.ensure_session(st, "s1", tenant_id="t", harness="x", now=0)
    ST.ensure_session(st, "s2", tenant_id="t", harness="x", now=0)
    now = 1_000_000.0
    line = PR.skill_prompt(st, "t", "s1", now, ["a-b", "c-d", "e-f"], 5)
    assert line == ("Sibyl Memory: 5 candidate skills from your recent sessions (a-b, c-d, e-f and 2 more). "
                    "Say /sibyl-save curate to review, or run sibyl skills proposals.")
    assert PR.skill_prompt(st, "t", "s1", now + 90000, ["a-b"], 1) is None          # once per session
    assert PR.skill_prompt(st, "t", "s2", now + 3600, ["a-b"], 1) is None           # 24 h
    assert PR.skill_prompt(st, "t", "s2", now + 86400, ["a-b"], 1).startswith("Sibyl Memory: 1 candidate skill ")
    st["reminders"] = False
    assert PR.skill_prompt(st, "t", "s3", now + 10 * 86400, ["a"], 1) is None


def _repeat(client: MemoryClient, verb: str, n: int = 4, *, start: float | None = None) -> None:
    start = time.time() - 3 * 86400 if start is None else start
    for i in range(n):
        client.write_event(acted={"kind": verb}, ts=_iso(start + i * 20 * 3600))


def _proposals(client: MemoryClient, status="pending"):
    return client.learner().list_proposals(status=status, limit=100)


def test_rejected_slug_cools_off_then_mutes_until_unmuted(tmp_path, entitle_as):
    entitle_as("pro")
    client = _client(tmp_path)
    _repeat(client, "deploy preview check")
    client.learn()
    [p] = [x for x in _proposals(client) if x.pattern_kind == "repeated_action"]
    client.reject_skill_proposal(p.id, note="not useful")
    _repeat(client, "deploy preview check")
    client.learn()
    assert not [x for x in _proposals(client) if x.proposed_slug == p.proposed_slug]
    # 31 days later the slug may come back
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with client.storage.transaction() as con:
        con.execute("UPDATE skill_proposals SET reviewed_at = ? WHERE id = ?", (old, p.id))
    _repeat(client, "deploy preview check")
    client.learn()
    [p2] = [x for x in _proposals(client) if x.proposed_slug == p.proposed_slug]
    client.reject_skill_proposal(p2.id)
    with client.storage.transaction() as con:
        con.execute("UPDATE skill_proposals SET reviewed_at = ? WHERE tenant_id = ?", (old, client.get_tenant()))
    _repeat(client, "deploy preview check")
    client.learn()
    assert not [x for x in _proposals(client) if x.proposed_slug == p.proposed_slug]    # twice: muted
    _repeat(client, "deploy preview check")
    client.learner().run(unmuted={p.proposed_slug: time.time()})
    assert [x for x in _proposals(client) if x.proposed_slug == p.proposed_slug]


def test_accepted_slug_is_not_proposed_again_and_muted_kinds_are_skipped(tmp_path, entitle_as):
    entitle_as("pro")
    client = _client(tmp_path)
    _repeat(client, "rotate the logs")
    client.learn()
    [p] = [x for x in _proposals(client) if x.pattern_kind == "repeated_action"]
    out = client.accept_skill_proposal(p.id)
    assert out["doc_key"] == f"skill/{p.proposed_slug}"
    assert client.get_reference(out["doc_key"]) is not None
    _repeat(client, "rotate the logs")
    rep = client.learner().run(exclude_kinds={"structural_similarity", "co_occurrence", "temporal_routine"})
    assert not [x for x in _proposals(client) if x.proposed_slug == p.proposed_slug]
    assert all(x.pattern_kind == "repeated_action" for x in
               (client.learner().get_proposal(i) for i in rep.proposal_ids))


def test_free_tier_gets_tier_gate_and_run_learner_does_nothing(tmp_path):
    client = _client(tmp_path)
    with pytest.raises(TierGateError):
        client.learner()
    assert PR.run_learner(client, home=Path.home()) is None
    assert client.storage is not None
    with client.storage.connection() as con:
        assert con.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0


def test_run_learner_on_pro_is_local_and_not_counted_as_a_user_write(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = _client(tmp_path)
    client.enable_lifecycle_counting(home)
    _repeat(client, "sync the fixtures")
    ST.flush_all()
    before = ST.LifecycleState(home).load()["tenants"].get(client.get_tenant(), {}).get("writes_total", 0)
    rep = PR.run_learner(client, home=home)
    assert rep is not None and rep.summarizer == "local-deterministic" and rep.proposals_made >= 1
    ST.flush_all()
    after = ST.LifecycleState(home).load()["tenants"].get(client.get_tenant(), {}).get("writes_total", 0)
    assert after == before           # the learner's writes are the plugin's, not the person's


def test_run_learner_writes_nothing_under_read_only(tmp_path, test_signer, entitled_test_home):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro", mem="read_only"))
    client = _client(tmp_path)
    assert PR.run_learner(client, home=Path.home()) is None


def test_read_relevant_names_at_most_three(tmp_path, entitle_as):
    entitle_as("pro")
    client = _client(tmp_path)
    now = time.time()
    rows = []
    for i in range(5):
        rows.append(("p%d" % i, client.get_tenant(), "repeated_action", f"skill-{i}", None, "b",
                     json.dumps(_row([now - 5 * 86400, now - 2 * 86400, now - 600])["evidence"]),
                     0.9 - i * 0.01, "local-deterministic"))
    with client.storage.transaction() as con:
        con.executemany("INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, "
                        "proposed_title, proposed_body, evidence, confidence, summarizer) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    names, total = PR.read_relevant(client.storage.db_path, client.get_tenant(), now, anchor=now - 86400)
    assert names == ["skill-0", "skill-1", "skill-2"] and total == 5


# ----------------------------------------------------------------------
# Accepted skill files (plan 3.3): the Codex quick_validate rules
# ----------------------------------------------------------------------

class _P:
    id = "abcdef12-0000"
    proposed_slug = "repeat-deploy-preview-check"
    proposed_title = "Repeat <Deploy> Preview: Check"
    pattern_kind = "repeated_action"
    proposed_body = "# Deploy preview check\n\n## Pattern\n\nRecurring action: `deploy-preview-check`\n"


def test_skill_file_passes_the_codex_rules_and_the_writer_keeps_edits(tmp_path):
    text = SK.proposal_skill_text(_P())
    name = SK.skill_name(_P.proposed_slug)
    assert SK.validate_skill_text(text, name) == []
    fm = SK._parse_frontmatter_yaml(text)
    assert set(fm) == {"name", "description"} and "<" not in fm["description"] and len(fm["description"]) <= 1024
    assert SK.skill_name("x" * 90 + "--y-") == "x" * 64
    assert SK.skill_name("--Weird Slug!!--") == "weird-slug"
    assert SK.validate_skill_text(text.replace("name: " + name, "name: Bad_Name"), name)
    assert SK.validate_skill_text("---\nname: a\ndescription: x\nextra: 1\n---\nbody", "a")
    home = Path.home()
    dest = home / ".codex" / "skills" / name / "SKILL.md"
    assert SK.write_skill(home, dest, text, harness="codex", slug=_P.proposed_slug) == "installed"
    assert SK.write_skill(home, dest, text, harness="codex", slug=_P.proposed_slug) == "unchanged"
    dest.write_text(text + "\nmy note\n")
    assert SK.write_skill(home, dest, text, harness="codex", slug=_P.proposed_slug) == "exists"
    assert dest.read_text().endswith("my note\n")
    # `sibyl skills uninstall` never removes an accepted skill
    SK.install(home)
    SK.uninstall(home)
    assert dest.exists()


# ----------------------------------------------------------------------
# Computer limit (plan 4.2): mem read_only because of acct.devices.over
# ----------------------------------------------------------------------

def test_read_only_for_too_many_computers_says_so(tmp_path, test_signer, entitled_test_home):
    acct = {"complete": True, "email_verified": True, "email_required": False,
            "devices": {"n": 3, "limit": 1, "over": True}}
    write_entitlement(entitled_test_home, test_signer.mint(mem="read_only", acct=acct))
    client = _client(tmp_path)
    client.list_entities()                                      # reading still works
    with pytest.raises(EntitlementError) as ei:
        client.set_entity("x", "y", {"a": 1})
    e = ei.value
    assert e.code == "policy_read_only" and e.reason == "policy_read_only"
    assert "3 computers signed in; Free is one computer" in str(e)
    assert "account.sibyllabs.org/security" in str(e) and "email" not in str(e)
    assert "Sign the other computers out" in e.recovery
    # the email reason keeps the email copy
    write_entitlement(entitled_test_home, test_signer.mint(mem="read_only"))
    with pytest.raises(EntitlementError) as ei2:
        _client(tmp_path, "b.db").set_entity("x", "y", {"a": 1})
    assert "confirmed email" in str(ei2.value)


# ----------------------------------------------------------------------
# SAVE.md (plan 1.2 item 7) and the honest copy
# ----------------------------------------------------------------------

def test_save_md_codex_row_cadence_and_hashes():
    t = SK.text("SAVE.md")
    assert "~/.codex/prompts" not in t
    assert "| codex | `~/.codex/skills/<name>/SKILL.md` |" in t
    assert "once per session" not in t and "one nudge per session" not in t
    assert "about every hour of active work" in t
    assert "sibyl skills proposals accept <id>" in t and "Never accept without the person's yes" in t
    wm = SK.watermark_fields(t)
    assert wm["hash"] == SK.watermark_hash(t)
    man = json.loads((Path(SK.__file__).parent / "_skills" / "manifest.json").read_text())
    import hashlib
    assert man["files"]["SAVE.md"]["sha256"] == hashlib.sha256(SK.shipped_bytes("SAVE.md")).hexdigest()
    assert "once per session" not in SK.shim_text("SAVE.md", "codex")


def test_process_start_runs_the_learner_on_pro_only(tmp_path, entitle_as):
    calls: list = []
    n = ProcessNotices("mcp")
    n.run_fn = lambda *a, **k: None
    n.learn_fn = lambda client, **k: calls.append(client)
    n.start(_client(tmp_path), home=Path.home(), cwd=tmp_path, background=False)
    assert calls == []
    entitle_as("pro")
    n2 = ProcessNotices("mcp")
    n2.run_fn = lambda *a, **k: None
    n2.learn_fn = lambda client, **k: calls.append(client)
    n2.start(_client(tmp_path), home=Path.home(), cwd=tmp_path, background=False)
    assert len(calls) == 1


# ----------------------------------------------------------------------
# R1B-4 (independent check of a7084df): F5 copy, F10 per-session compaction
# and the sequence floor. On a7084df these fail: the copy said "the most
# recently used computer", a compaction was tenant-wide (session B was due
# because session A compacted, and the free reminder said so in B), and
# state had no next_nudge_seq / note_seq_seen.
# ----------------------------------------------------------------------

def test_r1b4_read_only_copy_names_the_newest_sign_in():
    from sibyl_memory_client.exceptions import devices_read_only_message
    msg = devices_read_only_message({"devices": {"n": 3, "limit": 1, "over": True}}, "free")
    assert "new memories save only on the computer that signed in most recently." in msg
    assert "recently used" not in msg


def test_r1b4_a_compaction_makes_only_its_own_session_due():
    now = 10_000_000.0
    st = ST.default_state()
    _session(st, 10, sk="A", writes=1, compaction_seen=True)
    _session(st, 10, sk="B", writes=1)
    assert R.autosave_due(st, "t", "A", now) == "compaction"
    assert R.autosave_due(st, "t", "B", now) is None
    # writes stay tenant-wide (plan D6.5): B's own first-save rule still sees them
    _session(st, 10, sk="B", writes=5)
    assert R.autosave_due(st, "t", "B", now) == "first"


def test_r1b4_free_reminder_compaction_is_per_session():
    now = 10_000_000.0
    st = ST.default_state()
    _session(st, 10, sk="A", writes=1, compaction_seen=True)
    _session(st, 10, sk="B", writes=1)
    assert R.save_reminder(st, "t", now, pro=False, sk="B") is None
    assert R.save_reminder(st, "t", now, pro=False, sk="A").startswith("Sibyl Memory: 1 writes")


def test_r1b4_a_save_clears_the_session_compaction():
    now = 10_000_000.0
    st = ST.default_state()
    s = _session(st, 10, sk="A", writes=1, compaction_seen=True)
    s["started_at"] = now - 100
    assert ST.observe_marker(st, "t", now - 10) is True
    assert s["compaction_seen"] is False
    ST.tenant(st, "t")["writes_since_save"] = 1
    assert R.autosave_due(st, "t", "A", now) is None


def test_r1b4_old_tenant_level_compaction_keys_are_dropped(tmp_path):
    (tmp_path / ".sibyl-memory").mkdir()
    (tmp_path / ".sibyl-memory" / "lifecycle.json").write_text(json.dumps(
        {"tenants": {"t": {"writes_since_save": 2, "compaction_seen": True, "save_due_now": True}},
         "sessions": {"B": {"tenant": "t", "started_at": 1.0}}}))
    st = ST.LifecycleState(tmp_path).load()
    assert "compaction_seen" not in st["tenants"]["t"] and "save_due_now" not in st["tenants"]["t"]
    assert st["sessions"]["B"]["compaction_seen"] is False
    assert R.autosave_due(st, "t", "B", time.time()) is None


def test_r1b4_seq_floor_numbers_past_a_transcript_request_and_costs_nothing():
    now = 10_000_000.0
    st = ST.default_state()
    s = _session(st, 4000, writes=1)
    assert ST.next_nudge_seq(s) == 1
    ST.note_seq_seen(st, "t", "s", 1, now=now)
    t = ST.tenant(st, "t")
    assert s["nudges"] == [] and s["last_nudge_active"] is None
    assert ST.nudges_today(t, now) == 0 and ST.next_nudge_seq(s) == 2
    assert R.autosave_due(st, "t", "s", now) == "interval"     # nothing was consumed
    assert ST.record_nudge(st, "t", "s", now) == 2
    assert len(s["nudges"]) == 1 and ST.nudges_today(t, now) == 1
    assert ST.next_nudge_seq(s) == 3
    # the first-save rule does not fire again after a transcript-held request
    st2 = ST.default_state()
    s2 = _session(st2, 10, writes=5)
    ST.note_seq_seen(st2, "t", "s", 1, now=now)
    assert R.autosave_due(st2, "t", "s", now) is None
