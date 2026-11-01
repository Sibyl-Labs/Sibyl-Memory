"""`sibyl doctor`: end-to-end check of the MCP path an agent actually uses.

Starts the real ``sibyl-memory-mcp`` stdio server, lists its tools, saves a
clearly MARKED test memory, recalls it, deletes it, then verifies nothing is
left behind. Reports pass/fail per step.

Deletion note: the server's ``memory_forget`` tool ARCHIVES (the body is kept in
``archived_entities``). A doctor run must leave no data, so after exercising
``memory_forget`` the exact test row (a unique uuid name) is purged from both
``entities`` and ``archived_entities`` with a narrowly scoped DELETE, and the
purge is verified with row counts. Nothing but that one name is ever touched.

The MCP client here is a minimal newline-delimited JSON-RPC speaker over the
child's pipes (no dependency on the ``mcp`` package).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import _aesthetic as a

TEST_CATEGORY = "_sibyl_doctor"
REQUIRED_TOOLS = ("memory_remember", "memory_recall", "memory_forget")
STEP_TIMEOUT = 25.0


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


class McpStdioClient:
    """Just enough MCP over stdio: initialize, tools/list, tools/call."""

    def __init__(self, cmd: list, env: dict):
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._next = 1
        self._err = b""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _drain_err(self) -> None:
        try:
            for chunk in iter(lambda: self.proc.stderr.read(1024), b""):
                self._err = (self._err + chunk)[-2000:]
        except Exception:
            pass

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._q.put(msg)
        except Exception:
            pass

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, timeout: float = STEP_TIMEOUT) -> dict:
        rid = self._next
        self._next += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
            try:
                msg = self._q.get(timeout=left)
            except queue.Empty:
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]
            if self.proc.poll() is not None and self._q.empty():
                raise RuntimeError(f"server exited (code {self.proc.returncode}): "
                                   f"{self._err.decode(errors='replace')[:200]}")

    def initialize(self) -> dict:
        res = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "sibyl-doctor", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def call(self, tool: str, arguments: dict) -> tuple:
        """Returns (is_error, payload dict)."""
        res = self.request("tools/call", {"name": tool, "arguments": arguments})
        payload: Any = res.get("structuredContent")
        if not isinstance(payload, dict):
            payload = {}
            for part in res.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    try:
                        loaded = json.loads(part.get("text", ""))
                        if isinstance(loaded, dict):
                            payload = loaded
                            break
                    except ValueError:
                        payload = {"message": str(part.get("text", ""))[:300]}
        return bool(res.get("isError")), payload

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def purge_test_rows(db_path: Path, name: str) -> int:
    """Delete the doctor's test rows (exact category + unique name) from
    entities and archived_entities. Returns how many rows remain (0 = clean)."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        for table in ("entities", "archived_entities"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE category = ? AND name = ?",
                             (TEST_CATEGORY, name))
            except sqlite3.Error:
                pass
        conn.commit()
        left = 0
        for table in ("entities", "archived_entities"):
            try:
                left += conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE category = ? AND name = ?",
                    (TEST_CATEGORY, name)).fetchone()[0]
            except sqlite3.Error:
                pass
        return left
    finally:
        conn.close()


def run_checks(*, server_cmd: list, db_path: Path, cred_path: Path,
               purge: Callable[[Path, str], int] = purge_test_rows) -> list:
    steps: list = []
    env = dict(os.environ)
    env["SIBYL_MEMORY_DB"] = str(db_path)
    env["SIBYL_CREDENTIALS"] = str(cred_path)
    name = f"doctor-test-{uuid.uuid4().hex[:12]}"
    marker = f"SIBYL-DOCTOR-TEST-{uuid.uuid4().hex}"
    body = {"marker": marker, "note": "sibyl doctor test memory. Safe to delete; removed automatically."}

    try:
        client = McpStdioClient(server_cmd, env)
    except Exception as e:
        steps.append(Step("start server", False, f"{type(e).__name__}: {e}"))
        return steps

    saved = False
    try:
        try:
            info = client.initialize()
            sv = (info.get("serverInfo") or {})
            steps.append(Step("start server", True,
                              f"{sv.get('name', 'mcp server')} {sv.get('version', '')}".strip()))
        except Exception as e:
            steps.append(Step("start server", False, str(e)))
            return steps

        try:
            tools = [t.get("name") for t in client.request("tools/list").get("tools", [])]
            missing = [t for t in REQUIRED_TOOLS if t not in tools]
            if missing:
                steps.append(Step("list tools", False, "missing: " + ", ".join(missing)))
                return steps
            steps.append(Step("list tools", True, f"{len(tools)} tools"))
        except Exception as e:
            steps.append(Step("list tools", False, str(e)))
            return steps

        try:
            is_err, res = client.call("memory_remember",
                                      {"category": TEST_CATEGORY, "name": name, "body": body})
            if is_err or not res.get("ok", False):
                raw = json.dumps(res)[:300] if res else "tool error"
                code = res.get("code") if isinstance(res, dict) else None
                if code == "ACCOUNT_REQUIRED" or "ACCOUNT_REQUIRED" in raw:
                    steps.append(Step("save test memory", False,
                                      "sign-in required: run `sibyl init` in a terminal, then re-run `sibyl doctor`"))
                else:
                    steps.append(Step("save test memory", False, raw))
                for n in ("recall test memory", "delete test memory"):
                    steps.append(Step(n, False, "skipped (save failed)", skipped=True))
                return steps
            saved = True
            steps.append(Step("save test memory", True, f"{TEST_CATEGORY}/{name}"))
        except Exception as e:
            steps.append(Step("save test memory", False, str(e)))
            return steps

        try:
            is_err, res = client.call("memory_recall", {"category": TEST_CATEGORY, "name": name})
            text = json.dumps(res)
            if is_err or marker not in text:
                steps.append(Step("recall test memory", False, "saved memory did not come back"))
            else:
                steps.append(Step("recall test memory", True, "marker round-tripped"))
        except Exception as e:
            steps.append(Step("recall test memory", False, str(e)))

        try:
            is_err, res = client.call("memory_forget",
                                      {"category": TEST_CATEGORY, "name": name,
                                       "reason": "sibyl doctor cleanup"})
            if is_err:
                steps.append(Step("delete test memory", False, json.dumps(res)[:300]))
            else:
                steps.append(Step("delete test memory", True, "forgotten via memory_forget"))
        except Exception as e:
            steps.append(Step("delete test memory", False, str(e)))
    finally:
        client.close()
        if saved:
            # Runs even when a step above failed: the doctor never leaves data.
            try:
                left = purge(db_path, name)
                if left:
                    steps.append(Step("verify nothing left behind", False,
                                      f"{left} test row(s) remain for {TEST_CATEGORY}/{name}"))
                else:
                    steps.append(Step("verify nothing left behind", True, "0 test rows remain"))
            except Exception as e:
                steps.append(Step("verify nothing left behind", False,
                                  f"{type(e).__name__}: {e}"))
    return steps


def _server_cmd() -> Optional[list]:
    w = shutil.which("sibyl-memory-mcp")
    if w:
        return [w]
    cand = Path(sys.executable).parent / "sibyl-memory-mcp"
    if cand.exists():
        return [str(cand)]
    return None


def run_doctor(args: Any) -> int:
    db_path = Path(getattr(args, "db", None) or "~/.sibyl-memory/memory.db").expanduser()
    cred_path = Path(getattr(args, "credentials", None) or "~/.sibyl-memory/credentials.json").expanduser()
    as_json = bool(getattr(args, "json", False))
    cmd = _server_cmd()
    if cmd is None:
        steps = [Step("start server", False, "sibyl-memory-mcp not found. Run: pip install sibyl-memory-mcp")]
    else:
        steps = run_checks(server_cmd=cmd, db_path=db_path, cred_path=cred_path)
    ok = all(s.ok for s in steps)
    if as_json:
        print(json.dumps({"ok": ok, "steps": [s.__dict__ for s in steps]}, indent=2))
        return 0 if ok else 1
    print()
    for s in steps:
        mark = "PASS" if s.ok else ("SKIP" if s.skipped else "FAIL")
        line = f"{mark}  {s.name}" + (f"  ({s.detail})" if s.detail else "")
        print(a.success_line(line) if s.ok else a.err_line(line))
    print()
    print(a.success_line("Doctor: all steps passed.") if ok else a.err_line("Doctor: some steps failed."))
    print()
    return 0 if ok else 1
