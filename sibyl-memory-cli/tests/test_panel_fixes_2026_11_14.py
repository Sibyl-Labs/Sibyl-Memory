"""Regression tests for the 2026-11-14 review panel fixes (PANEL-FINDINGS.md
findings 1 to 8, 10, 15 and the tomli fallback). Temp HOME and an isolated
PATH only; nothing here runs a harness binary or reaches the network."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from sibyl_memory_cli import cli
from sibyl_memory_cli import doctor as D
from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import CodexWirer, DeepSeekWirer, HermesWirer

from _harness_fixtures import backups_dir, isolate_path, leftovers

REAL_TOML = S._tomllib
FIX = Path(__file__).resolve().parent / "fixtures" / "deepseek"


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    return fake


def _stub_plugin(self):
    d = Path(self.hermes_home) / "plugins" / "sibyl"
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("# stub\n")


@pytest.fixture(params=["parser", "no-parser"])
def toml_mode(request, monkeypatch):
    if request.param == "parser":
        if REAL_TOML is None:
            pytest.skip("no TOML parser in this environment")
    else:
        monkeypatch.setattr(S, "_tomllib", None)
    return request.param


def _toml(text: str):
    if REAL_TOML is None:
        pytest.skip("no TOML parser to check the result")
    return REAL_TOML.loads(text)


# =========================================================================
# tomli fallback
# =========================================================================

def test_toml_parser_available_on_every_supported_python():
    assert S._tomllib is not None
    want = "tomllib" if sys.version_info >= (3, 11) else "tomli"
    assert S._tomllib.__name__ == want


def test_pyproject_declares_tomli_below_311():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "'tomli>=2; python_version < \"3.11\"'" in text


# =========================================================================
# 1. Hermes config.yaml is edited as text
# =========================================================================

HERMES_CFG = """\
# Hermes config (hand-written)
model: &m gpt-x   # the default model
fallback: *m
prompt: "caf\u00e9 au lait"
memory:
  # which memory provider to use
  provider: honcho
  options: {a: 1}
tools:
  - web
"""


def _hermes(tmp_path, monkeypatch, text, *, plugin=True):
    h = tmp_path / "hermes-home"
    h.mkdir(exist_ok=True)
    cfg = h / "config.yaml"
    if text is not None:
        cfg.write_bytes(text.encode("utf-8"))
    if plugin:
        monkeypatch.setattr(HermesWirer, "_install_plugin", _stub_plugin)
    return HermesWirer(hermes_home=h), cfg


def test_hermes_edit_keeps_comments_anchors_and_unicode(tmp_path, monkeypatch):
    w, cfg = _hermes(tmp_path, monkeypatch, HERMES_CFG)
    out = w.wire(force=True)
    assert out.status == "wired", out.message
    new = cfg.read_text(encoding="utf-8")
    assert new == HERMES_CFG.replace("provider: honcho", "provider: sibyl")
    want = yaml.safe_load(HERMES_CFG)
    want["memory"]["provider"] = "sibyl"
    assert yaml.safe_load(new) == want


@pytest.mark.parametrize("text,expected", [
    ("model: x\n", "model: x\nmemory:\n  provider: sibyl\n"),
    ("model: x", "model: x\nmemory:\n  provider: sibyl\n"),
    ("# only a comment\n", "# only a comment\nmemory:\n  provider: sibyl\n"),
    ("", "memory:\n  provider: sibyl\n"),
    ("memory:\n  # c\n  other: 1\nz: 2\n", "memory:\n  # c\n  provider: sibyl\n  other: 1\nz: 2\n"),
    ("memory:   # c\nz: 2\n", "memory: {provider: sibyl}   # c\nz: 2\n"),
    ("memory: {}\n", "memory: {provider: sibyl}\n"),
    ("a: 1\r\nmemory:\r\n  provider: 'x'\r\n", "a: 1\r\nmemory:\r\n  provider: sibyl\r\n"),
    ("a: 1\r\n", "a: 1\r\nmemory:\r\n  provider: sibyl\r\n"),
])
def test_hermes_text_edit_cases(text, expected):
    assert S._hermes_provider_text(text) == expected


def test_hermes_scalar_memory_value_goes_through_the_overwrite_gate(tmp_path, monkeypatch):
    for scalar in ("honcho", "true"):
        text = f"model: x\nmemory: {scalar}\n"
        w, cfg = _hermes(tmp_path, monkeypatch, text)
        out = w.wire()
        assert out.status == "skipped" and out.overwrite_of, out.message
        assert cfg.read_text() == text
        out = w.wire(force=True)
        assert out.status == "wired"
        assert cfg.read_text() == "model: x\nmemory: {provider: sibyl}\n"


def test_hermes_unprovable_edit_gives_manual_lines_and_leaves_the_file(tmp_path, monkeypatch):
    text = "memory: {provider: honcho, x: 1}   # flow form\nmodel: x\n"
    w, cfg = _hermes(tmp_path, monkeypatch, text)
    out = w.wire(force=True)
    assert out.status == "error"
    assert out.manual_block == "memory:\n  provider: sibyl"
    assert yaml.safe_load(out.manual_block) == {"memory": {"provider": "sibyl"}}
    assert out.message.endswith(out.manual_block) and out.handoff[0] == "done"
    assert cfg.read_text() == text


def test_hermes_proof_failure_never_falls_back_to_safe_dump(tmp_path, monkeypatch):
    w, cfg = _hermes(tmp_path, monkeypatch, HERMES_CFG)
    real = yaml.safe_load
    calls = {"n": 0}

    def lying_load(t):
        calls["n"] += 1
        got = real(t)
        if isinstance(got, dict) and isinstance(got.get("memory"), dict) \
                and got["memory"].get("provider") == "sibyl":
            got["extra"] = 1           # the proof sees a change it did not ask for
        return got

    monkeypatch.setattr(yaml, "safe_load", lying_load)
    monkeypatch.setattr(yaml, "safe_dump", lambda *a, **k: pytest.fail("safe_dump called"))
    out = w.wire(force=True)
    assert out.status == "error" and out.manual_block
    assert cfg.read_bytes() == HERMES_CFG.encode("utf-8")


def test_hermes_symlinked_config_is_written_through(tmp_path, monkeypatch):
    dot = tmp_path / "dotfiles"
    dot.mkdir()
    real = dot / "config.yaml"
    real.write_text("model: x\n")
    w, cfg = _hermes(tmp_path, monkeypatch, None)
    cfg.symlink_to(real)
    assert w.wire().status == "wired"
    assert cfg.is_symlink() and os.readlink(cfg) == str(real)
    assert real.read_text() == "model: x\nmemory:\n  provider: sibyl\n"
    assert leftovers(dot) == [] and leftovers(cfg.parent) == []


# =========================================================================
# 2. Codex headers are found structurally
# =========================================================================

OURS_PATH = None   # resolved per test (the isolated fake binary)

FOREIGN_THEN_PROJECTS = """\
model = "o4"

[mcp_servers.sibyl_memory]
command = "npx"
args = ["-y", "other-server"]

[projects."/home/u/repo"]
trust_level = "trusted"

[projects.'C:\\x\\y']
trust_level = "trusted"

[mcp_servers."a@b"]
command = "a-b-mcp"
"""


def test_codex_force_replace_keeps_quoted_project_tables(tmp_path, toml_mode):
    p = tmp_path / "config.toml"
    p.write_text(FOREIGN_THEN_PROJECTS)
    w = CodexWirer(config_path=p)
    out = w.wire(force=True)
    assert out.status == "wired", out.message
    new = p.read_text()
    for kept in ('[projects."/home/u/repo"]', "[projects.'C:\\x\\y']", '[mcp_servers."a@b"]',
                 'command = "a-b-mcp"', 'model = "o4"'):
        assert kept in new, kept
    doc = _toml(new)
    assert doc["mcp_servers"]["sibyl_memory"] == {"command": S.mcp_command_path()}
    assert doc["projects"]["/home/u/repo"] == {"trust_level": "trusted"}
    assert doc["mcp_servers"]["a@b"] == {"command": "a-b-mcp"}


def test_codex_commandless_table_never_takes_the_next_servers_command(tmp_path, toml_mode):
    text = ('[mcp_servers.sibyl_memory]\nargs = []\n\n'
            '[mcp_servers."ctx@upstash"]\ncommand = "npx"\nargs = ["-y", "ctx"]\n')
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "wired", out.message
    doc = _toml(p.read_text())
    assert doc["mcp_servers"]["ctx@upstash"]["command"] == "npx"
    assert doc["mcp_servers"]["sibyl_memory"]["command"] == S.mcp_command_path()


def test_codex_header_inside_a_multiline_string_is_not_ours(tmp_path, toml_mode):
    text = 'notes = """\n[mcp_servers.sibyl_memory]\ncommand = "/old/sibyl-memory-mcp"\n"""\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    assert S._codex_scan(text)["headers"] == []
    w = CodexWirer(config_path=p)
    for force in (False, True):
        w.wire(force=force)
        assert p.read_text() == text
    assert w.doctor_probe()[0]["entry"] != "present"


def test_codex_table_replace_proof_checks_kept_lines_without_a_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_tomllib", None)
    w = CodexWirer(config_path=tmp_path / "config.toml")
    old = FOREIGN_THEN_PROJECTS
    good = w._replace_table(old)
    w._verify_table_replace(old, good)
    damaged = good.replace('[projects."/home/u/repo"]\ntrust_level = "trusted"\n', "")
    with pytest.raises(ValueError):
        w._verify_table_replace(old, damaged)


# =========================================================================
# 3. Codex append path is proven
# =========================================================================

def test_codex_append_refuses_inline_mcp_servers(tmp_path, toml_mode):
    text = 'mcp_servers = { github = { command = "gh-mcp" } }\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "error" and out.handoff[0] == "done"
    assert p.read_text() == text and backups_dir("codex").exists() is False
    entry = "{ " + out.manual_block.split("{", 1)[1]
    assert out.manual_block.startswith("sibyl_memory = {")
    assert _toml("x = " + entry)["x"] == {"command": S.mcp_command_path()}


def test_codex_append_refuses_a_file_ending_in_an_open_string(tmp_path, toml_mode):
    text = 'model = "o4"\nnote = """\nnever closed\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "error"
    assert p.read_text() == text


def test_codex_append_keeps_crlf(tmp_path, toml_mode):
    text = 'model = "o4"\r\n[other]\r\nx = 1\r\n'
    p = tmp_path / "config.toml"
    p.write_bytes(text.encode())
    out = CodexWirer(config_path=p).wire()
    assert out.status == "wired", out.message
    data = p.read_bytes().decode()
    assert "\n" not in data.replace("\r\n", "")
    assert data.startswith(text)
    _toml(data)


def test_codex_doctor_does_not_pass_a_file_codex_cannot_parse(tmp_path, toml_mode):
    # What an earlier setup appended to an inline mcp_servers table.
    text = ('mcp_servers = { github = { command = "gh-mcp" } }\n\n'
            f'[mcp_servers.sibyl_memory]\ncommand = "{S.mcp_command_path()}"\n')
    p = tmp_path / "config.toml"
    p.write_text(text)
    w = CodexWirer(config_path=p)
    assert w.doctor_probe()[0]["entry"] == "unreadable"
    assert w.current_state()["wired_with_sibyl"] is False
    w.wire(force=True)
    assert p.read_text() == text


def test_codex_setup_refuses_a_file_the_parser_rejects(tmp_path):
    if REAL_TOML is None:
        pytest.skip("needs a TOML parser")
    text = 'model = \n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    out = CodexWirer(config_path=p).wire()
    assert out.status == "error" and "not valid TOML" in out.message
    assert p.read_text() == text


# =========================================================================
# 4. Hermes installer refusal is honored (unstubbed installer)
# =========================================================================

def test_hermes_installer_refusal_stops_setup_before_the_config(tmp_path, monkeypatch):
    h = tmp_path / "hermes-home"
    (h / "plugins" / "sibyl" / "__pycache__").mkdir(parents=True)
    cfg = h / "config.yaml"
    text = "memory:\n  provider: honcho\n"
    cfg.write_text(text)
    w = HermesWirer(hermes_home=h)
    with contextlib.redirect_stdout(io.StringIO()):
        out = w.wire(force=True)
    assert out.status == "error" and "refused (code 2)" in out.message
    assert out.handoff[0] == "done" and "install-plugin --force" in out.handoff[1]
    assert cfg.read_text() == text
    assert not (h / "plugins" / "sibyl" / "__init__.py").exists()


def test_hermes_doctor_repair_reports_the_refusal(tmp_path):
    h = tmp_path / "hermes-home"
    (h / "plugins" / "sibyl" / "__pycache__").mkdir(parents=True)
    (h / "config.yaml").write_text("memory:\n  provider: sibyl\n")
    w = HermesWirer(hermes_home=h)
    probe = w.doctor_probe()[0]
    assert probe.get("broken")
    with contextlib.redirect_stdout(io.StringIO()):
        out = w.repair_mcp(probe)
    assert out.status == "error" and "Installed" not in out.message


# =========================================================================
# 5. _write_via_tmp
# =========================================================================

def test_write_via_tmp_mode_is_set_before_content_and_data_is_synced(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("old\n")
    target.chmod(0o600)
    old_umask = os.umask(0o022)
    seen = []
    real_fchmod, real_chmod, real_fsync, real_replace = os.fchmod, os.chmod, os.fsync, os.replace

    def fchmod(fd, mode):
        seen.append(("fchmod", os.fstat(fd).st_size, mode))
        return real_fchmod(fd, mode)

    def chmod(path, mode, *a, **k):
        seen.append(("chmod", os.stat(path).st_size, mode))
        return real_chmod(path, mode, *a, **k)

    def fsync(fd):
        seen.append(("fsync",))
        return real_fsync(fd)

    def replace(src, dst):
        st = os.stat(src)
        seen.append(("replace", st.st_size, stat.S_IMODE(st.st_mode)))
        return real_replace(src, dst)

    monkeypatch.setattr(S.os, "fchmod", fchmod)
    monkeypatch.setattr(S.os, "chmod", chmod)
    monkeypatch.setattr(S.os, "fsync", fsync)
    monkeypatch.setattr(S.os, "replace", replace)
    try:
        S._write_via_tmp(target, target.with_suffix(".toml.tmp"), "secret = 1\n")
    finally:
        os.umask(old_umask)
    modes = [e for e in seen if e[0] in ("fchmod", "chmod")]
    assert modes and all(size == 0 for _k, size, _m in modes), seen
    rep = [e for e in seen if e[0] == "replace"][0]
    assert rep[1] > 0 and rep[2] == 0o600
    assert sum(1 for e in seen if e[0] == "fsync") >= 2       # the file and the directory
    assert target.read_text() == "secret = 1\n" and stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_via_tmp_never_follows_a_preplaced_fixed_name(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    target = tmp_path / "mcp.json"
    target.write_text("{}\n")
    (tmp_path / "mcp.json.tmp").symlink_to(victim)
    S._write_via_tmp(target, target.with_suffix(".json.tmp"), '{"a": 1}\n')
    assert victim.read_text() == "keep me\n"
    assert target.read_text() == '{"a": 1}\n'
    assert (tmp_path / "mcp.json.tmp").is_symlink()           # another file is never removed


def test_write_via_tmp_failure_removes_only_its_own_temp(tmp_path, monkeypatch):
    tmp_path = tmp_path / "harness"
    tmp_path.mkdir()
    target = tmp_path / "config.yaml"
    target.write_text("a: 1\n")
    other = tmp_path / "config.yaml.tmp"
    other.write_text("another run\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S._write_via_tmp(target, target.with_suffix(".yaml.tmp"), "a: 2\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml", "config.yaml.tmp"]
    assert other.read_text() == "another run\n" and target.read_text() == "a: 1\n"


def test_write_via_tmp_new_file_follows_umask(tmp_path):
    old = os.umask(0o077)
    try:
        S._write_via_tmp(tmp_path / "new.json", tmp_path / "new.json.tmp", "{}\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "new.json").stat().st_mode) == 0o600


# =========================================================================
# 6. CLI bearer host gate
# =========================================================================

class _Resp:
    def __init__(self, body: bytes = b'{"ok": true}'):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def urlopen_spy(monkeypatch):
    calls = []

    def fake(req, timeout=None, context=None):
        calls.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake)
    return calls


@pytest.mark.parametrize("base", ["http://evil.example", "https://evil.example",
                                  "http://api.sibyllabs.org", "ftp://127.0.0.1"])
def test_bearer_is_not_sent_to_an_untrusted_base(monkeypatch, urlopen_spy, capsys, base):
    monkeypatch.setattr(cli, "API_BASE", base)
    for kw in ({"headers": {"Authorization": "Bearer t0k"}},
               {"headers": {"X-Sibyl-Entitlement": "e"}},
               {"body": {"account_id": "a", "session_token": "t0k"}}):
        with pytest.raises(cli.HttpError) as ei:
            cli.http_request("POST", "/api/plugin/access", **kw)
        assert ei.value.code == cli.UNTRUSTED_BASE_CODE
    assert urlopen_spy == []
    err = capsys.readouterr().err
    assert "SIBYL_API_BASE" in err and "t0k" not in err


@pytest.mark.parametrize("base", ["https://api.sibyllabs.org", "http://127.0.0.1:8787",
                                  "http://localhost:9"])
def test_bearer_goes_to_the_api_or_loopback(monkeypatch, urlopen_spy, base):
    monkeypatch.setattr(cli, "API_BASE", base)
    assert cli.http_request("POST", "/x", headers={"Authorization": "Bearer t"}) == {"ok": True}
    assert urlopen_spy == [f"{base}/x"]


def test_unauthenticated_call_is_not_gated(monkeypatch, urlopen_spy):
    monkeypatch.setattr(cli, "API_BASE", "http://staging.example")
    cli.http_request("POST", "/api/plugin/session-init", body={"device": "x"})
    assert urlopen_spy == ["http://staging.example/api/plugin/session-init"]


def test_legacy_upgrade_link_never_carries_the_bearer_to_another_host(monkeypatch):
    monkeypatch.setattr(cli, "UPGRADE_BASE", "http://evil.example/up")
    assert cli._legacy_upgrade_url("t0k") is None
    monkeypatch.setattr(cli, "UPGRADE_BASE", "https://sibyllabs.org/plugin/upgrade")
    assert cli._legacy_upgrade_url("t0k") == "https://sibyllabs.org/plugin/upgrade?session=t0k"


# =========================================================================
# 7. Quoted "command" key
# =========================================================================

@pytest.mark.parametrize("key", ['"command"', "'command'"])
def test_codex_quoted_command_key_is_updated_in_place(tmp_path, toml_mode, key):
    text = f'[mcp_servers.sibyl_memory]\n{key} = "/old/venv/bin/sibyl-memory-mcp"\n'
    p = tmp_path / "config.toml"
    p.write_text(text)
    w = CodexWirer(config_path=p)
    probe = w.doctor_probe()[0]
    assert probe["entry"] == "present" and probe["command"] == "/old/venv/bin/sibyl-memory-mcp"
    out = w.repair_mcp(probe)
    assert out.status == "wired", out.message
    new = p.read_text()
    assert new == f'[mcp_servers.sibyl_memory]\n{key} = "{S.mcp_command_path()}"\n'
    assert _toml(new)["mcp_servers"]["sibyl_memory"] == {"command": S.mcp_command_path()}


def test_codex_missing_command_is_not_fixable_without_a_parser(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("[mcp_servers.sibyl_memory]\nargs = []\n")
    w = CodexWirer(config_path=p)
    if REAL_TOML is not None:
        assert w.doctor_probe()[0]["fixable"] is True
    monkeypatch.setattr(S, "_tomllib", None)
    assert w.doctor_probe()[0]["fixable"] is False


# =========================================================================
# 8. DeepSeek Harness manual fallback
# =========================================================================

def _dsh_home(tmp_path):
    home = Path.home() / ".dsh"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _parse_printed_block(block: str):
    """Every printed block is YAML. A plain block parses with yaml.safe_load;
    one that holds DeepSeek Harness's own !!js tag (which safe_load does not
    construct) must compose with the stock SafeLoader and load with the
    SafeLoader subclass that only adds that tag."""
    yaml.compose(block, Loader=yaml.SafeLoader)
    if "!!js" in block:
        return S._dsh_yaml_load(block)
    return yaml.safe_load(block)


def test_deepseek_command_fallback_names_the_existing_row(tmp_path, monkeypatch):
    home = _dsh_home(tmp_path)
    hp = home / "cordis.patch.yml"
    old = (FIX / "ours_stale.yml").read_bytes()
    hp.write_bytes(old)
    monkeypatch.setattr(DeepSeekWirer, "_plan_text", lambda self, scan, ch: None)
    w = DeepSeekWirer()
    out = w.wire()
    assert out.status == "error"
    assert "Change the command of the existing sibyl-memory row" in out.message
    assert "- insert:" not in out.message and "Add this block" not in out.message
    assert _parse_printed_block(out.manual_block) == {"command": S.mcp_command_path()}
    assert "do not add a second row" in out.handoff[1]
    assert hp.read_bytes() == old


def test_deepseek_doctor_keeps_the_block_lines(tmp_path, monkeypatch, capsys):
    home = _dsh_home(tmp_path)
    hp = home / "cordis.patch.yml"
    hp.write_bytes((FIX / "ours_stale.yml").read_bytes())
    monkeypatch.setattr(DeepSeekWirer, "_plan_text", lambda self, scan, ch: None)
    w = DeepSeekWirer()
    probe = w.doctor_probe()[0]
    check = D.check_harness(w, probe, verified={})
    new, out = D.repair_check(w, probe, check, verified={})
    assert new.fix_block == out.manual_block and "\n" not in new.fix_message
    line = D._check_line(new)
    assert line.endswith("\n" + out.manual_block)
    assert _parse_printed_block(line.split("\n", 1)[1]) == {"command": S.mcp_command_path()}
    data = json.loads(json.dumps(D.asdict(new)))
    assert _parse_printed_block(data["fix_block"]) == {"command": S.mcp_command_path()}


@pytest.mark.parametrize("mode", ["add", "command", "replace"])
def test_every_deepseek_manual_block_is_valid_yaml(tmp_path, mode):
    w = DeepSeekWirer(dsh_home=_dsh_home(tmp_path))
    out = w._manual_block_error(Path("/p/cordis.patch.yml"), "lead.", mode=mode,
                                env={"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "4000"})
    got = _parse_printed_block(out.manual_block)
    cmd = S.mcp_command_path()
    if mode == "command":
        assert got == {"command": cmd}
    elif mode == "replace":
        assert got == [w._expected_row(cmd, {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "4000"})]
    else:
        assert got == [{"insert": [w._expected_row(cmd, {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "4000"})]}]
    assert out.message.endswith("\n" + out.manual_block)


def test_codex_and_hermes_manual_blocks_parse(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("model = 1\n")
    out = CodexWirer(config_path=p)._manual_table_error("reason.")
    assert _toml(out.manual_block)["mcp_servers"]["sibyl_memory"] == {"command": S.mcp_command_path()}
    assert yaml.safe_load("\n".join(S.HERMES_MANUAL_LINES)) == {"memory": {"provider": "sibyl"}}


# =========================================================================
# 10. Claude Code without the claude CLI is not reported as wired
# =========================================================================

def test_claude_without_cli_is_not_wired_and_hands_over_the_command(tmp_path, monkeypatch, capsys):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}\n")
    ns = cli.build_parser().parse_args(["setup", "claude-code", "--yes",
                                        "--claude-settings", str(settings)])
    rc = S.cmd_setup(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "MCP server is ready" not in out
    assert "next: claude mcp add --scope user sibyl-memory -- " in out
    assert "mcpServers" not in json.loads(settings.read_text())


# =========================================================================
# 15. Codex "already wired" needs a command of ours
# =========================================================================

def test_codex_commandless_table_is_not_already_wired(tmp_path, toml_mode):
    p = tmp_path / "config.toml"
    p.write_text("[mcp_servers.sibyl_memory]\nargs = []\n")
    w = CodexWirer(config_path=p)
    assert w.current_state()["wired_with_sibyl"] is False
    out = w.wire()
    assert out.status == "wired", out.message
    assert p.read_text() == (f'[mcp_servers.sibyl_memory]\ncommand = "{S.mcp_command_path()}"\n'
                             "args = []\n")


def test_codex_foreign_table_is_not_wired_for_other_callers(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[mcp_servers.sibyl_memory]\ncommand = "npx"\n')
    assert CodexWirer(config_path=p).current_state()["wired_with_sibyl"] is False


# =========================================================================
# 32. Verification layers that no test fed a failing plan before
# =========================================================================

def _dsh_patch(fixture: str) -> tuple:
    home = Path.home() / ".dsh"
    home.mkdir(parents=True, exist_ok=True)
    hp = home / "cordis.patch.yml"
    data = (FIX / fixture).read_bytes()
    hp.write_bytes(data)
    return hp, data


_REAL_ROW_LINES = DeepSeekWirer._row_lines


def _row_with(self_, command, env, **override):
    rows = _REAL_ROW_LINES(self_, command, env)
    for i, r in enumerate(rows):
        for k, v in override.items():
            if r.lstrip().startswith(k + ":"):
                rows[i] = r[: len(r) - len(r.lstrip())] + f"{k}: {v}"
    return rows


def test_deepseek_case_d_verification_rejects_a_row_that_parses_differently(monkeypatch):
    # The row stays a candidate of ours with the right command and a !!js cwd,
    # so only the "got == old + [insert]" proof can catch the extra arg.
    hp, old = _dsh_patch("user_patches.yml")
    monkeypatch.setattr(DeepSeekWirer, "_row_lines",
                        lambda self, c, e: _row_with(self, c, e, args="[--evil]"))
    out = DeepSeekWirer().wire()
    assert out.status == "error" and out.manual_block
    assert hp.read_bytes() == old


def test_deepseek_cwd_must_stay_a_js_expression(monkeypatch):
    # A plain string "process.cwd()" equals the _JsExpr in ==, so only the
    # type check on cwd catches it.
    hp, old = _dsh_patch("user_patches.yml")
    monkeypatch.setattr(DeepSeekWirer, "_row_lines",
                        lambda self, c, e: _row_with(self, c, e, cwd="'process.cwd()'"))
    out = DeepSeekWirer().wire()
    assert out.status == "error" and out.manual_block
    assert hp.read_bytes() == old


def test_deepseek_rescan_candidate_count_is_checked(monkeypatch):
    hp, old = _dsh_patch("ours_stale.yml")
    real = DeepSeekWirer._scan_text
    new_cmd = S.mcp_command_path()

    def scan(self, text):
        out = real(self, text)
        if new_cmd in text and out.get("candidates"):
            extra = dict(out["candidates"][0])
            extra["path"] = ["phantom"]
            out["candidates"] = out["candidates"] + [extra]
        return out

    monkeypatch.setattr(DeepSeekWirer, "_scan_text", scan)
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "Change the command of the existing" in out.message
    assert hp.read_bytes() == old


def test_atomic_text_refuses_a_symlinked_target(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("mine\n")
    link = tmp_path / "AGENTS.md"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        S._atomic_text(link, "replaced\n")
    assert link.is_symlink() and real.read_text() == "mine\n"
