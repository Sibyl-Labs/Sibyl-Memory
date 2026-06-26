"""Pre-launch hardening regressions for install_plugin (2026-06-25 audit).

MH-7: uninstall must also resolve + remove the 0.7+ provider-path copy
      (_memory_provider_dest), with the same symlink + _looks_like_sibyl_install
      guards, and report both paths. Before the fix, uninstall removed only the
      user-plugin path and left the provider-path adapter loading.
MH-8: _write_payload must stage the payload in a sibling temp dir and atomically
      os.replace it onto dest, so an interrupt mid-write can't leave a
      half-written plugin directory.
"""
from __future__ import annotations

import os
from pathlib import Path

from sibyl_memory_hermes import install_plugin as ip


def _sibyl_dir(parent: Path) -> Path:
    """Make a directory that looks like a prior Sibyl install (SEC-5 sentinel)."""
    d = parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text("name: sibyl\nversion: test\n")
    (d / "__init__.py").write_text("# sibyl adapter\n")
    return d


# ----------------------------------------------------------------------
# MH-7: uninstall removes BOTH paths
# ----------------------------------------------------------------------
def test_uninstall_removes_both_paths(tmp_path):
    hermes_home = tmp_path / ".hermes"
    user_path = hermes_home / "plugins" / "sibyl"
    mem_dir = tmp_path / "pkg" / "plugins" / "memory"
    provider_path = mem_dir / "sibyl"
    _sibyl_dir(user_path)
    _sibyl_dir(provider_path)

    rc = ip.uninstall(hermes_home, dry_run=False, memory_provider_path=str(mem_dir))
    assert rc == 0
    assert not user_path.exists(), "user-plugin path not removed"
    assert not provider_path.exists(), "provider-path copy not removed (MH-7)"


def test_uninstall_provider_path_honors_symlink_guard(tmp_path):
    hermes_home = tmp_path / ".hermes"
    user_path = hermes_home / "plugins" / "sibyl"
    mem_dir = tmp_path / "pkg" / "plugins" / "memory"
    mem_dir.mkdir(parents=True)
    _sibyl_dir(user_path)
    # provider path is a symlink -> must be refused, not rmtree'd through.
    real = tmp_path / "real_target"
    _sibyl_dir(real)
    (mem_dir / "sibyl").symlink_to(real, target_is_directory=True)

    rc = ip.uninstall(hermes_home, dry_run=False, memory_provider_path=str(mem_dir))
    # User path removed; provider symlink refused (rc surfaces the refusal).
    assert not user_path.exists()
    assert (mem_dir / "sibyl").is_symlink()  # untouched
    assert real.exists()                     # never followed
    assert rc == 3


def test_uninstall_provider_path_refuses_non_sibyl(tmp_path):
    hermes_home = tmp_path / ".hermes"
    user_path = hermes_home / "plugins" / "sibyl"
    mem_dir = tmp_path / "pkg" / "plugins" / "memory"
    provider_path = mem_dir / "sibyl"
    _sibyl_dir(user_path)
    # provider path exists but is NOT a Sibyl install -> refuse, don't destroy.
    provider_path.mkdir(parents=True)
    (provider_path / "important.txt").write_text("not ours")

    rc = ip.uninstall(hermes_home, dry_run=False, memory_provider_path=str(mem_dir))
    assert not user_path.exists()
    assert provider_path.exists()                       # refused, preserved
    assert (provider_path / "important.txt").exists()
    assert rc == 4


def test_uninstall_dry_run_removes_nothing(tmp_path):
    hermes_home = tmp_path / ".hermes"
    user_path = hermes_home / "plugins" / "sibyl"
    mem_dir = tmp_path / "pkg" / "plugins" / "memory"
    provider_path = mem_dir / "sibyl"
    _sibyl_dir(user_path)
    _sibyl_dir(provider_path)

    rc = ip.uninstall(hermes_home, dry_run=True, memory_provider_path=str(mem_dir))
    assert rc == 0
    assert user_path.exists()
    assert provider_path.exists()


def test_uninstall_nothing_to_remove(tmp_path, capsys):
    hermes_home = tmp_path / ".hermes"
    mem_dir = tmp_path / "pkg" / "plugins" / "memory"
    mem_dir.mkdir(parents=True)
    rc = ip.uninstall(hermes_home, dry_run=False, memory_provider_path=str(mem_dir))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing was removed" in out


# ----------------------------------------------------------------------
# MH-8: atomic write (no half-written plugin)
# ----------------------------------------------------------------------
def test_write_payload_is_atomic_no_partial_dir(tmp_path, monkeypatch):
    """If writing a payload file fails mid-way, dest must NOT exist as a
    half-written plugin directory — the work happened in a sibling temp dir."""
    dest = tmp_path / "plugins" / "sibyl"

    real_read = ip._read_payload
    calls = {"n": 0}

    def flaky_read(filename):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated interrupt mid-write")
        return real_read(filename)

    monkeypatch.setattr(ip, "_read_payload", flaky_read)

    raised = False
    try:
        ip._write_payload(dest, force=False, dry_run=False)
    except OSError:
        raised = True

    assert raised, "the simulated failure should propagate"
    assert not dest.exists(), "dest must not be left as a half-written plugin dir (MH-8)"
    # No stray staging dirs left behind in the parent.
    leftovers = [p for p in (tmp_path / "plugins").iterdir()
                 if p.name.startswith(".sibyl-plugin-")]
    assert leftovers == [], f"staging temp dir not cleaned up: {leftovers}"


def test_write_payload_success_writes_complete_dir(tmp_path):
    dest = tmp_path / "plugins" / "sibyl"
    rc = ip._write_payload(dest, force=False, dry_run=False)
    assert rc == 0
    assert (dest / "__init__.py").exists()
    assert (dest / "plugin.yaml").exists()
    # Bytes match the bundled payload exactly (atomic replace preserved content).
    assert (dest / "plugin.yaml").read_bytes() == ip._read_payload("plugin.yaml")


def test_write_payload_force_overwrites_prior_sibyl(tmp_path):
    dest = _sibyl_dir(tmp_path / "plugins" / "sibyl")
    # Add a stale file that the atomic replace should drop.
    (dest / "stale.txt").write_text("old")
    rc = ip._write_payload(dest, force=True, dry_run=False)
    assert rc == 0
    assert (dest / "__init__.py").exists()
    assert not (dest / "stale.txt").exists(), "atomic replace must not leave stale files"


def test_write_payload_dry_run_writes_nothing(tmp_path):
    dest = tmp_path / "plugins" / "sibyl"
    rc = ip._write_payload(dest, force=False, dry_run=True)
    assert rc == 0
    assert not dest.exists()
