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

Harness checks (cli 0.7.0, spec dsh-doctor v2 section 5): after the server
round trip, each detected harness's sibyl-memory wiring is probed and
reported (ok, missing, stale, broken, foreign, duplicate, unreadable,
disabled, overridden, unverifiable). Read-only by default. ``--fix`` repairs
stale and broken entries without asking; at a terminal each repair is asked,
default No. A missing entry is a warning and is never added (the
``sibyl setup <target>`` command is named instead). Doctor never installs
packages and only ever starts a command whose name starts with
sibyl-memory-mcp. Timestamped ``.bak`` files older versions left beside
harness configs are listed, never touched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
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
    from .setup import resolve_mcp_binary  # same lookup as setup (cli 0.6.1)
    w = resolve_mcp_binary()
    return [w] if w else None


# ----------------------------------------------------------------------
# Harness checks (spec dsh-doctor v2, 5.2 to 5.7)
# ----------------------------------------------------------------------

@dataclass
class HarnessCheck:
    name: str            # wirer.name
    display_name: str
    location: str        # "" or "home" / "profile:<name>" for DeepSeek Harness
    status: str          # ok | missing | stale | broken | foreign | duplicate | unreadable
                         # | disabled | overridden | unverifiable
    level: str           # pass | warn | fail
    command: Optional[str]
    config_path: str
    detail: str
    fixable: bool
    hint: str = ""       # e.g. "sibyl setup cursor" for missing
    fixed: bool = False
    fix_message: str = ""
    # Panel fix 8: text to paste by hand, kept with its newlines (valid YAML
    # or TOML as printed); printed under the FAIL line.
    fix_block: str = ""


_WARN_STATUSES = ("foreign", "duplicate", "unreadable", "disabled", "overridden", "unverifiable")


def _loc_suffix(location: str) -> str:
    if location.startswith("profile:"):
        return ", profile " + location.split(":", 1)[1]
    return ""


def _abs_command(cmd: str) -> Optional[str]:
    if os.path.isabs(cmd):
        return cmd
    w = shutil.which(cmd)
    return os.path.abspath(w) if w else None


def check_harness(wirer: Any, probe: dict, *, verified: dict) -> HarnessCheck:
    """Turn one probe into a check. ``verified`` caches _verify_mcp_starts
    results per absolute path (the round-trip server path is pre-seeded)."""
    from . import setup as S
    entry = probe.get("entry")
    cmd = probe.get("command")
    cfg = str(probe.get("config_path") or "")
    loc = probe.get("location") or ""
    pdetail = probe.get("detail") or ""

    def mk(status: str, level: str, detail: str, fixable: bool = False, hint: str = "") -> HarnessCheck:
        return HarnessCheck(wirer.name, wirer.display_name, loc, status, level, cmd, cfg,
                            detail, fixable, hint)

    if entry in _WARN_STATUSES:
        # Name the config file once (sandbox M4): a detail that already lists
        # it, such as the duplicate one, is not followed by it again.
        if not pdetail:
            c = mk(entry, "warn", f"{entry}: see {cfg}")
        elif cfg and cfg in pdetail:
            c = mk(entry, "warn", f"{entry}: {pdetail}")
        else:
            c = mk(entry, "warn", f"{entry}: {pdetail}; {cfg}")
    elif entry == "absent":
        hint = f"sibyl setup {wirer.name}"
        c = mk("missing", "warn", f"missing: no sibyl-memory entry; to add it run: {hint}",
               hint=hint)
    elif entry == "present" and probe.get("broken"):
        c = mk("broken", "fail", f"broken: {pdetail or 'the entry does not work'}; {cfg}",
               fixable=bool(probe.get("fixable", True)))
    elif entry == "present" and cmd is None:
        c = mk("ok", "pass", pdetail or "wired")
    elif entry == "present" and not S._command_resolves(cmd, strict=True):
        why = "does not exist" if os.path.isabs(cmd) else "is not on PATH"
        c = mk("stale", "fail", f"stale: {cmd} {why}; {cfg}", fixable=True)
    elif entry == "present":
        path = _abs_command(cmd) or cmd
        if not S._is_our_command(path):
            # Doctor only ever starts a command named sibyl-memory-mcp*.
            c = mk("foreign", "warn", f"foreign: the entry runs another command; {cfg}")
            c.command = None     # never a foreign command string in the output
        else:
            if path not in verified:
                verified[path] = bool(S._verify_mcp_starts(path)[0])
            if not verified[path]:
                mine = S.resolve_mcp_binary()
                alt = bool(mine and os.path.abspath(mine) != os.path.abspath(path))
                if alt and mine not in verified:
                    verified[mine] = bool(S._verify_mcp_starts(mine)[0])
                c = mk("broken", "fail", f"broken: {cmd} does not start; {cfg}",
                       fixable=bool(alt and verified.get(mine)))
            else:
                notes = []
                mine = S.resolve_mcp_binary()
                if mine and os.path.abspath(mine) != os.path.abspath(path):
                    notes.append("uses a different install than this CLI")
                if not os.path.isabs(cmd):
                    notes.append("relies on PATH")
                c = mk("ok", "pass", "; ".join([f"wired; {cmd}"] + notes))
    else:
        c = mk("unreadable", "warn", f"unreadable: unknown probe result; {cfg}")
    # Nothing to fix by hand: customized entries, and no installed server to
    # point at (Hermes broken excepted: its repair installs the plugin).
    if c.fixable and probe.get("customized"):
        c.fixable = False
        c.detail += f" ({pdetail})" if pdetail else (
            " (entry has custom env or args; update the path by hand)")
    if c.fixable and S.resolve_mcp_binary() is None and not (
            wirer.name == "hermes" and c.status == "broken"):
        c.fixable = False
    if c.status in ("stale", "broken") and not c.fixable and wirer.name == "claude-code" \
            and probe.get("customized"):
        binpath = S.mcp_command_path()
        c.detail = (f"{c.status}: entry has custom env or args; update the path by hand: "
                    f"claude mcp remove -s user sibyl-memory, then claude mcp add --scope user "
                    f"sibyl-memory -- {binpath} with your settings")
    return c


def find_legacy_backups() -> list:
    """Timestamped .bak files that sibyl versions before 0.7.0 wrote beside
    harness configs. Listed only: never opened, moved or deleted."""
    from . import setup as S
    out = []
    for name, cls in S.ALL_WIRERS.items():
        if name == "deepseek":
            continue          # it never wrote one
        try:
            w = cls()
        except Exception:
            continue
        cfg = getattr(w, "settings_path", None) if name == "claude-code" else getattr(w, "config_path", None)
        if cfg is None:
            continue
        cfg = Path(cfg)
        pat = re.compile(r"^" + re.escape(cfg.name) + r"\.\d{8}T\d{6}Z\.bak$")
        try:
            for f in cfg.parent.iterdir():
                if pat.match(f.name):
                    out.append(str(f.absolute()))
        except OSError:
            continue
    return sorted(set(out))


# `doctor --harness` names that check another harness's file: Grok Bot is
# wired into the Cursor file (sibyl setup grokbot), so its check is Cursor's.
DOCTOR_ALIASES: dict = {"grokbot": "cursor"}


def run_harness_checks(only: list, verified: dict) -> list:
    """[(wirer, probe, check)] for each detected harness (default paths;
    Grok Bot is the Cursor file, so it is not checked twice)."""
    from . import setup as S
    rows = []
    for name, cls in S.ALL_WIRERS.items():
        if only and name not in only:
            continue
        try:
            w = cls()
            present = w.is_present()
        except Exception:
            continue
        if not present:
            if only:
                rows.append((w, None, HarnessCheck(name, w.display_name, "", "ok", "pass", None,
                                                   str(getattr(w, "config_path", "") or ""),
                                                   "not detected", False)))
            continue
        try:
            probes = w.doctor_probe()
        except Exception as e:  # doctor_probe never raises; belt and braces
            probes = [{"entry": "unreadable", "command": None, "location": "",
                       "config_path": str(getattr(w, "config_path", "")), "detail": type(e).__name__}]
        for pr in probes:
            rows.append((w, pr, check_harness(w, pr, verified=verified)))
    return rows


def repair_check(wirer: Any, probe: dict, check: HarnessCheck, verified: dict) -> tuple:
    """Repair one check. Every repair runs with stdout captured (the Hermes
    plugin installer prints a banner; under --json that would break the
    document). Returns (new check, outcome or None)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = wirer.repair_mcp(probe)
    except Exception as e:
        check.fixed = False
        check.fix_message = f"repair failed ({type(e).__name__})"
        return check, None
    try:
        with contextlib.redirect_stdout(buf):
            probes = wirer.doctor_probe()
    except Exception:
        probes = []
    same = [p for p in probes if (p.get("location") or "") == (probe.get("location") or "")]
    newp = same[0] if same else (probes[0] if probes else probe)
    new = check_harness(wirer, newp, verified=verified)
    new.fixed = new.status == "ok"
    if not new.fixed:
        # A repair that did not take fails the run whatever the entry is now
        # (a failed Claude Code re-add leaves it unregistered: "missing").
        new.level = "fail"
        new.fixable = False
    text = str(out.message or "")
    block = getattr(out, "manual_block", None)
    if block and text.endswith(block):
        # Panel fix 8: the paste-by-hand block keeps its line breaks; only the
        # lead sentence is folded onto the FAIL line.
        text = text[:-len(block)]
        new.fix_block = block
    msg = " ".join(text.split())
    if out.backup_path:
        msg += f"; backup at {out.backup_path}"
    new.fix_message = msg
    return new, out


def _prompt_for(wirer: Any, check: HarnessCheck) -> str:
    from . import setup as S
    newp = S.mcp_command_path()
    if wirer.name == "hermes":
        return (f"Repair Hermes? (installs the Sibyl plugin into {wirer.plugin_dir}; "
                "config.yaml is not changed)")
    if wirer.name == "claude-code":
        return ("Repair Claude Code? (removes the sibyl-memory registration and adds it again "
                f"with {newp})")
    return (f"Repair {check.display_name}{_loc_suffix(check.location)}? "
            f"(points it at {newp}; a backup is kept)")


def _doctor_handoff(steps: list, server_cmd: Optional[list], checks: list, repairs: list) -> tuple:
    """(next, human, later) by the rules of spec 5.5, first match wins.
    ``repairs``: [(check after repair, outcome or None)]."""
    from . import setup as S
    later = tuple(dict.fromkeys(c.hint for c in checks if c.status == "missing" and c.hint))
    if not all(s.ok for s in steps):
        if any("sign-in required" in (s.detail or "") for s in steps):
            return "sibyl init", None, later
        if server_cmd is None:
            nxt, human, _l = S._binary_missing_handoff("doctor")
            return nxt, human, later + (("sibyl doctor",) if nxt != "done" else ())
        return "done", "run sibyl-memory-mcp in a terminal and tell me the first error line", later
    for c, out in repairs:
        if not c.fixed:
            if out is not None and out.handoff:
                nxt, human, more = out.handoff
                return nxt, human, later + tuple(more or ())
            why = " ".join((c.fix_message or c.detail or "an error").split())[:200]
            return "done", f"{c.display_name} wiring could not be repaired ({why})", later
    if any(c.fixable and c.level == "fail" for c in checks):
        return "sibyl doctor --fix", None, later
    for c in checks:
        if c.level == "fail":
            return "done", f"{c.config_path} needs fixing by hand ({c.detail})", later
    fixed = [c for c, _o in repairs if c.fixed]
    if fixed:
        asks = list(dict.fromkeys(S._RESTART_HUMAN.get(c.name, f"restart {c.display_name}")
                                  for c in fixed))
        return "done", "; ".join(asks), later
    return "done", None, later


def _check_line(c: HarnessCheck) -> str:
    who = f"{c.display_name}{_loc_suffix(c.location)}"
    if c.fixed:
        return a.success_line(f"FIXED  {who}  ({c.fix_message})")
    if c.level == "pass":
        return a.success_line(f"PASS  {who}  ({c.detail})")
    if c.level == "warn":
        return a.warn_line(f"WARN  {who}  ({c.detail})")
    line = f"FAIL  {who}  ({c.detail})"
    if c.fix_message:
        line += f"  not fixed: {c.fix_message}"
    if c.fix_block:
        # Column 0, unstyled, so it can be pasted as it is.
        return a.err_line(line) + "\n" + c.fix_block
    return a.err_line(line)


def _print_harness_report(checks: list, legacy: list) -> None:
    print()
    print("Harnesses")
    if not checks:
        print(a.dim("  none detected (sibyl setup wires one)"))
    for c in checks:
        print(_check_line(c))
    if legacy:
        print(a.dim(f"  INFO  {len(legacy)} backup file{'s' if len(legacy) != 1 else ''} from "
                    "older sibyl versions sit beside harness configs (not removed):"))
        for p in legacy:
            print(a.dim(f"        {p}"))


def run_doctor(args: Any) -> int:
    from . import _handoff as _h
    from . import setup as S
    db_path = Path(getattr(args, "db", None) or "~/.sibyl-memory/memory.db").expanduser()
    cred_path = Path(getattr(args, "credentials", None) or "~/.sibyl-memory/credentials.json").expanduser()
    as_json = bool(getattr(args, "json", False))
    fix = bool(getattr(args, "fix", False))
    only = [DOCTOR_ALIASES.get(n, S.SETUP_ALIASES.get(n, n))
            for n in (getattr(args, "harness", None) or [])]
    no_harness = bool(getattr(args, "no_harness", False))
    cmd = _server_cmd()
    if cmd is None:
        steps = [Step("start server", False, "sibyl-memory-mcp not found. Run: pip install sibyl-memory-mcp")]
    else:
        steps = run_checks(server_cmd=cmd, db_path=db_path, cred_path=cred_path)
    server_ok = all(s.ok for s in steps)

    verified: dict = {}
    if cmd is not None and any(s.name == "start server" and s.ok for s in steps):
        verified[os.path.abspath(cmd[0])] = True
    rows: list = []
    legacy: list = []
    if not no_harness:
        rows = run_harness_checks(only, verified)
        legacy = find_legacy_backups()

    def checks() -> list:
        return [c for _w, _p, c in rows]

    wants = [i for i, (_w, p, c) in enumerate(rows) if c.fixable and p is not None]
    prompted = False
    repairs: list = []
    if wants and fix:
        to_fix = wants
    elif wants and not as_json and sys.stdin.isatty() and sys.stdout.isatty():
        prompted = True
        _print_steps(steps)
        _print_harness_report(checks(), legacy)
        print()
        to_fix = [i for i in wants
                  if S._interactive_prompt(_prompt_for(rows[i][0], rows[i][2]), default="N") == "y"]
    else:
        to_fix = []
    attempted, fixed_ids = [], []
    for i in to_fix:
        w, p, c = rows[i]
        new, out = repair_check(w, p, c, verified)
        rows[i] = (w, p, new)
        repairs.append((new, out))
        ident = w.name + (f":{c.location}" if c.location else "")
        attempted.append(ident)
        if new.fixed:
            fixed_ids.append(ident)

    all_checks = checks()
    harness_fail = any(c.level == "fail" and not c.fixed for c in all_checks)
    ok = server_ok and not harness_fail
    nxt, human, later = _doctor_handoff(steps, cmd, all_checks, repairs)

    if as_json:
        payload = {
            "ok": ok,
            "server_ok": server_ok,
            "steps": [s.__dict__ for s in steps],
            "harnesses": [asdict(c) for c in all_checks],
            "fix": {"requested": fix, "attempted": attempted, "fixed": fixed_ids},
            "legacy_backups": {"count": len(legacy), "paths": legacy},
        }
        _h.json_handoff(payload, nxt, human)
        payload["later"] = list(later)
        print(json.dumps(payload, indent=2))
        return 0 if ok else 1

    if prompted:
        if repairs:
            print()
            for c, _o in repairs:
                print(_check_line(c))
    else:
        _print_steps(steps)
        if not no_harness:
            _print_harness_report(all_checks, legacy)
    print()
    warned = any(c.level == "warn" for c in all_checks)
    if not ok:
        print(a.err_line("Doctor: some steps failed."))
    elif warned:
        print(a.warn_line("Doctor: passed with warnings."))
    else:
        print(a.success_line("Doctor: all steps passed."))
    print()
    _h.handoff(nxt, human=human, later=later)
    return 0 if ok else 1


def _print_steps(steps: list) -> None:
    print()
    for s in steps:
        mark = "PASS" if s.ok else ("SKIP" if s.skipped else "FAIL")
        line = f"{mark}  {s.name}" + (f"  ({s.detail})" if s.detail else "")
        print(a.success_line(line) if s.ok else a.err_line(line))
