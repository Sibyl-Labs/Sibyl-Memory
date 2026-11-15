"""Fixes from the cli 0.7.0 build check (CHECK-FABLE, 2026-11-13).

B-1 Hermes setup on a broken config.yaml: no file text in any message, nothing
    installed or backed up.
B-2 A `!!js` command in our DeepSeek Harness row is an expression, not a stale
    path: ok in doctor, never rewritten without --force.
B-3 Codex without tomllib (Python 3.10): the table body ends only at a strict
    header, and an edit that would leave two command lines is refused.
N-1 A foreign Codex table is skipped without --force; --force replaces it whole.
N-2 Cursor and Codex writes keep a symlinked config linked (write through).
N-3 No foreign command string in doctor probes or checks.
N-4 DeepSeek Harness: the file is read again right before the write.
N-5 DeepSeek Harness: a read-only patch file is never replaced.

The first ten tests are the check's reproductions (they fail before the fixes);
the four after them pin behavior that held but had no test. Temp HOME and PATH
only; no harness binary is ever run."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import unittest.mock as um
from pathlib import Path

import pytest

import sibyl_memory_cli.doctor as D
import sibyl_memory_cli.setup as S

from _harness_fixtures import backups_dir, isolate_path, leftovers

OLD = "/old/venv/bin/sibyl-memory-mcp"
PLUGIN = "'@deepseek-ai/dsh-mcp-client'"
SECRET = "FAKESECRET123"


@pytest.fixture(autouse=True)
def fake_bin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "resolve_mcp_binary", lambda: str(fake))
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "ok"))
    return fake


def _dsh(tmp_path, text, name=".dsh"):
    h = tmp_path / name
    h.mkdir()
    p = h / "cordis.patch.yml"
    p.write_text(text)
    return h, p


def _our_row(command_line):
    return ("- insert:\n    - id: sibyl-memory\n      name: " + PLUGIN + "\n      config:\n"
            "        serverName: sibyl-memory\n"
            "        command: " + command_line + "\n"
            "        cwd: !!js process.cwd()\n")


# ======================================================================
# The check's reproductions
# ======================================================================

# ---- B-1 ----
def test_hermes_setup_parse_error_never_echoes_file_text(tmp_path):
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text(
        "memory:\n  provider: other\nmodel: {api_key: " + SECRET + ", base: x\n")
    before = (hh / "config.yaml").read_bytes()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = S.HermesWirer(hermes_home=hh).wire(force=True)
    assert out.status == "error"
    assert SECRET not in out.message
    assert SECRET not in buf.getvalue()
    assert (hh / "config.yaml").read_bytes() == before
    assert not (hh / "plugins" / "sibyl" / "__init__.py").exists()


# ---- B-2 ----
JS_ROW = _our_row('!!js "`${process.env.HOME}/.local/bin/sibyl-memory-mcp`"')


def test_deepseek_js_expression_command_is_not_stale_and_never_rewritten(tmp_path):
    h, p = _dsh(tmp_path, JS_ROW)
    w = S.DeepSeekWirer(dsh_home=h)
    pr = w.doctor_probe()[0]
    chk = D.check_harness(w, pr, verified={})
    assert chk.level != "fail" and not chk.fixable
    out = w.wire()
    assert out.status == "already"
    assert p.read_text() == JS_ROW


# ---- B-3 ----
@pytest.mark.parametrize("body", [
    'args = [\n  ["a"],\n]\ncommand = "%s"\n' % OLD,
    'env_list = [\n[1, 2],\n]\ncommand = "%s"\nstartup_timeout_sec = 5\n' % OLD,
])
def test_codex_no_tomllib_never_inserts_second_command(tmp_path, monkeypatch, body):
    monkeypatch.setattr(S, "_tomllib", None)
    p = tmp_path / "config.toml"
    p.write_text("[mcp_servers.sibyl_memory]\n" + body)
    w = S.CodexWirer(config_path=p)
    w.repair_mcp(w.doctor_probe()[0])
    table = p.read_text().split("[mcp_servers.sibyl_memory]", 1)[1]
    assert sum(1 for ln in table.splitlines() if ln.strip().startswith("command =")) == 1


# ---- N-1 ----
FOREIGN_CODEX = ('[mcp_servers.sibyl_memory]\ncommand = "npx"\nargs = ["-y", "engram"]\n'
                 '[mcp_servers.sibyl_memory.env]\nENGRAM_KEY = "x"\n')


def test_codex_foreign_table_is_not_already(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    out = S.CodexWirer(config_path=p)._wire_mcp(force=False)
    assert out.status == "skipped" and p.read_text() == FOREIGN_CODEX


def test_codex_force_on_foreign_table_never_leaves_hybrid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    S.CodexWirer(config_path=p)._wire_mcp(force=True)
    after = p.read_text()
    assert after == FOREIGN_CODEX or ("engram" not in after and "ENGRAM_KEY" not in after)


# ---- N-2 ----
@pytest.mark.parametrize("kind", ["cursor", "codex"])
def test_repair_does_not_replace_a_symlinked_config_with_a_file(tmp_path, kind):
    real = tmp_path / "dotfiles" / "cfg"
    real.parent.mkdir()
    if kind == "cursor":
        real.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": OLD}}}))
        link = tmp_path / "mcp.json"
        link.symlink_to(real)
        w = S.CursorWirer(config_path=link)
    else:
        real.write_text('[mcp_servers.sibyl_memory]\ncommand = "%s"\n' % OLD)
        link = tmp_path / "config.toml"
        link.symlink_to(real)
        w = S.CodexWirer(config_path=link)
    w.repair_mcp(w.doctor_probe()[0])
    assert link.is_symlink()


# ---- N-3 ----
def test_foreign_command_not_in_probe(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {
        "command": "env API_KEY=" + SECRET + " npx engram"}}}))
    pr = S.CursorWirer(config_path=p).doctor_probe()[0]
    assert pr["entry"] == "foreign" and SECRET not in json.dumps(pr)


# ---- N-4 ----
def test_deepseek_write_aborts_when_file_changed_after_scan(tmp_path, monkeypatch):
    h, p = _dsh(tmp_path, "- id: x\n  disabled: true\n")
    w = S.DeepSeekWirer(dsh_home=h)
    real_backup = S._timestamped_backup

    def racing_backup(path, *a, **k):
        b = real_backup(path, *a, **k)
        Path(path).write_text("- id: x\n  disabled: true\n- id: added-by-the-harness\n")
        return b

    monkeypatch.setattr(S, "_timestamped_backup", racing_backup)
    out = w.wire()
    assert "added-by-the-harness" in p.read_text()
    assert out.status in ("error", "wired")


# ======================================================================
# Passing before the fixes; pinned here (no test covered them)
# ======================================================================

def test_deepseek_command_update_refuses_block_scalar_and_alias(tmp_path):
    for n, (cmd, extra) in enumerate((("|\n          " + OLD, ""),
                                      ('&c "%s"' % OLD, "- id: o\n  config: {x: *c}\n"))):
        h = tmp_path / ("dsh%d" % n)
        h.mkdir()
        text = ("- insert:\n    - id: sibyl-memory\n      name: " + PLUGIN + "\n      config:\n"
                "        command: " + cmd + "\n        args: []\n" + extra)
        (h / "cordis.patch.yml").write_text(text)
        out = S.DeepSeekWirer(dsh_home=h).wire()
        assert out.status == "error" and (h / "cordis.patch.yml").read_text() == text
        assert [x.name for x in h.iterdir()] == ["cordis.patch.yml"]


def test_deepseek_readonly_dir_write_fails_cleanly(tmp_path):
    text = ("- insert:\n    - id: sibyl-memory\n      name: " + PLUGIN + "\n      config:\n"
            "        command: " + OLD + "\n")
    h, p = _dsh(tmp_path, text)
    os.chmod(h, 0o555)
    try:
        out = S.DeepSeekWirer(dsh_home=h).wire()
    finally:
        os.chmod(h, 0o755)
    assert out.status == "error" and p.read_text() == text
    assert [x.name for x in h.iterdir()] == ["cordis.patch.yml"]


def test_backup_refused_when_backups_dir_is_symlink(tmp_path):
    root = Path.home() / ".sibyl-memory"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "backups").symlink_to(elsewhere)
    p = tmp_path / "mcp.json"
    text = json.dumps({"mcpServers": {"sibyl-memory": {"command": OLD}}})
    p.write_text(text)
    out = S.CursorWirer(config_path=p).wire()
    assert out.status == "error" and p.read_text() == text and list(elsewhere.iterdir()) == []


def test_codex_crlf_and_trailing_comment_survive_repair(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes(('model = "o4"\r\n[mcp_servers.sibyl_memory]\r\ncommand = "%s"  # keep\r\n'
                   'startup_timeout_sec = 30\r\n' % OLD).encode())
    w = S.CodexWirer(config_path=p)
    out = w.repair_mcp(w.doctor_probe()[0])
    raw = p.read_bytes()
    assert out.status == "wired" and b"# keep\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


# ======================================================================
# Further tests for each fix
# ======================================================================

# ---- B-1 ----
def test_hermes_parse_error_message_is_type_and_position_only(tmp_path):
    hh = tmp_path / "hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text("model: {api_key: " + SECRET + ", base: x\n")
    out = S.HermesWirer(hermes_home=hh).wire()
    assert out.status == "error"
    assert "is not valid YAML (" in out.message and "line " in out.message
    assert "column " in out.message and "it was not modified" in out.message
    assert "api_key" not in out.message
    # the handoff line an agent reads carries the same brief only
    _next, human, _later = S._setup_handoff([out], {}, [], unanswered=False)
    assert SECRET not in (human or "") and "api_key" not in (human or "")
    assert not backups_dir("hermes").exists()
    assert leftovers(hh) == []


def test_hermes_write_failure_never_quotes_yaml_error(tmp_path, monkeypatch):
    # A YAML error raised during the write itself is reported by type and
    # position, never by str(e).
    hh = tmp_path / "hermes"
    (hh / "plugins" / "sibyl").mkdir(parents=True)
    (hh / "plugins" / "sibyl" / "__init__.py").write_text("")
    (hh / "config.yaml").write_text("memory:\n  provider: other\n")
    yaml = S._import_yaml()

    def boom(self, y):
        raise yaml.YAMLError("text from the file: " + SECRET)

    monkeypatch.setattr(S.HermesWirer, "_write_config_with_sibyl", boom)
    out = S.HermesWirer(hermes_home=hh).wire(force=True)
    assert out.status == "error" and SECRET not in out.message
    assert "YAMLError" in out.message


def test_hermes_non_mapping_top_level_message_kept(tmp_path):
    hh = tmp_path / "hermes"
    (hh / "plugins" / "sibyl").mkdir(parents=True)
    (hh / "plugins" / "sibyl" / "__init__.py").write_text("")
    (hh / "config.yaml").write_text("- a\n- b\n")
    out = S.HermesWirer(hermes_home=hh).wire(force=True)
    assert out.status == "error" and "top level is not a YAML mapping" in out.message


# ---- B-2 ----
def test_deepseek_js_probe_is_present_with_no_command_and_says_so(tmp_path):
    h, _p = _dsh(tmp_path, JS_ROW)
    w = S.DeepSeekWirer(dsh_home=h)
    pr = w.doctor_probe()[0]
    assert pr["entry"] == "present" and pr["command"] is None
    assert "!!js expression" in pr["detail"]
    chk = D.check_harness(w, pr, verified={})
    assert chk.status == "ok" and chk.level == "pass" and "!!js" in chk.detail


def test_deepseek_js_repair_refused_file_untouched(tmp_path):
    h, p = _dsh(tmp_path, JS_ROW)
    w = S.DeepSeekWirer(dsh_home=h)
    out = w.repair_mcp(w.doctor_probe()[0])
    assert out.status == "error" and "!!js" in out.message
    assert p.read_text() == JS_ROW
    assert not backups_dir("deepseek").exists()


def test_deepseek_js_note_on_setup_already(tmp_path):
    h, _p = _dsh(tmp_path, JS_ROW)
    out = S.DeepSeekWirer(dsh_home=h).wire()
    assert out.status == "already" and "!!js expression; not checked" in out.message
    assert "--force" in out.message


def test_deepseek_js_force_repoints(tmp_path, fake_bin):
    h, p = _dsh(tmp_path, JS_ROW)
    out = S.DeepSeekWirer(dsh_home=h).wire(force=True)
    assert out.status == "wired"
    data = S._dsh_yaml_load(p.read_text())
    assert data[0]["insert"][0]["config"]["command"] == str(fake_bin)
    assert type(data[0]["insert"][0]["config"]["cwd"]) is S._JsExpr


def test_deepseek_js_row_in_another_layer_left_alone_by_profile_setup(tmp_path):
    h, p = _dsh(tmp_path, JS_ROW)
    (h / "profiles" / "web").mkdir(parents=True)
    out = S.DeepSeekWirer(dsh_home=h, profile="web").wire(force=True)
    assert out.status == "already"
    assert p.read_text() == JS_ROW
    assert not (h / "profiles" / "web" / "cordis.patch.yml").exists()


def test_deepseek_js_plan_refused_without_force_flag(tmp_path):
    h, _p = _dsh(tmp_path, JS_ROW)
    w = S.DeepSeekWirer(dsh_home=h)
    scan = w._scan_file(h / "cordis.patch.yml", "home")
    cand = scan["candidates"][0]
    assert cand["command_js"] is True
    assert w._plan_text(scan, ("command", cand, "/x/sibyl-memory-mcp")) is None
    assert w._plan_text(scan, ("command", cand, "/x/sibyl-memory-mcp", True)) is not None


def test_deepseek_js_row_doctor_run_exits_zero(tmp_path, monkeypatch):
    h, p = _dsh(tmp_path, JS_ROW)
    w = S.DeepSeekWirer(dsh_home=h)
    checks = [D.check_harness(w, pr, verified={}) for pr in w.doctor_probe()]
    assert [c.level for c in checks] == ["pass"]
    assert p.read_text() == JS_ROW


# ---- B-3 ----
def test_codex_strict_header_regex():
    # Panel fix 2 (2026-11-14): the header regex became a structural parser.
    for ok in ("[a]", "[mcp_servers.x]", "  [[arr]]  # c", '["quoted".b]', "[a . b]",
               '[projects."/home/u/repo"]', "[projects.'C:\\x']", '[mcp_servers."a@b"]'):
        assert S._toml_header_path(ok) is not None, ok
    for bad in ('  ["a"],', "[1, 2],", "[", "]", "args = [", '["a", "b"]x'):
        assert S._toml_header_path(bad) is None, bad


def test_codex_body_skips_header_lookalike_inside_array():
    text = '[mcp_servers.sibyl_memory]\nargs = [\n  ["a"]\n]\ncommand = "%s"\n[next]\n' % OLD
    sc = S._codex_scan(text)
    assert sc["body"] == (1, 5) and sc["command"]["line"] == 4 and sc["command_lines"] == 1


def test_codex_body_ignores_brackets_in_strings_and_comments():
    text = ('[mcp_servers.sibyl_memory]\nnote = "a [ b"  # ] [\nx = \'[\'\n'
            'ml = """\n[not.a.header]\n"""\ncommand = "%s"\n[next]\n' % OLD)
    sc = S._codex_scan(text)
    assert sc["command"]["line"] == 6 and sc["body"] == (1, 7)


@pytest.mark.parametrize("tomllib_present", [False, True])
def test_codex_two_command_lines_refused_file_untouched(tmp_path, monkeypatch, tomllib_present):
    if tomllib_present and S._tomllib is None:
        pytest.skip("needs tomllib")
    if not tomllib_present:
        monkeypatch.setattr(S, "_tomllib", None)
    text = '[mcp_servers.sibyl_memory]\ncommand = "%s"\ncommand = "%s"\n' % (OLD, OLD)
    p = tmp_path / "config.toml"
    p.write_text(text)
    for out in (S.CodexWirer(config_path=p).wire(force=True),
                S.CodexWirer(config_path=p).repair_mcp({})):
        assert out.status == "error"
    assert p.read_text() == text
    assert not backups_dir("codex").exists()


def test_codex_no_tomllib_nested_array_updates_in_place(tmp_path, monkeypatch, fake_bin):
    monkeypatch.setattr(S, "_tomllib", None)
    text = ('model = "o4"\n[mcp_servers.sibyl_memory]\nargs = [\n  ["a"],\n  ["b"]\n]\n'
            'command = "%s"  # mine\n\n[other]\nx = 1\n' % OLD)
    p = tmp_path / "config.toml"
    p.write_text(text)
    w = S.CodexWirer(config_path=p)
    pr = w.doctor_probe()[0]
    assert pr["entry"] == "present" and pr["command"] == OLD
    out = w.repair_mcp(pr)
    assert out.status == "wired"
    assert p.read_text() == text.replace(OLD, str(fake_bin))


def test_codex_no_tomllib_verify_rejects_extra_change(tmp_path, monkeypatch, fake_bin):
    monkeypatch.setattr(S, "_tomllib", None)
    w = S.CodexWirer(config_path=tmp_path / "config.toml")
    old = '[mcp_servers.sibyl_memory]\ncommand = "%s"\nx = 1\n' % OLD
    good = old.replace(OLD, str(fake_bin))
    w._verify_command_edit(old, good)
    with pytest.raises(ValueError):
        w._verify_command_edit(old, good.replace("x = 1", "x = 2"))
    with pytest.raises(ValueError):
        w._verify_command_edit(old, good + 'command = "%s"\n' % fake_bin)


# ---- N-1 ----
def test_codex_foreign_force_replaces_whole_table(tmp_path, fake_bin):
    text = ('model = "o4"\n\n' + FOREIGN_CODEX + '\n[mcp_servers.gh]\ncommand = "npx"\n')
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = S.CodexWirer(config_path=p).wire(force=True)
    assert out.status == "wired" and "Replaced the sibyl_memory table" in out.message
    after = p.read_text()
    assert "engram" not in after and "ENGRAM_KEY" not in after
    assert "[mcp_servers.sibyl_memory.env]" not in after
    assert after.count("[mcp_servers.sibyl_memory]") == 1
    assert 'command = "%s"' % fake_bin in after
    assert after.startswith('model = "o4"\n') and '[mcp_servers.gh]\ncommand = "npx"\n' in after
    if S._tomllib is not None:
        doc = S._tomllib.loads(after)
        assert doc["mcp_servers"]["sibyl_memory"] == {"command": str(fake_bin)}
        assert doc["mcp_servers"]["gh"] == {"command": "npx"} and doc["model"] == "o4"
    assert len(list(backups_dir("codex").glob("config.toml.*.bak"))) == 1


def test_codex_foreign_force_no_tomllib_replaces_whole_table(tmp_path, monkeypatch, fake_bin):
    monkeypatch.setattr(S, "_tomllib", None)
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    out = S.CodexWirer(config_path=p).wire(force=True)
    assert out.status == "wired"
    assert p.read_text() == '[mcp_servers.sibyl_memory]\ncommand = "%s"\n' % fake_bin


def test_codex_foreign_prompt_declined_and_accepted(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    out = S.CodexWirer(config_path=p).wire(prompt_fn=lambda *a, **k: "n")
    assert out.status == "skipped" and p.read_text() == FOREIGN_CODEX
    out = S.CodexWirer(config_path=p).wire(prompt_fn=lambda *a, **k: "y")
    assert out.status == "wired" and "engram" not in p.read_text()


def test_codex_foreign_never_repaired_by_doctor(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    w = S.CodexWirer(config_path=p)
    pr = w.doctor_probe()[0]
    assert pr["entry"] == "foreign"
    out = w.repair_mcp(pr)
    assert out.status == "error" and p.read_text() == FOREIGN_CODEX


def test_codex_foreign_dry_run_says_replace(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_CODEX)
    out = S.CodexWirer(config_path=p).wire(force=True, dry_run=True)
    assert out.status == "dry-run" and "replace" in out.message
    assert p.read_text() == FOREIGN_CODEX


# ---- N-2 ----
@pytest.mark.parametrize("kind", ["cursor", "codex"])
@pytest.mark.parametrize("via", ["repair", "setup"])
def test_symlinked_config_written_through_and_link_kept(tmp_path, fake_bin, kind, via):
    real = tmp_path / "dotfiles" / ("mcp.json" if kind == "cursor" else "config.toml")
    real.parent.mkdir()
    link = tmp_path / "cfgdir" / real.name
    link.parent.mkdir()
    if kind == "cursor":
        real.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": OLD,
                                                                    "env": {"A": "1"}}}}))
        link.symlink_to(real)
        w = S.CursorWirer(config_path=link)
    else:
        real.write_text('[mcp_servers.sibyl_memory]\ncommand = "%s"\n' % OLD)
        link.symlink_to(real)
        w = S.CodexWirer(config_path=link)
    os.chmod(real, 0o600)
    out = w.repair_mcp(w.doctor_probe()[0]) if via == "repair" else w.wire(force=True)
    assert out.status == "wired", out.message
    assert link.is_symlink() and os.path.realpath(link) == str(real)
    assert str(fake_bin) in real.read_text() and OLD not in real.read_text()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    if kind == "cursor":
        assert json.loads(real.read_text())["mcpServers"]["sibyl-memory"]["env"] == {"A": "1"}
    assert leftovers(link.parent) == [] and leftovers(real.parent) == []


def test_codex_fresh_append_through_symlink_keeps_link(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text('model = "o4"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    out = S.CodexWirer(config_path=link).wire()
    assert out.status == "wired" and link.is_symlink()
    assert "[mcp_servers.sibyl_memory]" in real.read_text()


# ---- N-3 ----
def test_codex_foreign_command_not_in_probe_or_check(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.sibyl_memory]\ncommand = "env K=%s npx x"\n' % SECRET)
    w = S.CodexWirer(config_path=p)
    pr = w.doctor_probe()[0]
    assert pr["entry"] == "foreign" and pr["command"] is None
    chk = D.check_harness(w, pr, verified={})
    assert SECRET not in json.dumps(chk.__dict__)


def test_claude_code_foreign_command_not_in_probe(tmp_path, monkeypatch):
    w = S.ClaudeCodeWirer(settings_path=tmp_path / "settings.json")
    monkeypatch.setattr(S.ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: "/fake/claude"))
    monkeypatch.setattr(S.ClaudeCodeWirer, "_registered_via_cli", lambda self: True)
    monkeypatch.setattr(S.ClaudeCodeWirer, "_user_scope_entry",
                        lambda self: {"command": "env TOKEN=" + SECRET + " npx other"})
    pr = w.doctor_probe()[0]
    assert pr["entry"] == "foreign" and SECRET not in json.dumps(pr)


def test_cursor_foreign_check_holds_no_command(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {
        "command": "env API_KEY=" + SECRET + " npx engram"}}}))
    w = S.CursorWirer(config_path=p)
    chk = D.check_harness(w, w.doctor_probe()[0], verified={})
    assert chk.status == "foreign" and chk.command is None
    assert SECRET not in json.dumps(chk.__dict__)


def test_present_but_resolves_to_another_binary_reports_no_command(tmp_path):
    # A command of ours by name that resolves to a file with another name:
    # check_harness calls it foreign and drops the command string.
    other = tmp_path / "isolated-bin" / "other-server"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    w = S.CursorWirer(config_path=tmp_path / "mcp.json")
    pr = S._probe("present", w.config_path, command="sibyl-memory-mcp")
    with um.patch.object(D, "_abs_command", lambda cmd: str(other)):
        chk = D.check_harness(w, pr, verified={})
    assert chk.status == "foreign" and chk.command is None


# ---- N-4 ----
def test_deepseek_change_after_backup_is_an_error_and_survives(tmp_path, monkeypatch):
    h, p = _dsh(tmp_path, "- id: x\n  disabled: true\n")
    real_backup = S._timestamped_backup
    newer = "- id: x\n  disabled: true\n- id: added-by-the-harness\n"

    def racing_backup(path, *a, **k):
        b = real_backup(path, *a, **k)
        Path(path).write_text(newer)
        return b

    monkeypatch.setattr(S, "_timestamped_backup", racing_backup)
    out = S.DeepSeekWirer(dsh_home=h).wire()
    assert out.status == "error" and "changed while setup was running" in out.message
    assert p.read_text() == newer
    assert leftovers(h) == []


def test_deepseek_change_during_binary_install_is_an_error(tmp_path, monkeypatch):
    h, p = _dsh(tmp_path, _our_row(OLD))
    newer = _our_row(OLD) + "- id: other\n  disabled: true\n"

    def install_and_race(self):
        p.write_text(newer)
        return True

    monkeypatch.setattr(S.DeepSeekWirer, "_ensure_mcp_binary", install_and_race)
    out = S.DeepSeekWirer(dsh_home=h).wire()
    assert out.status == "error" and "changed while setup was running" in out.message
    assert p.read_text() == newer


def test_deepseek_repair_rereads_before_write(tmp_path, monkeypatch):
    h, p = _dsh(tmp_path, _our_row(OLD))
    w = S.DeepSeekWirer(dsh_home=h)
    pr = w.doctor_probe()[0]
    real_backup = S._timestamped_backup
    newer = _our_row(OLD).replace("serverName: sibyl-memory", "serverName: sibyl-memory  # me")

    def racing_backup(path, *a, **k):
        b = real_backup(path, *a, **k)
        Path(path).write_text(newer)
        return b

    monkeypatch.setattr(S, "_timestamped_backup", racing_backup)
    out = w.repair_mcp(pr)
    assert out.status == "error" and p.read_text() == newer


# ---- N-5 ----
def test_deepseek_readonly_patch_file_not_replaced(tmp_path):
    h, p = _dsh(tmp_path, _our_row(OLD))
    os.chmod(p, 0o444)
    try:
        w = S.DeepSeekWirer(dsh_home=h)
        for out in (w.wire(), w.repair_mcp(w.doctor_probe()[0])):
            assert out.status == "error" and "read-only" in out.message and str(p) in out.message
        assert p.read_text() == _our_row(OLD)
        assert stat.S_IMODE(p.stat().st_mode) == 0o444
        assert leftovers(h) == []
        assert not backups_dir("deepseek").exists()
    finally:
        os.chmod(p, 0o644)
