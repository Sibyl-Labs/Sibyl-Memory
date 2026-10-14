"""`sibyl setup`: auto-detect agent frameworks and wire SIBYL as memory provider.

Maximum-efficiency onboarding command. Single-command path for the user:

    pip install sibyl-memory-cli
    sibyl setup          # auto-detects Hermes, Claude Code and Codex, prompts per stack, wires

Three wirers:
  - HermesWirer:     install-plugin + edit $HERMES_HOME/config.yaml (memory.provider)
  - ClaudeCodeWirer: `claude mcp add --scope user sibyl-memory` (the discovery
                     surface); ~/.claude/settings.json holds the hooks and the
                     skills directory sits beside it
  - CodexWirer:      add [mcp_servers.sibyl_memory] to ~/.codex/config.toml

Each wirer follows the same protocol:
  is_present()       -> bool          (filesystem + PATH detect)
  current_state()    -> dict          (configured? wired-with-sibyl? current-value?)
  wire(force, dry_run, prompt_fn) -> WireOutcome

Destructive operations (overwriting an existing non-SIBYL config) default to NO
on the prompt. Fresh adds default to YES. --force overrides destructive guards.
--yes accepts all defaults (still respects the destructive-default-NO unless
--force is also passed). --dry-run prints intent without writing.

All config edits are atomic: backup to <file>.<utc-timestamp>.bak, write to
<file>.tmp, rename.
"""
from __future__ import annotations

import argparse
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


def _timestamped_backup(path: Path) -> Optional[Path]:
    """Copy ``path`` to a timestamped backup beside it, returning the backup
    path (or None if the source does not exist).

    B005 (audit #19): backups used a FIXED ``.bak`` suffix, so a second
    ``sibyl init`` overwrote the first run's backup — destroying the only
    pre-change copy. We append a UTC timestamp to the full filename so every run
    keeps its own backup, e.g. ``config.yaml`` becomes
    ``config.yaml.20260630T142530Z.bak`` (the original extension is preserved).

    The timestamp is computed BEFORE it is used in the filename (the external
    PR that reported this referenced an undefined ``ts`` inside the f-string and
    raised NameError — that bug is not reproduced here).
    """
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(f"{path.name}.{ts}.bak")
    shutil.copy2(path, backup)
    return backup


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
        return False, "MCP binary not found on PATH"
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
# HermesWirer
# ----------------------------------------------------------------------

class HermesWirer:
    name = "hermes"
    display_name = "Hermes"
    initial = "h"

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

        # 3. Dry-run report — never installs or writes.
        if dry_run:
            actions = []
            if not state["plugin_installed"]:
                actions.append(f"install plugin at {self.plugin_dir}")
            actions.append(f"set memory.provider=sibyl in {self.config_path}")
            return WireOutcome(self.name, "dry-run", "Would: " + "; ".join(actions))

        # 4. Install plugin if missing — only now that the gate has passed.
        if not state["plugin_installed"]:
            try:
                self._install_plugin()
            except Exception as e:
                return WireOutcome(
                    self.name, "error",
                    f"install-plugin failed: {type(e).__name__}: {e}",
                )

        # 5. Real write. Backup, then atomic rename.
        backup = self._backup_config()
        try:
            self._write_config_with_sibyl(yaml)
        except Exception as e:
            return WireOutcome(
                self.name, "error",
                f"config write failed: {type(e).__name__}: {e}",
                backup_path=backup,
            )
        return WireOutcome(
            self.name, "wired",
            f"Wired memory.provider=sibyl in {self.config_path}",
            backup_path=backup,
        )

    def _install_plugin(self) -> None:
        from sibyl_memory_hermes.install_plugin import install
        install(hermes_home=Path(self.hermes_home), force=False, dry_run=False)

    def _backup_config(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.config_path)

    def _write_config_with_sibyl(self, yaml) -> None:
        cfg: dict = {}
        if self.config_path.exists():
            raw = self.config_path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                cfg = loaded
            elif loaded is not None:
                # Fail fast: a non-mapping top level means the config is not
                # something we can merge into. Silently reinitializing would
                # destroy the user's existing Hermes settings.
                raise ValueError(
                    f"{self.config_path} top level is not a YAML mapping "
                    f"(got {type(loaded).__name__}). Fix the file or move it "
                    "aside and re-run; it was not modified."
                )
        if not isinstance(cfg.get("memory"), dict):
            cfg["memory"] = {}
        cfg["memory"]["provider"] = "sibyl"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, self.config_path)


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
        return shutil.which(self.MCP_BINARY) is not None

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
        binary = shutil.which(self.MCP_BINARY)
        if not binary:
            return False, f"'{self.MCP_BINARY}' not found on PATH"
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
        # Claude Code actually discovers servers). Otherwise fall back to settings.json.
        if cli_registered is None:
            wired = bool(sibyl_block == self.SIBYL_MCP_BLOCK and mcp_binary)
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
                f"'{self.MCP_BINARY}' not on PATH. Install it: pip install {self.MCP_PACKAGE}"
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
        binpath = shutil.which(self.MCP_BINARY) or self.MCP_BINARY
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
        backup = self._backup_settings()
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
        self._backup_settings()
        _atomic_text(self.settings_path, json.dumps(new, indent=2) + "\n")
        return [f"hooks: removed ours from {self.settings_path}"]

    def wire_lifecycle(self, *, force: bool = False, dry_run: bool = False) -> list:
        lines = install_canonical_skills(dry_run=dry_run)
        lines += install_shims("claude-code", self.skill_shim_targets(), dry_run=dry_run, force=force)
        lines += self.wire_hooks(dry_run=dry_run)
        return lines

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        out = self._wire_mcp(force=force, dry_run=dry_run, prompt_fn=prompt_fn)
        if out.status in ("wired", "already", "dry-run"):
            try:
                out.lifecycle = self.wire_lifecycle(force=force, dry_run=dry_run)
            except Exception as e:  # never fail the MCP wiring on the extras
                out.lifecycle = [f"skills/hooks not installed: {type(e).__name__}: {e}"]
        return out

    def _wire_mcp(self, *, force: bool = False, dry_run: bool = False,
                  prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        # Preferred path: if the `claude` CLI exists, register through it (reliable
        # discovery). The settings.json logic below is the no-CLI fallback only.
        if self._claude_cli():
            return self._wire_via_cli(force=force, dry_run=dry_run)

        state = self.current_state()

        # Config block matches but binary is missing: fix the binary, not short-circuit
        if state["sibyl_mcp"] == self.SIBYL_MCP_BLOCK and not state["mcp_binary_found"]:
            if dry_run:
                return WireOutcome(
                    self.name, "dry-run",
                    f"Would install {self.MCP_PACKAGE} (config present, binary missing)",
                )
            if not self._ensure_mcp_binary(prompt_fn=prompt_fn):
                return WireOutcome(
                    self.name, "error",
                    f"Config is set but '{self.MCP_BINARY}' not on PATH. "
                    f"Install it: pip install {self.MCP_PACKAGE}" + self._install_hint(),
                    handoff=_binary_missing_handoff(self.name),
                )
            return WireOutcome(
                self.name, "wired",
                f"Installed {self.MCP_PACKAGE} (config was already present in {self.settings_path})",
            )

        if state["wired_with_sibyl"]:
            return WireOutcome(
                self.name, "already",
                f"Claude Code already has SIBYL Memory MCP server in {self.settings_path}",
            )

        if state["sibyl_mcp"] and not force:
            if prompt_fn is None:
                return WireOutcome(
                    self.name, "skipped",
                    "Existing sibyl-memory MCP entry differs. Use --force to overwrite.",
                    overwrite_of="a different sibyl-memory MCP entry",
                )
            ans = prompt_fn(
                "Claude Code has 'sibyl-memory' MCP entry but pointing elsewhere. Update?",
                default="N",
            )
            if ans != "y":
                return WireOutcome(self.name, "skipped", "MCP entry update declined.",
                                   overwrite_of="a different sibyl-memory MCP entry")

        if dry_run:
            verb = "update" if state["sibyl_mcp"] else "add"
            extra = ""
            if not state["mcp_binary_found"]:
                extra = f" + install {self.MCP_PACKAGE}"
            return WireOutcome(
                self.name, "dry-run",
                f"Would {verb} mcpServers.sibyl-memory in {self.settings_path}{extra}",
            )

        # Ensure binary before writing config
        if not self._ensure_mcp_binary(prompt_fn=prompt_fn):
            return WireOutcome(
                self.name, "error",
                f"'{self.MCP_BINARY}' not on PATH after install attempt. "
                f"Install manually: pip install {self.MCP_PACKAGE}" + self._install_hint(),
                handoff=_binary_missing_handoff(self.name),
            )

        backup = self._backup_settings()
        try:
            self._write_settings_with_sibyl()
        except Exception as e:
            return WireOutcome(
                self.name, "error",
                f"settings write failed: {type(e).__name__}: {e}",
                backup_path=backup,
            )
        return WireOutcome(
            self.name, "wired",
            f"Added SIBYL Memory MCP server to {self.settings_path}",
            backup_path=backup,
        )

    def _backup_settings(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.settings_path)

    def _write_settings_with_sibyl(self) -> None:
        cfg: dict = {}
        if self.settings_path.exists():
            raw = self.settings_path.read_text(encoding="utf-8")
            if raw.strip():
                try:
                    loaded = json.loads(raw)
                except ValueError as e:
                    # Fail fast: never atomically replace a corrupt settings.json
                    # with a sibyl-only file (that destroys the user's other
                    # mcpServers, permissions, hooks, env).
                    raise ValueError(
                        f"{self.settings_path} is not valid JSON ({e}). "
                        "Fix the file or move it aside and re-run setup; "
                        "your settings were not modified."
                    ) from e
                if isinstance(loaded, dict):
                    cfg = loaded
                else:
                    raise ValueError(
                        f"{self.settings_path} top level is not a JSON object "
                        f"(got {type(loaded).__name__}). Fix the file or move it "
                        "aside and re-run setup; your settings were not modified."
                    )
        if not isinstance(cfg.get("mcpServers"), dict):
            cfg["mcpServers"] = {}
        cfg["mcpServers"]["sibyl-memory"] = self.SIBYL_MCP_BLOCK
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.settings_path)


# ----------------------------------------------------------------------
# CodexWirer  — Codex discovers MCP servers from ~/.codex/config.toml, so editing
# that file IS the reliable method (unlike Claude's settings.json). Append the
# [mcp_servers.sibyl_memory] table if absent. Atomic, .bak backup, idempotent.
# ----------------------------------------------------------------------

class CodexWirer:
    name = "codex"
    display_name = "OpenAI Codex"
    initial = "x"

    MCP_BINARY = "sibyl-memory-mcp"
    MCP_PACKAGE = "sibyl-memory-mcp"
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
        return shutil.which(self.MCP_BINARY) is not None

    def _mcp_command(self) -> str:
        """Resolved absolute path to the MCP binary, falling back to the bare
        name if it cannot be resolved (mirrors the Claude wirer fix)."""
        return shutil.which(self.MCP_BINARY) or self.MCP_BINARY

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

    def current_state(self) -> dict:
        exists = self.config_path.exists()
        wired = False
        if exists:
            try:
                wired = self.HEADER in self.config_path.read_text(encoding="utf-8")
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
        binary = shutil.which(self.MCP_BINARY)
        if not binary:
            return False, f"'{self.MCP_BINARY}' not found on PATH"
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
                  prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()
        if state["wired_with_sibyl"] and not force:
            return WireOutcome(self.name, "already",
                f"Codex already has the sibyl-memory MCP server in {self.config_path}")
        if dry_run:
            verb = "create + add" if not state["config_exists"] else "append"
            return WireOutcome(self.name, "dry-run",
                f"Would {verb} [mcp_servers.sibyl_memory] in {self.config_path}")
        if not self._ensure_mcp_binary():
            return WireOutcome(self.name, "error",
                f"'{self.MCP_BINARY}' not on PATH. Install it: pip install {self.MCP_PACKAGE}"
                + self._install_hint(), handoff=_binary_missing_handoff(self.name))
        backup = self._backup_config()
        try:
            self._append_block()
        except Exception as e:
            return WireOutcome(self.name, "error",
                f"config write failed: {type(e).__name__}: {e}", backup_path=backup)
        return WireOutcome(self.name, "wired",
            f"Added [mcp_servers.sibyl_memory] to {self.config_path}", backup_path=backup)

    def _backup_config(self) -> Optional[Path]:
        # B005 (audit #19): timestamped backup so repeated runs don't clobber it.
        return _timestamped_backup(self.config_path)

    def _append_block(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.config_path.exists():
            existing = self.config_path.read_text(encoding="utf-8")
            if self.HEADER in existing:          # idempotent guard
                return
        new_text = existing.rstrip("\n") + ("\n" if existing.strip() else "") + self._block_text()
        tmp = self.config_path.with_suffix(".toml.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, self.config_path)


# ----------------------------------------------------------------------
# Registry + dispatch
# ----------------------------------------------------------------------

ALL_WIRERS: dict = {
    "hermes": HermesWirer,
    "claude-code": ClaudeCodeWirer,
    "codex": CodexWirer,
}


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
    return kw


def cmd_setup(args: argparse.Namespace) -> int:
    """`sibyl setup` entry point. Auto-detect, then wire."""
    from . import _handoff as _h
    global _PROMPT_EOF
    _PROMPT_EOF = False
    bold, cyan, dim, green, red, yellow = _color_fns()
    which_human = ("which agent you use (Claude Code, Codex, Hermes) and where its config lives, "
                   "if not in the default place")

    # Resolve target wirers
    target = getattr(args, "target", None)
    if target:
        if target not in ALL_WIRERS:
            print(red(f"Unknown setup target: {target}"))
            print(f"Available: {', '.join(ALL_WIRERS)}")
            _h.handoff("sibyl setup <hermes|claude-code|codex>", human=which_human)
            return 1
        wirers: dict = {target: ALL_WIRERS[target](**_wirer_kwargs(args, target))}
        skip_present_check = True   # explicit target = wire it even if not detected on PATH
    else:
        wirers = {name: cls(**_wirer_kwargs(args, name)) for name, cls in ALL_WIRERS.items()}
        skip_present_check = False

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

    if not detected:
        print(yellow("No agent frameworks detected on this machine."))
        print()
        print(dim("Looked for:"))
        for name, w in wirers.items():
            st = w.current_state()
            loc = st.get("hermes_home") or st.get("settings_path")
            print(f"  {w.display_name}: {loc}")
        print()
        print(dim("To override detection, point setup at a custom path:"))
        print(f"  {cyan('sibyl setup --hermes-home /custom/path')}")
        print(f"  {cyan('sibyl setup --claude-settings /custom/settings.json')}")
        print()
        # Row X3: nothing to wire is not a stop for the account; the agent
        # moves on and keeps the targeted setup for when its person answers.
        _h.handoff("sibyl status", human=which_human, later=("sibyl setup <target>",))
        return 0

    # Detection summary
    print(dim("Detected:"))
    for name, w in detected.items():
        st = w.current_state()
        loc = st.get("hermes_home") or st.get("settings_path")
        print(f"  {w.display_name} at {loc}")
    print()

    # Multi-framework picker
    selected = list(detected.keys())
    if len(detected) > 1 and not args.yes:
        choices = ", ".join(f"[{w.initial}]{w.display_name}" for w in detected.values())
        try:
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
    outcomes: list = []
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
    _h.handoff(nxt, human=human, later=later)
    return 0 if all(o.status != "error" for o in outcomes) and not any_verify_fail else 2


_RESTART_HUMAN = {
    "claude-code": "restart Claude Code, or type /mcp and reconnect sibyl-memory",
    "hermes": "restart Hermes so it loads the sibyl memory provider",
    "codex": "restart Codex so it loads the sibyl_memory MCP server",
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
