"""`sibyl doctor` harness checks and repair (spec dsh-doctor v2, section 5).

A stub stdio MCP server stands in for the real one (as in
test_doctor_2026_10_28.py). Temp HOME and an isolated PATH only: the harness
binaries (claude, codex, cursor, hermes, dsh) are hidden whatever is installed,
and no test runs a real claude, DeepSeek Harness or dsh."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import doctor as D
from sibyl_memory_cli import setup as S

from _harness_fixtures import _REAL_WHICH, backups_dir, isolate_path, leftovers
from conftest import assert_convention
from test_doctor_2026_10_28 import STUB

FIX = Path(__file__).resolve().parent / "fixtures" / "deepseek"


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB)
    monkeypatch.setattr(D, "_server_cmd", lambda: [sys.executable, str(stub)])
    monkeypatch.setenv("STUB_MODE", "ok")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(S.HermesWirer, "_install_plugin", _stub_plugin)
    return fake


def _stub_plugin(self):
    print("=== Sibyl plugin installer banner (stub) ===")
    d = Path(self.hermes_home) / "plugins" / "sibyl"
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("# stub\n")


H = Path.home


def _ns(tmp_path, *, json_out=False, fix=False, harness=None, no_harness=False, bare=False):
    db = tmp_path / "store" / "memory.db"
    db.parent.mkdir(exist_ok=True)
    base = dict(db=str(db), credentials=str(tmp_path / "c.json"), json=json_out)
    if not bare:
        base.update(fix=fix, harness=harness, no_harness=no_harness)
    return argparse.Namespace(**base)


def run(tmp_path, capsys, **kw):
    rc = D.run_doctor(_ns(tmp_path, **kw))
    cap = capsys.readouterr()
    if kw.get("json_out"):
        return rc, json.loads(cap.out), cap.out
    return rc, None, cap.out


def by_name(doc, name):
    return [h for h in doc["harnesses"] if h["name"] == name]


# --- harness fixtures in the temp home ---------------------------------------

def cursor_cfg(command, **extra):
    p = H() / ".cursor" / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"type": "stdio", "command": command}
    entry.update(extra)
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": entry, "other": {"command": "npx"}}}))
    return p


def codex_cfg(text):
    p = H() / ".codex" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def dsh_put(layer, fixture=None, text=None):
    home = H() / ".dsh"
    if layer == "home":
        p = home / "cordis.patch.yml"
    else:
        p = home / "profiles" / layer / "cordis.patch.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    p.write_text(text if text is not None else (FIX / fixture).read_text())
    return p


def hermes_cfg(text):
    h = Path(os.environ["HERMES_HOME"])
    h.mkdir(parents=True, exist_ok=True)
    p = h / "config.yaml"
    if text is not None:
        p.write_text(text)
    return p


STALE = "/old/venv/bin/sibyl-memory-mcp"


# --- shape and defaults ---------------------------------------------------------

def test_no_harness_detected_exit_0_and_json_shape(tmp_path, capsys):
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    assert rc == 0
    assert doc["harnesses"] == [] and doc["ok"] is True and doc["server_ok"] is True
    assert len(doc["steps"]) == 6
    for k in ("fix", "legacy_backups", "next", "human_action", "later"):
        assert k in doc
    assert doc["next"] == "done" and doc["later"] == []


def test_bare_namespace_still_works(tmp_path, capsys, _isolated_path):
    cursor_cfg(str(_isolated_path))
    rc = D.run_doctor(_ns(tmp_path, json_out=True, bare=True))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and by_name(doc, "cursor")[0]["status"] == "ok"


def test_existing_doctor_json_test_unaffected_by_claude_on_path(tmp_path, capsys, monkeypatch):
    marker = tmp_path / "claude-was-run"
    fake_claude = tmp_path / "isolated-bin" / "claude"
    fake_claude.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    fake_claude.chmod(0o755)
    monkeypatch.setattr(shutil, "which", _REAL_WHICH)   # claude is now on PATH
    ns = argparse.Namespace(db=str(tmp_path / "m" / "memory.db"),
                            credentials=str(tmp_path / "c.json"), json=True)
    (tmp_path / "m").mkdir()
    assert D.run_doctor(ns) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["steps"]) == 6
    assert not marker.exists()


def test_no_harness_flag_skips_checks(tmp_path, capsys):
    cursor_cfg(STALE)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, no_harness=True)
    assert rc == 0 and doc["harnesses"] == [] and doc["legacy_backups"]["count"] == 0


def test_harness_filter_limits_checks_and_repairs(tmp_path, capsys, _isolated_path):
    c = cursor_cfg(STALE)
    x = codex_cfg(f'[mcp_servers.sibyl_memory]\ncommand = "{STALE}"\n')
    before = x.read_text()
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True, harness=["cursor"])
    assert [h["name"] for h in doc["harnesses"]] == ["cursor"]
    assert doc["fix"]["fixed"] == ["cursor"]
    assert json.loads(c.read_text())["mcpServers"]["sibyl-memory"]["command"] == str(_isolated_path)
    assert x.read_text() == before
    rc, doc, _ = run(tmp_path, capsys, json_out=True, harness=["hermes"])
    assert doc["harnesses"][0]["detail"] == "not detected" and doc["harnesses"][0]["level"] == "pass"


def test_harness_alias_dsh_accepted(tmp_path, capsys, _isolated_path):
    ns = cli.build_parser().parse_args(["doctor", "--harness", "dsh", "--json"])
    assert ns.harness == ["dsh"]
    dsh_put("home", text=(FIX / "ours_stale.yml").read_text().replace(STALE, str(_isolated_path)))
    cursor_cfg(STALE)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, harness=["dsh"])
    assert [h["name"] for h in doc["harnesses"]] == ["deepseek"] and rc == 0


# --- Cursor -------------------------------------------------------------------------

def test_cursor_ok(tmp_path, capsys, _isolated_path):
    cursor_cfg(str(_isolated_path))
    rc, _d, out = run(tmp_path, capsys)
    assert rc == 0 and "PASS  Cursor" in out and "Doctor: all steps passed." in out


def test_cursor_missing_is_warning_exit_0_with_setup_hint(tmp_path, capsys):
    (H() / ".cursor").mkdir()
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    c = by_name(doc, "cursor")[0]
    assert rc == 0 and c["status"] == "missing" and c["level"] == "warn"
    assert c["hint"] == "sibyl setup cursor" and doc["later"] == ["sibyl setup cursor"]
    rc, _d, out = run(tmp_path, capsys)
    assert "Doctor: passed with warnings." in out


def test_cursor_stale_path_fails_exit_1(tmp_path, capsys):
    cursor_cfg(STALE)
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    c = by_name(doc, "cursor")[0]
    assert rc == 1 and doc["ok"] is False and doc["server_ok"] is True
    assert c["status"] == "stale" and c["fixable"] is True and doc["next"] == "sibyl doctor --fix"


def test_cursor_stale_fixed_with_fix_flag_keeps_env_and_args(tmp_path, capsys, _isolated_path):
    p = cursor_cfg(STALE, env={"SIBYL_MEMORY_DB": "/data/m.db"}, args=["--x"])
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and doc["ok"] is True and doc["fix"]["fixed"] == ["cursor"]
    entry = json.loads(p.read_text())["mcpServers"]["sibyl-memory"]
    assert entry == {"type": "stdio", "command": str(_isolated_path),
                     "env": {"SIBYL_MEMORY_DB": "/data/m.db"}, "args": ["--x"]}
    assert leftovers(p.parent) == []


def test_cursor_broken_binary_not_fixable_when_same_path(tmp_path, capsys, monkeypatch, _isolated_path):
    p = cursor_cfg(str(_isolated_path))
    before = p.read_text()
    monkeypatch.setattr(S, "_verify_mcp_starts",
                        lambda b: (False, "crash") if b == str(_isolated_path) else (True, "ok"))
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "cursor")[0]
    assert rc == 1 and c["status"] == "broken" and c["fixable"] is False
    assert p.read_text() == before and doc["fix"]["attempted"] == []


def test_cursor_foreign_never_touched_by_fix(tmp_path, capsys):
    p = H() / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "/opt/other"}}}))
    before = p.read_text()
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and by_name(doc, "cursor")[0]["status"] == "foreign"
    assert p.read_text() == before


def test_cursor_corrupt_json_unreadable_never_touched_and_not_echoed(tmp_path, capsys):
    p = H() / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text('{"mcpServers": {"x": "SECRETJSON123"')
    rc, doc, raw = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "cursor")[0]
    assert rc == 0 and c["status"] == "unreadable" and "line 1" in c["detail"]
    assert "SECRETJSON123" not in raw
    _rc, _d, out = run(tmp_path, capsys)
    assert "SECRETJSON123" not in out
    assert p.read_text() == '{"mcpServers": {"x": "SECRETJSON123"'


# --- DeepSeek Harness ----------------------------------------------------------------

def test_deepseek_missing_not_fixed_by_fix_flag(tmp_path, capsys):
    (H() / ".dsh" / "profiles" / "default").mkdir(parents=True)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    d = by_name(doc, "deepseek")[0]
    assert rc == 0 and d["status"] == "missing" and d["level"] == "warn"
    assert "sibyl setup deepseek" in doc["later"] and doc["next"] == "done"
    assert not (H() / ".dsh" / "cordis.patch.yml").exists()
    assert list((H() / ".dsh" / "profiles" / "default").iterdir()) == []


def test_deepseek_stale_fixed_with_fix_flag_keeps_user_env(tmp_path, capsys, _isolated_path):
    p = dsh_put("home", "ours_stale_custom.yml")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and doc["fix"]["fixed"] == ["deepseek:home"]
    cfg = S._dsh_yaml_load(p.read_text())[0]["insert"][0]["config"]
    assert cfg["command"] == str(_isolated_path)
    assert cfg["env"] == {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "9000"} and cfg["startupTimeoutMs"] == 9000
    assert "# moved when I rebuilt the venv" in p.read_text()


def _ok_row():
    return (FIX / "ours_stale.yml").read_text().replace(STALE, str(Path(os.environ["PATH"].split(":")[0])
                                                              / "sibyl-memory-mcp"))


def test_deepseek_two_profiles_two_checks_no_duplicate(tmp_path, capsys):
    dsh_put("web", text=_ok_row())
    dsh_put("tui", text=_ok_row())
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    ds = by_name(doc, "deepseek")
    assert rc == 0 and [(d["location"], d["status"]) for d in ds] == [
        ("profile:tui", "ok"), ("profile:web", "ok")]


def test_deepseek_repairs_stale_row_in_each_profile(tmp_path, capsys, _isolated_path):
    pw = dsh_put("web", "ours_stale.yml")
    pt = dsh_put("tui", "ours_stale.yml")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and sorted(doc["fix"]["fixed"]) == ["deepseek:profile:tui", "deepseek:profile:web"]
    for p in (pw, pt):
        assert str(_isolated_path) in p.read_text()
    assert len(list(backups_dir("deepseek", "profile-web").iterdir())) == 1
    assert len(list(backups_dir("deepseek", "profile-tui").iterdir())) == 1


def test_deepseek_home_plus_profile_reported_duplicate_not_fixed(tmp_path, capsys):
    ph = dsh_put("home", "ours_stale.yml")
    pw = dsh_put("web", "ours_stale.yml")
    before = (ph.read_text(), pw.read_text())
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    ds = by_name(doc, "deepseek")
    assert {d["status"] for d in ds} == {"duplicate"} and len(ds) == 2
    assert all(d["fixable"] is False for d in ds) and doc["fix"]["attempted"] == []
    assert (ph.read_text(), pw.read_text()) == before


def test_deepseek_broken_yaml_reported_unreadable_secret_not_echoed(tmp_path, capsys):
    dsh_put("home", "broken_with_fake_value.yml")
    rc, doc, raw = run(tmp_path, capsys, json_out=True, fix=True)
    d = by_name(doc, "deepseek")[0]
    assert d["status"] == "unreadable" and "line 3" in d["detail"]
    assert "FAKESECRET123" not in raw and "GITHUB_TOKEN" not in raw
    _rc, _d, out = run(tmp_path, capsys)
    assert "FAKESECRET123" not in out


def test_deepseek_disabled_and_overridden_reported_no_write(tmp_path, capsys):
    pw = dsh_put("web", "ours_disabled.yml")
    pt = dsh_put("tui", "ours_overridden.yml")
    before = (pw.read_text(), pt.read_text())
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    st = {d["location"]: d["status"] for d in by_name(doc, "deepseek")}
    assert st == {"profile:web": "disabled", "profile:tui": "overridden"}
    assert rc == 0 and (pw.read_text(), pt.read_text()) == before


def test_deepseek_debian_dsh_home_not_checked(tmp_path, capsys):
    shutil.copytree(FIX / "debian_dsh_home", H() / ".dsh")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert by_name(doc, "deepseek") == []
    assert sorted(p.name for p in (H() / ".dsh").iterdir()) == ["dsh.conf", "group", "machines.list"]


# --- Codex ---------------------------------------------------------------------------

def test_codex_stale_fixed_with_fix_flag(tmp_path, capsys, _isolated_path):
    p = codex_cfg(f'model = "o4"\n[mcp_servers.sibyl_memory]\ncommand = "{STALE}"  # old\n')
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and doc["fix"]["fixed"] == ["codex"]
    assert p.read_text() == f'model = "o4"\n[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"  # old\n'


def test_codex_table_without_command_is_broken_and_fixed(tmp_path, capsys, _isolated_path):
    p = codex_cfg("[mcp_servers.sibyl_memory]\n")
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    assert by_name(doc, "codex")[0]["status"] == "broken" and rc == 1
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and f'command = "{_isolated_path}"' in p.read_text()


def test_codex_inline_table_unreadable_no_write(tmp_path, capsys):
    text = 'mcp_servers.sibyl_memory = { command = "/x/sibyl-memory-mcp" }\n'
    p = codex_cfg(text)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert by_name(doc, "codex")[0]["status"] == "unreadable" and p.read_text() == text


# --- Hermes --------------------------------------------------------------------------

def test_hermes_other_provider_is_foreign_not_fixed(tmp_path, capsys):
    p = hermes_cfg("memory:\n  provider: mem0\n")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    h = by_name(doc, "hermes")[0]
    assert h["status"] == "foreign" and "mem0" in h["detail"] and rc == 0
    assert p.read_text() == "memory:\n  provider: mem0\n"


def test_hermes_missing_provider_not_fixed_by_fix_flag(tmp_path, capsys):
    p = hermes_cfg("model: x\n")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert by_name(doc, "hermes")[0]["status"] == "missing" and rc == 0
    assert p.read_text() == "model: x\n"


def test_hermes_plugin_missing_is_broken_and_repair_leaves_config_byte_identical(tmp_path, capsys):
    p = hermes_cfg("memory:\n  provider: sibyl\n# my comment\n")
    before = p.read_bytes()
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    assert by_name(doc, "hermes")[0]["status"] == "broken" and rc == 1
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and doc["fix"]["fixed"] == ["hermes"]
    assert p.read_bytes() == before
    assert not backups_dir("hermes").exists()


# --- Claude Code -------------------------------------------------------------------------

class FakeClaude:
    def __init__(self, monkeypatch, *, registered=True, add_rc=0):
        self.calls = []
        self.registered = registered
        self.add_rc = add_rc
        monkeypatch.setattr(S.ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: "/fake/claude"))
        monkeypatch.setattr(S, "_run", self.run)
        (H() / ".claude").mkdir(exist_ok=True)
        (H() / ".claude" / "settings.json").write_text("{}")

    def run(self, cmd, timeout=20.0):
        self.calls.append(list(cmd))
        assert cmd[0] == "claude"
        if cmd[1:3] == ["mcp", "get"]:
            return (0 if self.registered else 1), "", ""
        if cmd[1:3] == ["mcp", "remove"]:
            self.registered = False
            _claude_json(None)
            return 0, "", ""
        if cmd[1:3] == ["mcp", "add"]:
            if self.add_rc:
                return self.add_rc, "", "add failed"
            self.registered = True
            _claude_json({"type": "stdio", "command": cmd[-1], "args": [], "env": {}})
            return 0, "", ""
        return 1, "", "unknown"


def _claude_json(entry, **extra):
    p = H() / ".claude.json"
    data = {"oauthAccount": {"emailAddress": "FAKEACCOUNT@example.invalid"}, "numStartups": 3}
    if entry is not None:
        data["mcpServers"] = {"sibyl-memory": entry}
    data.update(extra)
    p.write_text(json.dumps(data))
    return p


def test_claude_no_cli_is_unverifiable_exit_0_even_with_fix(tmp_path, capsys):
    (H() / ".claude").mkdir()
    (H() / ".claude" / "settings.json").write_text("{}")
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "claude-code")[0]
    assert rc == 0 and c["status"] == "unverifiable" and c["fixable"] is False


def test_claude_cli_registered_ok_without_command(tmp_path, capsys, monkeypatch):
    FakeClaude(monkeypatch)
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    c = by_name(doc, "claude-code")[0]
    assert rc == 0 and c["status"] == "ok" and c["command"] is None and "outside user scope" in c["detail"]


def test_claude_cli_not_registered_missing(tmp_path, capsys, monkeypatch):
    FakeClaude(monkeypatch, registered=False)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert by_name(doc, "claude-code")[0]["status"] == "missing" and rc == 0
    assert "sibyl setup claude-code" in doc["later"]


def test_claude_stale_plain_entry_repaired_by_remove_and_add(tmp_path, capsys, monkeypatch, _isolated_path):
    fc = FakeClaude(monkeypatch)
    _claude_json({"type": "stdio", "command": STALE, "args": [], "env": {}})
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and doc["fix"]["fixed"] == ["claude-code"]
    assert ["claude", "mcp", "remove", "-s", "user", "sibyl-memory"] in fc.calls
    assert ["claude", "mcp", "add", "--scope", "user", "sibyl-memory", "--", str(_isolated_path)] in fc.calls


def test_claude_stale_customized_entry_not_fixable_shows_manual_command(tmp_path, capsys, monkeypatch):
    fc = FakeClaude(monkeypatch)
    _claude_json({"type": "stdio", "command": STALE, "args": [], "env": {"SIBYL_MEMORY_DB": "/d"}})
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "claude-code")[0]
    assert rc == 1 and c["status"] == "stale" and c["fixable"] is False
    assert "claude mcp add --scope user sibyl-memory --" in c["detail"]
    assert not any(call[1:3] in (["mcp", "remove"], ["mcp", "add"]) for call in fc.calls)
    assert doc["next"] == "done" and "needs fixing by hand" in doc["human_action"]


def test_claude_failed_readd_reported_not_fixed_with_manual_command(tmp_path, capsys, monkeypatch, _isolated_path):
    FakeClaude(monkeypatch, add_rc=1)
    _claude_json({"type": "stdio", "command": STALE})
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "claude-code")[0]
    assert rc == 1 and c["fixed"] is False and doc["fix"]["fixed"] == []
    assert doc["next"] == f"claude mcp add --scope user sibyl-memory -- {_isolated_path}"


def test_claude_json_never_echoed(tmp_path, capsys, monkeypatch):
    FakeClaude(monkeypatch)
    _claude_json({"type": "stdio", "command": STALE})
    _rc, _d, raw = run(tmp_path, capsys, json_out=True)
    _rc, _d, out = run(tmp_path, capsys)
    for text in (raw, out):
        assert "FAKEACCOUNT" not in text and "numStartups" not in text


# --- repair semantics -----------------------------------------------------------------------

def test_default_is_read_only_without_tty(tmp_path, capsys):
    p = cursor_cfg(STALE)
    before = p.read_text()
    rc, _d, out = run(tmp_path, capsys)
    assert rc == 1 and p.read_text() == before
    assert out.rstrip().endswith("next: sibyl doctor --fix")


def _tty(monkeypatch, answers):
    asked = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    def prompt(q, default="Y"):
        asked.append((q, default))
        return answers(q)

    monkeypatch.setattr(S, "_interactive_prompt", prompt)
    return asked


def test_tty_prompt_default_no_changes_nothing(tmp_path, capsys, monkeypatch):
    p = cursor_cfg(STALE)
    before = p.read_text()
    asked = _tty(monkeypatch, lambda q: "n")
    rc, _d, out = run(tmp_path, capsys)
    assert asked and asked[0][1] == "N" and "Repair Cursor?" in asked[0][0]
    assert "a backup is kept" in asked[0][0]
    assert rc == 1 and p.read_text() == before
    assert out.rstrip().endswith("next: sibyl doctor --fix")


def test_tty_prompt_yes_repairs_only_that_harness(tmp_path, capsys, monkeypatch, _isolated_path):
    c = cursor_cfg(STALE)
    x = codex_cfg(f'[mcp_servers.sibyl_memory]\ncommand = "{STALE}"\n')
    before = x.read_text()
    asked = _tty(monkeypatch, lambda q: "y" if "Cursor" in q else "n")
    rc, _d, out = run(tmp_path, capsys)
    assert len(asked) == 2
    assert json.loads(c.read_text())["mcpServers"]["sibyl-memory"]["command"] == str(_isolated_path)
    assert x.read_text() == before
    assert "FIXED  Cursor" in out and rc == 1


def test_json_never_prompts(tmp_path, capsys, monkeypatch):
    cursor_cfg(STALE)

    def boom(*a, **k):
        raise AssertionError("prompted under --json")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(S, "_interactive_prompt", boom)
    monkeypatch.setattr("builtins.input", boom)
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    assert rc == 1 and doc["fix"]["attempted"] == []


def test_json_fix_stdout_is_one_valid_json_document_with_hermes_repair(tmp_path, capsys):
    hermes_cfg("memory:\n  provider: sibyl\n")
    cursor_cfg(STALE)
    rc = D.run_doctor(_ns(tmp_path, json_out=True, fix=True))
    raw = capsys.readouterr().out
    doc = json.loads(raw)
    assert "banner" not in raw
    assert sorted(doc["fix"]["fixed"]) == ["cursor", "hermes"] and rc == 0


def _missing_setup(name, monkeypatch):
    if name == "hermes":
        Path(os.environ["HERMES_HOME"]).mkdir(parents=True)
    elif name == "claude-code":
        FakeClaude(monkeypatch, registered=False)
    elif name == "codex":
        codex_cfg('model = "o4"\n')
    elif name == "cursor":
        (H() / ".cursor").mkdir()
    elif name == "deepseek":
        (H() / ".dsh" / "profiles").mkdir(parents=True)


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*")) if ".sibyl-memory" not in p.parts}


@pytest.mark.parametrize("name", ["hermes", "claude-code", "codex", "cursor", "deepseek"])
def test_fix_never_wires_missing_harness(name, tmp_path, capsys, monkeypatch):
    _missing_setup(name, monkeypatch)
    before = _tree(H())
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert by_name(doc, name)[0]["status"] == "missing" and rc == 0
    assert doc["fix"]["attempted"] == []
    assert _tree(H()) == before


def test_repair_never_calls_wire_or_lifecycle(tmp_path, capsys, monkeypatch, _isolated_path):
    cursor_cfg(STALE)
    codex_cfg(f'[mcp_servers.sibyl_memory]\ncommand = "{STALE}"\n')
    dsh_put("home", "ours_stale.yml")
    hermes_cfg("memory:\n  provider: sibyl\n")

    def boom(*a, **k):
        raise AssertionError("repair called a wiring path")

    for cls in (S.CursorWirer, S.CodexWirer, S.DeepSeekWirer, S.HermesWirer, S.ClaudeCodeWirer):
        monkeypatch.setattr(cls, "wire", boom)
        if hasattr(cls, "wire_lifecycle"):
            monkeypatch.setattr(cls, "wire_lifecycle", boom)
    monkeypatch.setattr(S.ClaudeCodeWirer, "wire_hooks", boom)
    monkeypatch.setattr(S, "install_shims", boom)
    monkeypatch.setattr(S, "install_canonical_skills", boom)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    assert rc == 0 and sorted(doc["fix"]["fixed"]) == ["codex", "cursor", "deepseek:home", "hermes"]


def test_no_installed_server_means_nothing_fixable(tmp_path, capsys, monkeypatch):
    p = cursor_cfg(STALE)
    before = p.read_text()
    monkeypatch.setattr(S, "resolve_mcp_binary", lambda: None)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "cursor")[0]
    assert c["status"] == "stale" and c["fixable"] is False and p.read_text() == before
    assert doc["next"] == "done" and "needs fixing by hand" in doc["human_action"]


def test_different_install_is_ok_with_note(tmp_path, capsys):
    other = tmp_path / "other" / "bin" / "sibyl-memory-mcp"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    cursor_cfg(str(other))
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    c = by_name(doc, "cursor")[0]
    assert rc == 0 and c["status"] == "ok" and "uses a different install than this CLI" in c["detail"]


def test_fix_backups_land_under_sibyl_home(tmp_path, capsys):
    p = cursor_cfg(STALE)
    rc, doc, _ = run(tmp_path, capsys, json_out=True, fix=True)
    c = by_name(doc, "cursor")[0]
    assert "backup at " + str(backups_dir("cursor")) in c["fix_message"]
    assert leftovers(p.parent) == []


# --- reporting ------------------------------------------------------------------------------

def test_legacy_bak_files_reported_info_exit_unaffected(tmp_path, capsys, _isolated_path):
    cursor_cfg(str(_isolated_path))
    codex_cfg(f'[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"\n')
    a = H() / ".cursor" / "mcp.json.20261028T101500Z.bak"
    b = H() / ".codex" / "config.toml.20260914T080000Z.bak"
    fixed_name = hermes_cfg("memory:\n  provider: sibyl\n").parent / "config.yaml.bak"
    _stub_plugin(S.HermesWirer())
    capsys.readouterr()                  # drop the stub installer's banner
    for f in (a, b, fixed_name):
        f.write_text("old")
    rc, doc, _ = run(tmp_path, capsys, json_out=True)
    assert rc == 0 and doc["ok"] is True
    assert doc["legacy_backups"] == {"count": 2, "paths": sorted([str(a), str(b)])}
    rc, _d, out = run(tmp_path, capsys)
    assert "2 backup files from older sibyl versions" in out and str(a) in out
    assert "Doctor: all steps passed." in out
    assert a.exists() and b.exists() and fixed_name.exists()


def _case(name, tmp_path, monkeypatch):
    if name == "signin":
        monkeypatch.setenv("STUB_MODE", "noaccount")
        return "sibyl init", None
    if name == "binary":
        monkeypatch.setattr(D, "_server_cmd", lambda: None)
        nxt = S._binary_missing_handoff("doctor")[0]
        return nxt, "ANY"
    if name == "server-other":
        monkeypatch.setenv("STUB_MODE", "missing")
        return "done", "run sibyl-memory-mcp in a terminal and tell me the first error line"
    if name == "repair-failed":
        FakeClaude(monkeypatch, add_rc=1)
        _claude_json({"type": "stdio", "command": STALE})
        return f"claude mcp add --scope user sibyl-memory -- {S.mcp_command_path()}", None
    if name == "unrepaired":
        cursor_cfg(STALE)
        return "sibyl doctor --fix", None
    if name == "by-hand":
        cursor_cfg(STALE)
        monkeypatch.setattr(S, "resolve_mcp_binary", lambda: None)
        return "done", "needs fixing by hand"
    if name == "repaired":
        cursor_cfg(STALE)
        return "done", "reload MCP servers in Cursor"
    if name == "clear":
        return "done", None
    raise AssertionError(name)


@pytest.mark.parametrize("name", ["signin", "binary", "server-other", "repair-failed",
                                  "unrepaired", "by-hand", "repaired", "clear"])
def test_handoff_block_is_last_and_matches_rules(name, tmp_path, capsys, monkeypatch):
    want_next, want_human = _case(name, tmp_path, monkeypatch)
    fix = name in ("repair-failed", "repaired", "by-hand")
    D.run_doctor(_ns(tmp_path, fix=fix))
    out = capsys.readouterr()
    h = assert_convention(out.out, out.err)
    assert h["next"] == want_next
    if want_human is None:
        assert h["human"] is None
    elif want_human != "ANY":
        assert want_human in (h["human"] or "")


def test_missing_adds_later_line_not_next(tmp_path, capsys):
    (H() / ".cursor").mkdir()
    (H() / ".dsh" / "profiles").mkdir(parents=True)
    D.run_doctor(_ns(tmp_path))
    h = assert_convention(capsys.readouterr().out)
    assert h["next"] == "done"
    assert h["later"] == ["sibyl setup cursor", "sibyl setup deepseek"]


def test_no_secret_shaped_values_in_output(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_FAKETOKENVALUE123")
    monkeypatch.setenv("SIBYL_MEMORY_MCP_OUTPUT_TOKENS", "777123")
    cursor_cfg(STALE)
    dsh_put("home", "user_patches.yml")
    _rc, _d, raw = run(tmp_path, capsys, json_out=True, fix=True)
    _rc, _d, out = run(tmp_path, capsys)
    for text in (raw, out):
        assert "ghp_FAKETOKENVALUE123" not in text and "777123" not in text


def test_output_says_deepseek_harness_not_bare_dsh(tmp_path, capsys):
    import re
    dsh_put("home", "ours_stale.yml")
    dsh_put("web", "ours_disabled.yml")
    _rc, _d, out = run(tmp_path, capsys)
    _rc, _d, out2 = run(tmp_path, capsys, fix=True)
    assert "DeepSeek Harness" in out
    allowed = re.compile(r"\.dsh\b|DSH_HOME|--dsh-home|--dsh-profile|dsh-mcp-client")
    # Paths are not user-facing text (sandbox M6): strip them first.
    paths = re.compile(r"(?:~|\.)?(?:/[^\s;,:'\"()]+)+")
    for t in (out, out2):
        assert not re.search(r"\bdsh\b", allowed.sub("", paths.sub("", t)), re.I)
