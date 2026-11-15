"""`sibyl setup deepseek` (spec dsh-doctor v2, section 3): the DeepSeekWirer.

Temp HOME and an isolated PATH only. Nothing here runs DeepSeek Harness, node,
npm or any binary named dsh; a planted `dsh` on PATH proves it is never run.
Patch files are edited as text and every write is verified by parsing."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import setup as S
from sibyl_memory_cli.setup import ALL_WIRERS, DeepSeekWirer

from _harness_fixtures import backups_dir, isolate_path, leftovers

FIX = Path(__file__).resolve().parent / "fixtures" / "deepseek"
TEMPLATE = (FIX / "template.yml").read_text()


@pytest.fixture(autouse=True)
def _isolated_path(monkeypatch, tmp_path):
    fake = isolate_path(monkeypatch, tmp_path)
    monkeypatch.setattr(S, "_verify_mcp_starts", lambda b: (True, "MCP server verified (stub)"))
    return fake


@pytest.fixture
def home() -> Path:
    return Path.home() / ".dsh"


def _put(path: Path, fixture: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (FIX / fixture).read_bytes()
    path.write_bytes(data)
    return data


def _profile(home: Path, name: str) -> Path:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    return d / "cordis.patch.yml"


def _load(path: Path):
    return S._dsh_yaml_load(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list:
    out = []
    for patch in _load(path) or []:
        if isinstance(patch, dict) and isinstance(patch.get("insert"), list):
            out.extend(patch["insert"])
    return out


def _backups(sub: str = "") -> list:
    d = backups_dir("deepseek", sub)
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def _no_leftovers(*dirs):
    for d in dirs:
        assert leftovers(d) == [], d


def _expected(cmd, env=None):
    return DeepSeekWirer()._expected_row(str(cmd), env)


def _setup(argv, capsys=None):
    ns = cli.build_parser().parse_args(["setup"] + argv)
    rc = S.cmd_setup(ns)
    out = capsys.readouterr().out if capsys else ""
    return rc, out


# --- detection and names ---------------------------------------------------

def test_registered_as_deepseek_and_autodetected():
    assert ALL_WIRERS["deepseek"] is DeepSeekWirer
    assert list(ALL_WIRERS)[-1] == "deepseek"
    w = DeepSeekWirer()
    assert w.name == "deepseek" and w.display_name == "DeepSeek Harness" and w.initial == "d"
    assert "deepseek" in S.SETUP_TARGETS and "dsh" in S.SETUP_TARGETS


def test_dsh_alias_resolves_to_deepseek(home, capsys, _isolated_path):
    rc, out = _setup(["dsh", "--yes"], capsys)
    assert rc == 0
    assert "deepseek: Added the SIBYL Memory MCP server" in out
    assert _rows(home / "cordis.patch.yml") == [_expected(_isolated_path)]


def test_home_resolution_env_blank_and_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("DSH_HOME", "   ")
    assert DeepSeekWirer().dsh_home == Path.home() / ".dsh"
    monkeypatch.setenv("DSH_HOME", "~/custom-dsh")
    assert DeepSeekWirer().dsh_home == Path.home() / "custom-dsh"
    monkeypatch.setenv("DSH_HOME", "rel/dsh")
    assert DeepSeekWirer().dsh_home == Path(os.path.abspath("rel/dsh"))
    assert DeepSeekWirer(dsh_home=tmp_path / "x").dsh_home == tmp_path / "x"
    monkeypatch.delenv("DSH_HOME")
    assert DeepSeekWirer().dsh_home == Path.home() / ".dsh"


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", ".", "..", "node_modules"])
def test_invalid_profile_names_rejected(bad):
    with pytest.raises(ValueError):
        DeepSeekWirer(profile=bad)


def test_not_detected_when_home_missing(home):
    assert not home.exists()
    assert DeepSeekWirer().is_present() is False


def test_not_detected_with_debian_dsh_only_home(home):
    shutil.copytree(FIX / "debian_dsh_home", home)
    assert DeepSeekWirer().is_present() is False


def test_not_detected_by_dsh_binary_on_path_and_never_executed(home, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "dsh-was-run"
    bindir = tmp_path / "isolated-bin"
    dsh = bindir / "dsh"
    dsh.write_text(f"#!/bin/sh\ntouch {marker}\n")
    dsh.chmod(0o755)
    assert DeepSeekWirer().is_present() is False
    _setup(["--yes"], capsys)
    DeepSeekWirer().current_state()
    _setup(["deepseek", "--dry-run", "--yes"], capsys)
    assert not marker.exists()


def test_detected_by_profiles_dir(home):
    (home / "profiles").mkdir(parents=True)
    assert DeepSeekWirer().is_present() is True


def test_detected_by_home_patch_file(home):
    home.mkdir()
    (home / "cordis.patch.yml").write_text("[]\n")
    assert DeepSeekWirer().is_present() is True


def test_autodetect_never_creates_dsh_home(home, capsys):
    _setup(["--yes"], capsys)
    assert not home.exists()


# --- explicit setup and the home gate ---------------------------------------

def test_explicit_setup_missing_home_creates_it(home, _isolated_path):
    out = DeepSeekWirer().wire()
    assert out.status == "wired" and out.backup_path is None
    p = home / "cordis.patch.yml"
    assert _load(p) == [{"insert": [_expected(_isolated_path)]}]
    assert p.read_text().startswith("# DeepSeek Harness user patch layer:")
    assert _backups() == []
    _no_leftovers(home)


def test_explicit_setup_empty_home_proceeds(home):
    home.mkdir()
    assert DeepSeekWirer().wire().status == "wired"
    assert (home / "cordis.patch.yml").is_file()


def test_explicit_setup_debian_dsh_home_stops_and_writes_nothing(home):
    shutil.copytree(FIX / "debian_dsh_home", home)
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    out = DeepSeekWirer().wire(force=True)
    assert out.status == "error"
    assert "distributed shell" in out.message and "Nothing was written" in out.message
    assert out.handoff[0] == "sibyl setup deepseek --dsh-home <folder>"
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before
    assert not (Path.home() / ".sibyl-memory" / "backups").exists()


def test_explicit_setup_foreign_home_without_markers_stops(home):
    home.mkdir()
    (home / "notes.txt").write_text("x")
    out = DeepSeekWirer().wire()
    assert out.status == "error"
    assert "does not look like a DeepSeek Harness home" in out.message
    assert "distributed shell" not in out.message
    assert sorted(p.name for p in home.iterdir()) == ["notes.txt"]


def test_explicit_setup_home_is_a_file_stops(home):
    home.write_text("not a dir")
    out = DeepSeekWirer().wire()
    assert out.status == "error" and home.read_text() == "not a dir"


def test_dry_run_creates_nothing(home):
    out = DeepSeekWirer().wire(dry_run=True)
    assert out.status == "dry-run" and "Would add the sibyl-memory row" in out.message
    assert not home.exists()
    assert not (Path.home() / ".sibyl-memory" / "backups").exists()


# --- adds -----------------------------------------------------------------

def test_template_file_replaces_empty_list_only(home, _isolated_path):
    p = _profile(home, "default")
    hp = home / "cordis.patch.yml"
    hp.write_text(TEMPLATE)
    out = DeepSeekWirer().wire()
    assert out.status == "wired"
    new = hp.read_text()
    comments = TEMPLATE[:TEMPLATE.index("[]")]
    assert new.startswith(comments)
    assert "\n[]\n" not in new
    assert _load(hp) == [{"insert": [_expected(_isolated_path)]}]
    assert not p.exists()
    assert len(_backups()) == 1 and _backups()[0].read_text() == TEMPLATE


def test_comment_only_file_appends(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "comment_only.yml").decode()
    assert DeepSeekWirer().wire().status == "wired"
    assert hp.read_text().startswith(old)
    assert _load(hp) == [{"insert": [_expected(_isolated_path)]}]


def test_append_preserves_user_patches_byte_for_byte(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "user_patches.yml").decode()
    before = _load(hp)
    out = DeepSeekWirer().wire()
    assert out.status == "wired"
    new = hp.read_text()
    assert new.startswith(old)
    after = _load(hp)
    assert after[:-1] == before and after[-1] == {"insert": [_expected(_isolated_path)]}
    gh = after[1]["insert"][0]["config"]
    assert type(gh["env"]["GITHUB_TOKEN"]) is S._JsExpr and type(gh["cwd"]) is S._JsExpr
    assert out.backup_path.parent == backups_dir("deepseek")
    assert out.backup_path.read_text() == old
    _no_leftovers(home)


def test_no_trailing_newline_handled(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "no_trailing_newline.yml").decode()
    assert DeepSeekWirer().wire().status == "wired"
    assert hp.read_text().startswith(old + "\n\n# Sibyl Memory")
    assert len(_load(hp)) == 2


def test_crlf_preserved(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "crlf.yml")
    assert DeepSeekWirer().wire().status == "wired"
    raw = hp.read_bytes()
    assert raw.startswith(old)
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert _rows(hp)[-1] == _expected(_isolated_path)


def test_bom_preserved(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    _put(hp, "bom.yml")
    assert DeepSeekWirer().wire().status == "wired"
    raw = hp.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf# with a BOM\n")
    assert raw.count(b"\xef\xbb\xbf") == 1
    assert _rows(hp) == [_expected(_isolated_path)]


def test_command_with_spaces_and_quotes_roundtrips(home, tmp_path, monkeypatch):
    odd = tmp_path / 'dir with "quotes" and \\ back'
    odd.mkdir()
    b = odd / "sibyl-memory-mcp"
    b.write_text("#!/bin/sh\nexit 0\n")
    b.chmod(0o755)
    monkeypatch.setattr(S, "resolve_mcp_binary", lambda: str(b))
    assert DeepSeekWirer().wire().status == "wired"
    assert _rows(home / "cordis.patch.yml")[0]["config"]["command"] == str(b)


def test_pass_env_written_into_row_env_when_set_and_numeric(home, monkeypatch, capsys, _isolated_path):
    monkeypatch.setenv("SIBYL_MEMORY_MCP_OUTPUT_TOKENS", "9000")
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_MIN_TOKENS", "12")
    rc, out = _setup(["deepseek", "--yes"], capsys)
    assert rc == 0
    env = _rows(home / "cordis.patch.yml")[0]["config"]["env"]
    assert env == {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "9000", "SIBYL_MEMORY_RANKED_MIN_TOKENS": "12"}
    assert "kept SIBYL_MEMORY_MCP_OUTPUT_TOKENS in the row's env" in out


def test_pass_env_non_numeric_ignored(home, monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_MCP_OUTPUT_TOKENS", "9000; rm -rf /")
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_MIN_TOKENS", "12345678")
    out = DeepSeekWirer().wire()
    assert out.status == "wired" and not out.lifecycle
    assert _rows(home / "cordis.patch.yml")[0]["config"]["env"] == {}


def test_no_other_env_copied(home, monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_DB", "/data/m.db")
    monkeypatch.setenv("GITHUB_TOKEN", "123")
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    DeepSeekWirer().wire()
    text = (home / "cordis.patch.yml").read_text()
    assert _rows(home / "cordis.patch.yml")[0]["config"]["env"] == {}
    assert "/data/m.db" not in text and "GITHUB_TOKEN" not in text


def _refuses_unchanged(home, fixture):
    hp = home / "cordis.patch.yml"
    old = _put(hp, fixture)
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "could not change" in out.message
    assert "- insert:" in out.message          # the block is printed for manual use
    assert hp.read_bytes() == old and _backups() == []


def test_indented_list_refuses_file_unchanged(home):
    _refuses_unchanged(home, "indented_list.yml")


def test_flow_list_refuses_file_unchanged(home):
    _refuses_unchanged(home, "flow_list.yml")


def _never_modified(home, fixture):
    hp = home / "cordis.patch.yml"
    old = _put(hp, fixture)
    for force in (False, True):
        out = DeepSeekWirer().wire(force=force)
        assert out.status == "error" and "is not a valid DeepSeek Harness patch file" in out.message
    assert hp.read_bytes() == old and _backups() == []


def test_broken_yaml_never_modified(home):
    _never_modified(home, "broken.yml")


def test_not_a_list_never_modified(home):
    _never_modified(home, "not_a_list.yml")


def test_unknown_tag_never_modified(home):
    _never_modified(home, "unknown_tag.yml")


def test_parse_error_never_echoes_file_text(home, capsys):
    hp = home / "cordis.patch.yml"
    _put(hp, "broken_with_fake_value.yml")
    out = DeepSeekWirer().wire()
    assert out.status == "error"
    assert "FAKESECRET123" not in out.message and "GITHUB_TOKEN" not in out.message
    assert "at line 3, column" in out.message
    rc, printed = _setup(["deepseek", "--yes"], capsys)
    assert rc != 0
    assert "FAKESECRET123" not in printed and "GITHUB_TOKEN" not in printed
    st = DeepSeekWirer().current_state()
    assert "FAKESECRET123" not in json.dumps(st)


def test_symlinked_patch_file_refused_and_block_printed(home, tmp_path):
    real = tmp_path / "real.yml"
    real.write_text(TEMPLATE)
    home.mkdir()
    (home / "cordis.patch.yml").symlink_to(real)
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "is a symlink" in out.message and "- insert:" in out.message
    assert real.read_text() == TEMPLATE and (home / "cordis.patch.yml").is_symlink()


def test_file_mode_preserved(home):
    hp = home / "cordis.patch.yml"
    _put(hp, "user_patches.yml")
    os.chmod(hp, 0o600)
    assert DeepSeekWirer().wire().status == "wired"
    assert stat.S_IMODE(hp.stat().st_mode) == 0o600


def test_verification_failure_aborts(home, monkeypatch):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "user_patches.yml")
    real = S._dsh_yaml_load
    calls = {"n": 0}

    def fake(text):
        calls["n"] += 1
        got = real(text)
        if "sibyl-memory-mcp" in text and isinstance(got, list):
            return got[:-1] + [{"insert": [{"id": "wrong"}]}]
        return got

    monkeypatch.setattr(S, "_dsh_yaml_load", fake)
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "could not change" in out.message
    assert hp.read_bytes() == old and _backups() == []


def test_pyyaml_missing_errors_cleanly(home, monkeypatch):
    monkeypatch.setattr(S, "_import_yaml", lambda: None)
    out = DeepSeekWirer().wire()
    assert out.status == "error" and out.handoff[0] == "pip install pyyaml"
    assert not home.exists()


def test_binary_missing_error_and_handoff(home, monkeypatch):
    monkeypatch.setattr(S, "resolve_mcp_binary", lambda: None)
    monkeypatch.setattr(S, "_install_pkg_or_instruct", lambda pkg: "pip failed (stub)")
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "is not installed" in out.message
    assert out.handoff and "sibyl-memory-mcp" in out.handoff[0]
    assert not home.exists()


# --- idempotency and updates -------------------------------------------------

def test_idempotent_second_run_already_no_backup(home):
    assert DeepSeekWirer().wire().status == "wired"
    before = (home / "cordis.patch.yml").read_bytes()
    out = DeepSeekWirer().wire()
    assert out.status == "already" and out.backup_path is None
    assert (home / "cordis.patch.yml").read_bytes() == before and _backups() == []
    assert DeepSeekWirer().current_state()["wired_with_sibyl"] is True


def test_stale_path_updates_command_only(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "ours_stale.yml").decode()
    assert DeepSeekWirer().current_state()["wired_with_sibyl"] is False
    out = DeepSeekWirer().wire()
    assert out.status == "wired" and "Updated the sibyl-memory command" in out.message
    assert hp.read_text() == old.replace('"/old/venv/bin/sibyl-memory-mcp"', json.dumps(str(_isolated_path)))
    assert len(_backups()) == 1


def test_stale_path_keeps_user_env_timeouts_and_comment(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "ours_stale_custom.yml").decode()
    assert DeepSeekWirer().wire().status == "wired"
    new = hp.read_text()
    assert new == old.replace("/old/venv/bin/sibyl-memory-mcp", json.dumps(str(_isolated_path)))
    cfg = _rows(hp)[0]["config"]
    assert cfg["env"] == {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "9000"}
    assert cfg["startupTimeoutMs"] == 9000 and cfg["toolCallTimeoutMs"] == 120000
    assert "# moved when I rebuilt the venv" in new


def test_stale_path_in_nested_group_updated(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "ours_nested_in_group.yml").decode()
    assert DeepSeekWirer().wire().status == "wired"
    assert hp.read_text() == old.replace("'/old/bin/sibyl-memory-mcp'", json.dumps(str(_isolated_path)))


def test_stale_path_in_flow_row_updated(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "ours_flow_row.yml").decode()
    assert DeepSeekWirer().wire().status == "wired"
    assert hp.read_text() == old.replace("/old/bin/sibyl-memory-mcp", json.dumps(str(_isolated_path)))


def _other_install(tmp_path) -> Path:
    other = tmp_path / "other-venv" / "bin" / "sibyl-memory-mcp"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    return other


def test_working_different_install_is_already_with_note(home, tmp_path):
    other = _other_install(tmp_path)
    hp = home / "cordis.patch.yml"
    hp.parent.mkdir()
    hp.write_text((FIX / "ours_stale.yml").read_text().replace("/old/venv/bin/sibyl-memory-mcp", str(other)))
    before = hp.read_bytes()
    out = DeepSeekWirer().wire()
    assert out.status == "already" and "uses a different install" in out.message
    assert hp.read_bytes() == before


def test_force_repoints_different_install(home, tmp_path, _isolated_path):
    other = _other_install(tmp_path)
    hp = home / "cordis.patch.yml"
    hp.parent.mkdir()
    hp.write_text((FIX / "ours_stale.yml").read_text().replace("/old/venv/bin/sibyl-memory-mcp", str(other)))
    out = DeepSeekWirer().wire(force=True)
    assert out.status == "wired"
    assert _rows(hp)[0]["config"]["command"] == str(_isolated_path)


def test_disabled_row_left_alone(home):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "ours_disabled.yml")
    out = DeepSeekWirer().wire(force=True)
    assert out.status == "already" and "disabled: true" in out.message
    assert hp.read_bytes() == old


# --- foreign rows -----------------------------------------------------------

def test_foreign_same_id_skipped_without_force(home):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "foreign_same_id.yml")
    out = DeepSeekWirer().wire()
    assert out.status == "skipped" and out.overwrite_of
    assert hp.read_bytes() == old and _backups() == []


def test_foreign_same_id_prompt_default_no(home):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "foreign_same_id.yml")
    asked = []

    def prompt(q, default="Y"):
        asked.append((q, default))
        return default.lower()

    out = DeepSeekWirer().wire(prompt_fn=prompt)
    assert asked and asked[0][1] == "N" and "pointing elsewhere" in asked[0][0]
    assert out.status == "skipped" and hp.read_bytes() == old


def test_foreign_same_id_force_replaces_whole_row(home, _isolated_path):
    hp = home / "cordis.patch.yml"
    old_text = _put(hp, "foreign_same_id.yml").decode()
    old = _load(hp)
    out = DeepSeekWirer().wire(force=True)
    assert out.status == "wired" and "Replaced" in out.message
    new = _load(hp)
    assert new[0]["insert"][0] == _expected(_isolated_path)
    assert new[0]["insert"][1:] == old[0]["insert"][1:] and new[1:] == old[1:]
    text = hp.read_text()
    assert "engram" not in text and "args: [mcp]" not in text and "ENGRAM_DIR" not in text
    assert text.startswith("# keep this comment\n- insert:\n    - id: sibyl-memory\n")
    assert text.endswith(old_text[old_text.index("    - id: after-row"):])


def test_foreign_flow_row_force_refuses_and_prints_block(home):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "foreign_flow_row.yml")
    out = DeepSeekWirer().wire(force=True)
    # Panel fix 8: the row exists, so the manual text replaces it (never a
    # second `- insert:` block).
    assert out.status == "error" and "Replace the existing sibyl-memory row" in out.message
    assert "- insert:" not in out.message and out.manual_block.startswith("- id: ")
    assert hp.read_bytes() == old and _backups() == []


# --- layers and stacks ------------------------------------------------------

def test_profile_target_requires_existing_profile_dir(home):
    (home / "profiles").mkdir(parents=True)
    out = DeepSeekWirer(profile="web").wire()
    assert out.status == "error" and "profile 'web' does not exist" in out.message
    assert not (home / "profiles" / "web").exists()


def test_profile_target_writes_profile_patch_backup_in_profile_subdir(home, _isolated_path):
    p = _profile(home, "web")
    p.write_text(TEMPLATE)
    out = DeepSeekWirer(profile="web").wire()
    assert out.status == "wired"
    assert _rows(p) == [_expected(_isolated_path)]
    assert not (home / "cordis.patch.yml").exists()
    assert out.backup_path.parent == backups_dir("deepseek", "profile-web")
    assert _backups() == []          # nothing directly under backups/deepseek/


def test_rows_in_two_profiles_are_not_duplicates(home):
    _profile(home, "web")
    _profile(home, "tui")
    assert DeepSeekWirer(profile="web").wire().status == "wired"
    assert DeepSeekWirer(profile="tui").wire().status == "wired"
    st = DeepSeekWirer().current_state()
    assert st["duplicate"] is False and st["wired_with_sibyl"] is True
    out = DeepSeekWirer().wire()
    assert out.status == "already" and "profile(s) tui, web" in out.message
    assert not (home / "cordis.patch.yml").exists()


def test_row_in_home_and_profile_is_duplicate(home):
    p = _profile(home, "web")
    _put(p, "ours_stale.yml")
    hp = home / "cordis.patch.yml"
    _put(hp, "ours_stale.yml")
    before = (hp.read_bytes(), p.read_bytes())
    for force in (False, True):
        out = DeepSeekWirer().wire(force=force)
        assert out.status == "skipped" and "2 sibyl-memory entries" in out.message
        out = DeepSeekWirer(profile="web").wire(force=force)
        assert out.status == "skipped"
    assert (hp.read_bytes(), p.read_bytes()) == before and _backups() == []


def test_duplicate_in_one_file_skipped_even_with_force(home):
    hp = home / "cordis.patch.yml"
    old = _put(hp, "duplicate_in_file.yml")
    out = DeepSeekWirer().wire(force=True)
    assert out.status == "skipped" and hp.read_bytes() == old


def test_home_target_with_profile_row_never_adds_second_row(home, _isolated_path):
    p = _profile(home, "web")
    p.write_text(TEMPLATE)
    assert DeepSeekWirer(profile="web").wire().status == "wired"
    out = DeepSeekWirer().wire()
    assert out.status == "already"
    assert not (home / "cordis.patch.yml").exists()
    # and the other way round: a home row covers a profile target
    shutil.rmtree(home)
    _profile(home, "web")
    assert DeepSeekWirer().wire().status == "wired"
    out = DeepSeekWirer(profile="web").wire()
    assert out.status == "already" and "covers every profile" in out.message
    assert not _profile(home, "web").exists()


def test_stale_rows_in_two_profiles_each_repaired_where_they_live(home, _isolated_path):
    pw = _profile(home, "web")
    pt = _profile(home, "tui")
    _put(pw, "ours_stale.yml")
    _put(pt, "ours_stale_custom.yml")
    out = DeepSeekWirer().wire()
    assert out.status == "wired" and str(pw) in out.message and str(pt) in out.message
    assert _rows(pw)[0]["config"]["command"] == str(_isolated_path)
    assert _rows(pt)[0]["config"]["command"] == str(_isolated_path)
    assert _rows(pt)[0]["config"]["env"] == {"SIBYL_MEMORY_MCP_OUTPUT_TOKENS": "9000"}
    assert not (home / "cordis.patch.yml").exists()
    assert len(_backups("profile-web")) == 1 and len(_backups("profile-tui")) == 1


def test_profiles_glob_skips_dot_and_node_modules(home):
    for n in (".hidden", "node_modules", "real"):
        _put(_profile(home, n), "ours_stale.yml")
    layers = [l["layer"] for l in DeepSeekWirer().current_state()["layers"]]
    assert layers == ["home", "profile:real"]


def test_broken_other_layer_mentioning_us_blocks_add(home):
    p = _profile(home, "web")
    _put(p, "broken_with_fake_value.yml")
    out = DeepSeekWirer().wire()
    assert out.status == "error" and "mentions sibyl-memory" in out.message
    assert "FAKESECRET123" not in out.message
    assert not (home / "cordis.patch.yml").exists()


def test_broken_other_layer_not_mentioning_us_is_skipped_with_note(home):
    p = _profile(home, "web")
    _put(p, "broken.yml")
    out = DeepSeekWirer().wire()
    assert out.status == "wired" and "does not parse and was skipped" in out.message
    assert (home / "cordis.patch.yml").is_file()


# --- cmd_setup ---------------------------------------------------------------

def test_cmd_setup_deepseek_yes_end_to_end(home, capsys, _isolated_path):
    rc, out = _setup(["deepseek", "--yes"], capsys)
    assert rc == 0
    assert "deepseek: Added the SIBYL Memory MCP server" in out
    assert "next: sibyl status" in out
    assert "ask your human: start a new DeepSeek Harness session" in out
    rc, out = _setup(["deepseek", "--yes"], capsys)
    assert rc == 0 and "already has the sibyl-memory MCP server" in out


def test_cmd_setup_print_config_deepseek(home, capsys, _isolated_path):
    rc, out = _setup(["deepseek", "--print-config"], capsys)
    assert rc == 0
    assert "cwd: !!js process.cwd()" in out and json.dumps(str(_isolated_path)) in out
    assert str(home / "cordis.patch.yml") in out
    assert "SIBYL_MEMORY_MCP_OUTPUT_TOKENS" in out
    assert not home.exists()
    rc, out = _setup(["--print-config"], capsys)
    assert "sibyl setup deepseek" in out


def test_cmd_setup_bad_profile_exit_1(home, capsys):
    rc, out = _setup(["deepseek", "--dsh-profile", "../etc", "--yes"], capsys)
    assert rc == 1
    assert "not a valid DeepSeek Harness profile name" in out
    assert out.rstrip().endswith("next: sibyl setup deepseek --dsh-profile <name>")


def test_no_terminal_default_all_includes_deepseek_when_detected(home, monkeypatch, capsys):
    (home / "profiles" / "default").mkdir(parents=True)
    (Path.home() / ".cursor").mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    def eof(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    rc, out = _setup([], capsys)
    assert "no terminal: wiring all detected" in out
    assert "deepseek: Added" in out and (home / "cordis.patch.yml").is_file()


def test_user_facing_text_says_deepseek_harness_not_bare_dsh(home, capsys):
    import re
    texts = []
    _setup(["deepseek", "--yes"], capsys)
    for argv in (["deepseek", "--yes"], ["deepseek", "--print-config"], ["dsh", "--dry-run", "--yes"]):
        texts.append(_setup(argv, capsys)[1])
    texts.append(S._RESTART_HUMAN["deepseek"])
    shutil.rmtree(home)
    shutil.copytree(FIX / "debian_dsh_home", home)
    texts.append(DeepSeekWirer().wire().message)
    allowed = re.compile(r"~/\.dsh|\.dsh\b|DSH_HOME|--dsh-home|--dsh-profile|dsh-mcp-client|"
                         r"'dsh' distributed shell")
    # Paths are not user-facing text: a temp dir such as .../dsh-sandbox/...
    # would otherwise match \bdsh\b (sandbox M6).
    paths = re.compile(r"(?:~|\.)?(?:/[^\s;,:'\"()]+)+")
    for t in texts:
        rest = allowed.sub("", paths.sub("", t))
        assert not re.search(r"\bdsh\b", rest, re.I), rest
