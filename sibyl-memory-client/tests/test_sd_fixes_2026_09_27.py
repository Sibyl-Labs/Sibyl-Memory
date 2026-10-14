"""Independent-check fixes on the save/debloat build (SD-7.., 2026-09-27).

F1  automatic trim is authorised per FILE PATH, never per file name;
F2  a file below a symlinked folder is refused before anything is written;
F3  no backup survives an apply that did not rewrite its file; backups, plans
    and the weight journal are bounded;
F4  undo restores the backup the last apply recorded, and refuses a backup
    identical to the file as it is now;
F10 the write counter runs only for clients the plugin's own surfaces enable;
F12 the sibling ``*-history`` append is idempotent.

Each test fails on a6338cc (named in the test's docstring where the failure is
behavioural rather than a missing name).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import runner as RUN
from sibyl_memory_client.lifecycle import state as ST

CUR = "# Rules\n\n" + "".join(f"- current rule {i} stays\n" for i in range(12)) + "\n"
HIST = "## History\n" + "".join(f"- old line {i}\n" for i in range(30))


@pytest.fixture(autouse=True)
def _no_heartbeat(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")


def _age(p: Path, seconds: float = 3600) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def _file(p: Path, text: str = CUR + HIST) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _age(p)
    return p


def _client(tmp_path: Path, name: str = "m.db") -> MemoryClient:
    return MemoryClient.local(tmp_path / name)


def _raw_policy(home: Path, data: dict) -> None:
    d = home / ".sibyl-memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / "debloat.json").write_text(json.dumps(data))


# ----------------------------------------------------------------------
# F1: per-path authorisation
# ----------------------------------------------------------------------

def test_f1_auth_id_is_the_normalised_path_home_files_as_tilde(tmp_path):
    home = Path.home()
    assert P.auth_id(home / "proj" / "sub" / ".." / "AGENTS.md", home) == "~/proj/AGENTS.md"
    assert P.auth_id(home / "CLAUDE.md", home) == "~/CLAUDE.md"
    assert P.auth_id(tmp_path / "x" / "AGENTS.md", home) == (tmp_path / "x" / "AGENTS.md").as_posix()
    for bare in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md", "~/../etc/hosts", "", "~/"):
        assert not P.is_path_id(bare)
        with pytest.raises(ValueError):
            P.set_auto(bare, True, home)


def test_f1_cross_repo_a_name_authorised_in_one_project_trims_nothing_elsewhere(tmp_path, entitle_as):
    """a6338cc: `sibyl debloat auto AGENTS.md` stored the bare name, and the
    SessionStart run in ANY other project trimmed that project's AGENTS.md."""
    entitle_as("pro")
    home = Path.home()
    a = _file(tmp_path / "repo-a" / "AGENTS.md")
    b = _file(tmp_path / "repo-b" / "AGENTS.md")
    b_before = b.read_bytes()
    # What a6338cc's `sibyl debloat auto AGENTS.md` wrote in repo-a:
    _raw_policy(home, {"mode": "auto", "auto_files": ["AGENTS.md"]})
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=b.parent, harness="t")
    assert r.applied == [] and b.read_bytes() == b_before
    # The per-path authorisation of repo-a's file applies there only.
    P.set_auto(P.auth_id(a, home), True, home)
    r = RUN.run_automatic(_client(tmp_path, "b.db"), home=home, cwd=b.parent, harness="t",
                          now=time.time() + 120)
    assert r.applied == [] and b.read_bytes() == b_before
    r = RUN.run_automatic(_client(tmp_path, "c.db"), home=home, cwd=a.parent, harness="t",
                          now=time.time() + 240)
    assert [x["applied"] for x in r.applied] == [True] and "old line 3" not in a.read_text()
    assert b.read_bytes() == b_before


def test_f1_project_claude_md_authorisation_never_matches_home_claude_md(tmp_path, entitle_as):
    """a6338cc matched ``c.key`` too, so `auto CLAUDE.md` also matched ~/CLAUDE.md."""
    entitle_as("pro")
    home = Path.home()
    proj_f = _file(tmp_path / "proj" / "CLAUDE.md")
    home_f = _file(home / "CLAUDE.md")
    home_before = home_f.read_bytes()
    _raw_policy(home, {"mode": "auto", "auto_files": ["CLAUDE.md"]})
    RUN.run_automatic(_client(tmp_path), home=home, cwd=proj_f.parent, harness="t")
    assert home_f.read_bytes() == home_before
    P.set_auto(P.auth_id(proj_f, home), True, home)
    r = RUN.run_automatic(_client(tmp_path, "b.db"), home=home, cwd=proj_f.parent, harness="t",
                          now=time.time() + 120)
    done = [x["file"] for x in r.applied if x["applied"]]
    assert done == ["CLAUDE.md"] and home_f.read_bytes() == home_before


def test_f1_name_only_entries_are_dropped_with_a_one_time_notice(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    keep = P.auth_id(_file(tmp_path / "other" / "AGENTS.md"), home)
    _raw_policy(home, {"mode": "auto", "auto_files": ["AGENTS.md", keep]})
    assert P.legacy_auto_entries(home) == ["AGENTS.md"]
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=f.parent, harness="t")
    assert r.line.startswith("context trim: automatic trim is now set per file path")
    assert "AGENTS.md was set by name only and is back to manual" in r.line
    pol = json.loads((home / ".sibyl-memory" / "debloat.json").read_text())
    assert pol["auto_files"] == [keep] and pol["mode"] == "auto"
    assert P.legacy_auto_entries(home) == [] and P.take_legacy_notice(home) is None
    r2 = RUN.run_automatic(_client(tmp_path, "b.db"), home=home, cwd=f.parent, harness="t",
                           now=time.time() + 120)
    assert r2.line is None or "per file path" not in r2.line
    # Only name-only entries: the mode falls back to propose.
    _raw_policy(home, {"mode": "auto", "auto_files": ["CLAUDE.md"]})
    assert "CLAUDE.md was set by name only" in P.take_legacy_notice(home)
    assert P.load(home)["mode"] == "propose" and P.load(home)["auto_files"] == []


# ----------------------------------------------------------------------
# F2: symlinked folders are refused before any write
# ----------------------------------------------------------------------

def test_f2_symlinked_dot_claude_pointing_outside_home_is_refused_before_backup_or_memory(tmp_path, entitle_as):
    """a6338cc: backup and memory copy were written, then the rewrite failed
    on the symlinked parent, at every session start. SD-11: a symlinked
    folder is resolved; a real location outside the real home and the
    project is refused, still before anything is written."""
    entitle_as("pro")
    home = Path.home()
    real = tmp_path / "dotfiles" / "claude"
    f = _file(real / "CLAUDE.md")
    before = f.read_bytes()
    (home / ".claude").symlink_to(real, target_is_directory=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    client = _client(tmp_path)
    c, _ = A.find_candidate("~/.claude/CLAUDE.md", home, proj)
    assert c is not None
    res = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    assert not res.applied and res.reason == "refused: outside your home and this project"
    assert f.read_bytes() == before
    assert not A.backups_root(home).exists() or list(A.backups_root(home).iterdir()) == []
    assert client.get_reference(f"debloat/~/.claude/CLAUDE.md/{time.strftime('%Y-%m-%d')}") is None


def test_f2_a_symlink_two_levels_up_is_resolved_never_written_through(tmp_path):
    """a6338cc wrote THROUGH a symlinked grandparent (the writer only checked
    the immediate parent); 4abce3a refused the file outright. SD-11: the trim
    reads and writes the real location, so no symlinked folder is on the
    written path and the link itself is left alone."""
    home = Path.home()
    realroot = tmp_path / "realroot"
    f = _file(realroot / "proj" / "AGENTS.md")
    link = tmp_path / "linkroot"
    link.symlink_to(realroot, target_is_directory=True)
    cwd = link / "proj"
    c, _ = A.find_candidate("AGENTS.md", home, cwd)
    res = A.apply_one(c, home=home, cwd=cwd, destination="sibling")
    assert res.applied, res.reason
    assert "old line 3" not in f.read_text() and (realroot / "proj" / "AGENTS-history.md").is_file()
    assert link.is_symlink() and os.readlink(link) == str(realroot)
    assert A.backup_rel(cwd / "AGENTS.md", home) == A.backup_rel(f, home)
    ok, msg, _ = A.undo(cwd / "AGENTS.md", home=home, cwd=cwd)
    assert ok, msg
    assert "old line 3" in f.read_text() and link.is_symlink()


# ----------------------------------------------------------------------
# F3: bounded growth
# ----------------------------------------------------------------------

def test_f3_an_apply_that_does_not_rewrite_leaves_no_backup(tmp_path, entitle_as, monkeypatch):
    """a6338cc kept backups/<ts>/ for every attempt, rewritten or not."""
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    client = _client(tmp_path)
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    monkeypatch.setattr(client, "set_reference", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert not A.apply_one(c, home=home, cwd=f.parent, destination="memory", client=client).applied
    real = A.atomic_write_bytes

    def fail_source(path, data, **kw):
        if Path(path) == f:
            raise OSError("injected")
        return real(path, data, **kw)
    monkeypatch.undo()
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setattr(A, "atomic_write_bytes", fail_source)
    res = A.apply_one(c, home=home, cwd=f.parent, destination="memory", client=client)
    assert not res.applied and "rewrite failed" in res.reason and res.backup is None
    root = A.backups_root(home)
    assert not root.exists() or list(root.iterdir()) == []


def test_f3_backups_per_file_are_capped_at_the_newest_20(tmp_path):
    """a6338cc kept every backup (25 here)."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    t0 = time.time()
    for i in range(25):
        f.write_text(CUR + HIST.replace("old line", f"round {i} old line"))
        _age(f)
        res = A.apply_one(c, home=home, cwd=f.parent, destination="sibling", now=t0 + i)
        assert res.applied, res.reason
    dirs = [d for d in A.backups_root(home).iterdir() if d.is_dir()]
    assert len(dirs) == A.MAX_BACKUPS_PER_FILE == 20
    kept = A.backups_of(A.backup_rel(f, home), home)
    assert len(kept) == 20 and "round 24" in kept[-1].read_text() and "round 5 " in kept[0].read_text()


def test_f3_plans_keep_the_newest_50(tmp_path, entitle_as):
    """a6338cc wrote plans/<ts>.json at every process start, forever."""
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    client = _client(tmp_path)
    t0 = time.time()
    for i in range(55):
        RUN.run_automatic(client, home=home, cwd=f.parent, harness="t", now=t0 + i * 61)
    plans = sorted((A.debloat_dir(home) / "plans").glob("*.json"))
    assert len(plans) == RUN.MAX_PLANS == 50
    stamp = time.strftime("%Y-%m-%dT%H_%M_%S", time.gmtime(t0 + 54 * 61))
    assert plans[-1].name == f"{stamp}.json"


def test_f3_weight_journal_rotates_past_256_kb(tmp_path):
    """a6338cc appended to history.jsonl forever."""
    home = Path.home()
    d = A.debloat_dir(home)
    d.mkdir(parents=True)
    (d / "history.jsonl").write_bytes(b'{"x":1}\n' * 40000)
    A.journal(home, {"ts": 1, "total_tok": 2})
    assert (d / "history.jsonl.1").stat().st_size > 256 * 1024
    assert (d / "history.jsonl").read_text() == '{"total_tok":2,"ts":1}\n'


# ----------------------------------------------------------------------
# F4: undo uses the recorded backup
# ----------------------------------------------------------------------

def test_f4_undo_prefers_the_recorded_backup_and_refuses_an_identical_one(tmp_path):
    """a6338cc restored the NEWEST backup: after an interrupted later attempt
    that is a copy of the current file, so 'undo' changed nothing and said
    'restored'."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    original = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    res = A.apply_one(c, home=home, cwd=f.parent, destination="sibling")
    assert res.applied and res.backup
    trimmed = f.read_bytes()
    # A later attempt interrupted after its backup (a kill): a newer backup
    # holding the trimmed bytes.
    A.backup(f, trimmed, home=home, cwd=f.parent, now=time.time() + 60)
    ok, msg, src = A.undo(f, home=home, cwd=f.parent)
    assert not ok and msg.startswith("nothing to undo: the backup") and f.read_bytes() == trimmed
    ok, msg, src = A.undo(f, home=home, cwd=f.parent, prefer=res.backup)
    assert ok and src == res.backup and f.read_bytes() == original
    # Another project's AGENTS.md never gets this project's backup: neither
    # through the recorded path nor as "the newest backup" (a6338cc stored
    # both under project/AGENTS.md, so this undo restored repo 1's file here).
    other_text = CUR + "## History\n- a line only the other project has\n"
    other = _file(tmp_path / "other" / "AGENTS.md", other_text)
    ok, msg, _ = A.undo(other, home=home, cwd=other.parent, prefer=res.backup)
    assert not ok and msg == "no backup found" and other.read_text() == other_text
    ok, msg, _ = A.undo(other, home=home, cwd=other.parent)
    assert not ok and other.read_text() == other_text
    assert A.backup_rel(other, home) != A.backup_rel(f, home)


def test_f4_the_runner_records_the_backup_in_last_apply(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    P.set_auto(P.auth_id(f, home), True, home)
    RUN.run_automatic(_client(tmp_path), home=home, cwd=f.parent, harness="t")
    res = ST.LifecycleState(home).load()["debloat"]["last_apply"]["results"][0]
    assert res["applied"] and Path(res["backup"]).read_text() == CUR + HIST


# ----------------------------------------------------------------------
# F12: sibling append is idempotent
# ----------------------------------------------------------------------

def test_f12_reapply_after_a_failed_rewrite_does_not_append_the_history_twice(tmp_path, monkeypatch):
    """a6338cc appended the same block again on the re-apply."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    real = A.atomic_write_bytes

    def fail_source(path, data, **kw):
        if Path(path) == f:
            raise OSError("injected")
        return real(path, data, **kw)
    monkeypatch.setattr(A, "atomic_write_bytes", fail_source)
    assert not A.apply_one(c, home=home, cwd=f.parent, destination="sibling").applied
    sib = A.sibling_path(f)
    once = sib.read_text()
    assert once.count("## History") == 1
    monkeypatch.setattr(A, "atomic_write_bytes", real)
    assert A.apply_one(c, home=home, cwd=f.parent, destination="sibling").applied
    assert sib.read_text() == once


# ----------------------------------------------------------------------
# F5 (client side): tmp sweep
# ----------------------------------------------------------------------

def test_f5_stale_sibyl_tmp_files_next_to_a_candidate_are_swept(tmp_path):
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    old = f.parent / ".AGENTS.md.abc123.sibyl-tmp"
    fresh = f.parent / ".AGENTS.md.def456.sibyl-tmp"
    other = f.parent / ".CLAUDE.md.x.sibyl-tmp"
    for p in (old, fresh, other):
        p.write_text("partial")
    _age(old, 120)
    _age(other, 120)
    assert A.sweep_stale_tmp(f) == 1
    assert not old.exists() and fresh.exists() and other.exists()
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    _age(fresh, 120)
    A.apply_one(c, home=home, cwd=f.parent, destination="sibling")
    assert not fresh.exists()


# ----------------------------------------------------------------------
# F10: the write counter is opt-in
# ----------------------------------------------------------------------

def test_f10_a_plain_memory_client_writes_no_lifecycle_file(tmp_path, monkeypatch):
    """a6338cc: EVERY MemoryClient write fed ~/.sibyl-memory/lifecycle.json
    (plus a timer thread and an exit hook), whatever store it wrote to."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("SIBYL_MEMORY_DB", str(other / "memory.db"))
    client = MemoryClient.local(other / "memory.db")
    for i in range(12):
        client.set_entity("fact", f"f{i}", {"v": i})
    client.set_state("sibyl_save:last_save", {"ts": "now"})
    ST.flush_all()
    assert not (Path.home() / ".sibyl-memory" / "lifecycle.json").exists()
    assert not (other / ".sibyl-memory").exists() and not (other / "lifecycle.json").exists()
    assert ST.pending_writes(client.get_tenant()) == 0


def test_f10_an_enabled_client_counts_into_the_home_it_was_given(tmp_path):
    client = _client(tmp_path)
    alt = tmp_path / "alt-home"
    client.enable_lifecycle_counting(alt)
    for i in range(10):
        client.set_entity("fact", f"f{i}", {"v": i})
    ST.flush_all()
    t = ST.LifecycleState(alt).load()["tenants"][client.get_tenant()]
    assert t["writes_since_save"] == 10
    assert not (Path.home() / ".sibyl-memory" / "lifecycle.json").exists()


# ----------------------------------------------------------------------
# F5 (client side): a gate that never refreshes; the cap gate's local test
# ----------------------------------------------------------------------

def test_f5_a_no_refresh_gate_never_calls_the_network_in_any_band(tmp_path, test_signer):
    """a6338cc had no such gate: the hooks' client could block 4 s on the
    bootstrap refresh or start a background refresh the hook's exit killed."""
    from sibyl_memory_client import _entitlement as E
    from sibyl_memory_client.testing import write_credentials, write_entitlement
    calls = []

    def refresh(a, t):
        calls.append(a)
        return E.RefreshOutcome("transient", code="offline")
    d = tmp_path / "sm"
    write_credentials(d)
    # setup incomplete (credentials, no entitlement): the normal gate refreshes, blocking
    g = E.EntitlementGate(d / "credentials.json", refresh_fn=refresh, allow_refresh=False)
    assert g.prime().code == "setup_incomplete" and g.evaluate().code == "setup_incomplete"
    assert g.refresh().code == "refresh_disabled"
    # fresh but past its renew-after: the normal gate starts a background refresh
    now = time.time()
    write_entitlement(d, test_signer.mint(tier="pro", cap=None, now=now - 2 * 86400))
    g = E.EntitlementGate(d / "credentials.json", refresh_fn=refresh, allow_refresh=False)
    dec = g.evaluate()
    assert dec.state == "fresh" and dec.entitlement.tier == "pro"
    time.sleep(0.2)
    assert calls == []
    normal = E.EntitlementGate(d / "credentials.json", refresh_fn=refresh, background_refresh=False)
    normal.evaluate()
    assert calls, "control: the ordinary gate does refresh here"


def test_f5_cap_gate_decides_locally_only_with_a_fresh_trusted_uncapped_cache(tmp_path):
    from sibyl_memory_client._capcheck import CapGate, TierCache, TierCacheEntry
    size = {"n": 1000}
    cache = TierCache(tmp_path / "tier_cache.json")

    def gate(hint, ent_tier):
        return CapGate(account_id="acct", session_token="tok", db_size_fn=lambda: size["n"],
                       local_tier_hint=hint, cache=cache, cap_bytes=5000,
                       check_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")),
                       entitlement_tier_fn=lambda: ent_tier)
    assert gate("pro", "pro").decides_locally() is False            # paid, no cache: would call
    assert gate("free", "free").decides_locally(100) is False         # SD-11: capped is never local
    assert gate("free", "free").decides_locally(10_000) is False      # free, over: would call
    cache.store(TierCacheEntry(account_id="acct", tier="pro", checked_at=time.time(), cap_bytes=None))
    assert gate("pro", "pro").decides_locally(10**9) is True          # fresh uncapped cache
    assert gate("free", "free").decides_locally(10**9) is False       # cache alone never unlocks
    cache.store(TierCacheEntry(account_id="acct", tier="pro", checked_at=time.time() - 30 * 86400,
                               cap_bytes=None))
    assert gate("pro", "pro").decides_locally() is False              # stale cache


# ----------------------------------------------------------------------
# F6 / F7 / F9: the procedure text says what the plugin really does
# ----------------------------------------------------------------------

def test_f6_f7_save_md_codex_delivery_and_when_the_pro_nudge_comes():
    """a6338cc: 'a custom prompt in Codex' (none is written) and 'asks you to
    save at the end of a session' (Stop fires at a turn end)."""
    from sibyl_memory_client import skills as SK
    t = SK.text("SAVE.md")
    assert "a custom prompt in Codex" not in t
    assert "a marked block in Codex's global AGENTS.md" in t
    assert "at the end of a session (the Pro auto-save continuation)" not in t
    # R1 (2026-09-29): the cadence is by active time and the Codex row is fixed.
    assert "once early, after 5 memory writes or a compaction, then about every hour of active work" in t
    assert "one nudge per session" not in t
    changes = (Path(SK.__file__).parent / "_skills" / "CHANGES.md").read_text()
    assert "`~/.codex/skills/<name>/SKILL.md`" in changes and "Known stale" not in changes


def test_f9_debloat_md_explains_sibyl_keep_and_it_is_true():
    """a6338cc's DEBLOAT.md named the markers but never said which live
    sections the splitter would take for history."""
    from sibyl_memory_client import skills as SK
    from sibyl_memory_client.lifecycle import splitter as S
    t = SK.text("DEBLOAT.md")
    assert "Protect what is current but looks like history" in t and '"Log format"' in t
    assert "`<!-- sibyl:keep -->` ... `<!-- /sibyl:keep -->`" in t
    doc = ("# Rules\n\n" + "".join(f"- rule {i}\n" for i in range(20)) + "\n## Log format\n"
           + "".join(f"- log field {i} is required\n" for i in range(10)))
    assert "Log format" in S.split(doc, kind="markdown").sections
    kept = doc.replace("## Log format\n", "<!-- sibyl:keep -->\n## Log format\n") + "<!-- /sibyl:keep -->\n"
    pl = S.split(kept, kind="markdown")
    assert "Log format" not in pl.sections and "log field 3" in pl.current


def test_skill_files_watermark_and_manifest_are_consistent_after_the_edit():
    import hashlib
    import json as _json
    from sibyl_memory_client import skills as SK
    d = Path(SK.__file__).parent / "_skills"
    man = _json.loads((d / "manifest.json").read_text())
    for name in SK.SKILL_NAMES:
        doc = (d / name).read_text(encoding="utf-8")
        assert SK.watermark_fields(doc)["hash"] == SK.watermark_hash(doc)
        assert man["files"][name]["sha256"] == hashlib.sha256((d / name).read_bytes()).hexdigest()
