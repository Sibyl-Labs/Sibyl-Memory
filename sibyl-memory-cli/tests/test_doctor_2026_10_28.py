"""`sibyl doctor` (A5). A stub stdio MCP server stands in for the real one on the
happy path (the real server's account gate verifies a production-signed
entitlement, which a test cannot mint); the real server is exercised for
start + tools/list + the no-account failure. Temp HOME/store only."""
import argparse
import json
import shutil
import sqlite3
import sys
import textwrap

import pytest

from sibyl_memory_cli import doctor as D

STUB = textwrap.dedent('''
    import json, sys, os, sqlite3
    db = os.environ["SIBYL_MEMORY_DB"]
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS entities (category TEXT, name TEXT, body TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS archived_entities (category TEXT, name TEXT, body TEXT)")
    conn.commit()
    MODE = os.environ.get("STUB_MODE", "ok")
    def reply(i, result): print(json.dumps({"jsonrpc":"2.0","id":i,"result":result}), flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m: continue
        meth, p, i = m["method"], m.get("params", {}), m["id"]
        if meth == "initialize":
            reply(i, {"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"stub","version":"0"}})
        elif meth == "tools/list":
            names = ["memory_remember","memory_recall","memory_forget"]
            if MODE == "missing": names = names[:1]
            reply(i, {"tools":[{"name":n,"inputSchema":{"type":"object"}} for n in names]})
        elif meth == "tools/call":
            t, a = p["name"], p["arguments"]
            if t == "memory_remember":
                if MODE == "noaccount":
                    reply(i, {"isError":True,"content":[{"type":"text","text":json.dumps({"ok":False,"code":"ACCOUNT_REQUIRED"})}]}); continue
                conn.execute("INSERT INTO entities VALUES (?,?,?)",(a["category"],a["name"],json.dumps(a["body"]))); conn.commit()
                reply(i, {"structuredContent":{"ok":True},"content":[]})
            elif t == "memory_recall":
                r = conn.execute("SELECT body FROM entities WHERE category=? AND name=?",(a["category"],a["name"])).fetchone()
                if r: reply(i, {"structuredContent":{"ok":True,"entity":{"body":json.loads(r[0])}},"content":[]})
                else: reply(i, {"isError":True,"content":[{"type":"text","text":"{\\"code\\":\\"NOT_FOUND\\"}"}]})
            elif t == "memory_forget":
                r = conn.execute("SELECT body FROM entities WHERE category=? AND name=?",(a["category"],a["name"])).fetchone()
                if MODE != "leave_entity":
                    conn.execute("DELETE FROM entities WHERE category=? AND name=?",(a["category"],a["name"]))
                conn.execute("INSERT INTO archived_entities VALUES (?,?,?)",(a["category"],a["name"],r[0])); conn.commit()
                reply(i, {"structuredContent":{"ok":True},"content":[]})
''')


@pytest.fixture
def stub(tmp_path, monkeypatch):
    f = tmp_path / "stub_server.py"
    f.write_text(STUB)
    return [sys.executable, str(f)]


def _run(stub, tmp_path, mode="ok", monkeypatch=None, **kw):
    monkeypatch.setenv("STUB_MODE", mode)
    db = tmp_path / "store" / "memory.db"
    db.parent.mkdir(exist_ok=True)
    return D.run_checks(server_cmd=stub, db_path=db, cred_path=tmp_path / "c.json", **kw), db


def _count(db, table):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_happy_path_all_pass_and_nothing_left(stub, tmp_path, monkeypatch):
    steps, db = _run(stub, tmp_path, monkeypatch=monkeypatch)
    assert [s.name for s in steps] == ["start server", "list tools", "save test memory",
        "recall test memory", "delete test memory", "verify nothing left behind"]
    assert all(s.ok for s in steps), steps
    assert _count(db, "entities") == 0 and _count(db, "archived_entities") == 0


def test_preexisting_user_rows_untouched(stub, tmp_path, monkeypatch):
    db = tmp_path / "store" / "memory.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE entities (category TEXT, name TEXT, body TEXT)")
    conn.execute("CREATE TABLE archived_entities (category TEXT, name TEXT, body TEXT)")
    conn.execute("INSERT INTO entities VALUES ('people','alice','{}')")
    conn.execute("INSERT INTO archived_entities VALUES ('_sibyl_doctor','other','{}')")
    conn.commit(); conn.close()
    steps, _ = _run(stub, tmp_path, monkeypatch=monkeypatch)
    assert all(s.ok for s in steps)
    assert _count(db, "entities") == 1 and _count(db, "archived_entities") == 1


def test_leftover_row_is_purged_and_verified(stub, tmp_path, monkeypatch):
    # server fails to delete the live row; the purge still removes it
    steps, db = _run(stub, tmp_path, mode="leave_entity", monkeypatch=monkeypatch)
    assert steps[-1].name == "verify nothing left behind" and steps[-1].ok
    assert _count(db, "entities") == 0


def test_purge_failure_is_reported(stub, tmp_path, monkeypatch):
    steps, _ = _run(stub, tmp_path, monkeypatch=monkeypatch, purge=lambda db, n: 2)
    assert not steps[-1].ok and "2 test row" in steps[-1].detail


def test_missing_tools_fail(stub, tmp_path, monkeypatch):
    steps, _ = _run(stub, tmp_path, mode="missing", monkeypatch=monkeypatch)
    assert steps[-1].name == "list tools" and not steps[-1].ok


def test_no_account_message_and_skips(stub, tmp_path, monkeypatch):
    steps, _ = _run(stub, tmp_path, mode="noaccount", monkeypatch=monkeypatch)
    by = {s.name: s for s in steps}
    assert not by["save test memory"].ok and "sibyl init" in by["save test memory"].detail
    assert by["recall test memory"].skipped and by["delete test memory"].skipped


def test_server_that_cannot_start(tmp_path):
    steps = D.run_checks(server_cmd=["/nonexistent/server"], db_path=tmp_path / "m.db",
                         cred_path=tmp_path / "c.json")
    assert len(steps) == 1 and not steps[0].ok


@pytest.mark.skipif(shutil.which("sibyl-memory-mcp") is None, reason="sibyl-memory-mcp not on PATH")
def test_real_server_starts_lists_tools_and_fails_cleanly_without_account(tmp_path):
    db = tmp_path / "s" / "memory.db"
    steps = D.run_checks(server_cmd=[shutil.which("sibyl-memory-mcp")], db_path=db,
                         cred_path=tmp_path / "none.json")
    by = {s.name: s for s in steps}
    assert by["start server"].ok and by["list tools"].ok
    assert not by["save test memory"].ok and "sibyl init" in by["save test memory"].detail
    # nothing was saved, so no purge step and no rows
    assert "verify nothing left behind" not in by
    if db.exists():
        assert _count(db, "entities") == 0


def test_cmd_doctor_json_and_exit_code(stub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(D, "_server_cmd", lambda: stub)
    monkeypatch.setenv("STUB_MODE", "ok")
    ns = argparse.Namespace(db=str(tmp_path / "m" / "memory.db"),
                            credentials=str(tmp_path / "c.json"), json=True)
    (tmp_path / "m").mkdir()
    assert D.run_doctor(ns) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and len(out["steps"]) == 6
    monkeypatch.setenv("STUB_MODE", "noaccount")
    assert D.run_doctor(ns) == 1
