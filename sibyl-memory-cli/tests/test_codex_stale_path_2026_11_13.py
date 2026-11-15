"""Codex stale command path (spec dsh-doctor v2, 4.3) and the Cursor stale
path / merge fix (4.4). Before cli 0.7.0, `sibyl setup codex --force` took a
backup, `_append_block` returned early on its header guard, and the outcome
said "wired" with nothing changed. Temp HOME and PATH only."""
from __future__ import annotations

import json
import sys

import pytest

from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import CodexWirer, CursorWirer

from _harness_fixtures import backups_dir, isolate_path, leftovers


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    return fake


def _cfg(tmp_path, text):
    p = tmp_path / ".codex" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


STALE = ('model = "o4"\n\n[mcp_servers.sibyl_memory]\ncommand = "/old/venv/bin/sibyl-memory-mcp"\n\n'
         '[other]\nx = 1\n')


def _backups():
    d = backups_dir("codex")
    return sorted(d.glob("config.toml.*.bak")) if d.is_dir() else []


def test_force_rewrites_stale_command_keeps_other_tables(tmp_path, _isolated_path):
    p = _cfg(tmp_path, STALE)
    out = CodexWirer(config_path=p).wire(force=True)
    assert out.status == "wired" and "Updated the sibyl_memory command" in out.message
    want = STALE.replace("/old/venv/bin/sibyl-memory-mcp", str(_isolated_path))
    assert p.read_text() == want
    assert len(_backups()) == 1 and _backups()[0].read_text() == STALE
    assert leftovers(p.parent) == []


def test_stale_without_force_is_updated_not_already(tmp_path, _isolated_path):
    p = _cfg(tmp_path, STALE)
    assert CodexWirer(config_path=p).current_state()["wired_with_sibyl"] is False
    out = CodexWirer(config_path=p).wire()
    assert out.status == "wired"
    assert f'command = "{_isolated_path}"' in p.read_text()
    assert p.read_text().count("[mcp_servers.sibyl_memory]") == 1


def test_force_keeps_trailing_comment_and_subtable_env(tmp_path, _isolated_path):
    text = ('[mcp_servers.sibyl_memory]  # ours\n'
            '  command = "/old/sibyl-memory-mcp"   # moved venv\n'
            'startup_timeout_sec = 30\n'
            '[mcp_servers.sibyl_memory.env]\n'
            'SIBYL_MEMORY_DB = "/data/m.db"\n')
    p = _cfg(tmp_path, text)
    assert CodexWirer(config_path=p).wire(force=True).status == "wired"
    new = p.read_text()
    assert new == text.replace("/old/sibyl-memory-mcp", str(_isolated_path))
    assert '  command = "' in new and "   # moved venv\n" in new


def test_force_inserts_command_when_table_has_none(tmp_path, _isolated_path):
    text = 'model = "o4"\n[mcp_servers.sibyl_memory]\nargs = []\n'
    p = _cfg(tmp_path, text)
    out = CodexWirer(config_path=p).wire(force=True)
    assert out.status == "wired"
    assert p.read_text() == ('model = "o4"\n[mcp_servers.sibyl_memory]\n'
                             f'command = "{_isolated_path}"\nargs = []\n')


def test_crlf_line_endings_preserved(tmp_path, _isolated_path):
    text = STALE.replace("\n", "\r\n")
    p = _cfg(tmp_path, "")
    p.write_bytes(text.encode())
    assert CodexWirer(config_path=p).wire(force=True).status == "wired"
    raw = p.read_bytes().decode()
    assert raw == text.replace("/old/venv/bin/sibyl-memory-mcp", str(_isolated_path))


def test_force_noop_when_current_reports_already_no_backup(tmp_path, _isolated_path):
    p = _cfg(tmp_path, f'[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"\n')
    before = p.read_text()
    out = CodexWirer(config_path=p).wire(force=True)
    assert out.status == "already"
    assert p.read_text() == before and _backups() == []


def test_fresh_append_unchanged(tmp_path, _isolated_path):
    existing = 'model = "o4"\n[other]\nx = 1\n\n\n'
    p = _cfg(tmp_path, existing)
    assert CodexWirer(config_path=p).wire().status == "wired"
    # byte comparison against the 0.6.2 output
    assert p.read_text() == (existing.rstrip("\n") + "\n"
                             + f'\n[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"\n')
    q = tmp_path / "fresh" / "config.toml"
    assert CodexWirer(config_path=q).wire().status == "wired"
    assert q.read_text() == f'\n[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"\n'


def test_current_state_stale_absolute_path_is_not_wired(tmp_path):
    p = _cfg(tmp_path, STALE)
    assert CodexWirer(config_path=p).current_state()["wired_with_sibyl"] is False


def test_current_state_bare_name_still_wired(tmp_path):
    p = _cfg(tmp_path, '[mcp_servers.sibyl_memory]\ncommand = "sibyl-memory-mcp"\n')
    assert CodexWirer(config_path=p).current_state()["wired_with_sibyl"] is True
    assert CodexWirer(config_path=p).wire().status == "already"


def test_header_inside_comment_is_absent(tmp_path, _isolated_path):
    text = '# [mcp_servers.sibyl_memory]\n# command = "x"\nmodel = "o4"\n'
    p = _cfg(tmp_path, text)
    w = CodexWirer(config_path=p)
    assert w.current_state()["wired_with_sibyl"] is False
    out = w.wire()
    assert out.status == "wired" and "Added" in out.message
    assert p.read_text().startswith(text)


def test_inline_table_form_is_unreadable_and_never_appended(tmp_path):
    text = 'mcp_servers.sibyl_memory = { command = "/x/sibyl-memory-mcp" }\n'
    p = _cfg(tmp_path, text)
    for force in (False, True):
        out = CodexWirer(config_path=p).wire(force=force)
        assert out.status == "error" and "form setup does not edit" in out.message
        assert p.read_text() == text
    assert _backups() == []


def test_two_headers_never_edited(tmp_path):
    text = ('[mcp_servers.sibyl_memory]\ncommand = "/a/sibyl-memory-mcp"\n'
            '[mcp_servers.sibyl_memory]\ncommand = "/b/sibyl-memory-mcp"\n')
    p = _cfg(tmp_path, text)
    assert CodexWirer(config_path=p).wire(force=True).status == "error"
    assert p.read_text() == text


def test_table_without_command_is_broken(tmp_path, _isolated_path):
    # setup inserts the command; doctor reports it as broken (see the doctor tests)
    p = _cfg(tmp_path, "[mcp_servers.sibyl_memory]\n")
    out = CodexWirer(config_path=p).wire(force=True)
    assert out.status == "wired"
    assert p.read_text() == f'[mcp_servers.sibyl_memory]\ncommand = "{_isolated_path}"\n'


def test_foreign_command_under_our_table_needs_force(tmp_path, _isolated_path):
    text = '[mcp_servers.sibyl_memory]\ncommand = "/gone/other-server"\n'
    p = _cfg(tmp_path, text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "skipped" and out.overwrite_of
    assert p.read_text() == text
    assert CodexWirer(config_path=p).wire(force=True).status == "wired"


def test_unparsed_command_value_never_edited(tmp_path):
    text = '[mcp_servers.sibyl_memory]\ncommand = """/x/sibyl-memory-mcp"""\n'
    p = _cfg(tmp_path, text)
    assert CodexWirer(config_path=p).wire(force=True).status == "error"
    assert p.read_text() == text


def test_probe_reads_basic_and_literal_strings():
    assert S._toml_string_value('"C:\\\\bin\\\\sibyl-memory-mcp.exe"  # c') == (
        "C:\\bin\\sibyl-memory-mcp.exe", "  # c")
    assert S._toml_string_value("'C:\\bin\\sibyl-memory-mcp.exe'") == (
        "C:\\bin\\sibyl-memory-mcp.exe", "")
    assert S._toml_string_value('"a \\"q\\" b"') == ('a "q" b', "")
    assert S._toml_string_value('"bad \\n escape"') is None
    assert S._toml_string_value('"unterminated') is None
    assert S._toml_string_value('"x" trailing') is None
    assert S._toml_string_value("['a']") is None


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib verification needs Python 3.11+")
def test_invalid_toml_never_modified(tmp_path):
    text = ('[mcp_servers.sibyl_memory]\ncommand = "/old/sibyl-memory-mcp"\n'
            '[broken\nx = = 1\n')
    p = _cfg(tmp_path, text)
    out = CodexWirer(config_path=p).wire(force=True)
    assert out.status == "error" and "not valid TOML" in out.message
    assert p.read_text() == text and _backups() == []


# --- Cursor (4.4): a stale path of ours is updated, and the merge keeps keys ----

def test_cursor_stale_own_entry_updated_without_prompt_keeps_env_and_args(tmp_path, _isolated_path):
    p = tmp_path / ".cursor" / "mcp.json"
    p.parent.mkdir()
    entry = {"type": "stdio", "command": "/old/venv/bin/sibyl-memory-mcp",
             "env": {"SIBYL_MEMORY_DB": "/data/m.db"}, "args": ["--x"], "note": 1}
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": entry, "other": {"command": "npx"}}}))
    w = CursorWirer(config_path=p)
    assert w.current_state()["wired_with_sibyl"] is False

    def no_prompt(*a, **k):
        raise AssertionError("our own stale entry must not prompt")

    out = w.wire(prompt_fn=no_prompt)
    assert out.status == "wired" and "Updated the sibyl-memory command" in out.message
    data = json.loads(p.read_text())
    assert data["mcpServers"]["sibyl-memory"] == {**entry, "command": str(_isolated_path)}
    assert data["mcpServers"]["other"] == {"command": "npx"}


def test_cursor_foreign_entry_replaced_whole_only_with_force(tmp_path, _isolated_path):
    p = tmp_path / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"mcpServers": {"sibyl-memory": {"command": "/opt/other", "env": {"A": "1"}}}}))
    out = CursorWirer(config_path=p).wire()
    assert out.status == "skipped" and out.overwrite_of
    assert CursorWirer(config_path=p).wire(force=True).status == "wired"
    assert json.loads(p.read_text())["mcpServers"]["sibyl-memory"] == {
        "type": "stdio", "command": str(_isolated_path)}
