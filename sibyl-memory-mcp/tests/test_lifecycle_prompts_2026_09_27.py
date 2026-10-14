"""sibyl-memory-mcp 0.3.0 save/debloat surface (spec plugin-save-debloat,
tests 8.2 items 18-22 and the prompts-without-credentials adversarial case).

All fail on the pre-change tree: the server listed no prompts and no
resources, and results carried no lifecycle notice."""
from __future__ import annotations

import asyncio
import builtins
import json
import os
import time
from pathlib import Path

import pytest
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import client as client_mod
from sibyl_memory_client import skills as SK
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import notices as N
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import runner as RUN
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

CURRENT = "# Agent rules\n\nRun the tests first.\nUse the venv.\nKeep answers short.\nNo secrets.\n\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    client_mod._NOTICE_SHOWN.clear()
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setattr(server, "_LIFECYCLE", N.ProcessNotices("mcp"))
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    yield
    client_mod._NOTICE_SHOWN.clear()


def _server():
    mcp = server.build_server()
    return mcp, mcp._mcp_server.request_handlers[CallToolRequest]


def _call(handler, name, args):
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": name, "arguments": args})))
    root = res.root
    text = root.content[0].text if root.content else ""
    return root.isError, text


def _age(p: Path) -> None:
    t = time.time() - 3600
    os.utime(p, (t, t))


# 18 ------------------------------------------------------------------------

def test_18_prompts_listed_with_arguments_and_render_the_canonical_text(entitled_test_home):
    mcp, _h = _server()
    prompts = {p.name: p for p in asyncio.run(mcp.list_prompts())}
    assert set(prompts) >= {"save", "debloat"}
    assert [a.name for a in prompts["save"].arguments] == ["mode"]
    assert [a.name for a in prompts["debloat"].arguments] == ["mode"]
    r = asyncio.run(mcp.get_prompt("save", {"mode": "preview"}))
    text = r.messages[0].content.text
    assert r.messages[0].role == "user"
    assert text.startswith("<!-- SIBYL-SKILL-WATERMARK-BEGIN")
    assert text.rstrip().endswith("Mode: preview")
    assert SK.body(SK.text("SAVE.md")).strip() in text
    # A person's edit of the installed file is what the prompt serves.
    SK.install(Path.home())
    p = Path.home() / ".sibyl-memory" / "skills" / "SAVE.md"
    p.write_text(SK.text("SAVE.md") + "\nMy own extra step.\n")
    text2 = asyncio.run(mcp.get_prompt("save", {})).messages[0].content.text
    assert "My own extra step." in text2 and text2.rstrip().endswith("Mode: run")
    d = asyncio.run(mcp.get_prompt("debloat", {"mode": "trim"})).messages[0].content.text
    assert "sibyl debloat --json" in d and d.rstrip().endswith("Mode: trim")


def test_18_unknown_mode_is_a_validation_error_not_a_crash(entitled_test_home):
    mcp, _h = _server()
    with pytest.raises(ValueError) as e:
        asyncio.run(mcp.get_prompt("save", {"mode": "delete-everything"}))
    assert "mode" in str(e.value)
    # the server still works afterwards
    assert asyncio.run(mcp.get_prompt("save", {"mode": "headless"})).messages


# 19 ------------------------------------------------------------------------

def test_19_resources_and_a_stat_only_context_weight(entitled_test_home, monkeypatch):
    mcp, _h = _server()
    res = {str(r.uri): r for r in asyncio.run(mcp.list_resources())}
    assert set(res) >= {"sibyl://skills/SAVE.md", "sibyl://skills/DEBLOAT.md",
                        "sibyl://skills/manifest", "sibyl://context/weight"}
    assert res["sibyl://skills/SAVE.md"].mimeType == "text/markdown"
    assert res["sibyl://context/weight"].mimeType == "application/json"
    got = list(asyncio.run(mcp.read_resource("sibyl://skills/SAVE.md")))
    assert got[0].mime_type == "text/markdown" and "SIBYL-SKILL-WATERMARK-BEGIN" in got[0].content
    Path("AGENTS.md").write_text("a" * 40000)
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    w = list(asyncio.run(mcp.read_resource("sibyl://context/weight")))
    monkeypatch.setattr(builtins, "open", real_open)
    rec = json.loads(w[0].content)
    assert rec["total_tok"] == 10000 and rec["files"][0]["file"] == "AGENTS.md"
    assert not any(o.endswith("AGENTS.md") for o in opened)       # stat only, never the body
    man = json.loads(list(asyncio.run(mcp.read_resource("sibyl://skills/manifest")))[0].content)
    assert man["package_version"] == SK.version()


# 20 ------------------------------------------------------------------------

def test_20_and_adv_prompts_and_resources_work_with_no_credentials_tools_still_refuse(entitled_test_home):
    (entitled_test_home / "credentials.json").unlink()
    mcp, h = _server()
    assert asyncio.run(mcp.get_prompt("save", {"mode": "run"})).messages
    assert list(asyncio.run(mcp.read_resource("sibyl://skills/DEBLOAT.md")))[0].content
    assert json.loads(list(asyncio.run(mcp.read_resource("sibyl://context/weight")))[0].content)["weight"]
    is_err, text = _call(h, "memory_list", {})
    assert is_err and json.loads(text.split(": ", 1)[-1] if not text.startswith("{") else text)["code"] \
        == "ACCOUNT_REQUIRED"


# 21 ------------------------------------------------------------------------

def _bump(tid: str, n: int) -> None:
    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_total"] = int(t.get("writes_total") or 0) + n
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
        t["since_save_started_at"] = time.time() - 60
    ST.LifecycleState().update(_u)


def _tid(home: Path) -> str:
    c = json.loads((home / "credentials.json").read_text())
    return c.get("tenant_id") or c["account_id"]


def test_21_save_due_notice_once_per_window_never_on_errors_account_first(entitled_test_home, monkeypatch):
    _mcp, h = _server()
    is_err, text = _call(h, "memory_list", {})
    assert not is_err and "notice" not in json.loads(text)
    _bump(_tid(entitled_test_home), 25)
    is_err, text = _call(h, "memory_recall", {"category": "no", "name": "such"})
    assert is_err and "writes since the last save" not in text          # never on errors
    is_err, text = _call(h, "memory_list", {})
    notice = json.loads(text)["notice"]
    assert notice.startswith("Sibyl Memory: 25 writes since the last save.") and "on Pro" in notice
    is_err, text = _call(h, "memory_list", {})
    assert "notice" not in json.loads(text)                             # 6 h window
    # An account notice takes precedence (never two in one result).
    ST.LifecycleState().update(lambda st: ST.tenant(st, _tid(entitled_test_home)).update(
        last_save_reminder_at=None))
    monkeypatch.setattr(client_mod.MemoryClient, "entitlement_notice", lambda self: "ACCOUNT NOTICE")
    is_err, text = _call(h, "memory_list", {})
    assert json.loads(text)["notice"] == "ACCOUNT NOTICE"


def test_21_reminders_off_silences(entitled_test_home):
    _mcp, h = _server()
    _bump(_tid(entitled_test_home), 40)
    ST.LifecycleState().update(lambda st: st.update(reminders=False))
    is_err, text = _call(h, "memory_list", {})
    assert "notice" not in json.loads(text)


def test_21_pro_process_start_report_once_and_free_never_applies(entitled_test_home, test_signer,
                                                                   monkeypatch):
    f = Path("AGENTS.md")
    f.write_text(CURRENT + HIST)
    _age(f)
    applied: list[str] = []
    real_apply = A.apply_one

    def spy(c, **kw):
        applied.append(c.label)
        return real_apply(c, **kw)
    monkeypatch.setattr(A, "apply_one", spy)
    # Free: the process start does nothing, whatever the policy says.
    P.save({"mode": "auto", "auto_files": [P.auth_id(f.resolve(), Path.home())]}, Path.home())
    _mcp, h = _server()
    _call(h, "memory_list", {})
    server._LIFECYCLE.join(5)
    assert applied == [] and f.read_text() == CURRENT + HIST
    # Pro: a new process (fresh notices) runs the plan and applies the named file.
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    monkeypatch.setattr(server, "_LIFECYCLE", N.ProcessNotices("mcp"))
    for k, v in (("client", None), ("creds_mtime", None), ("ent_mtime", None)):
        server._client_cache[k] = v
    _mcp, h = _server()
    _call(h, "memory_list", {})
    server._LIFECYCLE.join(10)
    assert applied == ["AGENTS.md"]
    notices = []
    for _ in range(3):
        is_err, text = _call(h, "memory_list", {})
        notices.append(json.loads(text).get("notice"))
    assert notices[0].startswith("context trim: AGENTS.md ") and "history stored to memory as debloat/AGENTS.md/" in notices[0]
    assert notices[1:] == [None, None]
    assert "old decision" not in f.read_text()


def test_21_pro_propose_default_reports_a_digest_and_changes_nothing(entitled_test_home, test_signer):
    f = Path("AGENTS.md")
    f.write_text(CURRENT + HIST)
    _age(f)
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    _mcp, h = _server()
    _call(h, "memory_list", {})
    server._LIFECYCLE.join(10)
    is_err, text = _call(h, "memory_list", {})
    assert "would move to memory" in json.loads(text)["notice"]
    assert f.read_text() == CURRENT + HIST


# SD-9 (F10): the server's clients count writes; the SDK alone does not ---

def test_f10_the_server_enables_write_counting_on_the_clients_it_builds(entitled_test_home):
    """a6338cc had no opt-in: counting happened inside every MemoryClient."""
    server._client_cache.update({"client": None})
    client = server._open_client()
    assert client._lifecycle_home == Path.home()
    mcp, h = _server()
    for i in range(10):
        err, _ = _call(h, "memory_remember", {"category": "fact", "name": f"f{i}", "body": {"v": i}})
        assert not err
    ST.flush_all()
    t = ST.LifecycleState().load()["tenants"][client.get_tenant()]
    assert t["writes_since_save"] >= 10
