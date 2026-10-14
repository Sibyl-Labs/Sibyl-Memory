"""Independent-check fixes on the save/debloat build, CLI side (SD-7.., 2026-09-27).

Each test fails on a6338cc (the docstring says how when it is behavioural).
"""
from __future__ import annotations

import builtins
import json
import os
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import lifecycle_cli as LC
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.testing import write_entitlement

CURRENT = "# Agent rules\n\n" + "".join(f"- rule {i} stays current\n" for i in range(10)) + "\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))


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


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - 3600
    os.utime(p, (t, t))
    return p


def _acct(sm: Path) -> list[str]:
    return ["--credentials", str(sm / "credentials.json"), "--db", str(sm / "memory.db")]


# ----------------------------------------------------------------------
# F1
# ----------------------------------------------------------------------

def test_f1_auto_names_this_file_at_its_path_and_manual_takes_it_back(tmp_path, pro, home, capsys,
                                                                      monkeypatch):
    """a6338cc stored the bare name 'AGENTS.md' and said 'Automatic context
    trim for AGENTS.md', which then matched AGENTS.md in every project."""
    a = _write(tmp_path / "repo-a" / "AGENTS.md", CURRENT + HIST)
    b = _write(tmp_path / "repo-b" / "AGENTS.md", CURRENT + HIST)
    monkeypatch.chdir(a.parent)
    asked: list[str] = []
    monkeypatch.setattr(LC.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda q="": asked.append(q) or "y")
    assert cli.main(_acct(pro) + ["debloat", "auto", "AGENTS.md"]) == 0
    out = capsys.readouterr().out
    fid_a = P.auth_id(a, home)
    assert f"Automatic context trim for this file at {fid_a} means:" in out
    assert "a file with the same name in another project" in out
    assert asked == [f"Let Sibyl trim this file at {fid_a} automatically? [y/N]: "]
    assert P.load(home)["auto_files"] == [fid_a]
    # `manual AGENTS.md` in repo-b removes nothing: that file was never on auto.
    monkeypatch.chdir(b.parent)
    assert cli.main(_acct(pro) + ["debloat", "manual", "AGENTS.md"]) == 0
    assert "was not on automatic; nothing changed" in capsys.readouterr().out
    assert P.load(home)["auto_files"] == [fid_a]
    # By path, from anywhere.
    assert cli.main(_acct(pro) + ["debloat", "manual", fid_a]) == 0
    assert P.load(home)["auto_files"] == [] and P.load(home)["mode"] == "propose"


def test_f1_cli_shows_the_one_time_notice_for_name_only_entries(tmp_path, sm, home, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (sm / "debloat.json").write_text(json.dumps({"mode": "auto", "auto_files": ["AGENTS.md"]}))
    cli.main(_acct(sm) + ["debloat", "plan"])
    out = capsys.readouterr().out
    assert "AGENTS.md was set by name only and is back to manual" in out
    cli.main(_acct(sm) + ["debloat", "plan"])
    assert "set by name only" not in capsys.readouterr().out
    assert json.loads((sm / "debloat.json").read_text())["auto_files"] == []


# ----------------------------------------------------------------------
# F4
# ----------------------------------------------------------------------

def test_f4_undo_restores_the_backup_the_last_apply_recorded(tmp_path, sm, home, capsys, monkeypatch):
    """a6338cc restored the newest backup; after an interrupted later attempt
    that copy equals the trimmed file, so undo 'restored' nothing."""
    f = _write(tmp_path / "proj" / "AGENTS.md", CURRENT + HIST)
    original = f.read_bytes()
    monkeypatch.chdir(f.parent)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    trimmed = f.read_bytes()
    assert trimmed != original
    A.backup(f, trimmed, home=home, cwd=f.parent, now=time.time() + 60)   # the interrupted attempt
    capsys.readouterr()
    assert cli.main(_acct(sm) + ["debloat", "undo", "AGENTS.md", "--yes"]) == 0
    assert f.read_bytes() == original
    # Undoing again: the recorded backup now equals the file; refused, clearly.
    assert cli.main(_acct(sm) + ["debloat", "undo", "AGENTS.md", "--yes"]) == 1
    assert "nothing to undo" in capsys.readouterr().out and f.read_bytes() == original


# ----------------------------------------------------------------------
# F5: the Claude Code hook path is bounded and never touches the network
# ----------------------------------------------------------------------

from sibyl_memory_cli import hooks as H  # noqa: E402
from sibyl_memory_client.lifecycle import state as ST  # noqa: E402


def _hook(event, payload, home, **kw):
    return H.run_hook(event, payload if isinstance(payload, str) else json.dumps(payload), home=home, **kw)


def _warm_tier_cache(sm: Path) -> None:
    from sibyl_memory_client._capcheck import TierCache, TierCacheEntry
    acct = json.loads((sm / "credentials.json").read_text())["account_id"]
    TierCache(sm / "tier_cache.json").store(TierCacheEntry(account_id=acct, tier="pro",
                                                           checked_at=time.time(), cap_bytes=None))


@pytest.fixture
def slow_net(monkeypatch):
    """/check-write that would take 3 s (then fail), counted."""
    from sibyl_memory_client import _capcheck
    calls = []

    def slow(url, payload, timeout=4.0):
        calls.append(url)
        time.sleep(3)
        raise _capcheck.TierVerificationError("unreachable")
    monkeypatch.setattr(_capcheck, "_default_check_write_fn", slow)
    return calls


def test_f5_pro_start_without_a_tier_cache_plans_only_and_calls_no_network(tmp_path, pro, home, slow_net):
    """a6338cc applied through the gated client, whose /check-write (4 s
    timeout, two retries) outlived Claude Code's 5 s hook timeout."""
    f = _write(tmp_path / "p" / "AGENTS.md", CURRENT + HIST)
    before = f.read_bytes()
    P.set_auto(P.auth_id(f, home), True, home)
    t = time.monotonic()
    out = _hook("session-start", {"session_id": "s", "cwd": str(f.parent)}, home)
    assert time.monotonic() - t < 1.5 and slow_net == []
    line = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "AGENTS.md is set to automatic; it is trimmed at a later start" in line
    assert f.read_bytes() == before
    assert not A.backups_root(home).exists() or list(A.backups_root(home).iterdir()) == []
    assert "plan only: no fresh tier cache" in (pro / "lifecycle.log").read_text()
    # With a fresh tier cache (what any MCP write leaves) the next start applies.
    _warm_tier_cache(pro)
    ST.LifecycleState(home).update(lambda st: st["debloat"].clear())
    out = _hook("session-start", {"session_id": "s2", "cwd": str(f.parent)}, home)
    assert "history stored to memory as debloat/AGENTS.md/" in out and slow_net == []


def test_f5_capsules_wait_for_a_tier_cache_and_the_write_budget(tmp_path, pro, home, slow_net, monkeypatch):
    """a6338cc wrote the capsule through /check-write inside the hook."""
    _hook("session-start", {"session_id": "c", "cwd": str(tmp_path)}, home)
    t = time.monotonic()
    _hook("pre-compact", {"session_id": "c"}, home)
    assert time.monotonic() - t < 1.5 and slow_net == []
    log = (pro / "lifecycle.log").read_text()
    assert "capsule not written (no fresh tier cache" in log
    from sibyl_memory_client import MemoryClient
    reader = MemoryClient.local(str(pro / "memory.db"), tenant_id=json.loads(
        (pro / "credentials.json").read_text())["tenant_id"], credentials_path=pro / "credentials.json")
    _warm_tier_cache(pro)
    monkeypatch.setattr(H, "WRITE_BUDGET_SECONDS", 0.0)
    _hook("pre-compact", {"session_id": "c"}, home)
    assert "capsule not written (out of time)" in (pro / "lifecycle.log").read_text()
    monkeypatch.setattr(H, "WRITE_BUDGET_SECONDS", 2.5)
    _hook("session-end", {"session_id": "c"}, home)
    kinds = [e["acted"]["kind"] for e in reader.read_events(limit=10)]
    assert kinds == ["session_capsule"] and slow_net == []


def test_f5_the_hook_client_never_refreshes_and_sends_no_heartbeat(pro, home):
    """a6338cc's hook client refreshed the entitlement in a background thread
    and flushed the usage heartbeat (4 s timeout) at process exit."""
    from sibyl_memory_client._heartbeat import _NullHeartbeat
    creds = json.loads((pro / "credentials.json").read_text())
    c = H._pro_client(pro / "memory.db", pro / "credentials.json", creds, creds["tenant_id"])
    assert isinstance(c._heartbeat, _NullHeartbeat)
    assert c.entitlement_gate._allow_refresh is False


def test_f5_a_start_that_used_up_its_budget_only_plans(tmp_path, pro, home, monkeypatch):
    f = _write(tmp_path / "p" / "AGENTS.md", CURRENT + HIST)
    before = f.read_bytes()
    P.set_auto(P.auth_id(f, home), True, home)
    _warm_tier_cache(pro)
    out = _hook("session-start", {"session_id": "b", "cwd": str(f.parent)}, home,
                started=time.monotonic() - 10)
    assert "is set to automatic; it is trimmed at a later start" in out and f.read_bytes() == before


# ----------------------------------------------------------------------
# F11: free hooks return before the client, the verification and the DB
# ----------------------------------------------------------------------

def test_f11_paid_tier_literal_matches_the_client():
    # The hooks mirror is the client's ONE paid set (tier convention 2026-10-05).
    from sibyl_memory_client.tiers import PAID_TIERS
    from sibyl_memory_client.lifecycle.entitled import PAID_TIERS as ENTITLED
    assert H.PAID_TIERS == PAID_TIERS == ENTITLED


def test_f11_claims_paid_for_elite_any_case_and_not_for_an_unknown_label(sm, test_signer):
    for label, want in (("elite", True), (" Elite ", True), ("gold", False)):
        write_entitlement(sm, test_signer.mint(tier=label))
        assert H.claims_paid(sm / "credentials.json") is want, label


def test_f11_claims_paid_reads_the_unverified_claim_and_a_forged_claim_unlocks_nothing(
        tmp_path, sm, home, test_signer):
    assert H.claims_paid(sm / "credentials.json") is False           # the free test home
    # A hand-edited claim of "pro" with a bad signature: the fast path sends it
    # to the verified check, which refuses it.
    tok = test_signer.mint(tier="free")
    head, seg, sig = tok.split(".")
    import base64
    claim = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    claim["tier"] = "pro"
    forged = ".".join([head, base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip("="), sig])
    write_entitlement(sm, forged)
    assert H.claims_paid(sm / "credentials.json") is True
    tid = json.loads((sm / "credentials.json").read_text())["tenant_id"]
    _hook("session-start", {"session_id": "x", "cwd": str(tmp_path)}, home)
    ST.LifecycleState(home).update(lambda st: ST.tenant(st, tid).update(writes_total=50))
    assert _hook("stop", {"session_id": "x", "stop_hook_active": False}, home) == ""


def test_f11_free_pre_compact_and_session_end_skip_verification_and_the_db(tmp_path, sm, home, monkeypatch):
    """a6338cc verified the entitlement and opened memory.db on every free
    PreCompact / SessionEnd."""
    from sibyl_memory_client.lifecycle import entitled as EN
    seen = []
    monkeypatch.setattr(EN, "automation_allowed_for_credentials", lambda *a, **k: seen.append("verify") or (False, "free"))
    monkeypatch.setattr(ST, "read_save_marker", lambda *a, **k: seen.append("db") or None)
    _hook("pre-compact", {"session_id": "f", "trigger": "auto"}, home)
    _hook("session-end", {"session_id": "f", "reason": "other"}, home)
    _hook("stop", {"session_id": "f", "stop_hook_active": False}, home)
    assert seen == []
    tid = json.loads((sm / "credentials.json").read_text())["tenant_id"]
    st = ST.LifecycleState(home).load()
    assert st["sessions"][ST.session_key("f")]["compaction_seen"] is True   # R1B-4: per session
    assert "compaction_seen" not in st["tenants"][tid]
    assert st["sessions"][ST.session_key("f")]["ended_at"]


def _run_entry(home: Path, event: str, payload: dict, probe: str) -> str:
    import subprocess
    import sys
    code = ("import sys, json; from sibyl_memory_cli._entry import main; rc = main(['hook', %r]); "
            "print('RC', rc); print('MODS', json.dumps(sorted(%s)))" % (event, probe))
    env = dict(os.environ, HOME=str(home), SIBYL_MEMORY_TELEMETRY="0")
    env.pop("SIBYL_MEMORY_DB", None)
    p = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), text=True,
                       capture_output=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_f11_the_installed_entry_runs_a_free_stop_with_the_standard_library_only(tmp_path, sm, home):
    """a6338cc's `sibyl` script imported the whole CLI, the client, the
    entitlement verifier and SQLite for every Stop (about 150 ms)."""
    out = _run_entry(home, "stop", {"session_id": "e", "stop_hook_active": False},
                     "m for m in sys.modules if m.startswith(('sibyl_memory_client', 'cryptography', "
                     "'sqlite3', 'sibyl_memory_cli.cli', 'urllib.request'))")
    assert "RC 0" in out
    assert json.loads(out.split("MODS ", 1)[1]) == []


def test_f11_entry_sends_every_other_command_to_the_cli(monkeypatch):
    from sibyl_memory_cli import _entry
    from sibyl_memory_cli import cli as C
    got = []
    monkeypatch.setattr(C, "main", lambda argv=None: got.append(argv) or 7)
    assert _entry.main(["status"]) == 7 and got == [["status"]]
    assert _entry.main(["hook", "nonsense"]) == 7            # argparse reports it, as before
    import sibyl_memory_cli
    assert sibyl_memory_cli.main is C.main and isinstance(sibyl_memory_cli.__version__, str)


# ----------------------------------------------------------------------
# F8: a hook whose package is gone exits 0 silently
# ----------------------------------------------------------------------

import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402

from sibyl_memory_cli import setup as SU  # noqa: E402


@pytest.mark.skipif(_shutil.which("bash") is None, reason="bash not available")
def test_f8_the_hook_command_exits_0_silently_when_the_binary_is_gone(tmp_path):
    """a6338cc wrote `<abs>/sibyl hook <event>`: after `pip uninstall` every
    session start, prompt and turn end ran a missing command (exit 127)."""
    fake = tmp_path / "venv bin" / "sibyl"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "ran $1 $2"; cat >/dev/null\n')
    fake.chmod(0o755)
    cmd = SU.hook_command(str(fake), "stop")
    assert SU.is_our_hook({"type": "command", "command": cmd})
    run = lambda: _subprocess.run(["bash", "-c", cmd], input="{}", text=True,  # noqa: E731
                                  capture_output=True, timeout=20)
    p = run()
    assert p.returncode == 0 and p.stdout == "ran hook stop\n"
    fake.unlink()
    p = run()
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""


def test_f8_setup_replaces_entries_of_the_earlier_shape_and_uninstall_removes_both(tmp_path, monkeypatch):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    old = {"hooks": {ev: [{"hooks": [{"type": "command", "command": f"/old/bin/sibyl hook {arg}",
                                      "timeout": 5}]}] for ev, arg in SU.HOOK_EVENTS}}
    p.write_text(json.dumps(old))
    monkeypatch.setattr(SU, "sibyl_binary", lambda: "/new/bin/sibyl")
    w = SU.ClaudeCodeWirer(settings_path=p)
    w.wire_hooks()
    cfg = json.loads(p.read_text())
    assert SU.count_our_hooks(cfg) == 5 and "/old/bin" not in json.dumps(cfg)
    assert cfg["hooks"]["Stop"] == [{"hooks": [{"type": "command", "timeout": 5, "command":
                                                "[ -x /new/bin/sibyl ] && exec /new/bin/sibyl hook stop; exit 0"}]}]
    w.unwire_hooks()
    assert "hooks" not in json.loads(p.read_text())


def test_f8_uninstall_help_says_to_run_it_before_pip_uninstall(capsys):
    with pytest.raises(SystemExit):
        cli.main(["skills", "uninstall", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "before `pip uninstall sibyl-memory-cli`" in out and "hooks" in out


# ----------------------------------------------------------------------
# Codex skills shim (Codex CLI 0.135.0 discovers $CODEX_HOME/skills)
# ----------------------------------------------------------------------

def _codex_quick_validate(skill_dir: Path) -> tuple[bool, str]:
    """The rules of the skill validator bundled in the installed Codex CLI
    0.135.0 (skill-creator/scripts/quick_validate.py, read from the binary)."""
    import re as _re
    import yaml
    content = (skill_dir / "SKILL.md").read_text()
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"
    m = _re.match(r"^---\n(.*?)\n---", content, _re.DOTALL)
    if not m:
        return False, "Invalid frontmatter format"
    fm = yaml.safe_load(m.group(1))
    if not isinstance(fm, dict):
        return False, "Frontmatter must be a YAML dictionary"
    if set(fm) - {"name", "description", "license", "allowed-tools", "metadata"}:
        return False, "unexpected keys"
    name, desc = str(fm.get("name", "")).strip(), str(fm.get("description", "")).strip()
    if not name or not _re.match(r"^[a-z0-9-]+$", name) or name.startswith("-") or "--" in name \
            or len(name) > 64:
        return False, "bad name"
    if not desc or "<" in desc or ">" in desc or len(desc) > 1024:
        return False, "bad description"
    return True, "Skill is valid!"


def test_codex_skill_shims_install_validate_and_uninstall(tmp_path, home, capsys):
    """a6338cc wrote no Codex skill (the AGENTS.md block only)."""
    from sibyl_memory_client import skills as SK
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    w = SU.CodexWirer(config_path=cfg)
    w.wire_lifecycle()
    for name, shim in SK.SHIM_NAMES.items():
        d = cfg.parent / "skills" / shim
        assert _codex_quick_validate(d) == (True, "Skill is valid!")
        t = (d / "SKILL.md").read_text()
        assert f"Read `~/.sibyl-memory/skills/{name}` and follow it exactly" in t
    st = SK.status(home)["shims"]
    assert {v["harness"] for k, v in st.items() if "/.codex/skills/" in k} == {"codex"}
    # An edited shim is kept on refresh and on uninstall; an unedited one goes.
    edited = cfg.parent / "skills" / "sibyl-debloat" / "SKILL.md"
    edited.write_text(edited.read_text() + "\nmy note\n")
    assert "user_modified" in " ".join(w.wire_lifecycle())
    assert edited.read_text().endswith("my note\n")
    out = SK.uninstall(home)
    assert out[str(cfg.parent / "skills" / "sibyl-save" / "SKILL.md")] == "removed"
    assert out[str(edited)] != "removed" and edited.exists()
    assert not (cfg.parent / "skills" / "sibyl-save" / "SKILL.md").exists()


# ----------------------------------------------------------------------
# Copy: migrate pointer (C6-9), automatic save timing (F7), uninstall (F8)
# ----------------------------------------------------------------------

def test_c6_9_migrate_pointer_claims_only_what_is_true(tmp_path):
    """a6338cc: 'the history that was here ... was extracted into Sibyl
    Memory' (the extraction stores entries, not necessarily this text)."""
    from sibyl_memory_cli import migrate as M
    text = "# Rules\n\n" + "".join(f"- rule {i}\n" for i in range(20)) + "\n## History\n" + \
        "".join(f"- 2025-01-0{i % 9 + 1} old thing {i}\n" for i in range(30))
    lean = M.lean_text_for(text, "AGENTS.md", tmp_path / "bk")
    assert lean is not None
    assert f"a full copy of this file is in the backup at {tmp_path / 'bk'}; the migrate extraction " \
           "stored entries in Sibyl Memory" in lean
    assert "was extracted into Sibyl Memory" not in lean
    lean2 = M.heuristic_lean("# A\n\n## one\nx\n## two\n" + "y\n" * 50)
    assert "the migrate extraction stored entries in Sibyl Memory" in lean2
    assert "accumulated memory now lives in Sibyl Memory" not in lean2


_REPO = Path(__file__).resolve().parents[2]
_READMES = ["README.md", "sibyl-memory-cli/README.md", "sibyl-memory-client/README.md",
            "sibyl-memory-hermes/README.md", "sibyl-memory-mcp/README.md", "sibyl-memory-langgraph/README.md"]


def test_f7_f8_every_readme_says_when_automatic_save_runs_and_how_to_uninstall():
    """a6338cc: 'Automatic save (the Claude Code Stop hook, session capsules)'
    read as a save at session end; no README said to remove the hooks first."""
    for rel in _READMES:
        t = " ".join((_REPO / rel).read_text().split())
        assert "sibyl skills uninstall" in t and "before `pip uninstall" in t or \
            "run `sibyl skills uninstall` first" in t, rel
        if rel != "sibyl-memory-langgraph/README.md":
            # R1 (2026-09-29): the cadence is by active time now; the "one nudge per
            # session" copy is gone from every README.
            assert ("once early (after 5 memory writes or a compaction), then about every 60 minutes "
                    "of active work") in t, rel
            assert "one nudge per session" not in t, rel
            assert "Automatic save (the Claude Code Stop hook, session capsules)" not in t, rel
            assert "\u2014" not in (_REPO / rel).read_text().split("## Save and debloat", 1)[1]
