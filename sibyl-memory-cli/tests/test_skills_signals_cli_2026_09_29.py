"""R2 `sibyl skills signals status|clear|off|on` and the two new kinds in
`sibyl skills proposals` (spec plugin-paid-tier-r2 1.3, 1.5, 1.6).

* status prints counts, size and dates, never an entry (a canary tool name and
  argument key are not printed); free tier and a locked device print "Skill
  signals are on Pro.";
* clear deletes the file; off stops recording; on starts it again from now; on
  the free tier `on` changes nothing;
* `mute tool_sequence` / `mute correction_loop` are accepted kinds; a
  tool_sequence proposal is listed, shown, accepted (a valid SKILL.md and the
  reference copy) and rejected through the CLI.

On b591aa1 these fail: `skills signals` is not a command (argparse exits 2)
and `mute tool_sequence` answers "Unknown kind".
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills as SK
from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

CANARY_TOOL = "mcp__canarysrv__canary_tool_4d4d"
CANARY_KEY = "canary_key_9b2c"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})

    def offline(*a, **k):
        raise cli.HttpError(0, {}, "offline")
    monkeypatch.setattr(cli, "http_request", offline)
    yield
    with SG._registry_lock:
        for b in SG._BUFFERS.values():
            b.discard()
        SG._BUFFERS.clear()


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


def _record(home: Path, names=("Read", "Edit", CANARY_TOOL), times=3, t0=None) -> None:
    t0 = time.time() - 7200 if t0 is None else t0
    sig = SG.load_unlocked(home)
    t = t0
    for i in range(times):
        evs = [(n, SG.arg_shape({CANARY_KEY: 1}), t + 300 * j) for j, n in enumerate(names)]
        evs.append((f"Sep{i}", SG.arg_shape({}), t + 300 * len(names)))
        SG.apply_tool_events(sig, "aaaaaaaa", evs, time.time())
        t += 300 * (len(names) + 1)
    SG.save_unlocked(home, sig)


# ----------------------------------------------------------------------
# sibyl skills signals
# ----------------------------------------------------------------------

def test_status_prints_counts_never_entries(home, pro, capsys):
    _record(home)
    assert cli.main(_acct(pro) + ["skills", "signals"]) == 0
    out = capsys.readouterr().out
    assert "recording" in out and "on" in out and "sequences" in out and "KB" in out
    assert CANARY_TOOL not in out and "canary" not in out.lower() and CANARY_KEY not in out
    assert "Read" not in out and "Edit" not in out
    assert cli.main(_acct(pro) + ["skills", "signals", "status"]) == 0
    assert "sequences" in capsys.readouterr().out


def test_status_on_free_and_on_a_locked_device_says_pro(home, sm, test_signer, capsys):
    assert cli.main(_acct(sm) + ["skills", "signals", "status"]) == 0
    assert "Skill signals are on Pro." in capsys.readouterr().out
    write_entitlement(sm, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 40 * 86400, tier="pro", cap=None, src="pro")))
    assert cli.main(_acct(sm) + ["skills", "signals", "status"]) == 0
    assert "Skill signals are on Pro." in capsys.readouterr().out
    assert not SG.path_of(home).exists()


def test_on_on_free_changes_nothing_off_and_clear_work_everywhere(home, sm, capsys):
    ST.LifecycleState(home).update(lambda st: st.setdefault("skills", ST.default_skills())
                                   .update(signals=False))
    assert cli.main(_acct(sm) + ["skills", "signals", "on"]) == 0
    assert "Skill signals are on Pro." in capsys.readouterr().out
    assert SG.enabled(home) is False
    _record(home)                                          # a file left from an earlier Pro period
    assert cli.main(_acct(sm) + ["skills", "signals", "status"]) == 0
    assert "earlier Pro period" in capsys.readouterr().out
    assert cli.main(_acct(sm) + ["skills", "signals", "clear"]) == 0
    assert not SG.path_of(home).exists()
    assert cli.main(_acct(sm) + ["skills", "signals", "off"]) == 0
    assert SG.enabled(home) is False


def test_clear_off_on_on_pro(home, pro, capsys):
    _record(home)
    assert cli.main(_acct(pro) + ["skills", "signals", "clear"]) == 0
    out = capsys.readouterr().out
    assert "signals.json deleted" in out
    assert not SG.path_of(home).exists()
    assert cli.main(_acct(pro) + ["skills", "signals", "clear"]) == 0
    assert "nothing was recorded" in capsys.readouterr().out
    assert cli.main(_acct(pro) + ["skills", "signals", "off"]) == 0
    assert SG.enabled(home) is False
    assert cli.main(_acct(pro) + ["skills", "signals"]) == 0
    assert "off (sibyl skills signals on)" in capsys.readouterr().out
    before = time.time()
    assert cli.main(_acct(pro) + ["skills", "signals", "on"]) == 0
    st = ST.LifecycleState(home).load()["skills"]
    assert st["signals"] is True and st["signals_since"] >= before


def test_save_fact_counts_as_an_entity_write_on_pro(home, pro, capsys):
    assert cli.main(_acct(pro) + ["save", "--fact", "the deploy key rotates monthly"]) == 0
    SG.flush_all()
    sig = SG.load(home)
    assert len(sig["entity_writes"]) == 1
    assert b"deploy key" not in SG.path_of(home).read_bytes()


# ----------------------------------------------------------------------
# sibyl skills proposals with the two new kinds
# ----------------------------------------------------------------------

def test_mute_accepts_the_new_kinds(home, sm, capsys):
    for k in ("tool_sequence", "correction_loop"):
        assert cli.main(_acct(sm) + ["skills", "proposals", "mute", k]) == 0
    assert set(ST.LifecycleState(home).load()["skills"]["muted_kinds"]) == {"tool_sequence", "correction_loop"}
    capsys.readouterr()
    assert cli.main(_acct(sm) + ["skills", "proposals", "mute", "nonsense"]) == 1
    assert "tool_sequence" in capsys.readouterr().out


def test_tool_sequence_proposal_list_show_accept_reject(home, pro, tmp_path, capsys):
    from sibyl_memory_client.lifecycle import proposals as PR
    _record(home, names=("Read", "Edit", "Bash"))
    c = _client(pro)
    rep = PR.run_learner(c, home=home)
    assert rep is not None and rep.proposals_made == 1
    p = c.learner().get_proposal(rep.proposal_ids[0])
    assert p.pattern_kind == "tool_sequence"
    assert cli.main(_acct(pro) + ["skills", "proposals"]) == 0
    out = capsys.readouterr().out
    assert "tool_sequence" in out and p.id[:8] in out
    assert cli.main(_acct(pro) + ["skills", "proposals", "show", p.id[:8]]) == 0
    assert "Recurring tool sequence: `Read -> Edit -> Bash`" in capsys.readouterr().out
    dest = tmp_path / "skills-out"
    assert cli.main(_acct(pro) + ["skills", "proposals", "accept", p.id, "--to", str(dest)]) == 0
    name = SK.skill_name(p.proposed_slug)
    text = (dest / name / "SKILL.md").read_text()
    assert SK.validate_skill_text(text, name) == []
    assert c.get_reference(f"skill/{p.proposed_slug}") is not None
    # mute tool_sequence: the next run proposes no tool sequence
    assert cli.main(_acct(pro) + ["skills", "proposals", "mute", "tool_sequence"]) == 0
    _record(home, names=("Grep", "Write", "Bash"), t0=time.time() - 3600)
    assert PR.run_learner(c, home=home).proposals_made == 0
    assert cli.main(_acct(pro) + ["skills", "proposals", "unmute", "tool_sequence"]) == 0
    rep2 = PR.run_learner(c, home=home)
    assert rep2.proposals_made == 1
    p2 = c.learner().get_proposal(rep2.proposal_ids[0])
    assert cli.main(_acct(pro) + ["skills", "proposals", "reject", p2.id]) == 0
    assert "not proposed again for 30 days" in capsys.readouterr().out
    assert PR.run_learner(c, home=home).proposals_made == 0
