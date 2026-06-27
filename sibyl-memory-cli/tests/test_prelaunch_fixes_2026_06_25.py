"""Pre-launch fix-pass regression tests (audit 2026-06-25).

One test per audit finding (CLI-1..CLI-16) proving the new, hardened behavior.
CLI-14 (bearer-in-browser-URL) is deferred — it needs a server-side one-time
exchange code, out of scope for this client-only pass — so it has no test here.

These exercise the crash-on-malformed-input cluster + durability/atomicity
hardening that the bounty submitter (D1/D2/D3) and the audit flagged.
"""
from __future__ import annotations

import json
import os
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import migrate as M


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _status_args(tmp_path, *, creds="credentials.json", db="memory.db",
                 tier_cache="tier_cache.json") -> Namespace:
    return Namespace(
        credentials=str(tmp_path / creds),
        db=str(tmp_path / db),
        tier_cache=str(tmp_path / tier_cache),
    )


# ----------------------------------------------------------------------
# CLI-1 — read_credentials() tolerates a corrupt credentials.json
# ----------------------------------------------------------------------

def test_cli1_read_credentials_corrupt_returns_none_and_warns(tmp_path, capsys):
    p = tmp_path / "credentials.json"
    p.write_text("{ this is not valid json", encoding="utf-8")
    out = cli.read_credentials(p)            # must NOT raise
    assert out is None
    msg = capsys.readouterr().out
    assert "corrupt" in msg.lower()
    assert "sibyl init --force" in msg


# ----------------------------------------------------------------------
# CLI-2 — write_credentials_atomic uses a unique mkstemp temp (no fixed .tmp)
# ----------------------------------------------------------------------

def test_cli2_write_credentials_atomic_no_fixed_tmp_and_0600(tmp_path):
    target = tmp_path / ".sibyl-memory" / "credentials.json"
    cli.write_credentials_atomic({"account_id": "a", "session_token": "s"}, path=target)
    assert json.loads(target.read_text())["account_id"] == "a"
    if hasattr(cli.os, "fchmod"):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # No leftover temp files, and specifically NOT the old fixed `.json.tmp` name.
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert not (target.parent / "credentials.json.tmp").exists()


def test_cli2_write_credentials_atomic_without_fchmod(tmp_path, monkeypatch):
    monkeypatch.delattr(cli.os, "fchmod", raising=False)
    target = tmp_path / ".sibyl-memory" / "credentials.json"

    cli.write_credentials_atomic({"account_id": "a", "session_token": "s"}, path=target)

    assert json.loads(target.read_text())["session_token"] == "s"
    assert list(target.parent.glob("*.tmp")) == []


def test_cli2_concurrent_writes_do_not_collide(tmp_path):
    # Unique-per-call mkstemp means two interleaved writes never fight over one
    # fixed temp name (the old unlink-then-O_EXCL race). Both must succeed.
    target = tmp_path / "credentials.json"
    for i in range(20):
        cli.write_credentials_atomic({"n": i}, path=target)
    assert json.loads(target.read_text())["n"] == 19
    assert list(tmp_path.glob("*.tmp")) == []


# ----------------------------------------------------------------------
# CLI-3 — non-SQLite DB is detected (status label + migrate abort)
# ----------------------------------------------------------------------

def test_cli3_is_sqlite_db_rejects_garbage_accepts_real(tmp_path):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(os.urandom(4096))
    assert cli.is_sqlite_db(garbage) is False
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert cli.is_sqlite_db(empty) is True      # 0-byte file is a valid empty DB
    from sibyl_memory_client import MemoryClient
    real = tmp_path / "real.db"
    MemoryClient.local(str(real), tenant_id="qa")
    assert cli.is_sqlite_db(real) is True


def test_cli3_status_labels_non_sqlite_db(tmp_path, capsys):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"account_id": "a"}), encoding="utf-8")
    db = tmp_path / "memory.db"
    db.write_bytes(os.urandom(2048))            # not a SQLite file
    rc = cli.cmd_status(_status_args(tmp_path))  # must NOT raise
    assert rc == 0
    out = capsys.readouterr().out
    assert "not a SQLite database" in out


def test_cli3_migrate_aborts_on_unreadable_db(tmp_path):
    # baseline distinguishes unreadable from 0
    db = tmp_path / "memory.db"; db.write_bytes(os.urandom(4096))
    assert M.db_baseline(db) == M.DB_UNREADABLE
    # orchestrator aborts before verify/debloat with originals intact
    home = tmp_path / "home"; (home / "myproj").mkdir(parents=True)
    cm = home / "myproj" / "CLAUDE.md"
    cm.write_text("# A\n## later\njunk\n", encoding="utf-8")
    before = cm.read_text()
    bad_db = home / ".sibyl-memory" / "memory.db"
    bad_db.parent.mkdir(parents=True)
    bad_db.write_bytes(os.urandom(4096))
    rep = M.run_guided_setup(home=home, cwd=home / "myproj", db_path=bad_db,
                             backup_parent=tmp_path / "bk", io=M.GuidedIO(scripted=["y"]),
                             wirers={}, extract_fn=lambda b, d: None)
    assert rep["ok"] is False
    assert rep["phases"]["verify"].get("unreadable") is True
    assert "debloat" not in rep["phases"]       # never reached the trim
    assert cm.read_text() == before             # original untouched


# ----------------------------------------------------------------------
# CLI-4 — corrupt tier_cache.json does not crash status
# ----------------------------------------------------------------------

def test_cli4_status_survives_corrupt_tier_cache(tmp_path, capsys):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"account_id": "a"}), encoding="utf-8")
    (tmp_path / "tier_cache.json").write_text("{ broken", encoding="utf-8")
    rc = cli.cmd_status(_status_args(tmp_path))  # must NOT raise
    assert rc == 0


# ----------------------------------------------------------------------
# CLI-5 — devices revoke: missing bearer_id bails; negative index rejected
# ----------------------------------------------------------------------

def _devices_args(tmp_path, idx):
    return Namespace(
        credentials=str(tmp_path / "credentials.json"),
        db=str(tmp_path / "memory.db"),
        tier_cache=str(tmp_path / "tier_cache.json"),
        sub="revoke", index=idx,
    )


def test_cli5_revoke_negative_index_rejected(tmp_path, capsys, monkeypatch):
    (tmp_path / "credentials.json").write_text(
        json.dumps({"account_id": "a", "session_token": "s"}), encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("must not hit the server for a negative index")
    monkeypatch.setattr(cli, "http_request", _boom)

    rc = cli.cmd_devices(_devices_args(tmp_path, -1))
    assert rc == 1
    assert "invalid index" in capsys.readouterr().out.lower()


def test_cli5_revoke_missing_bearer_id_bails_cleanly(tmp_path, capsys, monkeypatch):
    (tmp_path / "credentials.json").write_text(
        json.dumps({"account_id": "a", "session_token": "s"}), encoding="utf-8")
    # Server returns a device record with NO bearer_id (hostile/malformed).
    monkeypatch.setattr(cli, "http_request",
                        lambda *a, **k: {"devices": [{"device_label": "x"}]})
    rc = cli.cmd_devices(_devices_args(tmp_path, 0))   # must NOT raise KeyError
    assert rc == 2
    assert "bearer_id" in capsys.readouterr().out


# ----------------------------------------------------------------------
# CLI-6 — health expands ~ and wraps provider errors cleanly
# ----------------------------------------------------------------------

def test_cli6_health_wraps_provider_error(tmp_path, capsys, monkeypatch):
    import sibyl_memory_hermes

    class _BoomProvider:
        def __init__(self, *a, **k):
            raise RuntimeError("db open failed")

    monkeypatch.setattr(sibyl_memory_hermes, "SibylMemoryProvider", _BoomProvider)
    args = Namespace(db="~/nonexistent/memory.db")
    rc = cli.cmd_health(args)             # must NOT raise
    assert rc == 1
    assert "Health check failed" in capsys.readouterr().out


# ----------------------------------------------------------------------
# CLI-7 — memory list/recall tolerate SDK rows missing keys
# ----------------------------------------------------------------------

def test_cli7_memory_list_tolerates_missing_keys(tmp_path, capsys, monkeypatch):
    from sibyl_memory_client import MemoryClient

    db = tmp_path / "memory.db"
    MemoryClient.local(str(db), tenant_id="qa")

    class _FakeClient:
        @staticmethod
        def local(*a, **k):
            return _FakeClient()

        def list_entities(self, **k):
            return [{"status": "ok"}]          # no category/name keys

    monkeypatch.setattr("sibyl_memory_client.MemoryClient", _FakeClient)
    args = Namespace(db=str(db), mem_cmd="list", category=None, limit=50)
    rc = cli.cmd_memory(args)                   # must NOT raise KeyError
    assert rc == 0
    assert "?/?" in capsys.readouterr().out


# ----------------------------------------------------------------------
# CLI-13 — _ver_lt orders 1.2 == 1.2.0 and handles rc tags
# ----------------------------------------------------------------------

def test_cli13_version_compare_normalizes_and_handles_rc():
    assert cli._ver_lt("1.2", "1.2.0") is False     # equal, not "outdated"
    assert cli._ver_lt("1.2.0", "1.2") is False
    assert cli._ver_lt("1.2.0", "1.2.1") is True
    assert cli._ver_lt("0.3.16", "0.3.16") is False
    # rc precedes the final release
    assert cli._ver_lt("1.0.0rc1", "1.0.0") is True
    assert cli._ver_lt("1.0.0", "1.0.0rc1") is False


# ----------------------------------------------------------------------
# CLI-15 — init persists only allowlisted fields, rejects non-dict creds
# ----------------------------------------------------------------------

def test_cli15_init_allowlists_persisted_fields(tmp_path, monkeypatch):
    cred_path = tmp_path / "credentials.json"
    server_creds = {
        "account_id": "acct-1",
        "tier": "free",
        "wallet": "0xabc",
        "email": "u@example.com",
        "issued_at": "2026-06-25T00:00:00Z",
        "bearer_token": "bearer-xyz",
        # hostile / unexpected extras the server should not be able to plant:
        "is_admin": True,
        "__proto__": "x",
        "arbitrary": {"nested": "junk"},
    }
    monkeypatch.setattr(cli, "http_request",
                        lambda *a, **k: {"bound": True, "credentials": dict(server_creds)})
    # Disable the browser + banner side effects; loop runs once and binds.
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: None)
    args = Namespace(credentials=str(cred_path), force=True)
    rc = cli.cmd_init(args)
    assert rc == 0
    persisted = json.loads(cred_path.read_text())
    assert persisted["account_id"] == "acct-1"
    assert persisted["session_token"] == "bearer-xyz"     # bearer preferred
    assert "is_admin" not in persisted
    assert "__proto__" not in persisted
    assert "arbitrary" not in persisted


def test_cli15_init_rejects_non_dict_credentials(tmp_path, monkeypatch):
    cred_path = tmp_path / "credentials.json"
    monkeypatch.setattr(cli, "http_request",
                        lambda *a, **k: {"bound": True, "credentials": "not-a-dict"})
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: None)
    args = Namespace(credentials=str(cred_path), force=True)
    rc = cli.cmd_init(args)                 # must NOT raise
    assert rc == 2
    assert not cred_path.exists()           # nothing persisted from junk


# ----------------------------------------------------------------------
# CLI-16 — cap_bytes formatting tolerates non-int values
# ----------------------------------------------------------------------

def test_cli16_fmt_cap_bytes_defensive():
    assert cli._fmt_cap_bytes(None) == "unlimited"
    assert cli._fmt_cap_bytes(2_097_152) == "2,097,152"
    assert cli._fmt_cap_bytes("2097152") == "2,097,152"   # coerced
    assert cli._fmt_cap_bytes("lots") == "lots"           # uncoercible -> raw, no crash
    assert cli._fmt_cap_bytes(1048576.0) == "1,048,576"


# ----------------------------------------------------------------------
# CLI-8 — debloat: mkstemp/fsync/atomic + symlink refusal
# ----------------------------------------------------------------------

def test_cli8_debloat_refuses_symlink_target(tmp_path):
    real = tmp_path / "real.md"; real.write_text("# real\nsecret\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"; link.symlink_to(real)
    out = M.debloat_file(link, "lean", backup_exists=True)
    assert out["written"] is False
    assert "symlink" in out["error"]
    assert real.read_text() == "# real\nsecret\n"   # target through link untouched


def test_cli8_debloat_no_fixed_tmp_left(tmp_path):
    f = tmp_path / "CLAUDE.md"; f.write_text("# A\n## later\njunk\n" * 50, encoding="utf-8")
    lean = M.heuristic_lean(f.read_text())
    out = M.debloat_file(f, lean, backup_exists=True)
    assert out["written"] and f.read_text() == lean
    # No leftover temp of any shape, and not the old fixed `.sibyl-tmp` name.
    assert list(tmp_path.glob("*.sibyl-tmp")) == []
    assert not (tmp_path / "CLAUDE.md.sibyl-tmp").exists()


# ----------------------------------------------------------------------
# CLI-9 — backup durability + re-verify the specific backup before debloat
# ----------------------------------------------------------------------

def test_cli9_verify_backup_of_detects_missing_or_truncated(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    cm = home / "CLAUDE.md"; cm.write_text("# big\n" * 100, encoding="utf-8")
    files = M.scan_memory_files(home, cwd=home)
    bk = M.run_backup(files, tmp_path / "bk")
    assert bk.ok
    # backup matches now
    assert M.verify_backup_of(cm, bk.backup_dir, home=home, cwd=home) is True
    # truncate the backup copy -> re-verification must FAIL
    backup_copy = bk.backup_dir / "CLAUDE.md"
    backup_copy.write_text("x", encoding="utf-8")
    assert M.verify_backup_of(cm, bk.backup_dir, home=home, cwd=home) is False
    # delete the backup copy -> re-verification must FAIL
    backup_copy.unlink()
    assert M.verify_backup_of(cm, bk.backup_dir, home=home, cwd=home) is False


def test_cli9_orchestrator_skips_trim_when_backup_unverifiable(tmp_path):
    from sibyl_memory_client import MemoryClient

    home = tmp_path / "home"; (home / "proj").mkdir(parents=True)
    cm = home / "proj" / "CLAUDE.md"
    cm.write_text("# A\nidentity\n## later\njunk\njunk\n", encoding="utf-8")
    before = cm.read_text()
    db = home / ".sibyl-memory" / "memory.db"; db.parent.mkdir(parents=True)

    def fake_extract(backup_dir, db_path):
        c = MemoryClient.local(str(db_path), tenant_id="qa")
        c.set_entity("facts", "x", {"v": 1})

    # Sabotage the backup AFTER it is made but BEFORE the trim, by intercepting
    # confirm() to delete the backed-up CLAUDE.md just before debloat runs.
    class _SabotageIO(M.GuidedIO):
        def __init__(self, backup_holder):
            super().__init__(scripted=[])
            self._holder = backup_holder

        def confirm(self, q, *, default=True):
            # find + remove the backup copy of CLAUDE.md right before the trim
            for d in (self._holder["parent"]).glob("sibyl-migration-backup-*"):
                bc = d / "proj" / "CLAUDE.md"
                if bc.exists():
                    bc.unlink()
            return True   # say yes to trimming

    holder = {"parent": tmp_path / "bk"}
    rep = M.run_guided_setup(home=home, cwd=home / "proj", db_path=db,
                             backup_parent=tmp_path / "bk", io=_SabotageIO(holder),
                             wirers={}, extract_fn=fake_extract)
    # debloat must have refused because the backup could not be re-verified
    assert rep["phases"].get("debloat", {}).get("written") is False
    assert cm.read_text() == before               # original untouched


# ----------------------------------------------------------------------
# CLI-10 — declined Hermes overwrite installs nothing
# ----------------------------------------------------------------------

def test_cli10_declined_overwrite_installs_nothing(tmp_path, monkeypatch):
    from sibyl_memory_cli.setup import HermesWirer

    home = tmp_path / "hermes-home"; home.mkdir()
    (home / "config.yaml").write_text("memory:\n  provider: mem0\n", encoding="utf-8")

    installed = {"called": False}

    def _spy_install(self):
        installed["called"] = True
        (self.plugin_dir).mkdir(parents=True, exist_ok=True)
        (self.plugin_dir / "__init__.py").write_text("# stub\n")

    # monkeypatch (not raw assign/del) so the real _install_plugin is restored
    # cleanly after the test — a manual `del` would remove the method itself.
    monkeypatch.setattr(HermesWirer, "_install_plugin", _spy_install)
    w = HermesWirer(hermes_home=home)
    # prompt declines the overwrite
    outcome = w.wire(prompt_fn=lambda q, *, default: "n")
    assert outcome.status == "skipped"
    assert installed["called"] is False, "plugin must NOT be installed on a declined overwrite"
    assert not (home / "plugins" / "sibyl" / "__init__.py").exists()
    # config untouched
    assert "mem0" in (home / "config.yaml").read_text()


# ----------------------------------------------------------------------
# CLI-11 — pip install respects PEP 668 / pipx (no silent mutation) + surfaces
# ----------------------------------------------------------------------

def test_cli11_install_helper_respects_pep668(monkeypatch):
    from sibyl_memory_cli import setup as S
    import sibyl_memory_cli.cli as _cli

    calls = {"pip": 0}
    monkeypatch.setattr(_cli, "_detect_install_method", lambda: "pep668")
    monkeypatch.setattr(S, "_run", lambda *a, **k: calls.__setitem__("pip", calls["pip"] + 1) or (0, "", ""))
    msg = S._install_pkg_or_instruct("sibyl-memory-mcp")
    assert calls["pip"] == 0, "must NOT pip install into an externally-managed env"
    assert msg and "externally-managed" in msg
    assert "break-system-packages" in msg


def test_cli11_install_helper_surfaces_pip_failure(monkeypatch):
    from sibyl_memory_cli import setup as S
    import sibyl_memory_cli.cli as _cli

    monkeypatch.setattr(_cli, "_detect_install_method", lambda: "venv")
    monkeypatch.setattr(S, "_run",
                        lambda *a, **k: (1, "", "ERROR: could not find a version"))
    msg = S._install_pkg_or_instruct("sibyl-memory-mcp")
    assert msg and "failed" in msg
    assert "could not find a version" in msg


# ----------------------------------------------------------------------
# CLI-12 — verify_mcp_starts does not flag a slow-but-alive import as crashed
# ----------------------------------------------------------------------

def test_cli12_verify_does_not_crash_label_slow_import(monkeypatch):
    from sibyl_memory_cli.setup import ClaudeCodeWirer

    class _SlowAliveProc:
        """Simulates a stdio server that is still importing (alive, not exited)
        for the whole verification window, then blocks on stdin."""
        def __init__(self):
            self._polls = 0

        def poll(self):
            self._polls += 1
            return None              # never exits during the window -> healthy

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("sibyl_memory_cli.setup.shutil.which",
                        lambda name: "/usr/bin/sibyl-memory-mcp")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _SlowAliveProc())
    ok, msg = ClaudeCodeWirer().verify_mcp_starts()
    assert ok is True
    assert "verified" in msg.lower()


def test_cli12_verify_flags_quick_crash(monkeypatch):
    from sibyl_memory_cli.setup import ClaudeCodeWirer

    class _CrashProc:
        def __init__(self):
            import io
            self.stderr = io.BytesIO(b"ImportError: boom\n")

        def poll(self):
            return 1                 # exited non-zero immediately -> crash

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr("sibyl_memory_cli.setup.shutil.which",
                        lambda name: "/usr/bin/sibyl-memory-mcp")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _CrashProc())
    ok, msg = ClaudeCodeWirer().verify_mcp_starts()
    assert ok is False
    assert "crashed" in msg.lower()
    assert "ImportError" in msg
