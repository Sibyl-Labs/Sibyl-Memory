"""`sibyl setup`: auto-detect agent frameworks and wire SIBYL as memory provider.

Maximum-efficiency onboarding command. Single-command path for the user:

    pip install sibyl-memory-cli
    sibyl setup          # auto-detects Hermes, Claude Code, Codex, Cursor and DeepSeek Harness, prompts per stack, wires

Wirers:
  - HermesWirer:     install-plugin + edit $HERMES_HOME/config.yaml (memory.provider)
  - ClaudeCodeWirer: `claude mcp add --scope user sibyl-memory` (the discovery
                     surface); ~/.claude/settings.json holds the hooks and the
                     skills directory sits beside it
  - CodexWirer:      add [mcp_servers.sibyl_memory] to ~/.codex/config.toml
  - CursorWirer:     mcpServers.sibyl-memory in ~/.cursor/mcp.json (GrokBotWirer,
                     explicit target only, is the same wiring)
  - DeepSeekWirer:   one @deepseek-ai/dsh-mcp-client insert row in a DeepSeek
                     Harness patch file ($DSH_HOME or ~/.dsh, cordis.patch.yml),
                     edited as text and verified before every write

Each wirer follows the same protocol:
  is_present()       -> bool          (filesystem + PATH detect)
  current_state()    -> dict          (configured? wired-with-sibyl? current-value?)
  wire(force, dry_run, prompt_fn) -> WireOutcome

Destructive operations (overwriting an existing non-SIBYL config) default to NO
on the prompt. Fresh adds default to YES. --force overrides destructive guards.
--yes accepts all defaults (still respects the destructive-default-NO unless
--force is also passed). --dry-run prints intent without writing.

All config edits are atomic: back up to
~/.sibyl-memory/backups/<harness>/<file>.<utc-timestamp>.bak (0600), write a
temp file beside the target, rename (the temp file is removed on failure).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union


def backups_root() -> Path:
    """Where every wirer's config backups live (cli 0.7.0): under the plugin
    home, never beside the harness's own config. Computed at call time so an
    isolated HOME (tests, sandboxes) is honored."""
    return Path.home() / ".sibyl-memory" / "backups"


def _timestamped_backup(path: Path, harness: str = "other", sub: str = "") -> Optional[Path]:
    """Copy ``path`` to ``~/.sibyl-memory/backups/<harness>[/<sub>]/`` and
    return the backup path (or None if the source does not exist).

    B005 (audit #19): every run keeps its own backup. The name is the original
    file name plus a UTC timestamp, e.g. ``config.yaml.20260630T142530Z.bak``;
    a second backup in the same second gets ``.2.bak`` (up to ``.99.bak``) and
    an existing file is never overwritten (O_EXCL). The timestamp is computed
    BEFORE it is used in the file name.

    cli 0.7.0 (one footprint): backups used to sit beside the harness config.
    They now go under the plugin home with directories 0700 and files 0600
    (a config backup can carry secrets), whatever the source's mode was.
    Raises OSError (``_fsio.RefusedPath`` for a symlinked or non-directory
    backup folder) when the backup cannot be written; callers then leave the
    config alone.
    """
    if not path.exists():
        return None
    from sibyl_memory_client import _fsio
    plugin_home = Path.home() / ".sibyl-memory"
    if not plugin_home.exists():
        plugin_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    d = _fsio.ensure_private_dir(backups_root())
    d = _fsio.ensure_private_dir(d / harness)
    if sub:
        d = _fsio.ensure_private_dir(d / sub)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    data = path.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1, 100):
        name = f"{path.name}.{ts}.bak" if n == 1 else f"{path.name}.{ts}.{n}.bak"
        dest = d / name
        try:
            fd = os.open(str(dest), flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                try:
                    os.fchmod(fh.fileno(), 0o600)
                except (OSError, AttributeError):
                    pass
        except BaseException:
            try:
                os.unlink(str(dest))
            except OSError:
                pass
            raise
        return dest
    raise FileExistsError(f"too many backups of {path.name} in one second under {d}")


def _probe(entry: str, config_path: Any, *, command: Optional[str] = None, location: str = "",
           detail: str = "", **extra: Any) -> dict:
    """One doctor probe record (spec dsh-doctor v2, section 4). ``detail``
    never holds file contents: paths, commands and fixed wording only."""
    d = {"entry": entry, "command": command, "config_path": str(config_path),
         "location": location, "detail": detail}
    d.update(extra)
    return d


def _is_our_command(cmd: Any) -> bool:
    return bool(isinstance(cmd, str) and cmd
                and Path(cmd.replace("\\", "/")).name.startswith(MCP_BINARY_NAME))


def _backup_failed(name: str, config: Path, harness: str, e: BaseException) -> "WireOutcome":
    """Uniform outcome when a backup cannot be written: the config is left alone."""
    return WireOutcome(name, "error",
                       f"could not back up {config} to {backups_root() / harness} "
                       f"({type(e).__name__}); {config} was not modified")


def _current_umask() -> int:
    old = os.umask(0o022)
    os.umask(old)
    return old


def _write_via_tmp(target: Path, tmp: Path, text: str, *, through_symlink: bool = False) -> None:
    """Write ``text`` to a new temp file beside ``target`` and rename it over
    ``target``, keeping ``target``'s mode.

    Panel fix 5 (2026-11-14): the temp file is created with
    O_CREAT | O_EXCL | O_NOFOLLOW under a unique name derived from ``tmp``
    (never a fixed name another run or a pre-placed symlink can share), its
    mode is set to the target's mode (0666 & ~umask for a new file) BEFORE any
    content is written, the data is fsync'ed before the rename and the
    directory after it, and only this run's own temp file is removed on
    failure. The text is written as UTF-8 bytes with no newline translation.

    ``through_symlink`` (Cursor, Codex and Hermes, CHECK-FABLE N-2): when
    ``target`` is a symlink, the file it resolves to is replaced (the temp file
    sits beside that file) and the link itself is kept, so a config kept in a
    dotfiles folder stays linked and current. Without it a symlinked target is
    replaced by a regular file, as before."""
    import secrets
    target = Path(target)
    tmp = Path(tmp)
    if through_symlink and target.is_symlink():
        target = Path(os.path.realpath(str(target)))
    parent = target.parent
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    data = text.encode("utf-8")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    fd = -1
    tmp_path: Optional[Path] = None
    for _ in range(20):
        cand = parent / f"{tmp.stem}.{secrets.token_hex(6)}{tmp.suffix}"
        try:
            fd = os.open(str(cand), flags, 0o600)
        except FileExistsError:
            continue
        tmp_path = cand
        break
    if tmp_path is None:
        raise FileExistsError(f"could not create a temp file beside {target}")
    try:
        try:
            os.fchmod(fd, mode)
        except (OSError, AttributeError):
            pass
        fh = os.fdopen(fd, "wb")
        fd = -1
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(target))
        tmp_path = None
        try:
            dfd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            dfd = -1
        if dfd >= 0:
            try:
                os.fsync(dfd)
            except OSError:
                pass
            finally:
                os.close(dfd)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass
        raise


def _verify_mcp_starts(binary: Optional[str]) -> tuple:
    """Smoke-test an MCP stdio server binary: spawn it and confirm it does not
    crash on startup. Returns (ok: bool, message: str).

    B005 (audit #19): this was a method on ClaudeCodeWirer that CodexWirer
    called via the fragile cross-class dispatch
    ``ClaudeCodeWirer.verify_mcp_starts(self)``. Pulled out to a standalone
    helper so both wirers share one implementation cleanly. The only state it
    needed was the resolved binary path, so it takes that as an argument.

    Catches the common failures: ImportError (missing dep),
    ModuleNotFoundError, bad credentials file — all of which manifest within
    the first second as a non-zero exit. A slow-but-alive import is treated as
    healthy (it is blocking on stdin), not crashed.
    """
    if not binary:
        return False, "MCP binary not found"
    try:
        proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # CLI-12: MCP stdio servers block on stdin; a crash-on-import exits
        # quickly with a non-zero code. POLL the exit code over a short window
        # instead of a fixed sleep — a slow import (cold caches, heavy deps)
        # that is still alive at the deadline is treated as healthy.
        deadline = time.monotonic() + 3.0
        rc = None
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                break
            time.sleep(0.1)
        if rc is not None and rc != 0:
            # CLI-12: bound the stderr read so a server that floods stderr
            # before exiting can't make us block on an unbounded read.
            try:
                err = (proc.stderr.read(4096) or b"").decode(errors="replace").strip()
            except Exception:
                err = ""
            return False, f"Server crashed on startup (exit {rc}): {err[:200]}"
        if rc == 0:
            # Exited cleanly without blocking — unusual for a stdio server but
            # not a crash.
            return True, "MCP server verified (exited cleanly)"
        # Still running (blocking on stdin) — binary works.
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return True, "MCP server verified (starts cleanly)"
    except Exception as e:
        return False, f"Could not start server: {type(e).__name__}: {e}"


def _run(cmd: list[str], *, timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a command, return (rc, stdout, stderr). rc=127 if not found, 124 on timeout.
    Centralized so tests can monkeypatch one place."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as e:  # never let a wirer crash the whole flow
        return 1, "", f"{type(e).__name__}: {e}"


def _install_pkg_or_instruct(package: str) -> Optional[str]:
    """Attempt to install `package` into the current interpreter, respecting PEP 668.

    CLI-11: in an externally-managed (PEP 668) or pipx-managed environment we do
    NOT silently `pip install` (that mutates a system/managed env and surprises
    the user). We return an instruction string instead. For a plain venv/system
    install we run pip with output captured and return the captured stderr/stdout
    on failure so the caller can surface it. Returns None on success, otherwise a
    human-readable error/instruction string."""
    try:
        from .cli import _detect_install_method
        method = _detect_install_method()
    except Exception:
        method = "system"

    if method == "pep668":
        return (
            f"'{package}' is missing and this Python is externally-managed (PEP 668). "
            f"Install it yourself, ideally in a venv:\n"
            f"    pip install {package}\n"
            f"  or, if you understand the risk:\n"
            f"    pip install --break-system-packages {package}"
        )
    if method == "pipx":
        return (
            f"'{package}' is missing and the CLI is running under pipx. "
            f"Inject it into the pipx venv:\n"
            f"    pipx inject sibyl-memory-cli {package}"
        )
    rc, out, err = _run([sys.executable, "-m", "pip", "install", package], timeout=120.0)
    if rc == 0:
        return None
    detail = (err or out or "").strip()
    return f"pip install {package} failed (exit {rc}): {detail[:400]}" if detail \
        else f"pip install {package} failed (exit {rc})."

# Color helpers re-imported from cli module via late binding to avoid circular dep.
# When called via `sibyl setup` they resolve through the cli module's tty detection.
def _color_fns():
    from .cli import bold, cyan, dim, green, red, yellow
    return bold, cyan, dim, green, red, yellow


# ----------------------------------------------------------------------
# WireOutcome
# ----------------------------------------------------------------------

@dataclass
class WireOutcome:
    """Result of a wirer.wire() call. Composable across multiple wirers."""
    name: str
    status: str          # 'wired' / 'already' / 'skipped' / 'dry-run' / 'error'
    message: str
    backup_path: Optional[Path] = None
    # Save/debloat build: one line per skill file, shim, hook or instruction
    # block the wirer wrote (or would write, on --dry-run).
    lifecycle: list = field(default_factory=list)
    # Agent handoff (spec 2026-10-03 rows X8 to X11): (next, human, later) for
    # an error outcome; `overwrite_of` names what an existing entry is when the
    # wirer stopped short of replacing it (X6).
    handoff: Optional[tuple] = None
    overwrite_of: Optional[str] = None
    # Panel fix 8: text the person pastes by hand (valid YAML or TOML as
    # printed). It is also the tail of ``message``; doctor prints it on its
    # own lines instead of folding it into one.
    manual_block: Optional[str] = None


PEP668_HUMAN = ("this Python is externally managed (PEP 668); choose a venv or allow "
                "--break-system-packages")


def _binary_missing_handoff(name: str) -> tuple:
    """Row X9: the MCP binary is missing; how to get it depends on the install."""
    try:
        from .cli import _detect_install_method
        method = _detect_install_method()
    except Exception:
        method = "system"
    if method == "pep668":
        return ("done", PEP668_HUMAN, ())
    if method == "pipx":
        return ("pipx inject sibyl-memory-cli sibyl-memory-mcp", None, (f"sibyl setup {name}",))
    return ("pip install sibyl-memory-mcp", None, (f"sibyl setup {name}",))


# ----------------------------------------------------------------------
# Save / debloat wiring (spec plugin-save-debloat 3.5). Shared by the three
# wirers: the canonical procedure files in ~/.sibyl-memory/skills/, one thin
# shim per harness, the Claude Code hooks, the Codex AGENTS.md block.
# ----------------------------------------------------------------------

HOOK_EVENTS: tuple = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "prompt"),
    ("Stop", "stop"),
    ("PreCompact", "pre-compact"),
    ("SessionEnd", "session-end"),
)
# 5 s kept after measuring (release notes): the slowest hook, a Pro
# SessionStart that trims a file, takes about 0.3 s end to end and never waits
# on the network; a longer timeout would only lengthen a stall.
HOOK_TIMEOUT_SECONDS = 5
_HOOK_ARGS = "|".join(a for _e, a in HOOK_EVENTS)
# Our entries are recognised by their command: `<path>/sibyl hook <event>`
# (quoted or not), optionally inside the guard hook_command writes
# (`[ -x <path> ] && exec <path> hook <event>; exit 0`). No extra key is added
# to the hook object, so nothing the settings schema does not know ever lands
# in a person's settings.json.
_OUR_HOOK_RE = re.compile(r"(?:^|[/\\'\"\s])sibyl(?:\.exe)?['\"]?\s+hook\s+(" + _HOOK_ARGS
                          + r")\s*(?:;\s*exit\s+0\s*)?$")

CODEX_BLOCK_BEGIN = "<!-- sibyl:begin skills v1 -->"
CODEX_BLOCK_END = "<!-- sibyl:end -->"
CODEX_BLOCK_BODY = (
    "## Sibyl Memory\n"
    "Sibyl Memory is wired (MCP server `sibyl_memory`). To save a session, read "
    "`~/.sibyl-memory/skills/SAVE.md` and follow it (triggers: save, save session, sync "
    "memory). To measure or trim always-loaded context files, read "
    "`~/.sibyl-memory/skills/DEBLOAT.md`. Both are also MCP prompts on the `sibyl_memory` "
    "server (`save`, `debloat`). When a Sibyl Memory tool result carries a `notice` asking you "
    "to save, run the save procedure in headless mode, then continue the task."
)


def _skills_mod():
    try:
        from sibyl_memory_client import skills
        return skills
    except Exception:  # pragma: no cover - client without the skills module
        return None


def sibyl_binary() -> str:
    """Absolute path of the `sibyl` command for hook entries (a hook runs from
    Claude Code's own PATH, which may not include a venv's bin: the lesson
    from the MCP registration)."""
    w = shutil.which("sibyl")
    if w:
        return os.path.abspath(w)
    cand = Path(sys.executable).parent / "sibyl"
    if cand.exists():
        return str(cand)
    return "sibyl"


def hook_command(binary: str, event_arg: str) -> str:
    """The settings.json command for one hook. Claude Code runs a command
    hook through the shell (bash; the 2.1.283 schema: "Shell command to
    execute"), so an absolute path is wrapped in a guard: when the package was
    uninstalled without `sibyl skills uninstall`, the hook exits 0 silently
    instead of failing at every session start, prompt and turn end. The path
    is written twice rather than through a variable (no `$` in the command)."""
    q = shlex.quote(binary)
    if os.name != "nt" and os.path.isabs(binary):
        return f"[ -x {q} ] && exec {q} hook {event_arg}; exit 0"
    return f"{q} hook {event_arg}"


def is_our_hook(h: Any) -> bool:
    return (isinstance(h, dict) and h.get("type") == "command"
            and isinstance(h.get("command"), str) and bool(_OUR_HOOK_RE.search(h["command"])))


def merge_hooks(cfg: dict, binary: str) -> dict:
    """Return ``cfg`` with our five hook entries (replacing any previous ones
    of ours) and every other hook, matcher group and key left as it was.
    Raises ValueError when ``hooks`` exists but is not the documented shape
    (never clobbered)."""
    out = dict(cfg)
    hooks = out.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json 'hooks' is not an object; left unchanged")
    hooks = dict(hooks)
    for event, arg in HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings.json hooks.{event} is not a list; left unchanged")
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                rest = [h for h in g["hooks"] if not is_our_hook(h)]
                if len(rest) == len(g["hooks"]):
                    kept.append(g)
                elif rest:
                    kept.append({**g, "hooks": rest})
            else:
                kept.append(g)
        kept.append({"hooks": [{"type": "command", "command": hook_command(binary, arg),
                                "timeout": HOOK_TIMEOUT_SECONDS}]})
        hooks[event] = kept
    out["hooks"] = hooks
    return out


def remove_hooks(cfg: dict) -> dict:
    out = dict(cfg)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    hooks = dict(hooks)
    for event, _arg in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                rest = [h for h in g["hooks"] if not is_our_hook(h)]
                if rest:
                    kept.append({**g, "hooks": rest} if len(rest) != len(g["hooks"]) else g)
            else:
                kept.append(g)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    out["hooks"] = hooks
    if not hooks:
        out.pop("hooks")
    return out


def count_our_hooks(cfg: Any) -> int:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hooks"), dict):
        return 0
    n = 0
    for groups in cfg["hooks"].values():
        if isinstance(groups, list):
            for g in groups:
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    n += sum(1 for h in g["hooks"] if is_our_hook(h))
    return n


def install_canonical_skills(*, dry_run: bool = False, force: bool = False) -> list:
    """~/.sibyl-memory/skills/{SAVE,DEBLOAT}.md (never overwriting an edit)."""
    sk = _skills_mod()
    if sk is None:
        return ["skills: sibyl-memory-client has no skills module; update it"]
    d = sk.skills_dir(Path.home())
    if dry_run:
        return _dry_run_skills(sk, d, force=force)
    rep = sk.install(Path.home(), force=force)
    lines = [f"skills v{rep.version}: " + ", ".join(f"{n} {s}" for n, s in rep.results.items())]
    lines.extend(rep.messages)
    return lines


# `sibyl setup <agent> --dry-run` compares with what is installed
# and says "already in" for skills, shims, hooks and the Codex block that are
# already there, as it does for the MCP server. Reads only; never writes.

def _dry_run_skills(sk, d: Path, *, force: bool = False) -> list:
    status = getattr(sk, "status", None)
    try:
        files = status(Path.home()).get("files", {}) if callable(status) else {}
    except Exception:
        files = {}
    names = tuple(getattr(sk, "SKILL_NAMES", ("SAVE.md", "DEBLOAT.md")))
    states = {n: (files.get(n) or {}).get("state") for n in names}
    if names and all(st == "ok" for st in states.values()):
        return [f"skills v{sk.version()}: {', '.join(names)} already in {d}"]
    lines = []
    for n in names:
        st = states.get(n)
        if st == "ok":
            lines.append(f"skills: {n} already in {d}")
        elif st == "outdated":
            lines.append(f"would update {d}/{n} (v{sk.version()})")
        elif st == "edited":
            lines.append(f"would replace your edited {d}/{n}" if force
                         else f"would keep {d}/{n} (edited by you; the new version goes beside it)")
        elif st in ("symlink", "oversize"):
            lines.append(f"would leave {d}/{n} alone ({st})")
        else:
            lines.append(f"would install {d}/{n} (v{sk.version()})")
    return lines


def _dry_run_shim(sk, name: str, harness: str, path, *, force: bool = False) -> str:
    p = Path(path)
    try:
        want = sk.shim_text(name, harness).encode("utf-8")
    except Exception:
        return f"would write shim {p}"
    if p.is_symlink():
        return f"would leave shim {p} alone (symlink)"
    if not p.exists():
        return f"would write shim {p}"
    try:
        cur = p.read_bytes() if p.is_file() and p.stat().st_size <= 262144 else None
    except OSError:
        cur = None
    if cur == want:
        return f"shim {p}: already in"
    recorded = None
    status = getattr(sk, "status", None)
    try:
        recorded = (status(Path.home()).get("shims", {}).get(str(p)) or {}).get("state") if callable(status) else None
    except Exception:
        recorded = None
    if recorded == "ok" or force:
        return f"would update shim {p}"
    return f"would keep shim {p} (edited by you)"


def install_shims(harness: str, targets: dict, *, dry_run: bool = False, force: bool = False) -> list:
    """``targets``: {skill name: shim path}."""
    sk = _skills_mod()
    if sk is None:
        return []
    lines = []
    for name, path in targets.items():
        if dry_run:
            lines.append(_dry_run_shim(sk, name, harness, path, force=force))
            continue
        st = sk.install_shim(Path.home(), path, sk.shim_text(name, harness), harness=harness,
                             skill=name, force=force)
        lines.append(f"shim {path}: {st}")
        if st == "user_modified":
            lines.append(f"  {path} was edited by you and was kept (sibyl skills status).")
    return lines


def _atomic_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; not followed")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".sibyl-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        except OSError:
            pass
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def codex_block() -> str:
    return f"{CODEX_BLOCK_BEGIN}\n{CODEX_BLOCK_BODY}\n{CODEX_BLOCK_END}\n"


def upsert_block(existing: str, block: str) -> str:
    """Insert ``block`` once, or replace it in place between the markers;
    the text around it is untouched."""
    b = existing.find(CODEX_BLOCK_BEGIN)
    if b != -1:
        e = existing.find(CODEX_BLOCK_END, b)
        if e != -1:
            e_end = e + len(CODEX_BLOCK_END)
            if existing[e_end:e_end + 1] == "\n":
                e_end += 1
            return existing[:b] + block + existing[e_end:]
    if not existing:
        return block
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block


def remove_block(existing: str) -> str:
    b = existing.find(CODEX_BLOCK_BEGIN)
    if b == -1:
        return existing
    e = existing.find(CODEX_BLOCK_END, b)
    if e == -1:
        return existing
    e_end = e + len(CODEX_BLOCK_END)
    if existing[e_end:e_end + 1] == "\n":
        e_end += 1
    head = existing[:b]
    if head.endswith("\n\n"):
        head = head[:-1]
    out = head + existing[e_end:]
    return out


# ----------------------------------------------------------------------
# Lazy YAML import. Hermes wirer needs it; Claude-only users do not.
# ----------------------------------------------------------------------

def _import_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        return None


# ----------------------------------------------------------------------
# Tolerant YAML reader for DeepSeek Harness patch files (spec dsh-doctor v2,
# 3.2). Read-only: used to locate nodes and to prove that edited text parses
# to exactly the intended result. Nothing is ever re-serialized.
#
# PyYAML is not js-yaml: it accepts !!timestamp, !!binary, merge keys, YAML
# 1.1 booleans and duplicate mapping keys, which js-yaml JSON_SCHEMA (what
# DeepSeek Harness uses) rejects or reads differently. That is harmless here
# because existing bytes are never changed outside the one span we replace and
# our own text is fixed, but a file PyYAML accepts can still be invalid for
# DeepSeek Harness.
# ----------------------------------------------------------------------

DSH_JS_TAG = "tag:yaml.org,2002:js"


class _JsExpr(str):
    """A `!!js` scalar from a DeepSeek Harness patch file. Kept as text, never
    evaluated. It is a str subclass, so == cannot tell it from a plain string;
    callers that need to know check type(x) is _JsExpr."""


_DSH_LOADER: Any = None


def _dsh_loader_cls():
    """A SafeLoader subclass that accepts the one custom tag DeepSeek Harness
    uses (`!!js`). Built lazily so PyYAML stays a lazy import; the global
    SafeLoader is not mutated."""
    global _DSH_LOADER
    if _DSH_LOADER is None:
        yaml = _import_yaml()
        if yaml is None:
            raise ImportError("PyYAML is not installed")

        class _DshLoader(yaml.SafeLoader):
            pass

        _DshLoader.add_constructor(DSH_JS_TAG, lambda l, n: _JsExpr(l.construct_scalar(n)))
        _DSH_LOADER = _DshLoader
    return _DSH_LOADER


def _dsh_yaml_load(text: str):
    """Parse one YAML document (raises on anything PyYAML rejects, including a
    second document)."""
    yaml = _import_yaml()
    return yaml.load(text, Loader=_dsh_loader_cls())


def _dsh_yaml_compose(text: str):
    """The node tree with marks (character offsets into ``text``), or None."""
    yaml = _import_yaml()
    return yaml.compose(text, Loader=_dsh_loader_cls())


def _yaml_error_brief(e: BaseException) -> str:
    """The only way a YAML error reaches a message, a detail field or a log:
    the exception type and the line and column, never str(e), e.problem or
    e.context (those can carry text from the file, a secret included)."""
    name = type(e).__name__
    mark = getattr(e, "problem_mark", None)
    line = getattr(mark, "line", None)
    col = getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(col, int):
        return f"{name} at line {line + 1}, column {col + 1}"
    return name


# ----------------------------------------------------------------------
# HermesWirer
# ----------------------------------------------------------------------

class _HermesConfigNotMapping(ValueError):
    """Raised by HermesWirer._write_config_with_sibyl itself; its text holds a
    path and a type name only, so it may be shown."""


class _HandoffHold:
    """stdout for the Hermes installer: everything passes through as written
    except the handoff lines (next:, later:, ask your human:), which are held
    while they are the last lines seen and then printed or dropped by
    ``release``. Colors and tty checks still see the real stream."""

    _PREFIXES = ("next:", "later:", "ask your human:")

    def __init__(self, real) -> None:
        self._real = real
        self._partial = ""
        self._held: list = []

    def write(self, data: str) -> int:
        text = self._partial + data
        lines = text.split("\n")
        self._partial = lines.pop()
        for ln in lines:
            if ln.startswith(self._PREFIXES):
                self._held.append(ln + "\n")
            else:
                self._emit_held()
                self._real.write(ln + "\n")
        return len(data)

    def _emit_held(self) -> None:
        for h in self._held:
            self._real.write(h)
        self._held = []

    def release(self, keep: bool) -> None:
        if keep:
            self._emit_held()
            if self._partial:
                self._real.write(self._partial)
        elif self._partial and not self._partial.startswith(self._PREFIXES):
            self._real.write(self._partial)
        self._held, self._partial = [], ""
        self.flush()

    def flush(self) -> None:
        try:
            self._real.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._real, name)


class _HermesManualEdit(ValueError):
    """Raised by HermesWirer._write_config_with_sibyl when the text edit
    cannot be made or proven (panel fix 1); config.yaml is not modified."""


class _HermesPluginRefused(RuntimeError):
    """sibyl_memory_hermes.install_plugin.install() returned a refusal code
    (panel fix 4): 2 folder not empty, 3 symlink, 4 not a Sibyl install."""

    def __init__(self, rc: int, dest: Path):
        super().__init__(f"install-plugin refused (code {rc})")
        self.rc = rc
        self.dest = dest


_HERMES_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
HERMES_MANUAL_LINES = ("memory:", "  provider: sibyl")


def _hermes_provider_text(text: str) -> Optional[str]:
    """config.yaml text with memory.provider set to sibyl, edited as text
    (panel fix 1), or None when the edit cannot be made or proven. Nothing is
    re-serialized: with the node marks from yaml.compose, an existing
    memory.provider scalar is replaced in place, a block `memory:` mapping
    without provider gets one line, a scalar or empty `memory` value becomes
    `{provider: sibyl}`, and a file with no `memory` key gets two lines
    appended. The file's line ending is kept. The proof: yaml.safe_load of the
    new text equals the old data with only memory.provider changed."""
    import copy
    yaml = _import_yaml()
    if yaml is None:
        return None
    nl = "\r\n" if "\r\n" in text else "\n"
    old = yaml.safe_load(text)
    if old is None:
        old = {}
    if not isinstance(old, dict):
        return None
    want = copy.deepcopy(old)
    if isinstance(want.get("memory"), dict):
        want["memory"]["provider"] = "sibyl"
    else:
        want["memory"] = {"provider": "sibyl"}
    root = yaml.compose(text)
    append = f"memory:{nl}  provider: sibyl{nl}"

    def _appended() -> str:
        sep = "" if (text == "" or text.endswith("\n")) else nl
        return text + sep + append

    new: Optional[str] = None
    if root is None:
        new = _appended()
    elif (type(root).__name__ == "MappingNode" and not root.flow_style
            and root.start_mark.column == 0):
        pairs = [(k, v) for k, v in root.value
                 if type(k).__name__ == "ScalarNode" and k.value == "memory"]
        if len(pairs) > 1:
            return None
        if not pairs:
            new = _appended()
        else:
            k, v = pairs[0]
            a, b = v.start_mark.index, v.end_mark.index
            if a < k.end_mark.index:
                return None          # an alias to a node defined earlier
            kind = type(v).__name__
            if kind == "MappingNode" and not v.flow_style and v.value:
                prov = [(pk, pv) for pk, pv in v.value
                        if type(pk).__name__ == "ScalarNode" and pk.value == "provider"]
                if len(prov) > 1:
                    return None
                if prov:
                    pk, pv = prov[0]
                    pa, pb = pv.start_mark.index, pv.end_mark.index
                    if (type(pv).__name__ != "ScalarNode" or pv.style not in (None, "'", '"')
                            or pa < pk.end_mark.index or pa == pb):
                        return None
                    new = text[:pa] + "sibyl" + text[pb:]
                else:
                    fk = v.value[0][0]
                    idx, col = fk.start_mark.index, fk.start_mark.column
                    ls = idx - col
                    if ls < 0 or text[ls:idx].strip(" ") != "":
                        return None
                    new = text[:ls] + " " * col + "provider: sibyl" + nl + text[ls:]
            elif kind == "MappingNode" and v.flow_style and not v.value:
                new = text[:a] + "{provider: sibyl}" + text[b:]
            elif kind == "ScalarNode" and v.style in (None, "'", '"'):
                if a == b:
                    new = text[:a] + " {provider: sibyl}" + text[b:]
                else:
                    new = text[:a] + "{provider: sibyl}" + text[b:]
            else:
                return None
    else:
        return None
    try:
        got = yaml.safe_load(new)
    except Exception:
        return None
    if got != want:
        return None
    return new


class HermesWirer:
    name = "hermes"
    display_name = "Hermes"
    initial = "h"
    BACKUP_LABEL = "hermes"

    def __init__(self, *, hermes_home: Optional[Union[str, Path]] = None):
        self.hermes_home = (
            Path(hermes_home).expanduser() if hermes_home
            else self._auto_hermes_home()
        )
        self.config_path = self.hermes_home / "config.yaml"
        self.plugin_dir = self.hermes_home / "plugins" / "sibyl"

    @staticmethod
    def _auto_hermes_home() -> Path:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"

    def is_present(self) -> bool:
        # Present if HERMES_HOME exists OR `hermes` binary on PATH
        if self.hermes_home.exists():
            return True
        if shutil.which("hermes"):
            return True
        return False

    def current_state(self) -> dict:
        config_exists = self.config_path.exists()
        plugin_installed = (self.plugin_dir / "__init__.py").exists()
        memory_provider: Optional[str] = None
        if config_exists:
            yaml = _import_yaml()
            if yaml is not None:
                try:
                    raw = self.config_path.read_text(encoding="utf-8")
                    cfg = yaml.safe_load(raw) or {}
                    if isinstance(cfg, dict):
                        mem = cfg.get("memory")
                        if isinstance(mem, dict):
                            memory_provider = mem.get("provider")
                        elif mem is not None:
                            # Panel fix 1: `memory: honcho` or `memory: true` is
                            # an existing setting, so the overwrite gate runs.
                            label = str(mem) if (isinstance(mem, (str, int, float, bool))
                                                 and _HERMES_NAME_RE.match(str(mem))) \
                                else "a non-mapping value"
                            memory_provider = f"memory: {label}"
                except Exception:
                    pass
        return {
            "hermes_home": str(self.hermes_home),
            "config_path": str(self.config_path),
            "config_exists": config_exists,
            "plugin_installed": plugin_installed,
            "memory_provider": memory_provider,
            "wired_with_sibyl": memory_provider == "sibyl",
        }

    def skill_shim_targets(self) -> dict:
        """hermes-agent 0.19.0 documents ~/.hermes/skills/ (hermes_constants.
        get_skills_dir() = $HERMES_HOME/skills; SKILL.md with name +
        description frontmatter, discovered recursively)."""
        sk = _skills_mod()
        names = sk.SHIM_NAMES if sk is not None else {}
        return {n: self.hermes_home / "skills" / s / "SKILL.md" for n, s in names.items()}

    def wire_lifecycle(self, *, force: bool = False, dry_run: bool = False) -> list:
        lines = install_canonical_skills(dry_run=dry_run)
        lines += install_shims("hermes", self.skill_shim_targets(), dry_run=dry_run, force=force)
        return lines

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        out = self._wire_provider(force=force, dry_run=dry_run, prompt_fn=prompt_fn)
        if out.status in ("wired", "already", "dry-run"):
            try:
                out.lifecycle = self.wire_lifecycle(force=force, dry_run=dry_run)
            except Exception as e:  # never fail the provider wiring on the extras
                out.lifecycle = [f"skills not installed: {type(e).__name__}: {e}"]
        return out

    def _wire_provider(self, *, force: bool = False, dry_run: bool = False,
                       prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()
        yaml = _import_yaml()
        if yaml is None:
            return WireOutcome(
                self.name, "error",
                "PyYAML not installed. Run `pip install pyyaml` and retry.",
                handoff=("pip install pyyaml", None, ("sibyl setup hermes",)),
            )

        # 0. A config.yaml that cannot be read stops here, before any install,
        #    backup or write (CHECK-FABLE B-1). Only the error type and its
        #    line and column are reported: never the file text, which can hold
        #    credentials (spec 3.2).
        if self.config_path.exists():
            try:
                yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            except Exception as e:
                brief = _yaml_error_brief(e)
                kind = "is not valid YAML" if isinstance(e, getattr(yaml, "YAMLError", ())) \
                    else "could not be read"
                return WireOutcome(
                    self.name, "error",
                    f"{self.config_path} {kind} ({brief}). Fix the file or move it aside "
                    "and re-run; it was not modified.",
                )

        # 1. Already wired? no-op (no install needed; nothing to overwrite).
        if state["wired_with_sibyl"] and state["plugin_installed"]:
            return WireOutcome(
                self.name, "already",
                f"Hermes already has SIBYL as memory provider in {self.config_path}",
            )

        # 2. Existing non-SIBYL provider? confirm or refuse FIRST.
        #    CLI-10: the overwrite-confirm gate must run before any side effect
        #    (plugin install / config write) so a declined overwrite writes
        #    nothing — previously the plugin was installed before this gate.
        if state["memory_provider"] and state["memory_provider"] != "sibyl" and not force:
            if prompt_fn is None:
                return WireOutcome(
                    self.name, "skipped",
                    f"Existing memory.provider '{state['memory_provider']}'. Use --force to overwrite.",
                    overwrite_of=f"memory provider {state['memory_provider']}",
                )
            ans = prompt_fn(
                f"Hermes currently uses '{state['memory_provider']}' as memory provider. Overwrite with SIBYL?",
                default="N",
            )
            if ans != "y":
                return WireOutcome(self.name, "skipped", "Memory provider overwrite declined.",
                                   overwrite_of=f"memory provider {state['memory_provider']}")

        # 2b. Follow-up D5: prove the config.yaml edit is possible before any
        #     install or backup, so a file setup cannot edit ends with nothing
        #     installed, no backup and the two lines to set by hand.
        try:
            self._planned_config_text(yaml)
        except _HermesConfigNotMapping as e:
            return WireOutcome(self.name, "error", f"config write failed: {e}")
        except _HermesManualEdit:
            return self._manual_edit_outcome()
        except Exception as e:
            # Never str(e): a YAML or decode error can quote the file (spec 3.2).
            return WireOutcome(self.name, "error", f"config write failed: {_yaml_error_brief(e)}")

        # 3. Dry-run report — never installs or writes.
        if dry_run:
            actions = []
            if not state["plugin_installed"]:
                actions.append(f"install plugin at {self.plugin_dir}")
            actions.append(f"set memory.provider=sibyl in {self.config_path}")
            return WireOutcome(self.name, "dry-run", "Would: " + "; ".join(actions))

        # 4. Install plugin if missing — only now that the gate has passed.
        #    Panel fix 4: a refusal code or a plugin folder still without
        #    __init__.py stops here, before config.yaml is touched.
        if not state["plugin_installed"]:
            try:
                self._install_plugin()
            except _HermesPluginRefused as e:
                return self._refused_outcome(e, "memory.provider was not changed")
            except Exception as e:
                return WireOutcome(
                    self.name, "error",
                    f"install-plugin failed: {type(e).__name__}: {e}",
                )
            if not (self.plugin_dir / "__init__.py").exists():
                return WireOutcome(self.name, "error",
                    f"install-plugin did not install the plugin at {self.plugin_dir}; "
                    "memory.provider was not changed")

        # 5. Real write. Backup, then atomic rename.
        try:
            backup = self._backup_config()
        except (OSError, ValueError) as e:
            return _backup_failed(self.name, self.config_path, self.BACKUP_LABEL, e)
        try:
            self._write_config_with_sibyl(yaml)
        except _HermesConfigNotMapping as e:
            return WireOutcome(self.name, "error", f"config write failed: {e}",
                               backup_path=backup)
        except _HermesManualEdit:
            return self._manual_edit_outcome(backup)
        except Exception as e:
            # Never str(e): a YAML or decode error can quote the file (spec 3.2).
            return WireOutcome(
                self.name, "error",
                f"config write failed: {_yaml_error_brief(e)}",
                backup_path=backup,
            )
        return WireOutcome(
            self.name, "wired",
            f"Wired memory.provider=sibyl in {self.config_path}",
            backup_path=backup,
        )

    def _manual_edit_outcome(self, backup: Optional[Path] = None) -> WireOutcome:
        return WireOutcome(self.name, "error",
            f"could not change {self.config_path} safely; it was not modified. "
            "Set this in it by hand:\n" + "\n".join(HERMES_MANUAL_LINES),
            backup_path=backup, manual_block="\n".join(HERMES_MANUAL_LINES), handoff=(
                "done", f"set memory.provider to sibyl in {self.config_path} by hand "
                        "(the two printed lines); setup could not edit it safely", ()))

    # -- doctor (spec dsh-doctor v2, 4.2) -------------------------------------
    def doctor_probe(self) -> list:
        try:
            cfg: Any = None
            if self.config_path.exists():
                yaml = _import_yaml()
                if yaml is None:
                    return [_probe("unreadable", self.config_path,
                                   detail="PyYAML is not installed, so config.yaml cannot be read")]
                try:
                    cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
                except Exception as e:
                    return [_probe("unreadable", self.config_path, detail=_yaml_error_brief(e))]
            mem = cfg.get("memory") if isinstance(cfg, dict) else None
            if mem is not None and not isinstance(mem, dict):
                # Follow-up D5: `memory: honcho` is another setting, the same
                # case setup's overwrite gate stops at, not an absent one.
                name = str(mem) if (isinstance(mem, (str, int, float, bool))
                                    and _HERMES_NAME_RE.match(str(mem))) else "a non-mapping value"
                return [_probe("foreign", self.config_path,
                               detail=f"memory is {name}, not memory.provider sibyl")]
            provider = mem.get("provider") if isinstance(mem, dict) else None
            if not provider:
                return [_probe("absent", self.config_path)]
            if provider != "sibyl":
                name = str(provider) if re.match(r"^[A-Za-z0-9_.-]{1,40}$", str(provider)) else "another provider"
                return [_probe("foreign", self.config_path,
                               detail=f"memory.provider is {name}, not sibyl")]
            if (self.plugin_dir / "__init__.py").exists():
                return [_probe("present", self.config_path,
                               detail="memory.provider is sibyl; plugin installed")]
            return [_probe("present", self.config_path, broken=True, detail="plugin not installed")]
        except Exception as e:
            return [_probe("unreadable", self.config_path, detail=type(e).__name__)]

    def repair_mcp(self, probe: dict) -> WireOutcome:
        """Broken only: install the plugin and nothing else. config.yaml is not
        opened for writing and stays byte-identical, so no backup is taken."""
        try:
            self._install_plugin()
        except _HermesPluginRefused as e:
            return self._refused_outcome(e, "nothing was installed")
        except Exception as e:
            return WireOutcome(self.name, "error",
                               f"install-plugin failed: {type(e).__name__}: {e}")
        if not (self.plugin_dir / "__init__.py").exists():
            return WireOutcome(self.name, "error",
                f"install-plugin did not install the plugin at {self.plugin_dir}")
        return WireOutcome(self.name, "wired",
                           f"Installed the Sibyl plugin for Hermes at {self.plugin_dir}")

    def _install_plugin(self) -> None:
        """Run the Hermes plugin installer. Panel fix 4: install() signals a
        refusal by return code, not by raising; a non-zero code raises
        _HermesPluginRefused here."""
        from sibyl_memory_hermes.install_plugin import install
        out = _HandoffHold(sys.stdout)
        try:
            with contextlib.redirect_stdout(out):
                rc = install(hermes_home=Path(self.hermes_home), force=False, dry_run=False)
        except BaseException:
            out.release(True)
            raise
        refused = isinstance(rc, int) and not isinstance(rc, bool) and rc != 0
        # Minor (follow-up): on a refusal the installer's own handoff (next:
        # install-plugin --force) is dropped, so only setup's handoff prints.
        out.release(not refused)
        if refused:
            raise _HermesPluginRefused(rc, self.plugin_dir)

    def _refused_outcome(self, e: "_HermesPluginRefused", tail: str) -> WireOutcome:
        """Error outcome for an installer refusal, with what the person decides.
        The replacing command sits inside the question, never as next:."""
        dest = e.dest
        home = "" if Path(self.hermes_home) == self._auto_hermes_home() \
            else f" --hermes-home {shlex.quote(str(self.hermes_home))}"
        setup = "sibyl setup hermes" + home
        if e.rc == 2:
            why = f"{dest} already has files"
            human = (f"{dest} already has files that may be an older Sibyl plugin; if they can "
                     f"be replaced, run: sibyl-memory-hermes install-plugin --force{home}, then "
                     f"{setup}")
        elif e.rc == 3:
            why = f"{dest} is a symlink"
            human = f"{dest} is a symlink; remove it, then run {setup}"
        else:
            why = f"{dest} holds files that are not a Sibyl install"
            human = f"{dest} holds files that are not a Sibyl install; move them aside, then run {setup}"
        return WireOutcome(self.name, "error",
                           f"install-plugin refused (code {e.rc}): {why}; {tail}",
                           handoff=("done", human, ()))

    def _backup_config(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.config_path, self.BACKUP_LABEL)

    def _write_config_with_sibyl(self, yaml) -> None:
        """Set memory.provider: sibyl as a text edit (panel fix 1): comments,
        anchors, key order, quoting and non-ASCII text stay as written, and a
        symlinked config.yaml is written through (the link is kept, as for
        Cursor and Codex). Raises _HermesManualEdit when the edit cannot be
        proven; the file is never re-serialized."""
        raw, new = self._planned_config_text(yaml)
        if new == raw:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".yaml.tmp")
        _write_via_tmp(self.config_path, tmp, new, through_symlink=True)

    def _planned_config_text(self, yaml) -> tuple:
        """(current text, edited text) of config.yaml, nothing written.
        Raises _HermesConfigNotMapping or _HermesManualEdit when setup cannot
        make the edit (checked before any install or backup, follow-up D5)."""
        raw = ""
        if self.config_path.exists():
            with open(self.config_path, encoding="utf-8", newline="") as fh:
                raw = fh.read()
            loaded = yaml.safe_load(raw)
            if loaded is not None and not isinstance(loaded, dict):
                # Fail fast: a non-mapping top level means the config is not
                # something we can merge into. Silently reinitializing would
                # destroy the user's existing Hermes settings.
                raise _HermesConfigNotMapping(
                    f"{self.config_path} top level is not a YAML mapping "
                    f"(got {type(loaded).__name__}). Fix the file or move it "
                    "aside and re-run; it was not modified."
                )
        new = _hermes_provider_text(raw)
        if new is None:
            raise _HermesManualEdit(str(self.config_path))
        return raw, new


# ----------------------------------------------------------------------
# ClaudeCodeWirer
# ----------------------------------------------------------------------

class ClaudeCodeWirer:
    name = "claude-code"
    display_name = "Claude Code"
    initial = "c"

    SIBYL_MCP_BLOCK = {"command": "sibyl-memory-mcp"}
    MCP_BINARY = "sibyl-memory-mcp"
    MCP_PACKAGE = "sibyl-memory-mcp"
    MCP_NAME = "sibyl-memory"   # the server name as Claude Code knows it

    def __init__(self, *, settings_path: Optional[Union[str, Path]] = None):
        self.settings_path = (
            Path(settings_path).expanduser() if settings_path
            else Path.home() / ".claude" / "settings.json"
        )

    def is_present(self) -> bool:
        if self.settings_path.exists():
            return True
        if shutil.which("claude"):
            return True
        return False

    def _mcp_binary_found(self) -> bool:
        return resolve_mcp_binary() is not None

    @staticmethod
    def _claude_cli() -> Optional[str]:
        """Path to the `claude` binary, or None. The CLI is the reliable wiring +
        discovery surface — writing ~/.claude/settings.json (the old behavior) is NOT
        where Claude Code discovers MCP servers, which caused the registration bug."""
        return shutil.which("claude")

    def _registered_via_cli(self) -> Optional[bool]:
        """True/False if `claude mcp get <name>` reports the server; None if no CLI.
        This is the source-of-truth detection once the `claude` CLI exists."""
        if not self._claude_cli():
            return None
        rc, _o, _e = _run(["claude", "mcp", "get", self.MCP_NAME], timeout=15)
        return rc == 0

    _last_install_error: Optional[str] = None

    def _install_hint(self) -> str:
        """Append the captured install failure / PEP-668 instruction, if any,
        to the generic 'not on PATH' message. CLI-11."""
        return f"\n{self._last_install_error}" if self._last_install_error else ""

    def _ensure_mcp_binary(self, *, prompt_fn: Optional[Callable[..., str]] = None) -> bool:
        """Check for sibyl-memory-mcp binary; auto-install if missing.

        Returns True if binary is available after the call, False otherwise.

        CLI-11: respect PEP 668 — never silently `pip install` into an
        externally-managed (or pipx-managed) environment; instruct instead.
        On a plain install, capture pip output and surface it on failure rather
        than discarding it to /dev/null (opaque failures).
        """
        if self._mcp_binary_found():
            return True
        self._last_install_error = _install_pkg_or_instruct(self.MCP_PACKAGE)
        return self._mcp_binary_found()

    def verify_mcp_starts(self) -> tuple:
        """Smoke-test: spawn sibyl-memory-mcp and confirm it doesn't crash on startup.

        Returns (ok: bool, message: str). Delegates to the module-level
        ``_verify_mcp_starts`` helper (audit #19 B005) so the Claude and Codex
        wirers share one implementation instead of cross-class dispatch.
        """
        binary = resolve_mcp_binary()
        if not binary:
            return False, f"'{self.MCP_BINARY}' is not installed"
        return _verify_mcp_starts(binary)

    def current_state(self) -> dict:
        settings_exists = self.settings_path.exists()
        mcp_servers: dict = {}
        sibyl_block: Optional[dict] = None
        settings_parse_error: Optional[str] = None
        if settings_exists:
            try:
                cfg = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    raw_servers = cfg.get("mcpServers", {})
                    if isinstance(raw_servers, dict):
                        mcp_servers = raw_servers
                        sibyl_block = mcp_servers.get("sibyl-memory")
            except Exception as e:
                settings_parse_error = f"{type(e).__name__}: {e}"
        mcp_binary = self._mcp_binary_found()
        cli_registered = self._registered_via_cli()  # None when no `claude` CLI
        # Source of truth: when the claude CLI exists, trust `claude mcp get` (where
        # Claude Code actually discovers servers). Without it nothing is verifiable.
        if cli_registered is None:
            # Panel fix 10: without the CLI the registration cannot be seen,
            # and settings.json is not where Claude Code reads servers from.
            wired = False
        else:
            wired = bool(cli_registered and mcp_binary)
        return {
            "settings_path": str(self.settings_path),
            "settings_exists": settings_exists,
            "settings_parse_error": settings_parse_error,
            "mcp_servers_count": len(mcp_servers),
            "sibyl_mcp": sibyl_block,
            "mcp_binary_found": mcp_binary,
            "claude_cli": self._claude_cli() is not None,
            "cli_registered": cli_registered,
            "wired_with_sibyl": wired,
        }

    def _wire_via_cli(self, *, force: bool, dry_run: bool) -> WireOutcome:
        """Register through `claude mcp add --scope user` — the reliable path that
        writes where Claude Code actually discovers servers (fixes the settings.json
        registration/discovery bug). `--scope user` makes it global across projects."""
        if not dry_run and not self._ensure_mcp_binary():
            return WireOutcome(self.name, "error",
                f"'{self.MCP_BINARY}' is not installed. Install it: pip install {self.MCP_PACKAGE}"
                + self._install_hint(), handoff=_binary_missing_handoff(self.name))
        if self._registered_via_cli():
            if not force:
                return WireOutcome(self.name, "already",
                    "Claude Code already has the sibyl-memory MCP server (claude mcp).")
            if not dry_run:
                _run(["claude", "mcp", "remove", "-s", "user", self.MCP_NAME], timeout=15)
        # Register the RESOLVED absolute path, not the bare name: a user-scope server
        # is launched from Claude Code's own PATH, which may not include a venv's bin.
        # Bare-name registration shows "✗ Failed to connect" for venv installs; the
        # absolute path connects regardless of how PATH is set when claude launches it.
        binpath = mcp_command_path()
        cmd = ["claude", "mcp", "add", "--scope", "user", self.MCP_NAME, "--", binpath]
        if dry_run:
            return WireOutcome(self.name, "dry-run", "Would run: " + " ".join(cmd))
        rc, out, err = _run(cmd, timeout=30)
        if rc != 0:
            return WireOutcome(self.name, "error",
                f"`claude mcp add` failed (exit {rc}): {(err or out).strip()[:200]}",
                handoff=(f"claude mcp add --scope user {self.MCP_NAME} -- {binpath}", None, ()))
        # Post-wire verification (bug report, 2026-06-01): a 0 exit from
        # `claude mcp add` has been observed to not guarantee discovery. Confirm the
        # server actually shows in `claude mcp get`, and surface concrete remediation
        # instead of reporting a false success that leaves the MCP absent from /mcp.
        if self._registered_via_cli() is False:
            return WireOutcome(self.name, "error",
                "ran `claude mcp add` (exit 0) but the server is not in `claude mcp list`. "
                "restart Claude Code, then run `claude mcp list`; if still absent, run "
                f"`claude mcp add --scope user {self.MCP_NAME} -- {binpath}` manually.",
                handoff=("claude mcp list", "restart Claude Code", ()))
        return WireOutcome(self.name, "wired",
            "Registered sibyl-memory with Claude Code via `claude mcp add --scope user` (verified in `claude mcp list`).")

    # -- doctor (spec dsh-doctor v2, 4.1) -------------------------------------
    @staticmethod
    def _claude_user_config() -> Path:
        d = os.environ.get("CLAUDE_CONFIG_DIR")
        if d and d.strip():
            return Path(d).expanduser() / ".claude.json"
        return Path.home() / ".claude.json"

    def _user_scope_entry(self) -> Optional[dict]:
        """The user-scope sibyl-memory object from Claude Code's own config, or
        None. Only that one object is read out of the file (it holds account
        data); nothing else from it is ever returned or printed."""
        try:
            data = json.loads(self._claude_user_config().read_text(encoding="utf-8"))
        except Exception:
            return None
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        entry = servers.get(self.MCP_NAME) if isinstance(servers, dict) else None
        return entry if isinstance(entry, dict) else None

    def doctor_probe(self) -> list:
        cfgp = self._claude_user_config()
        try:
            if self._claude_cli() is None:
                return [_probe("unverifiable", cfgp, detail=(
                    "the claude command is not on PATH, so the registration cannot be checked"))]
            if not self._registered_via_cli():
                return [_probe("absent", cfgp)]
            entry = self._user_scope_entry()
            if entry is None or not isinstance(entry.get("command"), str):
                return [_probe("present", cfgp, detail=(
                    "registered (claude mcp) outside user scope or not readable; command path "
                    "not checked"))]
            cmd = entry["command"]
            if not _is_our_command(cmd):
                return [_probe("foreign", cfgp, command=None,
                               detail="the sibyl-memory registration runs another command")]
            extra = {}
            if (any(k not in ("type", "command", "args", "env") for k in entry)
                    or entry.get("args") not in (None, []) or entry.get("env") not in (None, {})):
                extra["customized"] = True
            return [_probe("present", cfgp, command=cmd, **extra)]
        except Exception as e:
            return [_probe("unreadable", cfgp, detail=type(e).__name__)]

    def repair_mcp(self, probe: dict) -> WireOutcome:
        """Stale or broken plain user-scope entry: remove, then add again with
        the resolved absolute path (verified with claude mcp get). A failed
        add carries the manual command as its handoff."""
        binpath = mcp_command_path()
        manual = f"claude mcp add --scope user {self.MCP_NAME} -- {shlex.quote(binpath)}"
        if self._claude_cli() is None:
            return WireOutcome(self.name, "error", "the claude command is not on PATH",
                               handoff=(manual, None, ()))
        if probe.get("customized"):
            return WireOutcome(self.name, "error", (
                "entry has custom env or args; update the path by hand: claude mcp remove -s user "
                f"sibyl-memory, then {manual} with your settings"))
        return self._wire_via_cli(force=True, dry_run=False)

    # -- save / debloat wiring (spec plugin-save-debloat 3.5) ---------------
    def skill_shim_targets(self) -> dict:
        sk = _skills_mod()
        names = sk.SHIM_NAMES if sk is not None else {}
        return {n: self.settings_path.parent / "skills" / s / "SKILL.md" for n, s in names.items()}

    def hooks_state(self) -> dict:
        """{'present': n of our hook entries, 'error': parse error or None}."""
        if not self.settings_path.exists():
            return {"present": 0, "error": None}
        try:
            cfg = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"present": 0, "error": f"{type(e).__name__}"}
        return {"present": count_our_hooks(cfg), "error": None}

    def _read_settings_for_edit(self) -> dict:
        cfg: dict = {}
        if self.settings_path.exists():
            if self.settings_path.is_symlink():
                raise ValueError(f"{self.settings_path} is a symlink; not followed")
            raw = self.settings_path.read_text(encoding="utf-8")
            if raw.strip():
                try:
                    loaded = json.loads(raw)
                except ValueError as e:
                    raise ValueError(
                        f"{self.settings_path} is not valid JSON ({e}). Fix the file or move it "
                        "aside and re-run setup; your settings were not modified.") from e
                if not isinstance(loaded, dict):
                    raise ValueError(f"{self.settings_path} top level is not a JSON object; "
                                     "your settings were not modified.")
                cfg = loaded
        return cfg

    def wire_hooks(self, *, dry_run: bool = False) -> list:
        binary = sibyl_binary()
        if dry_run:
            try:
                cfg = self._read_settings_for_edit()
                if merge_hooks(cfg, binary) == cfg:
                    return [f"hooks: {len(HOOK_EVENTS)} already in {self.settings_path}"]
            except ValueError as e:
                return [f"hooks would not be written: {e}"]
            return [f"would add hook {ev} -> {hook_command(binary, arg)} (timeout {HOOK_TIMEOUT_SECONDS}s)"
                    for ev, arg in HOOK_EVENTS]
        try:
            cfg = self._read_settings_for_edit()
            new = merge_hooks(cfg, binary)
        except ValueError as e:
            return [f"hooks not written: {e}"]
        if new == cfg:
            return [f"hooks: {len(HOOK_EVENTS)} already in {self.settings_path}"]
        try:
            backup = self._backup_settings()
        except (OSError, ValueError) as e:
            return [f"hooks not written: backup failed ({type(e).__name__})"]
        _atomic_text(self.settings_path, json.dumps(new, indent=2) + "\n")
        lines = [f"hooks: {len(HOOK_EVENTS)} written to {self.settings_path} "
                 f"({', '.join(e for e, _a in HOOK_EVENTS)}; command {binary})"]
        if backup:
            lines.append(f"  backup at {backup}")
        return lines

    def unwire_hooks(self) -> list:
        try:
            cfg = self._read_settings_for_edit()
        except ValueError as e:
            return [f"hooks not removed: {e}"]
        new = remove_hooks(cfg)
        if new == cfg:
            return ["hooks: none of ours present"]
        try:
            self._backup_settings()
        except (OSError, ValueError) as e:
            return [f"hooks not removed: backup failed ({type(e).__name__})"]
        _atomic_text(self.settings_path, json.dumps(new, indent=2) + "\n")
        return [f"hooks: removed ours from {self.settings_path}"]

    def wire_lifecycle(self, *, force: bool = False, dry_run: bool = False) -> list:
        lines = install_canonical_skills(dry_run=dry_run)
        lines += install_shims("claude-code", self.skill_shim_targets(), dry_run=dry_run, force=force)
        lines += self.wire_hooks(dry_run=dry_run)
        return lines

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        cli_missing = self._claude_cli() is None
        out = self._wire_mcp(force=force, dry_run=dry_run, prompt_fn=prompt_fn)
        if out.status in ("wired", "already", "dry-run") or (
                cli_missing and out.status == "skipped"):
            try:
                out.lifecycle = self.wire_lifecycle(force=force, dry_run=dry_run)
            except Exception as e:  # never fail the MCP wiring on the extras
                out.lifecycle = [f"skills/hooks not installed: {type(e).__name__}: {e}"]
        return out

    def _wire_mcp(self, *, force: bool = False, dry_run: bool = False,
                  prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        # The `claude` CLI is the only surface that registers a server where
        # Claude Code discovers it.
        if self._claude_cli():
            return self._wire_via_cli(force=force, dry_run=dry_run)
        # Panel fix 10 (2026-11-14): without the CLI nothing is written for the
        # MCP server (~/.claude/settings.json is not where Claude Code reads
        # servers from, so an entry there was reported as wired while Claude
        # Code never saw it). The outcome says so and carries the exact
        # command; hooks and skills are still installed by wire().
        binpath = mcp_command_path()
        manual = f"claude mcp add --scope user {self.MCP_NAME} -- {shlex.quote(binpath)}"
        if dry_run:
            return WireOutcome(self.name, "dry-run",
                f"Would not register the MCP server: the claude command is not on PATH. "
                f"Run: {manual}")
        if not self._ensure_mcp_binary(prompt_fn=prompt_fn):
            return WireOutcome(
                self.name, "error",
                f"'{self.MCP_BINARY}' is not installed. "
                f"Install it: pip install {self.MCP_PACKAGE}" + self._install_hint(),
                handoff=_binary_missing_handoff(self.name),
            )
        return WireOutcome(
            self.name, "skipped",
            "the claude command is not on PATH, so the MCP server was not registered with "
            f"Claude Code. Run: {manual}",
            handoff=(manual, "if claude is not found in this shell, run that command in a "
                             "terminal where Claude Code is installed", ()),
        )

    def _backup_settings(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.settings_path, "claude-code")


# ----------------------------------------------------------------------
# CodexWirer  — Codex discovers MCP servers from ~/.codex/config.toml, so editing
# that file IS the reliable method (unlike Claude's settings.json). Append the
# [mcp_servers.sibyl_memory] table if absent. Atomic, .bak backup, idempotent.
# ----------------------------------------------------------------------

# Codex config.toml grammar for our one table (spec dsh-doctor v2, 4.3). Shared
# by current_state, the doctor probe and the command update. Line based: a
# commented-out header never counts, and any other mention of sibyl_memory (an
# inline table, dotted keys) is a form setup does not edit.
#
# Panel fix 2 (2026-11-14): table headers are found structurally, not by a
# character-class regex. A line is a header when it starts with '[' at array
# depth 0 outside a multi-line string (a TOML value can never start a line
# with '[' there). Its key path is parsed with bare, basic-quoted and
# literal-quoted keys, so [projects."/home/u/repo"] (which Codex writes for
# every trusted folder) and [mcp_servers."ctx@upstash"] are headers too, and
# a [mcp_servers.sibyl_memory] line inside a """...""" value is not.
_CODEX_OURS = ("mcp_servers", "sibyl_memory")
_CODEX_COMMENT_RE = re.compile(r"^\s*#")
# The command key, bare or quoted (panel fix 7): "command" and 'command' are
# the same TOML key as command.
_CODEX_CMD_RE = re.compile(r"""^(\s*)(command|"command"|'command')\s*=\s*(.*)$""")
_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_TOML_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _strip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def _toml_basic_key(s: str, i: int) -> Optional[tuple]:
    """Parse a basic-quoted key starting at s[i] == '"'. (value, index after
    the closing quote) or None."""
    out = []
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == '"':
            return "".join(out), j + 1
        if c == "\\":
            e = s[j + 1:j + 2]
            if e in _TOML_ESCAPES:
                out.append(_TOML_ESCAPES[e])
                j += 2
                continue
            if e in ("u", "U"):
                n = 4 if e == "u" else 8
                hexd = s[j + 2:j + 2 + n]
                if len(hexd) != n or not re.fullmatch(r"[0-9A-Fa-f]+", hexd):
                    return None
                try:
                    out.append(chr(int(hexd, 16)))
                except ValueError:
                    return None
                j += 2 + n
                continue
            return None
        out.append(c)
        j += 1
    return None


def _toml_header_path(raw: str) -> Optional[tuple]:
    """(key path tuple, is_array_of_tables) when ``raw`` is a well-formed
    table or array-of-tables header line, else None."""
    s = raw.strip()
    if not s.startswith("["):
        return None
    arr = s.startswith("[[")
    i = 2 if arr else 1
    n = len(s)
    keys: list = []
    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            return None
        c = s[i]
        if c == '"':
            got = _toml_basic_key(s, i)
            if got is None:
                return None
            keys.append(got[0])
            i = got[1]
        elif c == "'":
            end = s.find("'", i + 1)
            if end == -1:
                return None
            keys.append(s[i + 1:end])
            i = end + 1
        else:
            m = _TOML_BARE_KEY_RE.match(s, i)
            if not m:
                return None
            keys.append(m.group())
            i = m.end()
        while i < n and s[i] in " \t":
            i += 1
        if i < n and s[i] == ".":
            i += 1
            continue
        break
    close = "]]" if arr else "]"
    if not s.startswith(close, i):
        return None
    rest = s[i + len(close):].strip()
    if rest and not rest.startswith("#"):
        return None
    return tuple(keys), arr


def _codex_line_kinds(lines: list) -> list:
    """Per line: None for a line that is not a table header, else
    (key path or None when malformed, is_array). Headers are depth-0 lines
    outside multi-line strings whose first character is '['."""
    out = []
    depth, ml = 0, None
    for ln in lines:
        raw = _strip_eol(ln)
        if depth == 0 and ml is None and raw.lstrip().startswith("["):
            hp = _toml_header_path(raw)
            out.append(hp if hp is not None else (None, False))
            depth, ml = 0, None
            continue
        out.append(None)
        depth, ml = _toml_line_state(raw, depth, ml)
    return out


def _codex_root_mcp_servers_key(lines: list) -> bool:
    """True when ``mcp_servers`` is assigned as a key before the first table
    header (``mcp_servers = { ... }`` or ``mcp_servers.x = ...``): appending a
    [mcp_servers.sibyl_memory] table to such a file is a TOML redefinition or
    an edit setup cannot prove without a parser."""
    depth, ml = 0, None
    for ln in lines:
        raw = _strip_eol(ln)
        if depth == 0 and ml is None:
            if raw.lstrip().startswith("["):
                return False
            if re.match(r"""^\s*(?:mcp_servers|"mcp_servers"|'mcp_servers')\s*[=.]""", raw):
                return True
        depth, ml = _toml_line_state(raw, depth, ml)
    return False


def _toml_text_open_at_end(text: str) -> bool:
    """True when the text ends inside a multi-line string or an open array."""
    depth, ml = 0, None
    for ln in text.splitlines():
        depth, ml = _toml_line_state(ln, depth, ml)
    return depth != 0 or ml is not None


def _toml_string_value(rest: str) -> Optional[tuple]:
    """Parse a TOML basic ("...") or literal ('...') string at the start of
    ``rest``. Returns (value, suffix): suffix is what follows the closing quote
    (only whitespace and an optional comment are allowed). Returns None for any
    other value (unparsed). In a basic string only the two escapes for a
    backslash and a double quote are understood; any other escape leaves the
    value unparsed."""
    if not rest:
        return None
    q = rest[0]
    if rest[:3] == q * 3:
        return None          # multi-line strings: not a form setup edits
    if q == "'":
        end = rest.find("'", 1)
        if end == -1:
            return None
        value, tail = rest[1:end], rest[end + 1:]
    elif q == '"':
        out = []
        i = 1
        closed = False
        while i < len(rest):
            c = rest[i]
            if c == "\\":
                nxt = rest[i + 1:i + 2]
                if nxt not in ("\\", '"'):
                    return None
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                closed = True
                break
            out.append(c)
            i += 1
        if not closed:
            return None
        value, tail = "".join(out), rest[i + 1:]
    else:
        return None
    if tail.strip() and not tail.lstrip().startswith("#"):
        return None
    return value, tail.rstrip()


def _toml_line_state(line: str, depth: int, ml: Optional[str]) -> tuple:
    """Carry (array bracket depth, open multi-line string delimiter) across one
    line of TOML text. Brackets inside strings and comments do not count."""
    i, n = 0, len(line)
    while i < n:
        if ml is not None:
            end = line.find(ml, i)
            if end == -1:
                return depth, ml
            i = end + 3
            ml = None
            continue
        c = line[i]
        if c == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml = line[i:i + 3]
            i += 3
            continue
        if c == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            end = line.find("'", i + 1)
            i = n if end == -1 else end + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth = max(0, depth - 1)
        i += 1
    return depth, ml


def _codex_body_end(lines: list, start: int) -> int:
    """Index of the first table header at or after ``start`` (a depth-0 line
    outside a multi-line array or string whose first character is '['), or
    len(lines). ``start`` is the line after a header."""
    depth, ml = 0, None
    j = start
    while j < len(lines):
        raw = _strip_eol(lines[j])
        if depth == 0 and ml is None and raw.lstrip().startswith("["):
            return j
        depth, ml = _toml_line_state(raw, depth, ml)
        j += 1
    return j


def _codex_body_command_lines(lines: list, b: int, e: int) -> list:
    """(index, match) for every command key line in lines[b:e] that sits at
    depth 0 outside a multi-line string (a line inside a value never counts)."""
    out = []
    depth, ml = 0, None
    for k in range(b, e):
        raw = _strip_eol(lines[k])
        if depth == 0 and ml is None:
            m = _CODEX_CMD_RE.match(raw)
            if m:
                out.append((k, m))
        depth, ml = _toml_line_state(raw, depth, ml)
    return out


def _codex_scan(text: str) -> dict:
    """Locate our table in a Codex config.toml text (no TOML parser needed)."""
    lines = text.splitlines(keepends=True)
    kinds = _codex_line_kinds(lines)
    headers = [i for i, k in enumerate(kinds)
               if k is not None and k[0] == _CODEX_OURS and not k[1]]
    sub_heads = [i for i, k in enumerate(kinds)
                 if k is not None and k[0] is not None and len(k[0]) > 2
                 and k[0][:2] == _CODEX_OURS]

    def _end(i: int) -> int:
        return next((j for j in range(i + 1, len(lines)) if kinds[j] is not None), len(lines))

    owned = set()            # lines inside our table body or a subtable of ours
    subtables = []           # (header line, body end) of [mcp_servers.sibyl_memory.*]
    for i in headers + sub_heads:
        e = _end(i)
        owned.update(range(i + 1, e))
        if i not in headers:
            subtables.append((i, e))
    other = False
    for i, ln in enumerate(lines):
        raw = _strip_eol(ln)
        if "sibyl_memory" not in raw or _CODEX_COMMENT_RE.match(raw):
            continue
        if i in headers or i in sub_heads or i in owned:
            continue
        other = True
    body = None
    cmd = {"line": None, "indent": "", "key": "command", "value": None, "suffix": "",
           "parsed": False}
    n_cmd = 0
    if headers:
        h = headers[0]
        e = _end(h)
        body = (h + 1, e)
        found = _codex_body_command_lines(lines, h + 1, e)
        n_cmd = len(found)
        if found:
            k, m = found[0]
            cmd["line"] = k
            cmd["indent"] = m.group(1)
            cmd["key"] = m.group(2)
            parsed = _toml_string_value(m.group(3))
            if parsed is not None:
                cmd["value"], cmd["suffix"] = parsed
                cmd["parsed"] = True
    return {"lines": lines, "headers": headers, "other_mention": other, "body": body,
            "command": cmd, "command_lines": n_cmd, "subtables": subtables}


try:  # Python 3.11+
    import tomllib as _tomllib
except ImportError:  # pragma: no cover - Python 3.10
    # Panel fix (2026-11-14): tomli is a declared dependency on Python < 3.11,
    # so the parse-and-compare proof runs on every supported Python. The text
    # checks stay as defense in depth and stand alone only if tomli is absent.
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ImportError:
        _tomllib = None


def _toml_loads(text: str) -> dict:
    """tomllib.loads after one leading UTF-8 BOM is dropped (follow-up D4):
    Codex reads a config saved with a BOM, tomllib rejects it. The text
    edits keep the BOM; only the parse skips it."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return _tomllib.loads(text)


class CodexWirer:
    name = "codex"
    display_name = "OpenAI Codex"
    initial = "x"

    MCP_BINARY = "sibyl-memory-mcp"
    MCP_PACKAGE = "sibyl-memory-mcp"
    BACKUP_LABEL = "codex"
    HEADER = "[mcp_servers.sibyl_memory]"
    # Fallback/canonical shape. The real block is built at wire time by
    # _block_text() using the RESOLVED absolute path — codex spawns the server
    # from its own captured environment, not the interactive shell, so a bare
    # command name can fail to resolve. `codex mcp add -- <path>` itself writes
    # the absolute path; we match that.
    BLOCK = '\n[mcp_servers.sibyl_memory]\ncommand = "sibyl-memory-mcp"\n'

    def __init__(self, *, config_path: Optional[Union[str, Path]] = None):
        self.config_path = (
            Path(config_path).expanduser() if config_path
            else Path.home() / ".codex" / "config.toml"
        )

    def is_present(self) -> bool:
        return self.config_path.exists() or shutil.which("codex") is not None

    def _mcp_binary_found(self) -> bool:
        return resolve_mcp_binary() is not None

    def _mcp_command(self) -> str:
        """Resolved absolute path to the MCP binary, falling back to the bare
        name if it cannot be resolved (mirrors the Claude wirer fix)."""
        return mcp_command_path()

    @staticmethod
    def _toml_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def _block_text(self) -> str:
        cmd = self._toml_escape(self._mcp_command())
        return f'\n[mcp_servers.sibyl_memory]\ncommand = "{cmd}"\n'

    _last_install_error: Optional[str] = None

    def _install_hint(self) -> str:
        return f"\n{self._last_install_error}" if self._last_install_error else ""

    def _ensure_mcp_binary(self) -> bool:
        # CLI-11: same hardening as the Claude wirer — respect PEP 668, surface
        # pip output on failure instead of discarding it.
        if self._mcp_binary_found():
            return True
        self._last_install_error = _install_pkg_or_instruct(self.MCP_PACKAGE)
        return self._mcp_binary_found()

    def _read_scan(self) -> Optional[dict]:
        if not self.config_path.exists():
            return None
        return _codex_scan(self._read_raw())

    def _read_raw(self) -> str:
        """The file text with its line endings as written (CRLF survives)."""
        with open(self.config_path, encoding="utf-8", newline="") as fh:
            return fh.read()

    def current_state(self) -> dict:
        exists = self.config_path.exists()
        wired = False
        if exists:
            try:
                sc = self._read_scan()
                cmd = sc["command"]
                stale = bool(cmd["parsed"] and os.path.isabs(cmd["value"] or "")
                             and not _command_resolves(cmd["value"]))
                # cli 0.7.0: a commented-out header, a second definition, another
                # form of sibyl_memory or a stale absolute path is not "wired".
                # Panel fix 15: nor is a table with no command line, an unparsed
                # one or one that runs another server; panel fix 3: nor is a
                # file a TOML parser rejects.
                wired = (len(sc["headers"]) == 1 and not sc["other_mention"] and not stale
                         and sc["command_lines"] == 1 and cmd["parsed"]
                         and _is_our_command(cmd["value"])
                         and self._toml_error("".join(sc["lines"])) is None
                         and not _codex_root_mcp_servers_key(sc["lines"]))
            except Exception:
                pass
        return {
            "config_path": str(self.config_path),
            "config_exists": exists,
            "mcp_binary_found": self._mcp_binary_found(),
            "wired_with_sibyl": wired,
        }

    def instructions(self) -> list[str]:
        """Manual steps the guided flow prints if it can't (or won't) auto-edit."""
        cmd = self._mcp_command()
        return [
            "Open a new terminal.",
            f"Add this to {self.config_path} (create the file if needed):",
            "    [mcp_servers.sibyl_memory]",
            f'    command = "{cmd}"',
            "Restart Codex, then come back here.",
        ]

    def verify_mcp_starts(self) -> tuple:
        # reuse the same stdio smoke-test via the shared module-level helper
        # (audit #19 B005 — no more cross-class dispatch).
        binary = resolve_mcp_binary()
        if not binary:
            return False, f"'{self.MCP_BINARY}' is not installed"
        return _verify_mcp_starts(binary)

    # -- save / debloat wiring (spec plugin-save-debloat 3.5, D9) -----------
    # Verified against the installed Codex CLI 0.135.0 (binary strings): it
    # reads a global AGENTS.md from CODEX_HOME (~/.codex/AGENTS.md) and
    # discovers skills under $CODEX_HOME/skills (~/.codex/skills when unset);
    # there is no ~/.codex/prompts custom-prompt loader in that version, so
    # the Codex delivery is the AGENTS.md block, two skill shims and the MCP
    # prompts. The skill format is the one the binary's bundled skill-creator
    # validates (scripts/quick_validate.py): SKILL.md in <skills>/<name>/, YAML
    # frontmatter with a hyphen-case `name` (at most 64 characters) and a
    # `description` (at most 1024, no angle brackets).
    @property
    def agents_path(self) -> Path:
        return self.config_path.parent / "AGENTS.md"

    def skill_shim_targets(self) -> dict:
        sk = _skills_mod()
        names = sk.SHIM_NAMES if sk is not None else {}
        return {n: self.config_path.parent / "skills" / s / "SKILL.md" for n, s in names.items()}

    def block_state(self) -> bool:
        try:
            return CODEX_BLOCK_BEGIN in self.agents_path.read_text(encoding="utf-8")
        except OSError:
            return False

    def wire_block(self, *, dry_run: bool = False) -> list:
        p = self.agents_path
        if dry_run:
            try:
                if (not p.is_symlink() and p.is_file()
                        and upsert_block(p.read_text(encoding="utf-8"), codex_block())
                        == p.read_text(encoding="utf-8")):
                    return [f"block: already in {p}"]
            except (OSError, UnicodeDecodeError):
                pass
            return [f"would add the Sibyl Memory block to {p}"]
        if p.is_symlink():
            return [f"block not written: {p} is a symlink"]
        existing = ""
        if p.exists():
            try:
                existing = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return [f"block not written: {p} unreadable ({type(e).__name__})"]
        new = upsert_block(existing, codex_block())
        if new == existing:
            return [f"block: already in {p}"]
        _atomic_text(p, new)
        return [f"block: written to {p}"]

    def unwire_block(self) -> list:
        p = self.agents_path
        if p.is_symlink() or not p.exists():
            return []
        existing = p.read_text(encoding="utf-8")
        new = remove_block(existing)
        if new == existing:
            return []
        _atomic_text(p, new)
        return [f"block: removed from {p}"]

    def wire_lifecycle(self, *, force: bool = False, dry_run: bool = False) -> list:
        return (install_canonical_skills(dry_run=dry_run) + self.wire_block(dry_run=dry_run)
                + install_shims("codex", self.skill_shim_targets(), dry_run=dry_run, force=force))

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        out = self._wire_mcp(force=force, dry_run=dry_run, prompt_fn=prompt_fn)
        if out.status in ("wired", "already", "dry-run"):
            try:
                out.lifecycle = self.wire_lifecycle(force=force, dry_run=dry_run)
            except Exception as e:
                out.lifecycle = [f"skills/block not installed: {type(e).__name__}: {e}"]
        return out

    def _wire_mcp(self, *, force: bool = False, dry_run: bool = False,
                  prompt_fn: Optional[Callable[..., str]] = None,
                  update_only: bool = False) -> WireOutcome:
        state = self.current_state()
        try:
            sc = self._read_scan()
        except (OSError, UnicodeDecodeError) as e:
            return WireOutcome(self.name, "error",
                f"{self.config_path} could not be read ({type(e).__name__}); it was not modified.")
        if sc is not None and (sc["other_mention"] or len(sc["headers"]) > 1):
            # Appending would be a TOML redefinition error (cli 0.7.0).
            return WireOutcome(self.name, "error",
                f"{self.config_path} already defines sibyl_memory in a form setup does not "
                "edit; it was not modified.")
        if sc is not None:
            # Panel fix 3: never edit a file the TOML parser already rejects.
            terr = self._toml_error("".join(sc["lines"]))
            if terr is not None:
                return WireOutcome(self.name, "error",
                    f"{self.config_path} is not valid TOML ({terr}); it was not modified. "
                    "Fix the file, then run sibyl setup codex again.",
                    handoff=("done", f"{self.config_path} is not valid TOML ({terr}); fix it, "
                                     "then run sibyl setup codex", ()))
        has_header = bool(sc and sc["headers"])
        if update_only and not has_header:
            return WireOutcome(self.name, "error",
                f"{self.config_path} has no [mcp_servers.sibyl_memory] table to repair; "
                "run sibyl setup codex to add one.")
        cmd = sc["command"] if has_header else None
        if has_header and cmd["line"] is not None and not cmd["parsed"]:
            return WireOutcome(self.name, "error",
                f"{self.config_path} has a sibyl_memory command value setup does not edit; "
                "it was not modified.")
        if has_header and sc["command_lines"] > 1:
            return WireOutcome(self.name, "error",
                f"{self.config_path} has more than one command line in the sibyl_memory table; "
                "it was not modified.")
        # The foreign gate runs before the "already" return (CHECK-FABLE N-1):
        # a table of that name that runs another server is not ours.
        foreign = bool(has_header and cmd["parsed"] and not _is_our_command(cmd["value"]))
        if foreign and update_only:
            return WireOutcome(self.name, "error",
                f"the sibyl_memory table in {self.config_path} runs another command; "
                "doctor does not change it.")
        if foreign and not force:
            # Another server under our table name: same rule as Cursor and Hermes.
            if prompt_fn is None or prompt_fn(
                    "Codex has a 'sibyl_memory' MCP entry pointing elsewhere. Replace it?",
                    default="N") != "y":
                return WireOutcome(self.name, "skipped",
                    f"Existing sibyl_memory entry in {self.config_path} points elsewhere. "
                    "Use --force to replace it.",
                    overwrite_of="a different sibyl_memory MCP entry")
        if state["wired_with_sibyl"] and not force and not foreign:
            return WireOutcome(self.name, "already",
                f"Codex already has the sibyl-memory MCP server in {self.config_path}")
        if dry_run:
            if foreign:
                return WireOutcome(self.name, "dry-run",
                    f"Would replace the sibyl_memory table in {self.config_path}")
            if has_header:
                return WireOutcome(self.name, "dry-run",
                    f"Would update the sibyl_memory command in {self.config_path}")
            verb = "create + add" if not state["config_exists"] else "append"
            return WireOutcome(self.name, "dry-run",
                f"Would {verb} [mcp_servers.sibyl_memory] in {self.config_path}")
        if not self._ensure_mcp_binary():
            return WireOutcome(self.name, "error",
                f"'{self.MCP_BINARY}' is not installed. Install it: pip install {self.MCP_PACKAGE}"
                + self._install_hint(), handoff=_binary_missing_handoff(self.name))
        new: Optional[str] = None
        if has_header:
            # Stale-path fix (cli 0.7.0): rewrite the command line in place; a
            # foreign table is replaced whole (no inherited args or env, the
            # same whole-entry rule as Cursor and DeepSeek Harness). The backup
            # is taken only when the text will change.
            existing = "".join(sc["lines"])
            try:
                new = (self._replace_table(existing) if foreign
                       else self._set_block_command(existing))
            except ValueError as e:
                return WireOutcome(self.name, "error", f"{self.config_path}: {e}; it was not modified.")
            if new == existing:
                return WireOutcome(self.name, "already",
                    f"Codex already has the sibyl-memory MCP server in {self.config_path}")
            try:
                if foreign:
                    self._verify_table_replace(existing, new)
                else:
                    self._verify_command_edit(existing, new)
            except ValueError as e:
                return WireOutcome(self.name, "error", str(e))
        else:
            # Panel fix 3: the appended table is planned and proven before the
            # backup; a file it cannot be added to safely gets the manual table.
            try:
                self._append_plan(self._read_raw() if self.config_path.exists() else "")
            except ValueError as e:
                return self._manual_table_error(str(e))
        try:
            backup = self._backup_config()
        except (OSError, ValueError) as e:
            return _backup_failed(self.name, self.config_path, self.BACKUP_LABEL, e)
        try:
            if new is not None:
                if self._read_raw() != existing:
                    return WireOutcome(self.name, "error",
                        f"{self.config_path} changed while setup was running; it was not "
                        "modified. Run it again.", backup_path=backup)
                _write_via_tmp(self.config_path, self.config_path.with_suffix(".toml.tmp"), new,
                               through_symlink=True)
            else:
                self._append_block()
        except Exception as e:
            return WireOutcome(self.name, "error",
                f"config write failed: {type(e).__name__}", backup_path=backup)
        if foreign:
            return WireOutcome(self.name, "wired",
                f"Replaced the sibyl_memory table in {self.config_path}", backup_path=backup)
        if has_header:
            return WireOutcome(self.name, "wired",
                f"Updated the sibyl_memory command in {self.config_path}", backup_path=backup)
        return WireOutcome(self.name, "wired",
            f"Added [mcp_servers.sibyl_memory] to {self.config_path}", backup_path=backup)

    # -- doctor (spec dsh-doctor v2, 4.3) -------------------------------------
    def doctor_probe(self) -> list:
        try:
            if not self.config_path.exists():
                return [_probe("absent", self.config_path)]
            sc = self._read_scan()
            if sc["other_mention"] or len(sc["headers"]) > 1:
                return [_probe("unreadable", self.config_path,
                               detail="sibyl_memory is defined in a form setup does not edit")]
            # Panel fix 3: a file Codex cannot parse is never reported healthy.
            terr = self._toml_error("".join(sc["lines"]))
            if terr is not None:
                return [_probe("unreadable", self.config_path, detail=f"not valid TOML ({terr})")]
            if sc["headers"] and _codex_root_mcp_servers_key(sc["lines"]):
                return [_probe("unreadable", self.config_path,
                               detail="mcp_servers is also set as a key; the table cannot be read")]
            if not sc["headers"]:
                return [_probe("absent", self.config_path)]
            cmd = sc["command"]
            if not cmd["parsed"]:
                # Panel fix 7: an insert is proven only with a TOML parser.
                return [_probe("present", self.config_path, broken=True, detail="no command in the table",
                               fixable=cmd["line"] is None and sc["command_lines"] == 0
                               and _tomllib is not None)]
            value = cmd["value"]
            if not _is_our_command(value):
                # Never the foreign command string (it can carry a credential).
                return [_probe("foreign", self.config_path, command=None,
                               detail="the sibyl_memory table runs another command")]
            return [_probe("present", self.config_path, command=value)]
        except Exception as e:
            return [_probe("unreadable", self.config_path, detail=type(e).__name__)]

    def repair_mcp(self, probe: dict) -> WireOutcome:
        """The same update path as setup --force, restricted to an existing
        table; never appends."""
        return self._wire_mcp(force=True, update_only=True)

    def _set_block_command(self, existing: str) -> str:
        """Set the command of our table to the resolved path. Only the one
        command line changes (its indent and trailing comment are kept); with no
        command line, one is inserted right after the header. Line endings are
        preserved. Raises ValueError when there is not exactly one header or the
        command value is in a form this does not edit."""
        sc = _codex_scan(existing)
        if len(sc["headers"]) != 1 or sc["other_mention"]:
            raise ValueError("sibyl_memory is defined in a form setup does not edit")
        lines = list(sc["lines"])
        esc = self._toml_escape(self._mcp_command())
        cmd = sc["command"]
        if cmd["line"] is not None:
            if not cmd["parsed"]:
                raise ValueError("the sibyl_memory command value is in a form setup does not edit")
            old = lines[cmd["line"]]
            eol = old[len(_strip_eol(old)):]
            lines[cmd["line"]] = f'{cmd["indent"]}{cmd["key"]} = "{esc}"{cmd["suffix"]}{eol}'
        else:
            h = sc["headers"][0]
            hdr = lines[h]
            eol = hdr[len(_strip_eol(hdr)):]
            if not eol:
                eol = "\r\n" if "\r\n" in existing else "\n"
                lines[h] = hdr + eol
            lines.insert(h + 1, f'command = "{esc}"{eol}')
        return "".join(lines)

    def _verify_command_edit(self, old: str, new: str) -> None:
        """Prove the command update changed only the command. On Python 3.11+
        both texts must load with tomllib and be equal except for that value;
        on 3.10 the text checks stand alone."""
        bad = f"could not change {self.config_path} safely; it was not modified."
        sc = _codex_scan(new)
        cmd = sc["command"]
        if len(sc["headers"]) != 1 or cmd["line"] is None or not cmd["parsed"]:
            raise ValueError(bad)
        if sc["command_lines"] != 1 or cmd["value"] != self._mcp_command():
            raise ValueError(bad)
        # Without tomllib (Python 3.10) the text checks stand alone, so they
        # are strict (CHECK-FABLE B-3): the old table held at most one command
        # line, the new one exactly one, and nothing else changed.
        osc = _codex_scan(old)
        if len(osc["headers"]) != 1 or osc["command_lines"] > 1 or osc["other_mention"]:
            raise ValueError(bad)
        if sc["other_mention"]:
            raise ValueError(bad)
        kept_new = [_strip_eol(x) for i, x in enumerate(sc["lines"]) if i != cmd["line"]]
        kept_old = [_strip_eol(x) for i, x in enumerate(osc["lines"])
                    if i != osc["command"]["line"]]
        if kept_new != kept_old:
            raise ValueError(bad)
        # Panel fix 2: the edited line sits inside our own table, before the
        # first depth-0 line starting with '[' after our header, whatever the
        # header parser made of that line.
        h = sc["headers"][0]
        if not (h < cmd["line"] < _codex_body_end(sc["lines"], h + 1)):
            raise ValueError(bad)
        if _tomllib is None:
            return
        try:
            old_doc = _toml_loads(old)
        except Exception:
            raise ValueError(f"{self.config_path} is not valid TOML; it was not modified.")
        try:
            new_doc = _toml_loads(new)
        except Exception:
            raise ValueError(bad)
        import copy
        want = copy.deepcopy(old_doc)
        try:
            want["mcp_servers"]["sibyl_memory"]["command"] = self._mcp_command()
        except (KeyError, TypeError):
            raise ValueError(bad)
        if new_doc != want:
            raise ValueError(bad)

    def _replace_table(self, existing: str) -> str:
        """Our whole table in place of a foreign one: the header line stays,
        its body and every [mcp_servers.sibyl_memory.*] subtable go, and one
        command line is written. Raises ValueError for a form this does not edit."""
        sc = _codex_scan(existing)
        if len(sc["headers"]) != 1 or sc["other_mention"] or sc["body"] is None:
            raise ValueError("sibyl_memory is defined in a form setup does not edit")
        lines = list(sc["lines"])
        h = sc["headers"][0]
        b, e = sc["body"]
        hdr = lines[h]
        eol = hdr[len(_strip_eol(hdr)):] or ("\r\n" if "\r\n" in existing else "\n")
        drop = set(range(b, e))
        for sh, se in sc["subtables"]:
            drop.update(range(sh, se))
        esc = self._toml_escape(self._mcp_command())
        out = []
        for i, ln in enumerate(lines):
            if i in drop:
                continue
            if i == h:
                out.append(_strip_eol(hdr) + eol)
                out.append(f'command = "{esc}"{eol}')
                # keep a blank line before what follows, as the body had
                if e < len(lines) and b < e and not _strip_eol(lines[e - 1]).strip():
                    out.append(eol)
                continue
            out.append(ln)
        return "".join(out)

    def _verify_table_replace(self, old: str, new: str) -> None:
        bad = f"could not change {self.config_path} safely; it was not modified."
        sc = _codex_scan(new)
        cmd = sc["command"]
        if (len(sc["headers"]) != 1 or sc["other_mention"] or sc["subtables"]
                or sc["command_lines"] != 1 or not cmd["parsed"]
                or cmd["value"] != self._mcp_command()):
            raise ValueError(bad)
        b, e = sc["body"]
        if any(_strip_eol(sc["lines"][k]).strip() and k != cmd["line"] for k in range(b, e)):
            raise ValueError(bad)
        # Panel fix 2: every old line outside our table and its subtables is
        # still there, unchanged and in order (checked before the parser
        # proof, so it also holds where no TOML parser is available).
        osc = _codex_scan(old)
        if len(osc["headers"]) != 1 or osc["body"] is None:
            raise ValueError(bad)
        oh = osc["headers"][0]
        ob, oe = osc["body"]
        odrop = set(range(oh, oe))
        for sh, se in osc["subtables"]:
            odrop.update(range(sh, se))
        kept_old = [x for i, x in enumerate(osc["lines"]) if i not in odrop]
        nh = sc["headers"][0]
        kept_new = [x for i, x in enumerate(sc["lines"]) if not (nh <= i < e)]
        if kept_old != kept_new or _strip_eol(sc["lines"][nh]) != _strip_eol(osc["lines"][oh]):
            raise ValueError(bad)
        if _tomllib is None:
            return
        try:
            old_doc = _toml_loads(old)
        except Exception:
            raise ValueError(f"{self.config_path} is not valid TOML; it was not modified.")
        try:
            new_doc = _toml_loads(new)
        except Exception:
            raise ValueError(bad)
        import copy
        want = copy.deepcopy(old_doc)
        try:
            want["mcp_servers"]["sibyl_memory"] = {"command": self._mcp_command()}
        except (KeyError, TypeError):
            raise ValueError(bad)
        if new_doc != want:
            raise ValueError(bad)

    def _backup_config(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.config_path, self.BACKUP_LABEL)

    @staticmethod
    def _toml_error(text: str) -> Optional[str]:
        """None when no TOML parser is available or ``text`` parses; else a
        brief (error type plus line and column, never file text)."""
        if _tomllib is None:
            return None
        try:
            _toml_loads(text)
        except Exception as e:
            m = re.search(r"\(at line (\d+), column (\d+)\)", str(e))
            where = f" at line {m.group(1)}, column {m.group(2)}" if m else ""
            return f"{type(e).__name__}{where}"
        return None

    def _append_plan(self, existing: str) -> str:
        """The file text with our table appended, proven before it is used
        (panel fix 3). With a TOML parser: the old text must parse and the new
        text must equal it plus mcp_servers.sibyl_memory = {command = path}.
        Without one, a file that sets mcp_servers as a key or ends inside an
        open string or array is refused. The file's line ending is kept.
        Raises ValueError with the reason."""
        bad = f"could not add the table to {self.config_path} safely; it was not modified."
        lines = existing.splitlines(keepends=True)
        if _codex_root_mcp_servers_key(lines):
            raise ValueError(
                f"{self.config_path} sets mcp_servers as a key (an inline table or dotted "
                "keys), so a [mcp_servers.sibyl_memory] table cannot be added; it was not "
                "modified.")
        if _toml_text_open_at_end(existing):
            raise ValueError(
                f"{self.config_path} ends inside an unterminated string or array; it was not "
                "modified.")
        nl = "\r\n" if "\r\n" in existing else "\n"
        cmd = self._mcp_command()
        block = self._block_text().replace("\n", nl)
        base = existing.rstrip("\r\n")
        new = base + (nl if existing.strip() else "") + block
        sc = _codex_scan(new)
        c = sc["command"]
        if (len(sc["headers"]) != 1 or sc["other_mention"] or sc["command_lines"] != 1
                or not c["parsed"] or c["value"] != cmd or not new.startswith(base)):
            raise ValueError(bad)
        if _tomllib is not None:
            try:
                old_doc = _toml_loads(existing)
            except Exception:
                raise ValueError(f"{self.config_path} is not valid TOML "
                                 f"({self._toml_error(existing)}); it was not modified.")
            try:
                new_doc = _toml_loads(new)
            except Exception:
                raise ValueError(bad)
            import copy
            want = copy.deepcopy(old_doc)
            ms = want.setdefault("mcp_servers", {})
            if not isinstance(ms, dict) or "sibyl_memory" in ms:
                raise ValueError(bad)
            ms["sibyl_memory"] = {"command": cmd}
            if new_doc != want:
                raise ValueError(bad)
        return new

    def _manual_table_error(self, reason: str) -> WireOutcome:
        """Error outcome with the exact text to add by hand (panel fix 3)."""
        cmd = self._toml_escape(self._mcp_command())
        try:
            inline = _codex_root_mcp_servers_key(self._read_raw().splitlines(keepends=True))
        except (OSError, UnicodeDecodeError):
            inline = False
        if inline:
            how = "Add this entry to the mcp_servers table in that file by hand:"
            block = f'sibyl_memory = {{ command = "{cmd}" }}'
        else:
            how = "Add this table to that file by hand:"
            block = f'[mcp_servers.sibyl_memory]\ncommand = "{cmd}"'
        return WireOutcome(self.name, "error", f"{reason} {how}\n{block}", handoff=(
            "done",
            f"add the printed sibyl_memory entry to {self.config_path} by hand (setup could not "
            "add it safely)", ()), manual_block=block)

    def _append_block(self) -> bool:
        """Add our table, or (cli 0.7.0), when it is already there, update its
        command in place. Returns True when the file was written. Before 0.7.0
        an existing header made this return early, so a stale path was never
        fixed even with --force while the outcome said "wired". Panel fix 3:
        the appended text is proven by _append_plan (ValueError otherwise)."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.config_path.exists():
            raw = self._read_raw()
            if _codex_scan(raw)["headers"]:
                existing = raw
                new_text = self._set_block_command(existing)
                if new_text == existing:
                    return False
                self._verify_command_edit(existing, new_text)
                tmp = self.config_path.with_suffix(".toml.tmp")
                _write_via_tmp(self.config_path, tmp, new_text, through_symlink=True)
                return True
            existing = raw
        new_text = self._append_plan(existing)
        tmp = self.config_path.with_suffix(".toml.tmp")
        _write_via_tmp(self.config_path, tmp, new_text, through_symlink=True)
        return True


# ----------------------------------------------------------------------
# CursorWirer: Cursor (and Grok Bot, which runs on a Cursor cloud machine)
# reads MCP servers from ~/.cursor/mcp.json. Format verified against Cursor's
# docs (https://cursor.com/docs/context/mcp): top-level "mcpServers", each
# stdio entry {"type": "stdio", "command": ..., "args": [...], "env": {...}}.
# Same protocol as the other wirers: timestamped backup, atomic write, a
# corrupt file is never replaced, foreign entries are never touched.
# ----------------------------------------------------------------------

MCP_BINARY_NAME = "sibyl-memory-mcp"


def _mcp_search_dirs() -> list:
    """Places the installer and pip put the MCP server binary that may not be
    on PATH yet: the running interpreter's scripts directory (a venv that is
    not activated) and the user bin directory (~/.local/bin, where
    sibyllabs.org/install and `pip install --user` put it on a fresh machine)."""
    return [Path(sys.executable).parent, Path.home() / ".local" / "bin"]


def resolve_mcp_binary() -> Optional[str]:
    """Absolute path of the installed MCP server binary, or None. PATH first,
    then the known install directories, so a fresh shell whose PATH does not
    yet include ~/.local/bin still finds it (cli 0.6.1)."""
    w = shutil.which(MCP_BINARY_NAME)
    if w:
        return os.path.abspath(w)
    for d in _mcp_search_dirs():
        cand = d / MCP_BINARY_NAME
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def mcp_command_path() -> str:
    """Absolute path of the MCP server binary for a client config (see
    resolve_mcp_binary). Falls back to the bare name."""
    return resolve_mcp_binary() or MCP_BINARY_NAME


def _command_resolves(cmd: Any, *, strict: bool = False) -> bool:
    """Whether a configured MCP command would start something (cli 0.7.0).
    An absolute path must be a file with the execute bit; a bare name must be
    on PATH, or (lenient mode, used by setup) be the server's own name while
    the server is installed where resolve_mcp_binary() finds it. Doctor uses
    strict=True: the harness spawns from its own PATH."""
    if not isinstance(cmd, str) or not cmd:
        return False
    if os.path.isabs(cmd):
        return os.path.isfile(cmd) and os.access(cmd, os.X_OK)
    if shutil.which(cmd):
        return True
    if not strict and cmd == MCP_BINARY_NAME:
        return resolve_mcp_binary() is not None
    return False


def mcp_servers_snippet(command: Optional[str] = None) -> str:
    """Copy-paste ``mcpServers`` JSON any MCP client accepts for a stdio
    server (A2: lets an agent wire itself from `sibyl setup` output)."""
    cmd = command or mcp_command_path()
    return json.dumps(
        {"mcpServers": {"sibyl-memory": {"type": "stdio", "command": cmd}}}, indent=2)


class CursorWirer:
    name = "cursor"
    display_name = "Cursor"
    initial = "u"

    MCP_BINARY = MCP_BINARY_NAME
    MCP_PACKAGE = "sibyl-memory-mcp"
    MCP_NAME = "sibyl-memory"

    def __init__(self, *, config_path: Optional[Union[str, Path]] = None):
        self.config_path = (
            Path(config_path).expanduser() if config_path
            else Path.home() / ".cursor" / "mcp.json"
        )

    def _entry(self) -> dict:
        return {"type": "stdio", "command": mcp_command_path()}

    def is_present(self) -> bool:
        if self.config_path.exists() or self.config_path.parent.is_dir():
            return True
        return bool(shutil.which("cursor") or shutil.which("cursor-agent"))

    def _mcp_binary_found(self) -> bool:
        # Cursor's config stores the absolute path, so the server does not
        # need to be on PATH; it only needs to be installed (cli 0.6.1).
        return resolve_mcp_binary() is not None

    _last_install_error: Optional[str] = None

    def _install_hint(self) -> str:
        return f"\n{self._last_install_error}" if self._last_install_error else ""

    def _ensure_mcp_binary(self) -> bool:
        if self._mcp_binary_found():
            return True
        self._last_install_error = _install_pkg_or_instruct(self.MCP_PACKAGE)
        return self._mcp_binary_found()

    def verify_mcp_starts(self) -> tuple:
        binary = resolve_mcp_binary()
        if not binary:
            return False, f"'{self.MCP_BINARY}' is not installed"
        return _verify_mcp_starts(binary)

    def current_state(self) -> dict:
        exists = self.config_path.exists()
        block: Optional[dict] = None
        parse_error: Optional[str] = None
        if exists:
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8") or "{}")
                servers = cfg.get("mcpServers") if isinstance(cfg, dict) else None
                if isinstance(servers, dict) and isinstance(servers.get(self.MCP_NAME), dict):
                    block = servers[self.MCP_NAME]
            except Exception as e:
                parse_error = f"{type(e).__name__}: {e}"
        cmd = (block or {}).get("command")
        is_ours = self._is_ours(block)
        return {
            "settings_path": str(self.config_path),
            "config_exists": exists,
            "config_parse_error": parse_error,
            "sibyl_mcp": block,
            "is_ours": is_ours,
            "mcp_binary_found": self._mcp_binary_found(),
            # cli 0.7.0: a stale absolute path is not "already wired".
            "wired_with_sibyl": bool(is_ours and _command_resolves(cmd)),
        }

    def _is_ours(self, block: Any) -> bool:
        cmd = block.get("command") if isinstance(block, dict) else None
        return bool(isinstance(cmd, str)
                    and Path(cmd.replace("\\", "/")).name.startswith(self.MCP_BINARY))

    # -- doctor (spec dsh-doctor v2, 4.4) -------------------------------------
    def _read_block_for_probe(self) -> dict:
        if not self.config_path.exists():
            return _probe("absent", self.config_path)
        raw = self.config_path.read_text(encoding="utf-8")
        try:
            cfg = json.loads(raw or "{}")
        except json.JSONDecodeError as e:
            return _probe("unreadable", self.config_path,
                          detail=f"JSONDecodeError at line {e.lineno}, column {e.colno}")
        if not isinstance(cfg, dict):
            return _probe("unreadable", self.config_path, detail="top level is not a JSON object")
        servers = cfg.get("mcpServers")
        block = servers.get(self.MCP_NAME) if isinstance(servers, dict) else None
        if block is None:
            return _probe("absent", self.config_path)
        if not isinstance(block, dict):
            return _probe("unreadable", self.config_path, detail="the sibyl-memory entry is not an object")
        cmd = block.get("command")
        if not self._is_ours(block):
            # Never the foreign command string (it can carry a credential).
            return _probe("foreign", self.config_path, command=None,
                          detail="the sibyl-memory entry runs another command")
        return _probe("present", self.config_path, command=cmd)

    def doctor_probe(self) -> list:
        try:
            return [self._read_block_for_probe()]
        except Exception as e:
            return [_probe("unreadable", self.config_path, detail=type(e).__name__)]

    def repair_mcp(self, probe: dict) -> WireOutcome:
        """The merge path only: our own entry gets the resolved command, every
        other key is kept. A foreign or missing entry is never touched."""
        p = self._read_block_for_probe()
        if p["entry"] != "present":
            return WireOutcome(self.name, "error",
                               f"no sibyl-memory entry of ours in {self.config_path} to repair")
        try:
            backup = _timestamped_backup(self.config_path, "cursor")
        except (OSError, ValueError) as e:
            return _backup_failed(self.name, self.config_path, "cursor", e)
        try:
            self._write_config()
        except Exception as e:
            return WireOutcome(self.name, "error",
                f"config write failed: {type(e).__name__}", backup_path=backup)
        return WireOutcome(self.name, "wired",
            f"Updated the sibyl-memory command in {self.config_path}", backup_path=backup)

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()
        if state["config_parse_error"]:
            return WireOutcome(self.name, "error",
                f"{self.config_path} is not valid JSON ({state['config_parse_error']}). "
                "Fix the file or move it aside and re-run setup; it was not modified.")
        if state["wired_with_sibyl"] and not force:
            return WireOutcome(self.name, "already",
                f"{self.display_name} already has the sibyl-memory MCP server in {self.config_path}")
        block = state["sibyl_mcp"]
        # cli 0.7.0: the "differs" gate is for an entry that is not ours. Our
        # own entry with a stale path is updated without a prompt.
        if block and not state["is_ours"] and not force:
            if prompt_fn is None:
                return WireOutcome(self.name, "skipped",
                    "Existing sibyl-memory MCP entry differs. Use --force to overwrite.",
                    overwrite_of="a different sibyl-memory MCP entry")
            if prompt_fn("Cursor has a 'sibyl-memory' MCP entry pointing elsewhere. Update?",
                         default="N") != "y":
                return WireOutcome(self.name, "skipped", "MCP entry update declined.",
                                   overwrite_of="a different sibyl-memory MCP entry")
        if dry_run:
            verb = "update" if block else "add"
            return WireOutcome(self.name, "dry-run",
                f"Would {verb} mcpServers.sibyl-memory in {self.config_path}")
        if not self._ensure_mcp_binary():
            looked = ", ".join(str(d) for d in _mcp_search_dirs())
            return WireOutcome(self.name, "error",
                f"'{self.MCP_BINARY}' is not installed (looked on PATH and in {looked}). "
                f"Install it: pip install {self.MCP_PACKAGE}"
                + self._install_hint())
        try:
            backup = _timestamped_backup(self.config_path, "cursor")
        except (OSError, ValueError) as e:
            return _backup_failed(self.name, self.config_path, "cursor", e)
        try:
            self._write_config()
        except Exception as e:
            return WireOutcome(self.name, "error",
                f"config write failed: {type(e).__name__}: {e}", backup_path=backup)
        if block and state["is_ours"]:
            return WireOutcome(self.name, "wired",
                f"Updated the sibyl-memory command in {self.config_path}", backup_path=backup)
        return WireOutcome(self.name, "wired",
            f"Added SIBYL Memory MCP server to {self.config_path}", backup_path=backup)

    def _write_config(self) -> None:
        cfg: dict = {}
        if self.config_path.exists():
            raw = self.config_path.read_text(encoding="utf-8")
            if raw.strip():
                try:
                    loaded = json.loads(raw)
                except ValueError as e:
                    raise ValueError(
                        f"{self.config_path} is not valid JSON ({e}). Fix the file or move "
                        "it aside and re-run setup; it was not modified.") from e
                if not isinstance(loaded, dict):
                    raise ValueError(
                        f"{self.config_path} top level is not a JSON object "
                        f"(got {type(loaded).__name__}); it was not modified.")
                cfg = loaded
        if not isinstance(cfg.get("mcpServers"), dict):
            cfg["mcpServers"] = {}
        cur = cfg["mcpServers"].get(self.MCP_NAME)
        if self._is_ours(cur):
            # cli 0.7.0: merge into our own entry; env, args and every other key
            # the person set are kept, only the command (and a missing type) change.
            merged = dict(cur)
            merged["command"] = mcp_command_path()
            merged.setdefault("type", "stdio")
            cfg["mcpServers"][self.MCP_NAME] = merged
        else:
            cfg["mcpServers"][self.MCP_NAME] = self._entry()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        _write_via_tmp(self.config_path, tmp, json.dumps(cfg, indent=2) + "\n",
                       through_symlink=True)


class GrokBotWirer(CursorWirer):
    """`sibyl setup grokbot`: Grok Bot runs on a Cursor cloud machine with local
    stdio MCP, so it is the Cursor wiring plus a printed confirmation. Explicit
    target only (never auto-detected, so Cursor is not wired twice)."""
    name = "grokbot"
    display_name = "Grok Bot (Cursor)"
    initial = "g"


# ----------------------------------------------------------------------
# DeepSeekWirer: DeepSeek Harness (developer preview) loads MCP servers from
# loader patch files: <home>/cordis.patch.yml (covers every profile) and
# <home>/profiles/<name>/cordis.patch.yml. One `@deepseek-ai/dsh-mcp-client`
# insert row is added as TEXT (never re-serialized), verified by parsing the
# result before any write. Detection is by DeepSeek Harness's own files only;
# no binary named dsh is ever looked up or run (the Debian dsh package, a
# distributed shell, also uses ~/.dsh). Spec dsh-doctor v2, section 3.
# ----------------------------------------------------------------------

class DeepSeekWirer:
    name = "deepseek"
    display_name = "DeepSeek Harness"
    initial = "d"

    MCP_BINARY = MCP_BINARY_NAME
    MCP_PACKAGE = "sibyl-memory-mcp"
    PLUGIN = "@deepseek-ai/dsh-mcp-client"
    ENTRY_ID = "sibyl-memory"
    SERVER_NAME = "sibyl-memory"
    PATCH_FILENAME = "cordis.patch.yml"
    PASS_ENV = ("SIBYL_MEMORY_MCP_OUTPUT_TOKENS", "SIBYL_MEMORY_RANKED_MIN_TOKENS")
    FOREIGN_HOME_MARKERS = ("dsh.conf", "machines.list", "group")
    BLOCK_COMMENT = ('# Sibyl Memory MCP server (added by "sibyl setup deepseek"; '
                     're-run it to update the path)')
    FILE_HEADER = ("# DeepSeek Harness user patch layer: a top-level YAML list of loader patch entries.",
                   "# Add your own entries above or below the Sibyl Memory block.")
    _PASS_ENV_VALUE = re.compile(r"^[0-9]{1,7}$")

    def __init__(self, *, dsh_home: Optional[Union[str, Path]] = None,
                 profile: Optional[str] = None):
        self.dsh_home = self._auto_dsh_home(dsh_home)
        if profile is not None:
            self._check_profile(profile)
        self.profile = profile
        self.home_patch = self.dsh_home / self.PATCH_FILENAME
        if profile:
            self.target_path = self.dsh_home / "profiles" / profile / self.PATCH_FILENAME
        else:
            self.target_path = self.home_patch
        self.config_path = self.target_path

    # -- paths and detection -------------------------------------------------
    @staticmethod
    def _auto_dsh_home(explicit: Optional[Union[str, Path]] = None) -> Path:
        """Mirror DeepSeek Harness: $DSH_HOME when set and not blank, else
        ~/.dsh; a leading ~ is expanded; the result is absolute."""
        if explicit:
            raw = str(explicit)
        else:
            env = os.environ.get("DSH_HOME")
            raw = env if env is not None and env.strip() else str(Path.home() / ".dsh")
        return Path(os.path.abspath(os.path.expanduser(raw)))

    @staticmethod
    def _check_profile(name: str) -> None:
        if (not name or "/" in name or "\\" in name
                or name in (".", "..", "node_modules")):
            raise ValueError(
                f"'{name}' is not a valid DeepSeek Harness profile name (it must be a folder "
                "name under the profiles directory: not empty, no slashes, not '.', '..' or "
                "'node_modules').")

    def is_present(self) -> bool:
        # DeepSeek Harness's own files only: never ~/.dsh existing, never PATH.
        try:
            return (self.dsh_home / "profiles").is_dir() or self.home_patch.is_file()
        except OSError:
            return False

    def _home_shape(self) -> str:
        h = self.dsh_home
        if not h.exists() and not h.is_symlink():
            return "missing"
        if not h.is_dir():
            return "not-a-dir"
        if self.is_present():
            return "dsh"
        try:
            empty = not any(h.iterdir())
        except OSError:
            return "foreign"
        return "empty" if empty else "foreign"

    def _profile_names(self) -> list:
        d = self.dsh_home / "profiles"
        try:
            names = [c.name for c in d.iterdir() if c.is_dir()]
        except OSError:
            return []
        return sorted(n for n in names if not n.startswith(".") and n != "node_modules")

    def _layer_path(self, layer: str) -> Path:
        if layer == "home":
            return self.home_patch
        return self.dsh_home / "profiles" / layer.split(":", 1)[1] / self.PATCH_FILENAME

    # -- the row -------------------------------------------------------------
    def _pass_env(self) -> dict:
        """The two variables DeepSeek Harness's environment scrub drops
        (their names contain TOKEN), copied into the row's env only when set
        to a plain number at setup time. Nothing else is ever copied."""
        env = {}
        for n in self.PASS_ENV:
            v = os.environ.get(n)
            if v is not None and self._PASS_ENV_VALUE.match(v):
                env[n] = v
        return env

    def _row_lines(self, command: str, env: Optional[dict]) -> list:
        """The nine row lines, `id:` through `cwd:`, indented relative to the
        row's first key."""
        env_text = json.dumps(env, ensure_ascii=False) if env else "{}"
        return [
            f"id: {self.ENTRY_ID}",
            f"name: '{self.PLUGIN}'",
            "config:",
            f"  serverName: {self.SERVER_NAME}",
            "  transport: stdio",
            f"  command: {json.dumps(command, ensure_ascii=False)}",
            "  args: []",
            f"  env: {env_text}",
            "  cwd: !!js process.cwd()",
        ]

    def _entry_text(self, command: str, nl: str = "\n", env: Optional[dict] = None) -> str:
        rows = self._row_lines(command, env)
        lines = [self.BLOCK_COMMENT, "- insert:", "    - " + rows[0]]
        lines += ["      " + r for r in rows[1:]]
        return nl.join(lines) + nl

    def _expected_row(self, command: str, env: Optional[dict] = None) -> dict:
        return {
            "id": self.ENTRY_ID,
            "name": self.PLUGIN,
            "config": {
                "serverName": self.SERVER_NAME,
                "transport": "stdio",
                "command": command,
                "args": [],
                "env": dict(env or {}),
                "cwd": _JsExpr("process.cwd()"),
            },
        }

    # -- scanning ------------------------------------------------------------
    JS_NOTE = "the command is a !!js expression; not checked"

    def _is_candidate(self, row: dict) -> tuple:
        """(candidate, ours) for one row mapping (3.4)."""
        cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
        c1 = row.get("id") == self.ENTRY_ID
        c2 = row.get("name") == self.PLUGIN and cfg.get("serverName") == self.SERVER_NAME
        cmd = cfg.get("command")
        c3 = bool(row.get("name") == self.PLUGIN and cmd is not None
                  and Path(str(cmd).replace("\\", "/")).name.startswith(self.MCP_BINARY))
        return (c1 or c2 or c3), c3

    @staticmethod
    def _map_child(node, key: str):
        """The value node for ``key`` in a MappingNode (the last one wins, as
        when loading), or None."""
        found = None
        for k, v in getattr(node, "value", None) or []:
            if getattr(k, "value", None) == key and type(k).__name__ == "ScalarNode":
                found = v
        return found

    def _walk_rows(self, rows: Any, nodes: Any, path: list, out: list) -> None:
        if not isinstance(rows, list):
            return
        node_items = list(getattr(nodes, "value", None) or []) \
            if type(nodes).__name__ == "SequenceNode" else []
        for j, row in enumerate(rows):
            node = node_items[j] if j < len(node_items) else None
            if not isinstance(row, dict):
                continue
            here = path + [j]
            if row.get("group") and isinstance(row.get("config"), list):
                self._walk_rows(row["config"], self._map_child(node, "config"),
                                here + ["config"], out)
                continue
            cand, ours = self._is_candidate(row)
            if not cand:
                continue
            cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
            cfg_node = self._map_child(node, "config")
            cmd_node = self._map_child(cfg_node, "command") if cfg_node is not None else None
            if cmd_node is not None and type(cmd_node).__name__ != "ScalarNode":
                cmd_node = None
            out.append({
                "id": row.get("id"),
                "serverName": cfg.get("serverName"),
                "command": cfg.get("command") if isinstance(cfg.get("command"), str) else None,
                # A `!!js` expression is evaluated by the harness at load time:
                # it is not a path, so it is never judged stale and never
                # rewritten without --force (CHECK-FABLE B-2, spec 3.2).
                "command_js": type(cfg.get("command")) is _JsExpr,
                "ours": ours,
                "disabled": row.get("disabled") is True,
                "path": here,
                "row_node": node,
                "command_node": cmd_node,
            })

    def _scan_text(self, text: str) -> dict:
        """Parse one patch file's text. Raises on a YAML error."""
        root = _dsh_yaml_compose(text)
        data = _dsh_yaml_load(text)
        res = {"top": None, "error": None, "data": data, "root": root,
               "candidates": [], "overrides": [], "manager": []}
        if data is None:
            res["top"] = "empty"
            return res
        if not isinstance(data, list):
            res["top"] = "other"
            res["error"] = "top level is not a YAML list"
            return res
        res["top"] = "list"
        items = list(root.value) if type(root).__name__ == "SequenceNode" else []
        for i, patch in enumerate(data):
            if not isinstance(patch, dict):
                continue
            node = items[i] if i < len(items) else None
            if "insert" in patch:
                self._walk_rows(patch.get("insert"), self._map_child(node, "insert"),
                                [i, "insert"], res["candidates"])
            elif "id" in patch:
                # Position kept: an override only reaches a row inserted
                # earlier in the composed list (sandbox M1).
                res["overrides"].append((patch.get("id"), i))
                if patch.get("id") == self.MANAGER_ID:
                    servers = self._manager_servers(patch)
                    if servers is not None:
                        res["manager"].append((i, servers))
        return res

    def _scan_file(self, path: Path, layer: str) -> dict:
        scan = {"layer": layer, "path": str(path), "exists": False, "error": None,
                "top": None, "text": None, "data": None, "root": None,
                "candidates": [], "overrides": [], "manager": [], "mentions": False}
        try:
            if not path.exists():
                return scan
        except OSError as e:
            scan["exists"] = True
            scan["error"] = type(e).__name__
            return scan
        scan["exists"] = True
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            scan["error"] = type(e).__name__
            return scan
        scan["text"] = text
        scan["mentions"] = "sibyl-memory" in text
        try:
            scan.update(self._scan_text(text))
        except Exception as e:
            scan["error"] = _yaml_error_brief(e)
        return scan

    def _scan_layers(self) -> list:
        layers = [self._scan_file(self.home_patch, "home")]
        for n in self._profile_names():
            layers.append(self._scan_file(self._layer_path("profile:" + n), "profile:" + n))
        if self.profile and not any(l["layer"] == "profile:" + self.profile for l in layers):
            layers.append(self._scan_file(self.target_path, "profile:" + self.profile))
        return layers

    def _target_layer(self) -> str:
        return "profile:" + self.profile if self.profile else "home"

    def _relevant(self, layers: list) -> list:
        if not self.profile:
            return layers
        keep = ("home", "profile:" + self.profile)
        return [l for l in layers if l["layer"] in keep]

    @staticmethod
    def _duplicate_in(layers: list) -> bool:
        """Judged per effective stack (the home patch plus one profile): two
        rows in one file, or one in home and one in a profile. Rows in two
        different profiles with nothing in home are not duplicates."""
        if any(len(l["candidates"]) > 1 for l in layers):
            return True
        home = [l for l in layers if l["layer"] == "home"]
        has_home = bool(home and home[0]["candidates"])
        return has_home and any(l["candidates"] for l in layers if l["layer"] != "home")

    @staticmethod
    def _stacks_for(layers: list, layer: str) -> list:
        """The effective stacks a file in ``layer`` takes part in, each in
        DeepSeek Harness's composition order (F6: the profile patch, then the
        home patch). A profile file is in one stack; the home patch is in one
        per profile (or alone when there is none)."""
        home = [l for l in layers if l["layer"] == "home"]
        profiles = [l for l in layers if l["layer"] != "home"]
        if layer != "home":
            return [[l for l in profiles if l["layer"] == layer] + home]
        return [[p] + home for p in profiles] or [home]

    @classmethod
    def _overridden(cls, cand: dict, layers: list, layer: str) -> bool:
        """A top-level `{id: <row id>, ...}` patch without `insert` that comes
        AFTER the row in the composed list: later in the same file, or in a
        later layer of the same stack (the home patch follows a profile patch).
        An override placed before the row is applied before the row exists
        and does not reach it (sandbox M1, observed: the server still starts)."""
        rid = cand.get("id")
        if rid is None or not cand.get("path"):
            return False
        top = cand["path"][0]
        for stack in cls._stacks_for(layers, layer):
            rank = next((k for k, l in enumerate(stack) if l["layer"] == layer), None)
            if rank is None:
                continue
            for k, l in enumerate(stack):
                for oid, idx in l["overrides"]:
                    if oid == rid and (k > rank or (k == rank and idx > top)):
                        return True
        return False

    MANAGER_ID = "mcp-manager"
    MANAGER_CLASH_TEXT = ("DeepSeek Harness's own MCP settings also define a server named "
                          "sibyl-memory ({path}, mcp-manager servers); two servers with one name "
                          "make DeepSeek Harness fail one of them (serverName already in use).")

    @classmethod
    def _manager_servers(cls, patch: dict) -> Optional[dict]:
        """For a top-level `{id: mcp-manager, config: {servers: {...}}}` patch
        (what DeepSeek Harness's own MCP settings write), the servers map as
        {name: enabled}; None when the patch does not set `servers`."""
        cfg = patch.get("config")
        if not isinstance(cfg, dict) or "servers" not in cfg:
            return None
        servers = cfg.get("servers")
        if not isinstance(servers, dict):
            return {}
        out = {}
        for name, entry in servers.items():
            enabled = not (isinstance(entry, dict) and entry.get("enabled") is False)
            out[str(name)] = enabled
        return out

    @classmethod
    def _manager_clash(cls, layers: list, layer: str) -> Optional[str]:
        """The path of the file whose DeepSeek Harness MCP settings
        (`mcp-manager` `servers`) also define an enabled server named
        sibyl-memory in a stack that ``layer`` belongs to, else None. The
        servers map is replaced whole when set from a patch, so the last
        `servers` in composition order is the one that counts. Two servers
        with one serverName: the harness fails one of them ("serverName ...
        is already in use")."""
        for stack in cls._stacks_for(layers, layer):
            last = None
            for l in stack:
                for _idx, servers in l.get("manager") or []:
                    last = (l["path"], servers)
            if last is not None and last[1].get(cls.SERVER_NAME):
                return last[0]
        return None

    @staticmethod
    def _plain(cand: dict, layer: str) -> dict:
        out = {k: cand.get(k) for k in ("id", "serverName", "command", "command_js", "ours",
                                        "disabled")}
        out["layer"] = layer
        return out

    def current_state(self) -> dict:
        state = {
            "settings_path": str(self.target_path),
            "dsh_home": str(self.dsh_home),
            "profile": self.profile,
            "config_exists": False,
            "config_parse_error": None,
            "layers": [],
            "sibyl_mcp": None,
            "entry_layer": None,
            "duplicate": False,
            "command": None,
            "mcp_binary_found": False,
            "wired_with_sibyl": False,
        }
        try:
            state["config_exists"] = self.target_path.exists()
            state["mcp_binary_found"] = self._mcp_binary_found()
            if _import_yaml() is None:
                return state
            layers = self._scan_layers()
            rel = self._relevant(layers)
            tgt = self._target_layer()
            for l in layers:
                state["layers"].append({
                    "layer": l["layer"], "path": l["path"], "exists": l["exists"],
                    "error": l["error"], "top": l["top"], "mentions": l["mentions"],
                    "candidates": [self._plain(c, l["layer"]) for c in l["candidates"]]})
                if l["layer"] == tgt:
                    state["config_parse_error"] = l["error"]
            cands = [(l["layer"], c) for l in rel for c in l["candidates"]]
            ok_rel = [l for l in rel if not l["error"]]
            state["duplicate"] = self._duplicate_in(rel) or any(
                self._manager_clash(ok_rel, l["layer"]) for l in ok_rel if l["candidates"])
            if cands:
                layer, c = cands[0]
                state["sibyl_mcp"] = self._plain(c, layer)
                state["entry_layer"] = layer
                state["command"] = c["command"]
            state["wired_with_sibyl"] = bool(
                cands and not state["duplicate"]
                and all(c["ours"] and not c["disabled"]
                        and (c.get("command_js") or _command_resolves(c["command"]))
                        for _l, c in cands))
        except Exception:
            pass
        return state

    # -- planning and verification (3.7) -------------------------------------
    @staticmethod
    def _set_at(obj: Any, path: list, value: Any) -> None:
        for k in path[:-1]:
            obj = obj[k]
        obj[path[-1]] = value

    @staticmethod
    def _get_at(obj: Any, path: list) -> Any:
        for k in path:
            obj = obj[k]
        return obj

    @staticmethod
    def _last_leaf_end(node) -> int:
        while True:
            kind = type(node).__name__
            if kind == "MappingNode" and not node.flow_style and node.value:
                node = node.value[-1][1]
            elif kind == "SequenceNode" and not node.flow_style and node.value:
                node = node.value[-1]
            else:
                return node.end_mark.index

    def _plan_text(self, scan: dict, change: tuple) -> Optional[str]:
        """New text for one file, or None when the change cannot be proven
        correct. Every case is verified by parsing the result."""
        try:
            return self._plan_text_inner(scan, change)
        except Exception:
            return None

    def _plan_text_inner(self, scan: dict, change: tuple) -> Optional[str]:
        import copy
        kind = change[0]
        text = scan.get("text") if scan.get("exists") else None
        nl = "\r\n" if text and "\r\n" in text else "\n"
        old = scan.get("data")
        expected = None
        if kind == "add":
            _k, command, env = change
            expected = self._expected_row(command, env)
            block = self._entry_text(command, nl, env)
            if not scan.get("exists"):                                   # A
                case = "A"
                new = nl.join(self.FILE_HEADER) + nl + block
            elif scan.get("error"):
                return None
            elif scan.get("top") == "empty":                              # B
                case = "B"
                sep = "" if (text == "" or text.endswith("\n")) else nl
                new = text + sep + block
            else:
                root = scan.get("root")
                if type(root).__name__ != "SequenceNode":
                    return None
                if root.flow_style and not root.value:                    # C
                    case = "C"
                    a, b = root.start_mark.index, root.end_mark.index
                    if text[a:b].strip() != "[]" and not text[a:b].startswith("["):
                        return None
                    new = text[:a] + block[:-len(nl)] + text[b:]
                elif not root.flow_style and root.value and root.start_mark.column == 0:  # D
                    case = "D"
                    sep = "" if text.endswith("\n") else nl
                    new = text + sep + nl + block
                else:
                    return None
        elif kind == "command":                                           # E
            case = "E"
            _k, cand, command = change[:3]
            allow_js = len(change) > 3 and change[3] is True
            if cand.get("command_js") and not allow_js:
                return None      # a !!js command changes only on --force (B-2)
            node = cand.get("command_node")
            if node is None or text is None:
                return None
            a, b = node.start_mark.index, node.end_mark.index
            new = text[:a] + json.dumps(command, ensure_ascii=False) + text[b:]
        elif kind == "replace":                                           # F
            case = "F"
            _k, cand, command, env = change
            expected = self._expected_row(command, env)
            node = cand.get("row_node")
            if text is None or type(node).__name__ != "MappingNode" or node.flow_style:
                return None
            a = node.start_mark.index
            b = self._last_leaf_end(node)
            col = node.start_mark.column
            rows = self._row_lines(command, env)
            new_row = rows[0] + "".join(nl + " " * col + r for r in rows[1:])
            new = text[:a] + new_row + text[b:]
        else:
            return None

        # -- verification: the new text must parse to exactly the intent.
        got = _dsh_yaml_load(new)
        if not isinstance(got, list):
            return None
        if case in ("A", "B", "C"):
            if got != [{"insert": [expected]}]:
                return None
            new_row_obj = got[0]["insert"][0]
        elif case == "D":
            if not isinstance(old, list) or got != old + [{"insert": [expected]}]:
                return None
            new_row_obj = got[-1]["insert"][0]
        elif case == "E":
            want = copy.deepcopy(old)
            self._set_at(want, cand["path"] + ["config", "command"], command)
            if got != want:
                return None
            new_row_obj = None
        else:
            want = copy.deepcopy(old)
            self._set_at(want, cand["path"], expected)
            if got != want:
                return None
            new_row_obj = self._get_at(got, cand["path"])
        if new_row_obj is not None and type(new_row_obj["config"]["cwd"]) is not _JsExpr:
            return None
        rescan = self._scan_text(new)
        before = len(scan.get("candidates") or [])
        after = rescan["candidates"]
        if len(after) != before + (1 if kind == "add" else 0):
            return None
        if kind == "add":
            mine = [c for c in after if c["path"] not in [x["path"] for x in scan.get("candidates") or []]]
        else:
            mine = [c for c in after if c["path"] == cand["path"]]
        if len(mine) != 1 or not mine[0]["ours"] or mine[0]["command"] != command:
            return None
        return new

    # -- binary helpers (same behavior as CursorWirer) -------------------------
    def _mcp_binary_found(self) -> bool:
        return resolve_mcp_binary() is not None

    _last_install_error: Optional[str] = None

    def _install_hint(self) -> str:
        return f"\n{self._last_install_error}" if self._last_install_error else ""

    def _ensure_mcp_binary(self) -> bool:
        if self._mcp_binary_found():
            return True
        self._last_install_error = _install_pkg_or_instruct(self.MCP_PACKAGE)
        return self._mcp_binary_found()

    def verify_mcp_starts(self) -> tuple:
        binary = resolve_mcp_binary()
        if not binary:
            return False, f"'{self.MCP_BINARY}' is not installed"
        return _verify_mcp_starts(binary)

    def print_config_text(self) -> str:
        cmd = mcp_command_path()
        lines = [
            "DeepSeek Harness (developer preview): add this block to a patch file. The home patch",
            "covers every profile; a profile patch covers one.",
            f"  home patch:    {self.home_patch}",
            f"  profile patch: {self.dsh_home / 'profiles' / '<name>' / self.PATCH_FILENAME}",
            "The file is a top-level YAML list; append the block (or replace a lone [] with it).",
            "",
            self._entry_text(cmd).rstrip("\n"),
            "",
            "DeepSeek Harness does not pass variables whose names contain TOKEN to the server, so",
            f"{' and '.join(self.PASS_ENV)} reach it only through the row's env.",
            "sibyl setup deepseek copies them there when they are set to a number at setup time;",
            "after that it never changes the row's env.",
        ]
        return "\n".join(lines)

    # -- wire (3.6) ----------------------------------------------------------
    def refusal(self) -> Optional[WireOutcome]:
        """The outcome when setup will refuse this home before reading any
        patch file, else None. cmd_setup asks first, so a home it is about to
        refuse is never shown as detected or offered with a Y/n (sandbox M3)."""
        # The home shape gate (explicit setup is the only path that writes)
        shape = self._home_shape()
        if shape in ("foreign", "not-a-dir"):
            return self._gate_error(shape)
        # A named profile must exist
        if self.profile and not (self.dsh_home / "profiles" / self.profile).is_dir():
            return WireOutcome(self.name, "error",
                f"profile '{self.profile}' does not exist under {self.dsh_home / 'profiles'}. "
                "Start DeepSeek Harness with that profile once, then re-run.")
        return None

    def _gate_error(self, shape: str) -> WireOutcome:
        h = self.dsh_home
        markers = False
        if shape == "foreign":
            markers = any((h / m).exists() for m in self.FOREIGN_HOME_MARKERS)
        msg = (f"{h} does not look like a DeepSeek Harness home (no profiles folder, no "
               f"{self.PATCH_FILENAME}).")
        if markers:
            msg += " It holds files of the unrelated 'dsh' distributed shell package."
        msg += (" Nothing was written. If DeepSeek Harness keeps its files elsewhere, run: "
                "sibyl setup deepseek --dsh-home <folder>. Otherwise start DeepSeek Harness once "
                "so it creates its profiles folder, then run sibyl setup deepseek again.")
        return WireOutcome(self.name, "error", msg, handoff=(
            "sibyl setup deepseek --dsh-home <folder>",
            f"the DeepSeek Harness home <folder> (the one that holds profiles/); {h} belongs to "
            "something else", ()))

    def _manual_block_error(self, path: Path, lead: str, mode: str = "add",
                            env: Optional[dict] = None) -> WireOutcome:
        """Error outcome with what to change by hand (panel fix 8). ``mode``:
        "add" (no row of ours yet: the whole insert block), "command" (our row
        exists: only its command line) or "replace" (a row with our id exists:
        the row to put in its place). Every printed block is valid YAML."""
        cmd = mcp_command_path()
        if mode == "command":
            block = f"command: {json.dumps(cmd, ensure_ascii=False)}"
            how = f"Change the command of the existing sibyl-memory row in {path} to:"
            human = (f"change the command of the existing sibyl-memory row in {path} to the "
                     "printed line (do not add a second row)")
        elif mode == "replace":
            rows = self._row_lines(cmd, env)
            block = "\n".join(["- " + rows[0]] + ["  " + r for r in rows[1:]])
            how = f"Replace the existing sibyl-memory row in {path} with:"
            human = (f"replace the existing sibyl-memory row in {path} with the printed row "
                     "(do not add a second row)")
        else:
            block = self._entry_text(cmd, env=env).rstrip()
            how = f"Add this block to {path}:"
            human = (f"add the printed block to {path}, or move that file aside and run "
                     "sibyl setup deepseek")
        return WireOutcome(self.name, "error", f"{lead} {how}\n{block}",
                           handoff=("done", human, ()), manual_block=block)

    def _backup_sub(self, layer: str) -> str:
        return "" if layer == "home" else "profile-" + layer.split(":", 1)[1]

    def _apply(self, plans: list, notes: list, verb: str) -> WireOutcome:
        """Backup then atomic write for each planned file (steps 14 and 15).
        Each plan is (layer, path, new text, the text the plan was built from).
        A read-only file is never replaced (CHECK-FABLE N-5), and the file is
        read again right before the write: if it changed since the plan was
        built (DeepSeek Harness rewrote it, or a pip install ran in between),
        nothing is written (N-4)."""
        done: list = []
        backups: list = []

        def _fail(msg: str) -> WireOutcome:
            if done:
                msg += "; already written: " + ", ".join(str(d) for d in done)
            return WireOutcome(self.name, "error", msg,
                               backup_path=backups[0] if backups else None)

        for layer, path, new, base in plans:
            if base is not None and path.exists() and not os.access(str(path), os.W_OK):
                return _fail(f"{path} is read-only; setup does not replace a read-only file. "
                             "It was not modified")
            try:
                b = _timestamped_backup(path, "deepseek", self._backup_sub(layer))
            except (OSError, ValueError) as e:
                msg = (f"could not back up {path} to {backups_root() / 'deepseek'} "
                       f"({type(e).__name__}); {path} was not modified")
                if done:
                    msg += "; already written: " + ", ".join(str(d) for d in done)
                return WireOutcome(self.name, "error", msg,
                                   backup_path=backups[0] if backups else None)
            if b:
                backups.append(b)
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    now = fh.read()
            except FileNotFoundError:
                now = None
            except (OSError, UnicodeDecodeError) as e:
                return _fail(f"could not read {path} again before writing ({type(e).__name__}); "
                             "it was not modified")
            if now != base:
                return _fail(f"{path} changed while setup was running; it was not modified. "
                             "Run it again")
            try:
                _atomic_text(path, new)
            except Exception as e:
                msg = f"could not write {path} ({type(e).__name__}); it was not modified"
                if done:
                    msg += "; already written: " + ", ".join(str(d) for d in done)
                return WireOutcome(self.name, "error", msg,
                                   backup_path=backups[0] if backups else None)
            done.append(path)
        where = ", ".join(str(d) for d in done)
        if verb == "Updated":
            msg = f"Updated the sibyl-memory command in {where}"
        else:
            msg = f"{verb} the SIBYL Memory MCP server in {where}"
        if len(backups) > 1:
            msg += "; more backups at " + ", ".join(str(b) for b in backups[1:])
        msg += "".join(notes)
        return WireOutcome(self.name, "wired", msg,
                           backup_path=backups[0] if backups else None)

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        # 1. PyYAML
        if _import_yaml() is None:
            return WireOutcome(self.name, "error",
                "PyYAML not installed. Run `pip install pyyaml` and retry.",
                handoff=("pip install pyyaml", None, ("sibyl setup deepseek",)))
        # 2. and 3. The home shape gate and the named profile
        refused = self.refusal()
        if refused is not None:
            return refused
        layers = self._scan_layers()
        rel = self._relevant(layers)
        tgt_name = self._target_layer()
        tgt = next(l for l in rel if l["layer"] == tgt_name)
        # 4. The target layer must parse
        if tgt["error"]:
            return WireOutcome(self.name, "error",
                f"{tgt['path']} is not a valid DeepSeek Harness patch file ({tgt['error']}). "
                "Fix the file or move it aside and re-run setup; it was not modified.")
        # 5. Another layer of the stack that does not parse
        notes: list = []
        for l in rel:
            if l is tgt or not l["error"]:
                continue
            if l["mentions"]:
                return WireOutcome(self.name, "error",
                    f"{l['path']} does not parse ({l['error']}) and mentions sibyl-memory. "
                    "Fix it first; nothing was changed.")
            notes.append(f" (note: {l['path']} does not parse and was skipped)")
        ok_layers = [l for l in rel if not l["error"]]
        # 6. Duplicates: which one to keep is not ours to guess (no --force)
        if self._duplicate_in(ok_layers):
            paths = [l["path"] for l in ok_layers if l["candidates"]]
            n = sum(len(l["candidates"]) for l in ok_layers)
            return WireOutcome(self.name, "skipped",
                f"{n} sibyl-memory entries found in one DeepSeek Harness stack "
                f"({', '.join(paths)}). Remove the extras, then re-run. Nothing was changed.")
        # 6b. A server named sibyl-memory in DeepSeek Harness's own MCP
        # settings (mcp-manager servers): a row of ours beside it would make
        # the harness fail one of the two. Setup never writes that map.
        clash = next((p for p in (self._manager_clash(ok_layers, l["layer"])
                                  for l in ok_layers if l is tgt or l["candidates"]) if p), None)
        if clash:
            return WireOutcome(self.name, "skipped", self.MANAGER_CLASH_TEXT.format(path=clash)
                               + " Remove one of them, then re-run. Nothing was changed.")
        changes: list = []        # (layer scan, change tuple, verb)
        if not tgt["candidates"]:
            others = [(l, c) for l in ok_layers if l is not tgt for c in l["candidates"]]
            if others:
                # 7. Never add a second row to a stack.
                left_alone: list = []
                for l, c in others:
                    if not c["ours"]:
                        out = self._foreign_gate(l, force, prompt_fn)
                        if out is not None:
                            return out
                        changes.append((l, ("replace", c, None, self._pass_env()), "Replaced"))
                    elif c["disabled"]:
                        left_alone.append(f" ({l['path']} has the row with disabled: true; "
                                          "setup leaves that alone)")
                    elif c.get("command_js"):
                        left_alone.append(f" ({l['path']}: {self.JS_NOTE})")
                    elif not _command_resolves(c["command"]):
                        changes.append((l, ("command", c, None), "Updated"))
                if not changes:
                    if self.profile:
                        msg = (f"DeepSeek Harness already has the sibyl-memory MCP server: the home "
                               f"patch {self.home_patch} already covers every profile.")
                    else:
                        names = [l["layer"].split(":", 1)[1] for l, _c in others]
                        msg = ("DeepSeek Harness already has the sibyl-memory MCP server in "
                               f"profile(s) {', '.join(names)} "
                               f"({', '.join(l['path'] for l, _c in others)}). To cover every "
                               "profile, remove those rows and run sibyl setup deepseek again.")
                    return WireOutcome(self.name, "already", msg + "".join(left_alone + notes))
            else:
                # 10. Nothing anywhere relevant: add.
                changes.append((tgt, ("add", None, self._pass_env()), "Added"))
        else:
            c = tgt["candidates"][0]
            if c["ours"]:
                # 8.
                if c["disabled"]:
                    return WireOutcome(self.name, "already",
                        f"The sibyl-memory row is present but disabled: true in {tgt['path']}; "
                        "setup leaves that alone."
                        + "".join(notes))
                if c.get("command_js") and not force:
                    return WireOutcome(self.name, "already",
                        f"DeepSeek Harness already has the sibyl-memory MCP server in {tgt['path']} "
                        f"({self.JS_NOTE}; --force repoints it)" + "".join(notes))
                resolves = _command_resolves(c["command"])
                if resolves and not force:
                    msg = f"DeepSeek Harness already has the sibyl-memory MCP server in {tgt['path']}"
                    if c["command"] != mcp_command_path():
                        msg += f" (uses a different install: {c['command']}; --force repoints it)"
                    return WireOutcome(self.name, "already", msg + "".join(notes))
                if c["command"] == mcp_command_path() and resolves:
                    return WireOutcome(self.name, "already",
                        f"DeepSeek Harness already has the sibyl-memory MCP server in {tgt['path']}"
                        + "".join(notes))
                changes.append((tgt, ("command", c, None), "Updated"))
            else:
                # 9.
                out = self._foreign_gate(tgt, force, prompt_fn)
                if out is not None:
                    return out
                changes.append((tgt, ("replace", c, None, self._pass_env()), "Replaced"))
        # 11. Dry run: before any install, directory creation or write.
        if dry_run:
            verbs = {v for _l, _c, v in changes}
            word = "add" if verbs == {"Added"} else ("update" if verbs == {"Updated"} else "replace")
            where = ", ".join(l["path"] for l, _c, _v in changes)
            return WireOutcome(self.name, "dry-run", f"Would {word} the sibyl-memory row in {where}")
        # 12. The server binary
        if not self._ensure_mcp_binary():
            looked = ", ".join(str(d) for d in _mcp_search_dirs())
            return WireOutcome(self.name, "error",
                f"'{self.MCP_BINARY}' is not installed (looked on PATH and in {looked}). "
                f"Install it: pip install {self.MCP_PACKAGE}" + self._install_hint(),
                handoff=_binary_missing_handoff(self.name))
        command = mcp_command_path()
        # 13. Plan every file; nothing is written unless every plan succeeded.
        plans: list = []
        env_lines: list = []
        for l, ch, _v in changes:
            path = Path(l["path"])
            if path.is_symlink():
                return self._manual_block_error(path,
                    f"{path} is a symlink; setup does not write through symlinks, so make this "
                    "change in the file it points at.",
                    mode=ch[0],
                    env=ch[3] if ch[0] == "replace" else ch[2] if ch[0] == "add" else None)
            if ch[0] == "add":
                ch = ("add", command, ch[2])
                env = ch[2]
            elif ch[0] == "command":
                # Only the target row reaches here with a !!js command, and
                # only under --force (B-2).
                ch = ("command", ch[1], command, bool(force))
                env = {}
            else:
                ch = ("replace", ch[1], command, ch[3])
                env = ch[3]
            for n in sorted(env or {}):
                env_lines.append(f"kept {n} in the row's env (DeepSeek Harness does not pass it "
                                 "through otherwise)")
            new = self._plan_text(l, ch)
            if new is None:
                return self._manual_block_error(path,
                    f"could not change {path} safely; it was not modified.",
                    mode=ch[0],
                    env=ch[3] if ch[0] == "replace" else ch[2] if ch[0] == "add" else None)
            plans.append((l["layer"], path, new, l["text"] if l.get("exists") else None))
        verbs = [v for _l, _c, v in changes]
        out = self._apply(plans, notes, verbs[0] if len(set(verbs)) == 1 else "Changed")
        if out.status == "wired":
            out.lifecycle = list(dict.fromkeys(env_lines))
        return out

    # -- doctor (spec dsh-doctor v2, 4.5) -------------------------------------
    def doctor_probe(self) -> list:
        try:
            if _import_yaml() is None:
                return [_probe("unreadable", self.home_patch, location="home",
                               detail="PyYAML is not installed, so the patch files cannot be read")]
            layers = self._scan_layers()
            ok = [l for l in layers if not l["error"]]
            home = next((l for l in ok if l["layer"] == "home"), None)
            dup: dict = {}
            for l in ok:
                if len(l["candidates"]) > 1:
                    dup.setdefault(l["layer"], set()).add(l["path"])
            if home is not None and home["candidates"]:
                for l in ok:
                    if l["layer"] != "home" and l["candidates"]:
                        pair = {home["path"], l["path"]}
                        dup.setdefault("home", set()).update(pair)
                        dup.setdefault(l["layer"], set()).update(pair)
            probes = []
            for l in layers:
                if l["error"]:
                    if l["mentions"]:
                        probes.append(_probe("unreadable", l["path"], location=l["layer"],
                                             detail=l["error"]))
                    continue
                if not l["candidates"]:
                    continue
                if l["layer"] in dup:
                    probes.append(_probe("duplicate", l["path"], location=l["layer"], detail=(
                        "more than one sibyl-memory row in one DeepSeek Harness stack: "
                        + ", ".join(sorted(dup[l["layer"]])))))
                    continue
                clash = self._manager_clash(ok, l["layer"])
                if clash:
                    probes.append(_probe("duplicate", l["path"], location=l["layer"],
                                         detail=self.MANAGER_CLASH_TEXT.format(path=clash)))
                    continue
                c = l["candidates"][0]
                js = bool(c.get("command_js"))
                cmd = None if js else c["command"]
                if not c["ours"]:
                    probes.append(_probe("foreign", l["path"], location=l["layer"],
                                         command=None,
                                         detail="another server holds the sibyl-memory id or name"))
                elif c["disabled"]:
                    probes.append(_probe("disabled", l["path"], location=l["layer"],
                                         command=cmd, detail="the row has disabled: true"))
                elif self._overridden(c, ok, l["layer"]):
                    probes.append(_probe("overridden", l["path"], location=l["layer"],
                                         command=cmd,
                                         detail="a patch with id sibyl-memory overrides the row"))
                elif js:
                    probes.append(_probe("present", l["path"], location=l["layer"],
                                         command=None, detail=self.JS_NOTE))
                else:
                    probes.append(_probe("present", l["path"], location=l["layer"],
                                         command=c["command"]))
            if not probes:
                probes.append(_probe("absent", self.home_patch, location="home"))
            return probes
        except Exception as e:
            return [_probe("unreadable", self.home_patch, location="home", detail=type(e).__name__)]

    def repair_mcp(self, probe: dict) -> WireOutcome:
        """Point our one row in the file the probe names at the resolved
        command: the same plan, verification, backup and write as setup."""
        layer = probe.get("location") or "home"
        path = self._layer_path(layer)
        scan = self._scan_file(path, layer)
        if scan["error"] or len(scan["candidates"]) != 1 or not scan["candidates"][0]["ours"]:
            return WireOutcome(self.name, "error",
                               f"no single sibyl-memory row of ours in {path} to repair")
        if scan["candidates"][0].get("command_js"):
            return WireOutcome(self.name, "error",
                f"the sibyl-memory command in {path} is a !!js expression; doctor does not "
                "change it (sibyl setup deepseek --force repoints it)")
        if path.is_symlink():
            return self._manual_block_error(path,
                f"{path} is a symlink; setup does not write through symlinks, so make this "
                "change in the file it points at.", mode="command")
        new = self._plan_text(scan, ("command", scan["candidates"][0], mcp_command_path()))
        if new is None:
            return self._manual_block_error(path,
                f"could not change {path} safely; it was not modified.", mode="command")
        return self._apply([(layer, path, new, scan["text"])], [], "Updated")

    def _foreign_gate(self, layer: dict, force: bool,
                      prompt_fn: Optional[Callable[..., str]]) -> Optional[WireOutcome]:
        if force:
            return None
        if prompt_fn is None:
            return WireOutcome(self.name, "skipped",
                f"Existing 'sibyl-memory' entry in {layer['path']} points elsewhere. "
                "Use --force to replace it.",
                overwrite_of="a different sibyl-memory MCP entry")
        if prompt_fn("DeepSeek Harness has a 'sibyl-memory' entry pointing elsewhere. Replace it?",
                     default="N") != "y":
            return WireOutcome(self.name, "skipped", "MCP entry replacement declined.",
                               overwrite_of="a different sibyl-memory MCP entry")
        return None


# ----------------------------------------------------------------------
# Registry + dispatch
# ----------------------------------------------------------------------

ALL_WIRERS: dict = {
    "hermes": HermesWirer,
    "claude-code": ClaudeCodeWirer,
    "codex": CodexWirer,
    "cursor": CursorWirer,
    "deepseek": DeepSeekWirer,
}

# Explicit-target-only wirers (never part of auto-detect).
EXPLICIT_WIRERS: dict = {
    "grokbot": GrokBotWirer,
}
# Aliases accepted as a setup target (and by doctor --harness); they map to a
# wirer name before anything else runs.
SETUP_ALIASES: dict = {"dsh": "deepseek"}
SETUP_TARGETS: tuple = tuple(ALL_WIRERS) + tuple(EXPLICIT_WIRERS) + tuple(SETUP_ALIASES)


# True once a prompt in this run met end of input (no terminal): an overwrite
# the person never answered is then handed back as a question (row X6).
_PROMPT_EOF = False


def _interactive_prompt(question: str, *, default: str = "Y") -> str:
    """Yes/no prompt. default 'Y' or 'N'. Returns 'y' or 'n'."""
    global _PROMPT_EOF
    default_label = "[Y/n]" if default.upper() == "Y" else "[y/N]"
    try:
        ans = input(f"{question} {default_label}: ").strip()
    except EOFError:
        # Row X5: say which default the transcript shows was taken.
        print(f"no terminal: default {default.upper()} taken")
        _PROMPT_EOF = True
        return default.lower()
    if not ans:
        return default.lower()
    return "y" if ans[:1].lower() == "y" else "n"


def _accept_defaults_prompt(question: str, *, default: str = "Y") -> str:
    """Non-interactive prompt. Returns the default. Used with --yes."""
    return default.lower()


def _wirer_kwargs(args: argparse.Namespace, name: str) -> dict:
    kw: dict = {}
    if name == "hermes" and getattr(args, "hermes_home", None):
        kw["hermes_home"] = args.hermes_home
    if name == "claude-code" and getattr(args, "claude_settings", None):
        kw["settings_path"] = args.claude_settings
    if name == "codex" and getattr(args, "codex_config", None):
        kw["config_path"] = args.codex_config
    if name in ("cursor", "grokbot") and getattr(args, "cursor_config", None):
        kw["config_path"] = args.cursor_config
    if name == "deepseek":
        if getattr(args, "dsh_home", None):
            kw["dsh_home"] = args.dsh_home
        if getattr(args, "dsh_profile", None) is not None:
            kw["profile"] = args.dsh_profile
    return kw


def _print_generic_config(dim, cyan) -> None:
    """A2: any MCP client (Grok Bot included) can wire itself from this."""
    cmd = mcp_command_path()
    print(dim("Any MCP client can use the server directly. Command (absolute path):"))
    print(f"  {cyan(cmd)}")
    if cmd == MCP_BINARY_NAME:
        print(dim(f"  ('{MCP_BINARY_NAME}' is not installed yet: pip install sibyl-memory-mcp)"))
    print()
    print(dim("Add this to the client's MCP config (the mcpServers block):"))
    print(mcp_servers_snippet(cmd))
    print()
    print(dim("Cursor or Grok Bot: sibyl setup cursor  |  sibyl setup grokbot"))
    print(dim("DeepSeek Harness: sibyl setup deepseek  (or: sibyl setup deepseek --print-config)"))
    print()


def _state_location(wirer: Any) -> Optional[str]:
    """Where a wirer looks for its config, for the setup summary: the Hermes
    home, else the settings or config file (Codex reports config_path).
    None when the state names none (sandbox M5: never print "None")."""
    try:
        st = wirer.current_state()
    except Exception:
        st = {}
    loc = st.get("hermes_home") or st.get("settings_path") or st.get("config_path")
    if not loc:
        loc = getattr(wirer, "config_path", None) or getattr(wirer, "settings_path", None)
    return str(loc) if loc else None


def cmd_setup(args: argparse.Namespace) -> int:
    """`sibyl setup` entry point. Auto-detect, then wire."""
    from . import _handoff as _h
    global _PROMPT_EOF
    _PROMPT_EOF = False
    bold, cyan, dim, green, red, yellow = _color_fns()
    which_human = ("which agent you use (Claude Code, Codex, Cursor, Hermes, DeepSeek Harness) "
                   "and where its config lives, if not in the default place")

    # Resolve target wirers
    target = getattr(args, "target", None)
    target = SETUP_ALIASES.get(target, target)
    try:
        if target:
            cls = ALL_WIRERS.get(target) or EXPLICIT_WIRERS.get(target)
            if cls is None:
                print(red(f"Unknown setup target: {target}"))
                print(f"Available: {', '.join(SETUP_TARGETS)}")
                names = [t for t in SETUP_TARGETS if t not in SETUP_ALIASES]
                _h.handoff("sibyl setup <" + "|".join(names) + ">", human=which_human)
                return 1
            wirers: dict = {target: cls(**_wirer_kwargs(args, target))}
            skip_present_check = True   # explicit target = wire it even if not detected on PATH
        else:
            wirers = {name: cls(**_wirer_kwargs(args, name)) for name, cls in ALL_WIRERS.items()}
            skip_present_check = False
    except ValueError as e:
        # A bad --dsh-profile name (DeepSeekWirer validates it).
        print(red(str(e)))
        _h.handoff("sibyl setup deepseek --dsh-profile <name>",
                   human="the DeepSeek Harness profile <name> (a folder under its profiles directory)")
        return 1

    print()
    print(bold("Sibyl Memory Plugin setup"))
    print()

    # Platform gate. `setup` wires a new machine, so it is one of the two
    # commands the native-Windows block applies to (see cli.NATIVE_WINDOWS_POLICY).
    from .cli import WINDOWS_HUMAN, print_platform_line
    if print_platform_line(gate=True) != 0:
        _h.handoff("done", human=WINDOWS_HUMAN)
        return 1
    print()

    # Detection
    if skip_present_check:
        detected = wirers
    else:
        detected = {n: w for n, w in wirers.items() if w.is_present()}

    if getattr(args, "print_config", False):
        _print_generic_config(dim, cyan)
        if target == "deepseek":
            print(wirers["deepseek"].print_config_text())
            print()
        return 0

    # A target setup will refuse before reading anything (a DeepSeek Harness
    # home that belongs to something else) is reported, never shown as
    # detected or asked about (sandbox M3).
    refused: list = []
    for name in list(detected):
        check = getattr(detected[name], "refusal", None)
        out = check() if callable(check) else None
        if out is not None:
            refused.append(out)
            del detected[name]

    if not detected and not refused:
        print(yellow("No agent frameworks detected on this machine."))
        print()
        print(dim("Looked for:"))
        for name, w in wirers.items():
            loc = _state_location(w)
            print(f"  {w.display_name}: {loc}" if loc else f"  {w.display_name}")
        print()
        print(dim("To override detection, point setup at a custom path:"))
        print(f"  {cyan('sibyl setup --hermes-home /custom/path')}")
        print(f"  {cyan('sibyl setup --claude-settings /custom/settings.json')}")
        print(f"  {cyan('sibyl setup --cursor-config /custom/mcp.json')}")
        print(f"  {cyan('sibyl setup deepseek --dsh-home /custom/folder')}")
        print()
        _print_generic_config(dim, cyan)
        # Row X3: nothing to wire is not a stop for the account; the agent
        # moves on and keeps the targeted setup for when its person answers.
        _h.handoff("sibyl status", human=which_human, later=("sibyl setup <target>",))
        return 0

    # Detection summary
    if detected:
        print(dim("Detected:"))
        for name, w in detected.items():
            loc = _state_location(w)
            print(f"  {w.display_name} at {loc}" if loc else f"  {w.display_name}")
        print()

    # Multi-framework picker
    selected = list(detected.keys())
    if len(detected) > 1 and not args.yes:
        choices = ", ".join(f"[{w.initial}]{w.display_name}" for w in detected.values())
        try:
            # Grok Bot onboarding (2026-10-28): never read a non-terminal stdin
            # (under `curl | sh` it is the installer script itself).
            if not sys.stdin.isatty():
                raise EOFError
            ans = input(
                f"Wire which? {choices}, [a]ll, [n]one (default: all): "
            ).strip().lower()
        except EOFError:
            # Finding 5.3.1 (handoff spec 2026-10-03): no terminal used to raise
            # a traceback here. The default is the same as --yes (row X4).
            ans = ""
            print("no terminal: wiring all detected (same as --yes)")
        if ans in ("n", "none"):
            print(dim("Skipping all."))
            print()
            _h.handoff("done")
            return 0
        elif ans in ("", "a", "all"):
            pass
        else:
            picked = [n for n, w in detected.items() if w.initial == ans[:1]]
            if not picked:
                print(red(f"No match for '{ans}'. Aborting."))
                _h.handoff("sibyl setup <target>", human=which_human)
                return 1
            selected = picked

    # Per-stack execution
    outcomes: list = list(refused)
    prompt_fn = _accept_defaults_prompt if args.yes else _interactive_prompt

    for name in selected:
        wirer = detected[name]
        st = wirer.current_state()

        # Pre-prompt for fresh adds (interactive only). Already-wired and
        # existing-other-provider are handled inside wire() itself.
        if (
            not args.yes
            and not st.get("wired_with_sibyl")
            and not st.get("memory_provider")
            and not st.get("sibyl_mcp")
        ):
            if name == "hermes":
                q = f"Set SIBYL as default memory provider in {wirer.display_name}?"
            else:
                q = f"Add SIBYL Memory as an MCP server in {wirer.display_name}?"
            ans = _interactive_prompt(q, default="Y")
            if ans != "y":
                outcomes.append(WireOutcome(name, "skipped", "Declined by user."))
                continue

        outcomes.append(
            wirer.wire(force=args.force, dry_run=args.dry_run, prompt_fn=prompt_fn)
        )

    # Report
    print()
    any_wired = False
    any_verify_fail = False
    for o in outcomes:
        marker = {
            "wired": green("✓"),
            "already": green("·"),
            "skipped": yellow("·"),
            "dry-run": cyan("→"),
            "error": red("✗"),
        }.get(o.status, "?")
        print(f"  {marker} {o.name}: {o.message}")
        if o.backup_path:
            print(f"      {dim('backup at')} {o.backup_path}")
        for ln in getattr(o, "lifecycle", None) or []:
            print(f"      {dim(ln)}")
        if o.status == "wired":
            any_wired = True

    # Post-wire verification: confirm MCP server actually boots
    verify_msgs: list = []
    for o in outcomes:
        if o.status not in ("wired", "already"):
            continue
        wirer = detected.get(o.name)
        if wirer and hasattr(wirer, "verify_mcp_starts"):
            ok, msg = wirer.verify_mcp_starts()
            if ok:
                print(f"  {green('✓')} {o.name}: {msg}")
            else:
                print(f"  {red('✗')} {o.name}: {msg}")
                any_verify_fail = True
                verify_msgs.append(msg)
    print()

    if any_wired or any(o.status == "already" for o in outcomes):
        if any_verify_fail:
            print(yellow("MCP server could not start. Fix the error above, then reconnect."))
        else:
            print(green("MCP server is ready."))
        print()
        # Claude Code specific reconnect instructions
        cc_active = any(
            o.name == "claude-code" and o.status in ("wired", "already")
            for o in outcomes
        )
        if cc_active:
            if any_wired:
                print(dim("  Claude Code: restart, or type /mcp and reconnect sibyl-memory."))
            else:
                print(dim("  Claude Code: if not connected, type /mcp and reconnect sibyl-memory."))
            print()

    nxt, human, later = _setup_handoff(outcomes, detected, verify_msgs,
                                       unanswered=bool(args.yes) or _PROMPT_EOF)
    for o in outcomes:
        if o.name == "grokbot" and o.status in ("wired", "already"):
            w = detected["grokbot"]
            print(bold("Grok Bot confirmation"))
            print(f"  server name : {w.MCP_NAME}")
            print(f"  command     : {mcp_command_path()}")
            print(f"  config file : {w.config_path}")
            print(dim("  Reload MCP servers in Cursor (or start a new Grok Bot session), then "
                      "run: sibyl doctor"))
            print()

    _h.handoff(nxt, human=human, later=later)
    return 0 if all(o.status != "error" for o in outcomes) and not any_verify_fail else 2


_RESTART_HUMAN = {
    "claude-code": "restart Claude Code, or type /mcp and reconnect sibyl-memory",
    "hermes": "restart Hermes so it loads the sibyl memory provider",
    "codex": "restart Codex so it loads the sibyl_memory MCP server",
    "deepseek": ("start a new DeepSeek Harness session; if the memory tools do not appear, "
                 "restart DeepSeek Harness"),
    "cursor": "reload MCP servers in Cursor",
}


def _setup_handoff(outcomes: list, detected: dict, verify_msgs: list, *, unanswered: bool) -> tuple:
    """(next, human, later) for the end of `sibyl setup` (rows X6 to X16):
    the first error, then a server that did not start, then an overwrite the
    person has not answered, then the restart of what was wired."""
    for o in outcomes:
        if o.status == "error":
            if o.handoff:
                return o.handoff
            # A config write or plugin install that failed is a permission or
            # disk problem, not a transient one: re-running would fail the same
            # way, so a person fixes it first.
            who = getattr(detected.get(o.name), "display_name", o.name)
            why = " ".join(str(o.message or "").split())[:200] or "an error"
            return ("done", f"{who} could not be wired ({why}); fix that, then run "
                            f"sibyl setup {o.name}", ())
    if verify_msgs:
        cred = any("credential" in str(m).lower() for m in verify_msgs)
        if cred:
            return ("sibyl status",
                    "the memory server reports a missing credential (above); sign this computer in "
                    "with sibyl init if it is not", ())
        return ("done", "run sibyl-memory-mcp in a terminal and tell me the first error line", ())
    if unanswered:
        for o in outcomes:
            if o.status == "skipped" and o.overwrite_of:
                w = detected.get(o.name)
                who = getattr(w, "display_name", o.name)
                return (f"sibyl setup {o.name} --force",
                        f"{who} already has {o.overwrite_of}; confirm replacing it with Sibyl", ())
    for o in outcomes:
        # Panel fix 10: a skipped outcome that carries its own command (Claude
        # Code with no claude CLI on PATH) hands that command over.
        if o.status == "skipped" and o.handoff and not o.overwrite_of:
            return o.handoff
    active = [o.name for o in outcomes if o.status in ("wired", "already")]
    if active:
        # Only a harness whose config changed needs a restart; an "already"
        # outcome wrote nothing, so it gets no restart ask.
        changed = [o.name for o in outcomes if o.status == "wired"]
        if not changed:
            human = None
        elif len(changed) == 1:
            human = _RESTART_HUMAN.get(changed[0])
        else:
            names = " / ".join(getattr(detected.get(n), "display_name", n) for n in changed)
            human = (f"restart {names} (in Claude Code: type /mcp and reconnect sibyl-memory) so it "
                     "loads the memory server")
        return ("sibyl status", human, ())
    return ("done", None, ())
