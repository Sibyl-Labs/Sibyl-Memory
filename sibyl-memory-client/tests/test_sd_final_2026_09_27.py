"""Final fixes on the save/debloat build after its second independent check
(SD-11.., 2026-09-27).

A   an apply interrupted between the rewrite and its bookkeeping keeps the
    backup (the source is rewritten, so the backup is the only original);
B   the SAVE shim says what `headless` really is, and installed shims refresh;
C   a symlinked folder above an instruction file (Fedora Atomic ``/home ->
    var/home``, stow-style ``~/.claude -> ~/dotfiles/claude``, a project root
    reached through a link) is resolved, never refused and never written
    through; identity (auth_id, backup slot) is the real path; a real location
    outside the real home and root is refused; stored ids migrate; a refused
    automatic file is named in the report;
E   the sibling append is idempotent across days;
F   the cap gate decides locally only on a fresh, trusted, uncapped cache.

Each test fails on 4abce3a (the docstring says how).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import runner as RUN

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


# ----------------------------------------------------------------------
# A: no data-loss window between the rewrite and `applied = True`
# ----------------------------------------------------------------------

def test_a_interrupt_right_after_the_rewrite_keeps_the_backup(tmp_path, monkeypatch):
    """4abce3a: the `finally` discarded the backup whenever `applied` was
    still False, so a KeyboardInterrupt landing after os.replace left the
    source rewritten AND its only original deleted."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    original = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    real = A.atomic_write_bytes

    def write_then_interrupt(path, data, **kw):
        out = real(path, data, **kw)
        if Path(path) == f:
            raise KeyboardInterrupt
        return out
    monkeypatch.setattr(A, "atomic_write_bytes", write_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        A.apply_one(c, home=home, cwd=f.parent, destination="sibling")
    assert f.read_bytes() != original                       # the rewrite landed
    kept = A.backups_of(A.backup_rel(f, home), home)
    assert len(kept) == 1 and kept[0].read_bytes() == original


def test_a_an_apply_that_never_touched_the_source_still_removes_its_backup(tmp_path, monkeypatch):
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    original = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    real = A.atomic_write_bytes

    def interrupt_before(path, data, **kw):
        if Path(path) == f:
            raise KeyboardInterrupt
        return real(path, data, **kw)
    monkeypatch.setattr(A, "atomic_write_bytes", interrupt_before)
    with pytest.raises(KeyboardInterrupt):
        A.apply_one(c, home=home, cwd=f.parent, destination="sibling")
    assert f.read_bytes() == original
    assert A.backups_of(A.backup_rel(f, home), home) == []


def test_a_a_source_edited_during_the_apply_keeps_the_backup(tmp_path, monkeypatch):
    """Not verifiably the file that was read: the backup is never discarded."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    original = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    real_backup = A.backup

    def backup_then_edit(path, data, **kw):
        out = real_backup(path, data, **kw)
        f.write_text(CUR + "- someone else's edit\n")
        return out
    monkeypatch.setattr(A, "backup", backup_then_edit)
    res = A.apply_one(c, home=home, cwd=f.parent, destination="sibling")
    assert not res.applied and "changed while planning" in res.reason
    kept = A.backups_of(A.backup_rel(f, home), home)
    assert len(kept) == 1 and kept[0].read_bytes() == original


# ----------------------------------------------------------------------
# B: the SAVE shim copy
# ----------------------------------------------------------------------

OLD_HEADLESS = "`headless` (the plugin asked at session end: ask nothing)"


def test_b_save_shim_says_one_nudge_after_5_writes_or_a_compaction():
    """4abce3a said the plugin asks 'at session end'."""
    t = skills.shim_text("SAVE.md", "claude-code")
    assert "at session end" not in t
    # R1 (2026-09-29): by active time, not once per session.
    assert "once early, after 5 memory writes or a compaction, then about every hour of active work" in t
    assert "once per session" not in t


def test_b_an_installed_shim_with_the_old_text_refreshes_through_the_checksum(tmp_path):
    home = tmp_path / "h"
    shim = home / ".claude" / "skills" / "sibyl-save" / "SKILL.md"
    new = skills.shim_text("SAVE.md", "claude-code")
    old = new.replace(new[new.index("`headless` ("):new.index("ask nothing)") + len("ask nothing)")],
                      OLD_HEADLESS)
    assert old != new and OLD_HEADLESS in old
    assert skills.install_shim(home, shim, old, harness="claude-code", skill="SAVE.md") == "installed"
    assert skills.install_shim(home, shim, new, harness="claude-code", skill="SAVE.md") == "updated"
    assert shim.read_text() == new
    man = json.loads((skills.skills_dir(home) / "manifest.json").read_text())
    assert man["shims"][str(shim)]["sha256"] == hashlib.sha256(new.encode()).hexdigest()
    assert skills.status(home)["shims"][str(shim)]["state"] == "ok"


# ----------------------------------------------------------------------
# C: symlinked folders are resolved; identity is the real path
# ----------------------------------------------------------------------

def _fedora_home(tmp_path: Path) -> tuple[Path, Path]:
    """(logical home, real home): ``<t>/home -> var/home`` like Fedora Atomic."""
    (tmp_path / "var" / "home" / "u").mkdir(parents=True)
    (tmp_path / "home").symlink_to("var/home", target_is_directory=True)
    return tmp_path / "home" / "u", tmp_path / "var" / "home" / "u"


def test_c_fedora_atomic_home_symlink_is_trimmed(tmp_path):
    """4abce3a refused every file under /home (an ancestor is a symlink), so
    debloat did nothing at all on Fedora Atomic / Silverblue."""
    home_l, home_r = _fedora_home(tmp_path)
    f = _file(home_l / ".claude" / "CLAUDE.md")
    proj = _file(tmp_path / "proj" / "README.md").parent
    c, _ = A.find_candidate("~/.claude/CLAUDE.md", home_l, proj)
    assert c is not None
    res = A.apply_one(c, home=home_l, cwd=proj, destination="sibling")
    assert res.applied, res.reason
    assert "old line 3" not in f.read_text()
    assert (home_r / ".claude" / "CLAUDE-history.md").is_file()
    assert (tmp_path / "home").is_symlink()
    assert A.backup_rel(f, home_l) == ".claude/CLAUDE.md"
    assert res.backup and Path(res.backup).is_file()
    ok, msg, _ = A.undo(f, home=home_l, cwd=proj)
    assert ok, msg
    assert "old line 3" in f.read_text()


def test_c_fedora_home_identity_is_stable_and_the_pro_run_trims(tmp_path, entitle_as):
    entitle_as("pro")
    home_l, home_r = _fedora_home(tmp_path)
    f = _file(home_l / ".claude" / "CLAUDE.md")
    proj = _file(tmp_path / "proj" / "README.md").parent
    assert P.auth_id(f, home_l) == P.auth_id(home_r / ".claude" / "CLAUDE.md", home_l) \
        == P.auth_id(f, home_r) == "~/.claude/CLAUDE.md"
    P.set_auto(P.auth_id(f, home_l), True, home_l)
    r = RUN.run_automatic(_client(tmp_path), home=home_l, cwd=proj, harness="t")
    assert [(a["file"], a["applied"]) for a in r.applied] == [("~/.claude/CLAUDE.md", True)], r.applied


def test_c_stow_dot_claude_symlink_is_trimmed_at_its_real_location(tmp_path, entitle_as):
    """4abce3a: `~/.claude -> ~/dotfiles/claude` refused the file, including
    for a file the person set to automatic (silently, every start)."""
    entitle_as("pro")
    home = Path.home()
    f = _file(home / "dotfiles" / "claude" / "CLAUDE.md")
    (home / ".claude").symlink_to(home / "dotfiles" / "claude", target_is_directory=True)
    proj = _file(tmp_path / "proj" / "README.md").parent
    c, _ = A.find_candidate("~/.claude/CLAUDE.md", home, proj)
    fid = P.auth_id(c.path, home)
    assert fid == P.auth_id(f, home) == "~/dotfiles/claude/CLAUDE.md"
    P.set_auto(fid, True, home)
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=proj, harness="t")
    assert [(a["file"], a["applied"]) for a in r.applied] == [("~/.claude/CLAUDE.md", True)], r.applied
    assert "old line 3" not in f.read_text() and (home / ".claude").is_symlink()
    assert r.applied[0]["key"].startswith("debloat/~/.claude/CLAUDE.md/")


def test_c_root_reached_through_a_symlink_has_the_real_identity(tmp_path, entitle_as):
    """Authorised at the real path, run from the link (and the other way):
    one file, one authorisation, one backup slot."""
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "real" / "proj" / "AGENTS.md")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    via = link / "proj" / "AGENTS.md"
    assert P.auth_id(via, home) == P.auth_id(f, home) == f.as_posix()
    assert A.backup_rel(via, home) == A.backup_rel(f, home)
    P.set_auto(P.auth_id(f, home), True, home)
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=link / "proj", harness="t")
    assert [a["applied"] for a in r.applied] == [True], r.applied
    assert "old line 3" not in f.read_text() and link.is_symlink()


def test_c_a_real_location_outside_home_and_root_is_refused(tmp_path, entitle_as):
    """The resolved file must stay under the real home or the real root."""
    entitle_as("pro")
    home = Path.home()
    outside = _file(tmp_path / "elsewhere" / "claude" / "CLAUDE.md")
    before = outside.read_bytes()
    proj = _file(tmp_path / "proj" / "README.md").parent
    (proj / ".claude").symlink_to(tmp_path / "elsewhere" / "claude", target_is_directory=True)
    c, _ = A.find_candidate(".claude/CLAUDE.md", home, proj)
    assert c is not None
    res = A.apply_one(c, home=home, cwd=proj, destination="sibling")
    assert res.reason == "refused: outside your home and this project"
    assert outside.read_bytes() == before and not A.backups_root(home).exists()
    ok, msg, _ = A.undo(proj / ".claude" / "CLAUDE.md", home=home, cwd=proj)
    assert not ok and "outside" in msg
    # set to automatic anyway (a hand-edited policy): the report names it
    P.set_auto(P.auth_id(c.path, home), True, home)
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=proj, harness="t")
    assert "on automatic but not trimmed: .claude/CLAUDE.md: refused: outside your home" in r.line
    assert outside.read_bytes() == before


def test_c_a_refused_automatic_file_is_named_in_the_report(tmp_path, entitle_as):
    """4abce3a: a file on automatic that the apply refused produced no line
    (the run said nothing, every start)."""
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    P.set_auto(P.auth_id(f, home), True, home)
    os.utime(f, None)                                        # just edited
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=f.parent, harness="t")
    assert r.line is not None and "on automatic but not trimmed: AGENTS.md: refused: modified in the " \
        "last 10 minutes" in r.line
    assert "old line 3" in f.read_text()


def test_c_stored_ids_migrate_to_the_real_path_or_are_dropped(tmp_path):
    """4abce3a stored the logical path; a run from the real path never matched
    it. Existing entries are re-resolved once; a missing file's entry goes."""
    home = Path.home()
    f = _file(tmp_path / "real" / "proj" / "AGENTS.md")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real", target_is_directory=True)
    logical = (link / "proj" / "AGENTS.md").as_posix()
    gone = (tmp_path / "nothere" / "AGENTS.md").as_posix()
    d = home / ".sibyl-memory"
    d.mkdir(parents=True, exist_ok=True)
    (d / "debloat.json").write_text(json.dumps({"mode": "auto", "auto_files": [logical, gone]}))
    line = P.take_legacy_notice(home)
    assert line is not None and gone in line and "no longer there; back to manual" in line
    pol = P.load(home)
    assert pol["auto_files"] == [f.as_posix()] and pol["mode"] == "auto"
    assert P.take_legacy_notice(home) is None                # once
    # only re-resolved, nothing dropped: silent
    (d / "debloat.json").write_text(json.dumps({"mode": "auto", "auto_files": [logical]}))
    assert P.take_legacy_notice(home) is None and P.load(home)["auto_files"] == [f.as_posix()]


def test_c_last_apply_records_the_file_id(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    P.set_auto(P.auth_id(f, home), True, home)
    RUN.run_automatic(_client(tmp_path), home=home, cwd=f.parent, harness="t")
    from sibyl_memory_client.lifecycle import state as ST
    res = ST.LifecycleState(home).load()["debloat"]["last_apply"]["results"]
    assert res[0]["id"] == f.as_posix() and res[0]["applied"] is True


# ----------------------------------------------------------------------
# E: sibling idempotence across days
# ----------------------------------------------------------------------

def test_e_a_retry_on_a_later_day_does_not_append_the_history_twice(tmp_path, monkeypatch):
    """4abce3a compared the whole block including the dated header, so a
    retry the next day appended the same history again."""
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    real = A.atomic_write_bytes

    def fail_source(path, data, **kw):
        if Path(path) == f:
            raise OSError("injected")
        return real(path, data, **kw)
    monkeypatch.setattr(A, "atomic_write_bytes", fail_source)
    t0 = time.time()
    assert not A.apply_one(c, home=home, cwd=f.parent, destination="sibling", now=t0).applied
    monkeypatch.setattr(A, "atomic_write_bytes", real)
    res = A.apply_one(c, home=home, cwd=f.parent, destination="sibling", now=t0 + 2 * 86400)
    assert res.applied, res.reason
    sib = A.sibling_path(f).read_text()
    assert sib.count("## History") == 1 and sib.count("- old line 7\n") == 1
    assert sib.count("<!-- moved from AGENTS.md on ") == 1


def test_e_different_history_on_a_later_day_is_appended(tmp_path):
    home = Path.home()
    f = _file(tmp_path / "proj" / "AGENTS.md")
    c, _ = A.find_candidate("AGENTS.md", home, f.parent)
    t0 = time.time()
    assert A.apply_one(c, home=home, cwd=f.parent, destination="sibling", now=t0).applied
    f.write_text(CUR + "## History\n" + "".join(f"- a newer old line {i}\n" for i in range(30)))
    _age(f)
    assert A.apply_one(c, home=home, cwd=f.parent, destination="sibling", now=t0 + 86400).applied
    sib = A.sibling_path(f).read_text()
    assert sib.count("<!-- moved from AGENTS.md on ") == 2 and "a newer old line" in sib


# ----------------------------------------------------------------------
# F: the hook never reaches the network path
# ----------------------------------------------------------------------

def test_f_a_capped_cache_is_never_local_however_small_the_estimate(tmp_path):
    """4abce3a answered True for a fresh capped cache under the cap: the real
    write (up to 2 MB of history per file) could then cross it and call the
    server from inside the hook."""
    from sibyl_memory_client._capcheck import CapGate, TierCache, TierCacheEntry
    cache = TierCache(tmp_path / "tier_cache.json")
    calls = []
    g = CapGate(account_id="acct", session_token="tok", db_size_fn=lambda: 1000,
                local_tier_hint="pro", cache=cache, cap_bytes=5000,
                check_fn=lambda *a, **k: calls.append(1) or {"ok": True, "tier": "free", "cap_bytes": 5000},
                entitlement_tier_fn=lambda: "pro")
    cache.store(TierCacheEntry(account_id="acct", tier="free", checked_at=time.time(), cap_bytes=5000))
    assert g.decides_locally(10) is False
    assert calls == []
    cache.store(TierCacheEntry(account_id="acct", tier="pro", checked_at=time.time(), cap_bytes=None))
    assert g.decides_locally(10) is True
    g.check(3 * 1024 * 1024)                         # any size: decided locally
    assert calls == []
    cache.store(TierCacheEntry(account_id="other", tier="pro", checked_at=time.time(), cap_bytes=None))
    assert g.decides_locally(10) is False
