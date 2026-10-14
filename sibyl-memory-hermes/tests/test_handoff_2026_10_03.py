"""Agent handoff in the Hermes adapter and install-plugin (spec agent-handoff-messages
2026-10-03, rows H1 to H9, section 1.5).

The adapter keeps calling Hermes' `tool_error(msg)`, parses the JSON it returns,
adds `next` and `human_action`, and re-dumps; the `error` text is unchanged.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from sibyl_memory_client.exceptions import (
    CapExceededError,
    ENTITLEMENT_HUMAN_ACTION,
    ENTITLEMENT_MESSAGES,
    ENTITLEMENT_NEXT,
    EntitlementError,
)
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_hermes import install_plugin as ip
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_module
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter


@pytest.fixture(autouse=True)
def _reset_warning_latch(monkeypatch):
    monkeypatch.setattr(adapter_module, "_signed_out_warned", False)


def _adapter(tmp_path) -> SibylAdapter:
    a = SibylAdapter()
    a.initialize("sess-1", hermes_home=str(tmp_path / "hh"))
    return a


# ---- H1 / H2: the gate refusal ------------------------------------------------

def test_h1_no_account(entitled_test_home, tmp_path) -> None:
    (entitled_test_home / "credentials.json").unlink()
    out = json.loads(_adapter(tmp_path).handle_tool_call("sibyl_list", {}))
    assert out == {"error": adapter_module._SIGNED_OUT_TOOL_ERROR, "next": "sibyl init",
                   "human_action": None}


@pytest.mark.parametrize("reason", ["signed_out", "under_review", "account_deleted", "client_too_old"])
def test_h2_other_codes_carry_the_exception_handoff(reason, entitled_test_home, tmp_path) -> None:
    write_entitlement(entitled_test_home, "", denied={"code": reason, "at": time.time()})
    out = json.loads(_adapter(tmp_path).handle_tool_call("sibyl_list", {}))
    assert out["error"] == ENTITLEMENT_MESSAGES[reason]          # summary: the text as before
    assert "next: " not in out["error"]
    assert out["next"] == ENTITLEMENT_NEXT[reason]
    assert out["human_action"] == ENTITLEMENT_HUMAN_ACTION[reason]


def test_parse_and_augment_keeps_extra_keys_of_tool_error(monkeypatch) -> None:
    monkeypatch.setattr(adapter_module, "tool_error",
                        lambda msg: json.dumps({"error": msg, "hermes_extra": 1}))
    out = json.loads(adapter_module._entitlement_tool_error(EntitlementError("signed_out")))
    assert out["hermes_extra"] == 1 and out["next"] == "sibyl init"
    assert out["error"] == ENTITLEMENT_MESSAGES["signed_out"]


def test_non_json_tool_error_falls_back_to_the_stub_shape(monkeypatch) -> None:
    monkeypatch.setattr(adapter_module, "tool_error", lambda msg: f"plain text: {msg}")
    out = json.loads(adapter_module._entitlement_tool_error(EntitlementError("no_account")))
    assert out == {"error": adapter_module._SIGNED_OUT_TOOL_ERROR, "next": "sibyl init",
                   "human_action": None}


# ---- H3 / H6: the log lines ---------------------------------------------------------

def test_h3_signed_out_warning_ends_with_next(entitled_test_home, tmp_path, caplog) -> None:
    (entitled_test_home / "credentials.json").unlink()
    a = _adapter(tmp_path)
    with caplog.at_level(logging.WARNING, logger=adapter_module.logger.name):
        a.handle_tool_call("sibyl_list", {})
    msgs = [r.getMessage() for r in caplog.records if "signed out" in r.getMessage()]
    assert len(msgs) == 1 and msgs[0].endswith(" next: sibyl init")


def test_h6_capsule_log_names_the_next_command() -> None:
    assert adapter_module._capsule_next("signed_out") == " next: sibyl init"
    assert adapter_module._capsule_next("client_too_old").startswith(" next: pip install -U")
    assert adapter_module._capsule_next("gate_error") == ""


# ---- H4 / H5 ----------------------------------------------------------------------------

def test_h4_provider_not_initialized() -> None:
    out = json.loads(SibylAdapter().handle_tool_call("sibyl_list", {}))
    assert out["error"] == "Sibyl provider not initialized"
    assert out["next"] == "done" and out["human_action"].startswith("restart Hermes")


def test_h5_cap_exceeded_keeps_the_class_name_only(entitled_test_home, tmp_path, monkeypatch) -> None:
    a = _adapter(tmp_path)

    def over(*args, **kw):
        raise CapExceededError("body text that must not leak", current_size=6, cap=5)
    monkeypatch.setattr(a._sibyl, "list", over)
    out = json.loads(a.handle_tool_call("sibyl_list", {}))
    assert out["error"] == "CapExceededError"
    assert out["next"] == "sibyl upgrade"
    assert out["human_action"].startswith("the free 5 MB memory cap is full")
    assert "must not leak" not in json.dumps(out)


def test_h5_other_errors_are_unchanged(entitled_test_home, tmp_path, monkeypatch) -> None:
    a = _adapter(tmp_path)

    def boom(*args, **kw):
        raise RuntimeError("secret body")
    monkeypatch.setattr(a._sibyl, "list", boom)
    assert json.loads(a.handle_tool_call("sibyl_list", {})) == {"error": "RuntimeError"}


# ---- H7 to H9: install-plugin ------------------------------------------------------------

def _block(out: str) -> list[str]:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    block = []
    for ln in reversed(lines):
        if ln.startswith(("later: ", "ask your human: ", "next: ")):
            block.insert(0, ln)
        else:
            break
    assert block and block[-1].startswith("next: "), lines[-3:]
    for b in block:
        assert "\u2014" not in b and "\x1b" not in b
    return block


def test_h7_success_hands_the_config_line_to_sibyl_setup(tmp_path, capsys) -> None:
    """The provider line is not set yet: `sibyl setup hermes` writes it (and
    asks for the restart), so the agent runs it; no hand edit of YAML."""
    hh = tmp_path / ".hermes"
    mem = tmp_path / "pkg" / "plugins" / "memory"
    mem.mkdir(parents=True)
    assert ip.install(hh, force=False, dry_run=False, memory_provider_path=str(mem)) == 0
    assert _block(capsys.readouterr().out) == [f"next: sibyl setup hermes --hermes-home {hh}"]


def test_h7_default_home_needs_no_flag(tmp_path, capsys, monkeypatch) -> None:
    hh = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hh))
    mem = tmp_path / "pkg" / "plugins" / "memory"
    mem.mkdir(parents=True)
    assert ip.install(hh, force=False, dry_run=False, memory_provider_path=str(mem)) == 0
    assert _block(capsys.readouterr().out) == ["next: sibyl setup hermes"]


def test_h7_provider_line_is_read_without_pyyaml(tmp_path, capsys, monkeypatch) -> None:
    """PyYAML absent: a line scan still sees memory.provider: sibyl, so the
    handoff does not send the agent to set a line that is already there."""
    import builtins
    real_import = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_yaml)
    hh = tmp_path / ".hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text("model: x\nmemory:\n  # ours\n  provider: 'sibyl'\n")
    assert ip._provider_is_sibyl(hh) is True
    (hh / "config.yaml").write_text("memory:\n  provider: mem0\nprovider: sibyl\n")
    assert ip._provider_is_sibyl(hh) is False


def test_h7_no_ask_when_the_provider_is_already_set(tmp_path, capsys, monkeypatch) -> None:
    hh = tmp_path / ".hermes"
    hh.mkdir()
    (hh / "config.yaml").write_text("memory:\n  provider: sibyl\n")
    mem = tmp_path / "pkg" / "plugins" / "memory"
    mem.mkdir(parents=True)
    monkeypatch.setenv("SIBYL_CREDENTIALS", str(tmp_path / "creds.json"))
    (tmp_path / "creds.json").write_text("{}")
    ip.install(hh, force=False, dry_run=False, memory_provider_path=str(mem))
    assert _block(capsys.readouterr().out) == ["next: sibyl status"]


def test_h8_refused_non_empty(tmp_path, capsys) -> None:
    hh = tmp_path / ".hermes"
    dest = hh / "plugins" / "sibyl"
    dest.mkdir(parents=True)
    (dest / "other.txt").write_text("x")
    assert ip.install(hh, force=False, dry_run=False, memory_provider_path=None) == 2
    assert _block(capsys.readouterr().out) == [
        f"ask your human: {dest} already has files; confirm replacing them",
        "next: sibyl-memory-hermes install-plugin --force"]


def test_h9_provider_path_not_detected(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(ip, "_memory_provider_dest", lambda override: None)
    assert ip.install(tmp_path / ".hermes", force=False, dry_run=False, memory_provider_path=None) == 0
    block = _block(capsys.readouterr().out)
    assert block[0] == "later: sibyl-memory-hermes install-plugin --memory-provider-path <dir>"
    assert block[1].startswith("ask your human: where your Hermes install's plugins/memory directory is")


def test_h9_provider_path_not_writable(tmp_path, capsys, monkeypatch) -> None:
    prov = tmp_path / "root-owned" / "sibyl"
    monkeypatch.setattr(ip, "_memory_provider_dest", lambda override: prov)
    real = ip._write_payload

    def guarded(dest, force, dry_run):
        if Path(dest) == prov:
            raise PermissionError("root owned")
        return real(dest, force, dry_run)
    monkeypatch.setattr(ip, "_write_payload", guarded)
    assert ip.install(tmp_path / ".hermes", force=False, dry_run=False, memory_provider_path=None) == 0
    block = _block(capsys.readouterr().out)
    assert block[-2] == f"ask your human: copy the adapter into {prov} with sudo (the two commands above)"
