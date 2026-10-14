"""sibyl-memory-hermes 0.5.0 account gate (spec account-gated-plugin 2026-09-27, 9.3).

The provider and the bundled adapter against the REAL gate in
sibyl-memory-client, with the conftest's isolated, test-entitled home.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

from sibyl_memory_client import EntitlementError
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_module
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter


@pytest.fixture(autouse=True)
def _reset_warning_latch(monkeypatch):
    monkeypatch.setattr(adapter_module, "_signed_out_warned", False)


def _signed_out(home):
    (home / "credentials.json").unlink()


def _adapter(tmp_path) -> SibylAdapter:
    a = SibylAdapter()
    a.initialize("sess-1", hermes_home=str(tmp_path / "hh"))
    return a


# 19 ------------------------------------------------------------------------

def test_provider_without_credentials_constructs_and_refuses(entitled_test_home, tmp_path) -> None:
    _signed_out(entitled_test_home)
    p = SibylMemoryProvider(db_path=tmp_path / "m.db")
    for call in (lambda: p.remember("c", "n", {"x": 1}), lambda: p.recall("c", "n"),
                 lambda: p.search("anything"), lambda: p.list(), lambda: p.save_context({}, {}),
                 lambda: p.load_context(), lambda: p.get_state("k"), lambda: p.forget("c", "n")):
        with pytest.raises(EntitlementError) as exc:
            call()
        assert exc.value.code == "no_account"


def test_require_credentials_keeps_its_meaning(entitled_test_home, tmp_path) -> None:
    from sibyl_memory_hermes.credentials import CredentialsNotFoundError
    _signed_out(entitled_test_home)
    with pytest.raises(CredentialsNotFoundError):
        SibylMemoryProvider(db_path=tmp_path / "m.db", require_credentials=True)


@pytest.mark.parametrize("tool,args", [
    ("sibyl_remember", {"category": "c", "name": "n", "body": {"x": 1}}),
    ("sibyl_recall", {"category": "c", "name": "n"}),
    ("sibyl_search", {"query": "anything at all"}),
    ("sibyl_list", {}),
])
def test_adapter_tool_calls_return_tool_error(tool, args, entitled_test_home, tmp_path) -> None:
    _signed_out(entitled_test_home)
    a = _adapter(tmp_path)
    out = a.handle_tool_call(tool, args)
    assert isinstance(out, str)
    payload = json.loads(out)
    # Agent handoff (spec 2026-10-03 row H1): same error text, plus next / human_action.
    assert payload["error"] == adapter_module._SIGNED_OUT_TOOL_ERROR
    assert payload["next"] == "sibyl init" and payload["human_action"] is None
    assert "sibyl init" in payload["error"]


def test_adapter_locked_reason_is_relayed(entitled_test_home, tmp_path) -> None:
    write_entitlement(entitled_test_home, "", denied={"code": "under_review", "at": time.time()})
    out = json.loads(_adapter(tmp_path).handle_tool_call("sibyl_list", {}))
    assert "under review" in out["error"] and "sibyl@sibyllabs.org" in out["error"]


def test_sync_turn_logs_once_and_never_retries(entitled_test_home, tmp_path, caplog, monkeypatch) -> None:
    _signed_out(entitled_test_home)
    a = _adapter(tmp_path)
    calls = []
    real = a._sibyl.save_context

    def counting(*args, **kw):
        calls.append(1)
        return real(*args, **kw)
    monkeypatch.setattr(a._sibyl, "save_context", counting)
    slept = []
    monkeypatch.setattr(adapter_module.time, "sleep", lambda s: slept.append(s))
    with caplog.at_level(logging.WARNING, logger=adapter_module.logger.name):
        for i in range(3):
            a.sync_turn(f"user {i}", f"assistant {i}")
            a._sync_thread.join(timeout=5)
        assert a.prefetch("a long enough prefetch query about things") == ""
        a.on_memory_write("add", "notes", "some content")
    assert len(calls) == 3              # one attempt per turn, no busy-retry
    assert slept == []                  # the retry backoff never ran
    warnings = [r for r in caplog.records if adapter_module._SIGNED_OUT_LOG in r.getMessage()]
    assert len(warnings) == 1


def test_is_available_stays_a_cheap_import_check(entitled_test_home, tmp_path) -> None:
    _signed_out(entitled_test_home)
    assert SibylAdapter().is_available() is True


# 20 ------------------------------------------------------------------------

def test_sibyl_tenant_id_never_bypasses_the_gate(entitled_test_home, tmp_path, monkeypatch) -> None:
    _signed_out(entitled_test_home)
    monkeypatch.setenv("SIBYL_TENANT_ID", "any-tenant-at-all")
    a = _adapter(tmp_path)
    assert a._sibyl.tenant_id == "any-tenant-at-all"
    out = json.loads(a.handle_tool_call("sibyl_remember", {"category": "c", "name": "n",
                                                           "body": {"x": 1}}))
    assert out["error"] == adapter_module._SIGNED_OUT_TOOL_ERROR
    assert out["next"] == "sibyl init" and out["human_action"] is None


def test_explicit_tenant_on_the_provider_never_bypasses(entitled_test_home, tmp_path) -> None:
    _signed_out(entitled_test_home)
    p = SibylMemoryProvider(db_path=tmp_path / "m.db", tenant_id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(EntitlementError):
        p.remember("c", "n", {"x": 1})


# 21 (existing suite green with the fixture) + the signed-in path ------------

def test_signed_in_adapter_round_trip(tmp_path) -> None:
    a = _adapter(tmp_path)
    assert json.loads(a.handle_tool_call("sibyl_remember", {"category": "c", "name": "n",
                                                            "body": {"x": 1}}))["ok"] is True
    got = json.loads(a.handle_tool_call("sibyl_recall", {"category": "c", "name": "n"}))
    assert got["entity"]["body"] == {"x": 1}


def test_tool_error_never_carries_the_token(entitled_test_home, tmp_path, test_signer) -> None:
    tok = test_signer.mint(mem="none")
    write_entitlement(entitled_test_home, tok)
    out = _adapter(tmp_path).handle_tool_call("sibyl_list", {})
    assert tok not in out and tok.split(".")[1] not in out
    assert "confirmed email" in json.loads(out)["error"]


# GATE-15: the automatic pre-sign-in adoption is announced with ONE log line ----

def _seed_pre_signin(tmp_path, n=2):
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    from sibyl_memory_client.testing import NullEntitlementGate
    db_dir = tmp_path / "hh" / "sibyl"
    db_dir.mkdir(parents=True, exist_ok=True)
    old = MemoryClient.local(str(db_dir / "memory.db"), tenant_id=DEFAULT_TENANT,
                             entitlement_gate=NullEntitlementGate())
    for i in range(n):
        old.set_entity("project", f"p{i}", {"note": f"secret body {i}"})
    old.storage.close()


def _adoption_lines(caplog):
    return [r for r in caplog.records
            if "written before sign-in" in r.getMessage() and r.name == adapter_module.logger.name]


@pytest.fixture()
def _fresh_notices():
    from sibyl_memory_client import client as client_mod
    client_mod._NOTICE_SHOWN.clear()
    yield
    client_mod._NOTICE_SHOWN.clear()


def test_adoption_logs_one_line(entitled_test_home, tmp_path, caplog, _fresh_notices) -> None:
    _seed_pre_signin(tmp_path, 2)
    a = _adapter(tmp_path)
    with caplog.at_level(logging.INFO, logger=adapter_module.logger.name):
        out = json.loads(a.handle_tool_call("sibyl_list", {}))
        assert {e["name"] for e in out["entities"]} == {"p0", "p1"}
        a.handle_tool_call("sibyl_list", {})
        a.handle_tool_call("sibyl_recall", {"category": "project", "name": "p0"})
    lines = _adoption_lines(caplog)
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "2 memories written before sign-in were moved into your account on this computer." in msg
    assert "secret body" not in msg and "p0" not in msg


def test_adoption_line_from_the_background_sync_turn(entitled_test_home, tmp_path, caplog, _fresh_notices) -> None:
    _seed_pre_signin(tmp_path, 1)
    a = _adapter(tmp_path)
    with caplog.at_level(logging.INFO, logger=adapter_module.logger.name):
        a.sync_turn("hello there", "hi")
        a.shutdown()
    assert len(_adoption_lines(caplog)) == 1


def test_no_adoption_line_without_pre_signin_rows(entitled_test_home, tmp_path, caplog, _fresh_notices) -> None:
    a = _adapter(tmp_path)
    with caplog.at_level(logging.INFO, logger=adapter_module.logger.name):
        a.handle_tool_call("sibyl_list", {})
    assert _adoption_lines(caplog) == []
