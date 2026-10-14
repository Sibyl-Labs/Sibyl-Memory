"""sibyl-memory-hermes 0.5.0 save/debloat (spec plugin-save-debloat, tests 8.3
items 23-25, D5).

All fail on the pre-change tree: the prompt block had no save coaching, the
adapter wrote no capsule, prefetch carried no lifecycle line, and
install-plugin wrote no skills."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from sibyl_memory_client import skills as SK
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_module
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter

CURRENT = "# Agent rules\n\nRun the tests first.\nUse the venv.\nKeep answers short.\nNo secrets.\n\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)


@pytest.fixture
def pro(entitled_test_home, test_signer):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    return entitled_test_home


def _adapter(tmp_path, sid="sess-1") -> SibylAdapter:
    a = SibylAdapter()
    a.initialize(sid, hermes_home=str(tmp_path / "hh"))
    return a


def _capsules(a: SibylAdapter) -> list[dict]:
    return [e["acted"]["body"] for e in a._sibyl.client.read_events(limit=50)
            if isinstance(e.get("acted"), dict) and e["acted"].get("kind") == "session_capsule"]


# 23 ------------------------------------------------------------------------

def test_23_prompt_block_coaching_and_the_one_pro_line(entitled_test_home, test_signer, tmp_path):
    free = _adapter(tmp_path).system_prompt_block()
    assert "~/.sibyl-memory/skills/SAVE.md" in free and "~/.sibyl-memory/skills/DEBLOAT.md" in free
    assert "sibyl_remember" in free and "automatic save is on Pro" in free
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    pro = _adapter(tmp_path, "s2").system_prompt_block()
    fl, pl = free.splitlines(), pro.splitlines()
    assert len(fl) == len(pl)
    diff = [(x, y) for x, y in zip(fl, pl) if x != y]
    assert len(diff) == 1
    assert "at natural end points without asking" in diff[0][1]
    # R1: the Pro line also says to obey the note that asks for a headless save.
    assert "headless mode whenever a Sibyl Memory note asks for it" in diff[0][1]
    assert "Sibyl records a session capsule anyway" in diff[0][1]


# 24 ------------------------------------------------------------------------

def test_24_shutdown_capsule_on_pro_only(entitled_test_home, test_signer, tmp_path):
    a = _adapter(tmp_path)
    a.sync_turn("hello", "hi there")
    a.shutdown()
    assert _capsules(a) == []                                  # free: none
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    b = _adapter(tmp_path, "sess-pro")
    b.sync_turn("user text SECRET-XYZ", "assistant text")
    b.shutdown()
    caps = _capsules(b)
    assert len(caps) == 1
    cap = caps[0]
    assert cap["harness"] == "hermes" and cap["turns"] == 1 and cap["cwd_name"] == "work"
    assert "SECRET-XYZ" not in json.dumps(cap) and "sess-pro" not in json.dumps(cap)
    b.shutdown()
    assert len(_capsules(b)) == 1                               # exactly one


def test_24_locked_gate_writes_none_and_logs_once(pro, tmp_path, caplog, test_signer):
    a = _adapter(tmp_path)
    write_entitlement(pro, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 40 * 86400, tier="pro", cap=None, src="pro")))
    with caplog.at_level(logging.INFO, logger=adapter_module.logger.name):
        a._write_capsule(reason="pre_compress")
        a._write_capsule(reason="shutdown")
    lines = [r for r in caplog.records if "no session capsule" in r.getMessage()]
    assert len(lines) == 1


# 25 ------------------------------------------------------------------------

def test_25_pre_compress_writes_a_capsule_on_pro_in_addition_to_the_flush(pro, tmp_path):
    a = _adapter(tmp_path)
    msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
    a.on_pre_compress(msgs)
    a._flush_thread.join(5)
    evs = a._sibyl.client.read_events(limit=10)
    kinds = [("capsule" if isinstance(e.get("acted"), dict) and e["acted"].get("kind") == "session_capsule"
              else "turn") for e in evs]
    assert kinds.count("capsule") == 1 and kinds.count("turn") == 1


def test_25_turn_journal_does_not_count_as_a_save_write(entitled_test_home, tmp_path):
    a = _adapter(tmp_path)
    for i in range(12):
        a.sync_turn(f"u{i}", f"a{i}")
        a._sync_thread.join(5)
    ST.flush_all()
    tid = a._sibyl.client.get_tenant()
    t = ST.LifecycleState().load().get("tenants", {}).get(tid, {})
    assert int(t.get("writes_since_save") or 0) == 0


def test_prefetch_carries_the_free_save_reminder_once_outside_the_fence(entitled_test_home, tmp_path):
    a = _adapter(tmp_path)
    a._sibyl.remember("project", "atlas", {"note": "the atlas project"})
    tid = a._sibyl.client.get_tenant()

    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_since_save"] = 30
        t["since_save_started_at"] = time.time() - 60
    ST.LifecycleState().update(_u)
    out = a.prefetch("what about the atlas project today")
    assert out.rstrip().endswith("Run /sibyl-save (or the save prompt) before you stop. Automatic save is on Pro.")
    note = out.split("## Sibyl Memory: note\n", 1)
    assert len(note) == 2 and "UNTRUSTED MEMORY CONTEXT END" in note[0]
    assert "## Sibyl Memory: note" not in a.prefetch("what about the atlas project again")


def test_initialize_on_pro_runs_the_debloat_plan_in_the_background(pro, tmp_path):
    f = Path("AGENTS.md")
    f.write_text(CURRENT + HIST)
    t = time.time() - 3600
    os.utime(f, (t, t))
    P.set_auto(P.auth_id(f.resolve(), Path.home()), True, Path.home())
    a = _adapter(tmp_path)
    a._notices.join(10)
    assert "old decision" not in f.read_text()
    out = a.prefetch("anything at all about the project")
    assert "context trim: AGENTS.md" in out


# D5 ------------------------------------------------------------------------

def test_install_plugin_writes_skills_in_the_documented_hermes_dir(entitled_test_home, tmp_path, capsys):
    from sibyl_memory_hermes.install_plugin import install
    hh = tmp_path / "hh2"
    hh.mkdir()
    rc = install(hermes_home=hh, force=False, dry_run=False, memory_provider_path=str(tmp_path / "prov"))
    assert rc == 0
    for name, shim in SK.SHIM_NAMES.items():
        p = hh / "skills" / shim / "SKILL.md"
        assert SK.frontmatter_fields(p.read_text())["name"] == shim
        assert f"~/.sibyl-memory/skills/{name}" in p.read_text()
    assert (Path.home() / ".sibyl-memory" / "skills" / "SAVE.md").exists()
    assert "procedures v1.1.0: SAVE.md installed" in capsys.readouterr().out


# SD-9 (F10): the adapter opts its client in; the SDK alone does not ---------

def test_f10_the_adapter_enables_write_counting(entitled_test_home, tmp_path):
    """a6338cc had no opt-in (counting lived inside every MemoryClient)."""
    a = _adapter(tmp_path)
    assert a._sibyl.client._lifecycle_home == Path.home()
    for i in range(10):
        a._sibyl.remember("fact", f"f{i}", {"v": i})
    ST.flush_all()
    t = ST.LifecycleState().load()["tenants"][a._sibyl.client.get_tenant()]
    assert t["writes_since_save"] == 10
