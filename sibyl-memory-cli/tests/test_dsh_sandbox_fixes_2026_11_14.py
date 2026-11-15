"""Fixes from the real DeepSeek Harness sandbox run (SANDBOX-RESULTS.md, M1 to M6).

M1: an `{id: sibyl-memory}` override only reaches our row when it comes after
    the row in the composed list (later in the same file, or in a later layer;
    the profile patch is applied before the home patch).
M2: a server named sibyl-memory in DeepSeek Harness's own MCP settings
    (`mcp-manager` `config.servers`) clashes with our row.
M3: setup refuses a home that is not DeepSeek Harness's before printing
    "Detected" or asking Y/n.
M4: doctor names a config path once per detail line.
M5: "Looked for" never prints None; doctor --harness accepts grokbot.

Temp HOME and an isolated PATH only. Nothing here runs DeepSeek Harness."""
from __future__ import annotations

import argparse
import builtins
import json
import shutil
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import doctor as D
from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import DeepSeekWirer

from _harness_fixtures import isolate_path
from test_doctor_2026_10_28 import STUB

FIX = Path(__file__).resolve().parent / "fixtures" / "deepseek"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB)
    monkeypatch.setattr(D, "_server_cmd", lambda: [sys.executable, str(stub)])
    monkeypatch.setenv("STUB_MODE", "ok")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return fake


def ROW(cmd):
    return ("- insert:\n"
            "    - id: sibyl-memory\n"
            "      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n"
            "        serverName: sibyl-memory\n"
            "        transport: stdio\n"
            f"        command: \"{cmd}\"\n"
            "        args: []\n"
            "        env: {}\n"
            "        cwd: !!js process.cwd()\n")


OVERRIDE = "- {id: sibyl-memory, disabled: true}\n"
OTHER = "- {id: ui-theme, config: {fontSize: 15}}\n"
MANAGER = ("- id: mcp-manager\n"
           "  name: \"@deepseek-ai/dsh-mcp-manager\"\n"
           "  config:\n"
           "    servers:\n"
           "      sibyl-memory:\n"
           "        transport: stdio\n"
           "        command: /some/venv/bin/sibyl-memory-mcp\n")


def put(layer, text):
    home = Path.home() / ".dsh"
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    p = home / "cordis.patch.yml" if layer == "home" else home / "profiles" / layer / "cordis.patch.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def doctor(tmp_path, capsys, **kw):
    db = tmp_path / "store" / "memory.db"
    db.parent.mkdir(exist_ok=True)
    ns = argparse.Namespace(db=str(db), credentials=str(tmp_path / "c.json"), json=kw.get("json", True),
                            fix=kw.get("fix", False), harness=kw.get("harness"), no_harness=False)
    rc = D.run_doctor(ns)
    out = capsys.readouterr().out
    return rc, (json.loads(out) if kw.get("json", True) else out)


def ds(doc):
    return {h["location"]: h for h in doc["harnesses"] if h["name"] == "deepseek"}


def setup(argv, capsys):
    ns = cli.build_parser().parse_args(["setup"] + argv)
    rc = S.cmd_setup(ns)
    return rc, capsys.readouterr().out


# --- M1: override placement ----------------------------------------------------------

def test_m1_override_after_row_same_file_is_overridden(tmp_path, capsys, _isolated):
    put("home", ROW(_isolated) + OTHER + OVERRIDE)
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "overridden"


def test_m1_override_before_row_same_file_is_not_overridden(tmp_path, capsys, _isolated):
    put("home", OVERRIDE + ROW(_isolated))
    rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "ok" and rc == 0


def test_m1_override_in_profile_does_not_reach_home_row(tmp_path, capsys, _isolated):
    # The profile patch is applied before the home patch (F6): its override
    # runs before the home row exists (sandbox: the server still started).
    put("home", ROW(_isolated))
    put("web", OVERRIDE)
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "ok"


def test_m1_override_in_home_reaches_profile_row(tmp_path, capsys, _isolated):
    put("home", OVERRIDE)
    put("web", ROW(_isolated))
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["profile:web"]["status"] == "overridden"


def test_m1_override_before_row_in_profile_file_is_not_overridden(tmp_path, capsys, _isolated):
    put("web", OVERRIDE + ROW(_isolated))
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["profile:web"]["status"] == "ok"


def test_m1_override_in_another_profile_does_not_count(tmp_path, capsys, _isolated):
    put("web", ROW(_isolated))
    put("tui", OVERRIDE)
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["profile:web"]["status"] == "ok"


def test_m1_override_in_group_row_position_uses_the_top_level_patch(tmp_path):
    # A row nested in a group is placed by its top-level insert patch.
    text = ("- insert:\n"
            "    - group: true\n"
            "      config:\n"
            + "".join("      " + ln + "\n" for ln in ROW("/x/sibyl-memory-mcp").splitlines()[1:])
            )
    w = DeepSeekWirer()
    scan_after = w._scan_text(text + OVERRIDE)
    scan_before = w._scan_text(OVERRIDE + text)
    for scan, want in ((scan_after, True), (scan_before, False)):
        layer = dict(scan, layer="home", path="p")
        cand = layer["candidates"][0]
        assert w._overridden(cand, [layer], "home") is want


# --- M2: a sibyl-memory server in DeepSeek Harness's own MCP settings ----------------

def test_m2_manager_server_in_profile_reported_duplicate(tmp_path, capsys, _isolated):
    # Exactly what the settings UI wrote in the sandbox (step 9 extra).
    p = put("web", ROW(_isolated) + OTHER + MANAGER)
    before = p.read_text()
    _rc, doc = doctor(tmp_path, capsys, fix=True)
    d = ds(doc)["profile:web"]
    assert d["status"] == "duplicate" and d["level"] == "warn" and d["fixable"] is False
    assert "mcp-manager" in d["detail"] and "already in use" in d["detail"]
    assert d["detail"].count(str(p)) == 1
    assert p.read_text() == before


def test_m2_manager_server_in_profile_clashes_with_home_row(tmp_path, capsys, _isolated):
    put("home", ROW(_isolated))
    put("web", MANAGER)
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "duplicate"


def test_m2_manager_disabled_or_other_name_or_replaced_is_no_clash(tmp_path, capsys, _isolated):
    put("home", ROW(_isolated))
    put("web", MANAGER.replace("        transport", "        enabled: false\n        transport", 1))
    put("tui", MANAGER.replace("sibyl-memory:", "other:"))
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "ok"
    # The servers map is replaced whole: a later servers map without our name wins.
    put("web", MANAGER)
    put("home", ROW(_isolated) + "- {id: mcp-manager, config: {servers: {}}}\n")
    _rc, doc = doctor(tmp_path, capsys)
    assert ds(doc)["home"]["status"] == "ok"


def test_m2_setup_skips_and_writes_nothing_when_manager_has_our_name(tmp_path, capsys, _isolated):
    p = put("web", "[]\n" + "")
    p.write_text(MANAGER)
    before = p.read_text()
    rc, out = setup(["deepseek", "--yes"], capsys)
    assert "mcp-manager" in out and "Nothing was changed" in out
    assert not (Path.home() / ".dsh" / "cordis.patch.yml").exists()
    assert p.read_text() == before
    assert DeepSeekWirer().current_state()["wired_with_sibyl"] is False


def test_m2_current_state_not_wired_with_manager_clash(_isolated):
    put("web", ROW(_isolated) + MANAGER)
    st = DeepSeekWirer().current_state()
    assert st["duplicate"] is True and st["wired_with_sibyl"] is False


# --- M3: refuse up front -------------------------------------------------------------

def test_m3_debian_home_refused_without_detected_or_prompt(capsys, monkeypatch):
    home = Path.home() / ".dsh"
    shutil.copytree(FIX / "debian_dsh_home", home)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda *a: asked.append(a) or "y")
    rc, out = setup(["deepseek"], capsys)
    assert rc == 2 and asked == []
    assert "Detected" not in out and "[Y/n]" not in out
    assert "distributed shell" in out and "Nothing was written" in out
    assert sorted(p.name for p in home.iterdir()) == ["dsh.conf", "group", "machines.list"]


def test_m3_missing_profile_refused_without_prompt(capsys, monkeypatch):
    (Path.home() / ".dsh" / "profiles").mkdir(parents=True)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda *a: asked.append(a) or "y")
    rc, out = setup(["deepseek", "--dsh-profile", "nope"], capsys)
    assert rc == 2 and asked == [] and "Detected" not in out
    assert "profile 'nope' does not exist" in out


# --- M4: one path per detail line ----------------------------------------------------

def test_m4_duplicate_detail_names_each_path_once(tmp_path, capsys, _isolated):
    ph = put("home", ROW(_isolated))
    pw = put("web", ROW(_isolated))
    _rc, doc = doctor(tmp_path, capsys)
    for d in ds(doc).values():
        assert d["status"] == "duplicate"
        assert d["detail"].count(str(ph)) == 1 and d["detail"].count(str(pw)) == 1


def test_m4_different_install_detail_names_command_once(tmp_path, capsys):
    other = tmp_path / "other-venv" / "bin" / "sibyl-memory-mcp"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    put("home", ROW(other))
    _rc, doc = doctor(tmp_path, capsys)
    d = ds(doc)["home"]
    assert d["status"] == "ok" and "uses a different install than this CLI" in d["detail"]
    assert d["detail"].count(str(other)) == 1


def test_m4_warn_without_detail_names_path_once():
    w = DeepSeekWirer()
    c = D.check_harness(w, {"entry": "unverifiable", "command": None, "location": "home",
                            "config_path": "/x/cordis.patch.yml", "detail": ""}, verified={})
    assert c.detail.count("/x/cordis.patch.yml") == 1


# --- M5: Looked for, doctor --harness grokbot -----------------------------------------

def test_m5_looked_for_never_prints_none(capsys):
    rc, out = setup(["--yes"], capsys)
    assert rc == 0 and "Looked for" in out
    assert ": None" not in out
    assert "OpenAI Codex: " + str(Path.home() / ".codex" / "config.toml") in out


def test_m5_doctor_harness_accepts_grokbot_as_cursor(tmp_path, capsys, _isolated):
    ns = cli.build_parser().parse_args(["doctor", "--harness", "grokbot"])
    assert ns.harness == ["grokbot"]
    p = Path.home() / ".cursor" / "mcp.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": str(_isolated)}}}))
    _rc, doc = doctor(tmp_path, capsys, harness=["grokbot"])
    assert [h["name"] for h in doc["harnesses"]] == ["cursor"]
