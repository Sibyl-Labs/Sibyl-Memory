"""R1 (plan plugin-paid-tier-2026-09-29, 2.3 / 3.2 / 9.4): the MCP notice channel
for Cursor, Codex and any MCP host.

* the Pro automatic-save request rides the next SUCCESSFUL result, once per
  interval of active time, never on an error, never on the free tier;
* precedence: the account notice, then the save request, then the skill line
  (one notice per result);
* inside Claude Code with our Stop hook wired, the server leaves the request to
  the hook (never two requests for one save);
* every tool call counts as activity, errors included.

On 3e13ca4 these fail: the server had no active-time accounting
(``_LIFECYCLE.note_activity``), no save request on Pro and no host check.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import client as client_mod
from sibyl_memory_client.lifecycle import notices as N
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    client_mod._NOTICE_SHOWN.clear()
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setattr(server, "_LIFECYCLE", N.ProcessNotices("mcp"))
    monkeypatch.setattr(server, "_HOST_CACHE", {})
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    yield
    client_mod._NOTICE_SHOWN.clear()


@pytest.fixture
def pro(entitled_test_home, test_signer):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    ST.LifecycleState().update(lambda st: ST.tenant(st, _tid(entitled_test_home)).update(tier_seen="paid"))
    return entitled_test_home


def _server():
    mcp = server.build_server()
    return mcp, mcp._mcp_server.request_handlers[CallToolRequest]


def _call(handler, name, args):
    res = asyncio.run(handler(CallToolRequest(method="tools/call",
                                              params={"name": name, "arguments": args})))
    root = res.root
    return root.isError, (root.content[0].text if root.content else "")


def _tid(home: Path) -> str:
    c = json.loads((home / "credentials.json").read_text())
    return c.get("tenant_id") or c["account_id"]


def _bump(tid: str, n: int) -> None:
    def _u(st):
        t = ST.tenant(st, tid)
        t["writes_total"] = int(t.get("writes_total") or 0) + n
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
    ST.LifecycleState().update(_u)


def _work(minutes: float) -> None:
    """``minutes`` of active time in this server process."""
    n = server._LIFECYCLE
    n._active += minutes * 60
    n.started_at = min(n.started_at, time.time() - minutes * 60 - 60)


def _notice(h, name="memory_list", args=None):
    is_err, text = _call(h, name, args or {})
    if is_err:
        return True, None
    return False, json.loads(text).get("notice")


def test_save_request_once_per_interval_never_on_errors(pro):
    _mcp, h = _server()
    _notice(h)
    _bump(_tid(pro), 2)
    _work(61)
    is_err, text = _call(h, "memory_recall", {"category": "no", "name": "such"})
    assert is_err and "Sibyl Memory Pro" not in text                    # never on an error
    err, notice = _notice(h)
    assert notice and notice.startswith("Sibyl Memory Pro: about 60 minutes of work since the last save.")
    assert "headless" in notice
    assert not any((_notice(h)[1] or "").startswith("Sibyl Memory Pro") for _ in range(3))
    _work(59)
    assert not any((_notice(h)[1] or "").startswith("Sibyl Memory Pro") for _ in range(2))
    _work(2)
    assert (_notice(h)[1] or "").startswith("Sibyl Memory Pro")
    st = ST.LifecycleState().load()
    assert st["tenants"][_tid(pro)]["nudges_today"] == 2


def test_free_never_gets_a_save_request(entitled_test_home):
    _mcp, h = _server()
    _notice(h)
    _bump(_tid(entitled_test_home), 3)
    _work(300)
    got = [_notice(h)[1] for _ in range(4)]
    assert not [g for g in got if g and g.startswith("Sibyl Memory Pro")]


def test_every_tool_call_counts_activity_errors_included():
    _mcp, h = _server()
    n = server._LIFECYCLE
    _call(h, "memory_list", {})
    first = n._last_event
    time.sleep(0.02)
    _call(h, "memory_recall", {"category": "no", "name": "such"})
    assert n._last_event > first and n.active_seconds > 0


def test_precedence_account_then_save_then_skills(pro, monkeypatch):
    tid = _tid(pro)
    _mcp, h = _server()
    _notice(h)
    # a relevant proposal waits
    c = server._open_client()
    now = time.time()
    ev = [{"event_id": str(i), "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t)), "snippet": "x"}
          for i, t in enumerate((now - 4 * 86400, now - 2 * 86400, now - 600))]
    with c.storage.transaction() as con:
        con.execute("INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, proposed_title, "
                    "proposed_body, evidence, confidence, summarizer) VALUES ('p1', ?, 'repeated_action', "
                    "'deploy-preview-check', NULL, 'b', ?, 0.8, 'local-deterministic')", (tid, json.dumps(ev)))
    _bump(tid, 2)
    _work(61)
    monkeypatch.setattr(client_mod.MemoryClient, "entitlement_notice", lambda self: "ACCOUNT NOTICE")
    assert _notice(h)[1] == "ACCOUNT NOTICE"
    monkeypatch.setattr(client_mod.MemoryClient, "entitlement_notice", lambda self: None)
    assert (_notice(h)[1] or "").startswith("Sibyl Memory Pro:")
    server._LIFECYCLE._skills_next_check = 0
    line = _notice(h)[1]
    assert line == ("Sibyl Memory: 1 candidate skill from your recent sessions (deploy-preview-check). "
                    "Say /sibyl-save curate to review, or run sibyl skills proposals.")
    server._LIFECYCLE._skills_next_check = 0
    assert _notice(h)[1] is None                                          # once per session


def _claude_code_host(monkeypatch):
    params = SimpleNamespace(clientInfo=SimpleNamespace(name="claude-code", version="2.1.283"))
    fake = SimpleNamespace(get_context=lambda: SimpleNamespace(session=SimpleNamespace(client_params=params)))
    monkeypatch.setattr(server, "_SERVER", fake)


def test_inside_claude_code_with_the_stop_hook_the_hook_carries_it(pro, monkeypatch):
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "[ -x /v/bin/sibyl ] && exec /v/bin/sibyl hook stop; exit 0"}]}]}}))
    _mcp, h = _server()
    _claude_code_host(monkeypatch)
    _notice(h)
    _bump(_tid(pro), 2)
    _work(120)
    assert not [n for n in (_notice(h)[1] for _ in range(3)) if n and n.startswith("Sibyl Memory Pro")]
    assert server._save_is_hooked() is True


def test_inside_claude_code_without_our_hook_the_server_asks(pro, monkeypatch):
    _mcp, h = _server()
    _claude_code_host(monkeypatch)
    _notice(h)
    _bump(_tid(pro), 2)
    _work(61)
    assert server._save_is_hooked() is False
    assert any((n or "").startswith("Sibyl Memory Pro") for n in (_notice(h)[1] for _ in range(3)))


def test_cursor_host_gets_the_request(pro, monkeypatch):
    params = SimpleNamespace(clientInfo=SimpleNamespace(name="cursor-vscode", version="1"))
    _mcp, h = _server()
    monkeypatch.setattr(server, "_SERVER", SimpleNamespace(get_context=lambda: SimpleNamespace(
        session=SimpleNamespace(client_params=params))))
    _notice(h)
    _bump(_tid(pro), 2)
    _work(61)
    assert server._host_name() == "cursor-vscode" and not server._save_is_hooked()
    assert any((n or "").startswith("Sibyl Memory Pro") for n in (_notice(h)[1] for _ in range(3)))


@pytest.mark.parametrize("name", ["claude code", "Claude Code", "claude-code"])
def test_both_claude_code_host_spellings_leave_the_request_to_the_hook(pro, monkeypatch, name):
    """F12 (R1 check): Claude Code 2.1.285 sends clientInfo.name "claude code";
    earlier builds "claude-code". Both are Claude Code."""
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "exec /v/bin/sibyl hook stop"}]}]}}))
    _server()
    params = SimpleNamespace(clientInfo=SimpleNamespace(name=name, version="2.1.285"))
    monkeypatch.setattr(server, "_SERVER", SimpleNamespace(get_context=lambda: SimpleNamespace(
        session=SimpleNamespace(client_params=params))))
    server._HOST_CACHE.clear()
    assert server._save_is_hooked() is True
    assert set(server._CLAUDE_CODE_HOSTS) == {"claude-code", "claude code"}
