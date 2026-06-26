"""`sibyl setup` guided onboarding flow (v2): backup -> wire MCP -> extract -> verify -> debloat.

Design (operator-locked 2026-05-31): one dynamic, resumable, guided flow that gets a
user "set up and optimized" no matter which harness they run. The CLI does the
DETERMINISTIC work (back up files, detect state, verify the DB, trim files) and
CONDUCTS; the user's own harness does the semantic EXTRACTION (it has the memory
tools). Every gap (no plugin, MCP not wired) prints exact per-harness instructions.

This module adds the new phases on top of the existing wirers in setup.py
(HermesWirer / ClaudeCodeWirer) and adds CodexWirer so all three harnesses are
first-class. Nothing here touches live files except the explicitly-confirmed
debloat step, and only after a verified backup exists.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import _aesthetic as A

# ----------------------------------------------------------------------
# 1. Memory/agent file discovery (per harness)
# ----------------------------------------------------------------------
# Candidate memory + agent files we back up + (optionally) extract from.
# Globs are resolved relative to `home`. Directories are copied whole.

HARNESS_FILES: dict[str, list[str]] = {
    "claude-code": ["CLAUDE.md", ".claude/CLAUDE.md", ".claude/settings.json"],
    "codex":       ["AGENTS.md", ".codex/config.toml", ".codex/AGENTS.md"],
    "hermes":      [".hermes/config.yaml", ".hermes/memory"],
    "generic":     ["AGENTS.md", "MEMORY.md", "memory.md", ".cursorrules", ".cursor/rules"],
}


@dataclass
class FoundFile:
    harness: str
    path: Path          # absolute
    rel: str            # path relative to home (for backup layout)
    is_dir: bool
    size: int


def _backup_rel(p: Path, home: Path, cwd: Optional[Path]) -> str:
    """Collision-free backup path for a source file. Files under home keep their
    home-relative path; files outside home (a project elsewhere) get a `project/`
    prefix; anything else `external/<name>`. This prevents a home file and a
    same-named project file from clobbering each other in the backup (data-loss bug)."""
    try:
        if p.is_relative_to(home):
            return str(p.relative_to(home))
    except (ValueError, OSError):
        pass
    if cwd:
        try:
            cwd = Path(cwd)
            if p.is_relative_to(cwd):
                return "project/" + str(p.relative_to(cwd))
        except (ValueError, OSError):
            pass
    return "external/" + p.name


def scan_memory_files(home: Optional[Path] = None, cwd: Optional[Path] = None) -> list[FoundFile]:
    """Find existing memory/agent files across harnesses. De-dupes by resolved path.
    Looks in both the user's home and the current project dir (CLAUDE.md lives in projects)."""
    home = Path(home).expanduser() if home else Path.home()
    roots = [home]
    if cwd:
        roots.append(Path(cwd))
    seen: set[Path] = set()
    found: list[FoundFile] = []
    for harness, rels in HARNESS_FILES.items():
        for rel in rels:
            for root in roots:
                p = (root / rel)
                if not p.exists():
                    continue
                try:
                    key = p.resolve()
                except OSError:
                    key = p
                if key in seen:
                    continue
                seen.add(key)
                is_dir = p.is_dir()
                size = _tree_size(p) if is_dir else p.stat().st_size
                found.append(FoundFile(harness, p, _backup_rel(p, home, cwd), is_dir, size))
    return found


def _tree_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _fsync_path(p: Path) -> None:
    """Best-effort fsync of a file or directory so a crash right after backup
    can't leave a partially-flushed copy. CLI-9: backups must survive a power
    loss before we trust them enough to trim the originals."""
    try:
        if p.is_dir():
            flags = getattr(os, "O_DIRECTORY", 0)
            fd = os.open(str(p), os.O_RDONLY | flags)
        else:
            fd = os.open(str(p), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # Directory fsync is not portable everywhere; never fail the backup on it.
        pass


# ----------------------------------------------------------------------
# 2. Backup (deterministic, verified, timestamped) — the safety win
# ----------------------------------------------------------------------

@dataclass
class BackupResult:
    backup_dir: Path
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0
    ok: bool = True
    error: Optional[str] = None


def backup_dir_name(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return "sibyl-migration-backup-" + now.strftime("%Y-%m-%dT%H_%M_%S")


def run_backup(files: list[FoundFile], dest_parent: Path, *, now: Optional[datetime] = None) -> BackupResult:
    """Copy each found file/dir into a fresh timestamped backup folder under dest_parent.
    Verifies byte counts. Never modifies sources. Aborts (ok=False) on first failure."""
    dest_parent = Path(dest_parent).expanduser()
    backup = dest_parent / backup_dir_name(now)
    res = BackupResult(backup_dir=backup)
    try:
        backup.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        res.ok = False; res.error = f"could not create backup dir: {e}"
        return res
    for f in files:
        target = backup / f.rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if f.is_dir:
                shutil.copytree(f.path, target, dirs_exist_ok=True)
                src_sz, dst_sz = _tree_size(f.path), _tree_size(target)
                # CLI-9: fsync every copied file in the tree so the backup is
                # durable before we ever trim an original.
                for cf in target.rglob("*"):
                    if cf.is_file():
                        _fsync_path(cf)
            else:
                shutil.copy2(f.path, target)
                src_sz, dst_sz = f.path.stat().st_size, target.stat().st_size
                _fsync_path(target)  # CLI-9: durable copy before any trim
            if src_sz != dst_sz:
                res.ok = False; res.error = f"byte mismatch on {f.rel} ({src_sz} != {dst_sz})"
                return res
            res.files.append(f.rel); res.total_bytes += dst_sz
        except Exception as e:
            res.ok = False; res.error = f"copy failed on {f.rel}: {type(e).__name__}: {e}"
            return res
    # CLI-9: fsync the backup directory itself so its directory entries (the
    # newly-created files) are persisted, not just the file contents.
    _fsync_path(backup)
    return res


# ----------------------------------------------------------------------
# 3. Wirers live in setup.py (canonical). Codex now auto-wires config.toml;
#    Claude Code registers via `claude mcp add --scope user`.
# ----------------------------------------------------------------------

from .setup import CodexWirer, ClaudeCodeWirer, HermesWirer  # noqa: E402  (canonical wirers)


# Per-harness wiring instructions for the guided flow (no silent edits across the board;
# we print and let the user run them, matching the operator's 'walk them through' intent).
def wire_instructions(harness: str) -> list[str]:
    if harness == "claude-code":
        return ["Open a new terminal and run:",
                "    claude mcp add sibyl-memory -- sibyl-memory-mcp",
                "Restart Claude Code (or /mcp -> reconnect sibyl-memory), then return here."]
    if harness == "codex":
        return CodexWirer().instructions()
    if harness == "hermes":
        return ["Open a new terminal and run:",
                "    sibyl-memory-hermes install-plugin",
                "Then set  memory.provider: sibyl  in ~/.hermes/config.yaml and restart Hermes."]
    return ["Register an MCP server named 'sibyl-memory' with command 'sibyl-memory-mcp' in your agent's MCP config, then restart it."]


# ----------------------------------------------------------------------
# 4. Extraction handoff — the harness does the semantic work, from the backup
# ----------------------------------------------------------------------

def extraction_prompt(harness: str, backup_dir: Path) -> str:
    """Tailored backup-first prompt the user runs IN their harness. Reads only from
    the backup; never edits live files. Mirrors the beta-page conventions."""
    tool = "sibyl_remember" if harness in ("claude-code", "codex") else "your memory tool"
    return (
        f"Read ONLY from the backup folder at {backup_dir} (never touch my live files). "
        "For every piece of accumulated memory in those files (facts and configs, preferences "
        "and patterns, project context, people and relationship notes), write each one into Sibyl "
        f"Memory using {tool}:\n"
        "  - facts/configs/env: structured key-value content\n"
        "  - preferences/patterns: tagged as preference\n"
        "  - project context/history: under a project namespace\n"
        "  - people/relationships: with the person's name as context\n"
        "Do not edit, trim, or delete any live file. When done, tell me how many entries you wrote "
        "in each category."
    )


# ----------------------------------------------------------------------
# 5. Verify — count what actually landed in the local Sibyl DB
# ----------------------------------------------------------------------

# Sentinel returned by db_baseline when the path EXISTS but is not a readable
# SQLite database (garbage bytes, wrong magic, locked, corrupt). Distinct from
# 0 (a valid empty DB / no DB yet). CLI-3 migrate half: the orchestrator must
# ABORT verify + debloat on this, never silently treat it as a 0-row DB and
# proceed to trim the user's real files against a backup it can't trust.
DB_UNREADABLE = -1


def _is_readable_db(db_path: Path) -> bool:
    """True if `db_path` is a non-empty file that opens as a real SQLite DB.

    A 0-byte file is treated as a fresh/empty DB by sqlite and counts as
    readable (no rows yet). Anything that fails the header check or PRAGMA is
    unreadable."""
    try:
        if not db_path.is_file():
            return False
        if db_path.stat().st_size == 0:
            return True
        with open(db_path, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                return False
        con = sqlite3.connect(str(db_path))
        try:
            con.execute("PRAGMA schema_version")
        finally:
            con.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def db_baseline(db_path: Path) -> int:
    """Total entity count now, to diff against after extraction.

    Returns 0 if no DB exists yet (or an empty DB with no rows), and
    DB_UNREADABLE (-1) when the path exists but is not a usable SQLite DB —
    CLI-3: the caller must distinguish "no DB" from "unreadable DB"."""
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        return 0
    if not _is_readable_db(db_path):
        return DB_UNREADABLE
    try:
        con = sqlite3.connect(str(db_path)); con.row_factory = sqlite3.Row
        n = con.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        con.close()
        return int(n)
    except sqlite3.Error:
        # Readable SQLite file but no `entities` table yet (fresh schema) —
        # that's 0 baseline, not an unreadable DB.
        return 0


def verify_new_entries(db_path: Path, baseline_total: int) -> dict:
    """Return {'new_total': N, 'by_category': {...}, 'ok': bool}. ok = new_total > 0.

    CLI-3: if the DB path exists but is unreadable, set ok=False and flag
    `unreadable` so the orchestrator aborts rather than reporting 0 new
    entries (which would falsely gate a debloat)."""
    db_path = Path(db_path).expanduser()
    out = {"new_total": 0, "by_category": {}, "ok": False}
    if not db_path.exists():
        return out
    if not _is_readable_db(db_path):
        out["unreadable"] = True
        out["error"] = "database file exists but is not a readable SQLite database"
        return out
    try:
        con = sqlite3.connect(str(db_path)); con.row_factory = sqlite3.Row
        total = con.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        cats = con.execute("SELECT category, COUNT(*) c FROM entities GROUP BY category ORDER BY c DESC").fetchall()
        con.close()
        out["new_total"] = max(0, int(total) - int(baseline_total))
        out["by_category"] = {r["category"]: int(r["c"]) for r in cats}
        out["ok"] = out["new_total"] > 0
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


# ----------------------------------------------------------------------
# 6. Debloat — confirmed trim of the live file; safe because backup exists
# ----------------------------------------------------------------------

KEEP_START, KEEP_END = "<!-- sibyl:keep -->", "<!-- /sibyl:keep -->"


def heuristic_lean(text: str) -> str:
    """Conservative lean version when the agent didn't provide one.
    If the file marks a keep-block, keep exactly that. Otherwise keep everything up to
    the first H2 section (identity/rules usually live at the top) and append a pointer.
    The full original is always in the backup, so this is reversible."""
    if KEEP_START in text and KEEP_END in text:
        core = text.split(KEEP_START, 1)[1].split(KEEP_END, 1)[0].strip()
    else:
        lines, core_lines = text.splitlines(), []
        seen_h2 = 0
        for ln in lines:
            if ln.startswith("## "):
                seen_h2 += 1
                if seen_h2 > 1:   # keep the first ## section (identity/core), trim the rest
                    break
            core_lines.append(ln)
        core = "\n".join(core_lines).strip()
    pointer = ("\n\n<!-- The rest of this file's accumulated memory now lives in Sibyl Memory "
               "and is recalled on demand. Full pre-migration backup is preserved. -->\n")
    return core + pointer


def verify_backup_of(live_path: Path, backup_dir: Path, *, home: Path, cwd: Optional[Path]) -> bool:
    """Re-stat the SPECIFIC backup copy of `live_path` and confirm it exists
    with a matching byte count.

    CLI-9: before trimming an original we must verify the backup file on disk
    right now — not trust the in-memory `bk.ok` flag from earlier, which can't
    catch a backup that was deleted/truncated/corrupted in the meantime."""
    try:
        rel = _backup_rel(Path(live_path), Path(home), cwd)
        backup_copy = Path(backup_dir) / rel
        if not backup_copy.is_file():
            return False
        return backup_copy.stat().st_size == Path(live_path).stat().st_size
    except OSError:
        return False


def debloat_file(live_path: Path, lean_text: str, *, backup_exists: bool, dry_run: bool = False) -> dict:
    """Atomically replace live_path with lean_text. REFUSES unless backup_exists is True.
    Returns {before, after, written, error}."""
    live_path = Path(live_path).expanduser()
    out = {"before": 0, "after": len(lean_text.encode()), "written": False}
    if not backup_exists:
        out["error"] = "refused: no verified backup exists"; return out
    # CLI-8: refuse to trim through a symlink. os.replace on a symlinked target
    # would clobber whatever the link points at — potentially a file outside
    # the intended scope. The debloat is the highest-blast-radius step; it must
    # only ever rewrite a regular file we backed up.
    if live_path.is_symlink():
        out["error"] = "refused: live file is a symlink"; return out
    if not live_path.exists():
        out["error"] = "live file not found"; return out
    out["before"] = live_path.stat().st_size
    if dry_run:
        return out
    # CLI-8: mkstemp (unique per-process) + fsync + atomic os.replace, instead
    # of a fixed `.sibyl-tmp` name that two runs could collide on.
    data = lean_text.encode("utf-8")
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(live_path.parent),
                               prefix=live_path.name + ".", suffix=".sibyl-tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp, str(live_path))
    out["written"] = True
    return out


# ----------------------------------------------------------------------
# 7. Orchestrator — the guided, resumable flow
# ----------------------------------------------------------------------

def detect_state(home: Optional[Path] = None, cwd: Optional[Path] = None, db_path: Optional[Path] = None) -> dict:
    """Snapshot for resumability: what's present, what's wired, how much memory exists."""
    from .setup import HermesWirer, ClaudeCodeWirer
    home = Path(home).expanduser() if home else Path.home()
    db_path = Path(db_path).expanduser() if db_path else (home / ".sibyl-memory" / "memory.db")
    wirers = {"claude-code": ClaudeCodeWirer(), "codex": CodexWirer(), "hermes": HermesWirer()}
    return {
        "files": scan_memory_files(home, cwd),
        "harnesses": {n: {"present": w.is_present(), **w.current_state()} for n, w in wirers.items()},
        "db_entries": db_baseline(db_path),
        "db_path": db_path,
    }


class GuidedIO:
    """IO seam so the guided flow is testable non-interactively. Pass `scripted`
    answers (list) to drive confirms/pauses without a TTY."""
    def __init__(self, scripted=None):
        self.scripted = list(scripted or [])
        self.lines: list[str] = []

    def say(self, s: str = "") -> None:
        self.lines.append(str(s))

    def confirm(self, q: str, *, default: bool = True) -> bool:
        if self.scripted:
            ans = self.scripted.pop(0)
        else:
            try:
                ans = input(f"{q} [{'Y/n' if default else 'y/N'}]: ").strip()
            except EOFError:
                ans = ""
        return default if not ans else ans.strip().lower().startswith("y")

    def pause(self, q: str = "press Enter to continue") -> None:
        if self.scripted:
            self.scripted.pop(0)
            return
        try:
            input(q)
        except EOFError:
            pass


def run_guided_setup(*, home=None, cwd=None, db_path=None, backup_parent=None,
                     io: Optional[GuidedIO] = None, wirers: Optional[dict] = None,
                     extract_fn: Optional[Callable[[Path, Path], None]] = None,
                     debloat: bool = True, force: bool = False, now=None) -> dict:
    """The assembled guided flow: backup -> auto-wire each harness (instructions on
    failure) -> extraction handoff -> verify -> confirmed debloat. Returns a structured
    report. `extract_fn(backup_dir, db_path)` performs/simulates extraction; default
    prints the prompt for the user to run in their own harness. `wirers` is injectable
    so tests (and isolation) never touch real config."""
    from .setup import ALL_WIRERS
    io = io or GuidedIO()
    home = Path(home).expanduser() if home else Path.home()
    db_path = Path(db_path).expanduser() if db_path else (home / ".sibyl-memory" / "memory.db")
    backup_parent = Path(backup_parent).expanduser() if backup_parent else home
    report: dict = {"ok": True, "phases": {}}

    # 1. scan + backup (deterministic, first, never modifies sources)
    files = scan_memory_files(home, cwd)
    report["files"] = [f.rel for f in files]
    if not files:
        io.say("No memory/agent files found. Nothing to migrate.")
        report["ok"] = False
        return report
    bk = run_backup(files, backup_parent, now=now)
    report["phases"]["backup"] = {"ok": bk.ok, "dir": str(bk.backup_dir), "files": len(bk.files)}
    if not bk.ok:
        io.say(f"Backup failed: {bk.error}. Aborting; nothing else touched.")
        report["ok"] = False
        return report
    io.say(f"Backed up {len(bk.files)} files -> {bk.backup_dir} (originals untouched)")

    # 2. detect + auto-wire each present harness; fall back to instructions
    if wirers is None:
        wirers = {n: cls() for n, cls in ALL_WIRERS.items()}
    detected = {n: w for n, w in wirers.items() if w.is_present()}
    wire_report = {}
    for name, w in detected.items():
        if w.current_state().get("wired_with_sibyl"):
            wire_report[name] = "already"
            continue
        outcome = w.wire(force=force)
        wire_report[name] = outcome.status
        if outcome.status not in ("wired", "already"):
            io.say(f"{name}: auto-wire incomplete ({outcome.message}). Do this manually:")
            for ln in wire_instructions(name):
                io.say("  " + ln)
    report["phases"]["wire"] = wire_report

    # 3. extraction (the harness does it; default prints the prompt + pauses)
    baseline = db_baseline(db_path)
    # CLI-3: if the DB path exists but is unreadable, do NOT proceed to verify
    # + debloat. A debloat trims the user's live files; gating it on a DB we
    # cannot read would be unsafe. Abort cleanly with the originals intact.
    if baseline == DB_UNREADABLE:
        io.say("Sibyl memory DB exists but is unreadable (not a valid SQLite database).")
        io.say("Aborting before verify/trim — your originals and backup are intact.")
        report["phases"]["verify"] = {"new_total": 0, "by_category": {}, "ok": False, "unreadable": True}
        report["ok"] = False
        return report
    target = next(iter(detected), "claude-code")
    if extract_fn is not None:
        extract_fn(bk.backup_dir, db_path)
    else:
        io.say("Run this in your agent (it reads the backup, writes to Sibyl):")
        io.say(extraction_prompt(target, bk.backup_dir))
        io.pause("After it finishes, press Enter to verify")

    # 4. verify
    v = verify_new_entries(db_path, baseline)
    report["phases"]["verify"] = v
    io.say(f"Verified {v['new_total']} new entries in Sibyl Memory.")

    # 5. debloat (confirmed; safe because the backup exists)
    cm = (Path(cwd) / "CLAUDE.md") if cwd else (home / "CLAUDE.md")
    if debloat and v["ok"] and cm.exists():
        if io.confirm(f"Trim {cm.name} to lean now? Full backup is safe at {bk.backup_dir}", default=False):
            # CLI-9: re-verify the actual backup copy on disk RIGHT NOW before
            # trimming, instead of trusting the earlier in-memory bk.ok.
            backup_ok_now = bk.ok and verify_backup_of(cm, bk.backup_dir, home=home, cwd=cwd)
            if not backup_ok_now:
                io.say(f"Backup of {cm.name} could not be re-verified — skipping trim. Original untouched.")
                report["phases"]["debloat"] = {"written": False, "before": cm.stat().st_size,
                                               "after": 0, "error": "backup re-verification failed"}
            else:
                lean = heuristic_lean(cm.read_text(encoding="utf-8", errors="replace"))
                d = debloat_file(cm, lean, backup_exists=backup_ok_now)
                report["phases"]["debloat"] = {"written": d["written"], "before": d["before"], "after": d["after"]}
                io.say(f"Trimmed {cm.name}. Backup safe at {bk.backup_dir}")
    return report
