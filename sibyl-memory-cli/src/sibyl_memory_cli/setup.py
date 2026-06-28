"""`sibyl setup`: auto-detect agent frameworks and wire SIBYL as memory provider.

Maximum-efficiency onboarding command. Single-command path for the user:

    pip install sibyl-memory-cli
    sibyl setup          # auto-detects Hermes + Claude Code, prompts per stack, wires

Two wirers in v0.1.4:
  - HermesWirer:     install-plugin + edit $HERMES_HOME/config.yaml (memory.provider)
  - ClaudeCodeWirer: edit ~/.claude/settings.json (mcpServers.sibyl-memory)

Each wirer follows the same protocol:
  is_present()       -> bool          (filesystem + PATH detect)
  current_state()    -> dict          (configured? wired-with-sibyl? current-value?)
  wire(force, dry_run, prompt_fn) -> WireOutcome

Destructive operations (overwriting an existing non-SIBYL config) default to NO
on the prompt. Fresh adds default to YES. --force overrides destructive guards.
--yes accepts all defaults (still respects the destructive-default-NO unless
--force is also passed). --dry-run prints intent without writing.

All config edits are atomic: backup to <file>.bak, write to <file>.tmp, rename.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union


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

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()
        yaml = _import_yaml()
        if yaml is None:
            return WireOutcome(
                self.name, "error",
                "PyYAML not installed. Run `pip install pyyaml` and retry.",
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
                )
            ans = prompt_fn(
                f"Hermes currently uses '{state['memory_provider']}' as memory provider. Overwrite with SIBYL?",
                default="N",
            )
            if ans != "y":
                return WireOutcome(self.name, "skipped", "Memory provider overwrite declined.")

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
        if not self.config_path.exists():
            return None
        import time as _t; backup = self.config_path.with_suffix(".yaml.bak." + _t.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(self.config_path, backup)
        return backup

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
# Standalone smoke-test (used by both ClaudeCodeWirer and CodexWirer)
# ----------------------------------------------------------------------

def _verify_mcp_starts(binary: str) -> tuple:
    """Smoke-test: spawn sibyl-memory-mcp and confirm it doesn't crash on startup.
    
    Returns (ok: bool, message: str).
    """
    import subprocess
    import time as _t

    binpath = shutil.which(binary)
    if not binpath:
        return False, f"'{binary}' not found on PATH"
    try:
        proc = subprocess.Popen(
            [binpath],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = _t.monotonic() + 3.0
        rc = None
        while _t.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                break
        if rc is None:
            proc.terminate()
            proc.wait(timeout=2)
            return True, f"'{binary}' started OK (still running at deadline)"
        stderr = proc.stderr.read().decode(errors="replace").strip()
        return False, f"'{binary}' exited with code {rc}: {stderr[:200]}"
    except Exception as e:
        return False, f"Failed to start '{binary}': {e}"

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

        Returns (ok: bool, message: str).  Catches the common failures:
        ImportError (missing dep), ModuleNotFoundError, bad credentials file.
        All of those manifest within the first second as a non-zero exit.
        """
        import subprocess
        import time

        binary = shutil.which(self.MCP_BINARY)
        if not binary:
            return False, f"'{self.MCP_BINARY}' not found on PATH"
        try:
            proc = subprocess.Popen(
                [binary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # CLI-12: MCP stdio servers block on stdin; a crash-on-import exits
            # quickly with a non-zero code. POLL the exit code over a short
            # window instead of a fixed sleep — a slow import (cold caches,
            # heavy deps) that is still alive at the deadline is treated as
            # healthy (it's blocking on stdin), not "crashed".
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
                # Exited cleanly without blocking — unusual for a stdio server
                # but not a crash.
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
                + self._install_hint())
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
                f"`claude mcp add` failed (exit {rc}): {(err or out).strip()[:200]}")
        # Post-wire verification (bug, cryptoxdylan 2026-06-01): a 0 exit from
        # `claude mcp add` has been observed to not guarantee discovery. Confirm the
        # server actually shows in `claude mcp get`, and surface concrete remediation
        # instead of reporting a false success that leaves the MCP absent from /mcp.
        if self._registered_via_cli() is False:
            return WireOutcome(self.name, "error",
                "ran `claude mcp add` (exit 0) but the server is not in `claude mcp list`. "
                "restart Claude Code, then run `claude mcp list`; if still absent, run "
                f"`claude mcp add --scope user {self.MCP_NAME} -- {binpath}` manually.")
        return WireOutcome(self.name, "wired",
            "Registered sibyl-memory with Claude Code via `claude mcp add --scope user` (verified in `claude mcp list`).")

    def wire(self, *, force: bool = False, dry_run: bool = False,
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
                )
            ans = prompt_fn(
                "Claude Code has 'sibyl-memory' MCP entry but pointing elsewhere. Update?",
                default="N",
            )
            if ans != "y":
                return WireOutcome(self.name, "skipped", "MCP entry update declined.")

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
        if not self.settings_path.exists():
            return None
        backup = self.settings_path.with_suffix(f".json.bak.{ts}")
        shutil.copy2(self.settings_path, backup)
        return backup

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
        # CORE-18: standalone smoke-test function to avoid fragile cross-class call
        return _verify_mcp_starts(self.MCP_BINARY)

    def wire(self, *, force: bool = False, dry_run: bool = False,
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
                + self._install_hint())
        backup = self._backup_config()
        try:
            self._append_block()
        except Exception as e:
            return WireOutcome(self.name, "error",
                f"config write failed: {type(e).__name__}: {e}", backup_path=backup)
        return WireOutcome(self.name, "wired",
            f"Added [mcp_servers.sibyl_memory] to {self.config_path}", backup_path=backup)

    def _backup_config(self) -> Optional[Path]:
        if not self.config_path.exists():
            return None
        backup = self.config_path.with_suffix(f".toml.bak.{ts}")
        shutil.copy2(self.config_path, backup)
        return backup

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


def _interactive_prompt(question: str, *, default: str = "Y") -> str:
    """Yes/no prompt. default 'Y' or 'N'. Returns 'y' or 'n'."""
    default_label = "[Y/n]" if default.upper() == "Y" else "[y/N]"
    try:
        ans = input(f"{question} {default_label}: ").strip()
    except EOFError:
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
    return kw


def cmd_setup(args: argparse.Namespace) -> int:
    """`sibyl setup` entry point. Auto-detect, then wire."""
    bold, cyan, dim, green, red, yellow = _color_fns()

    # Resolve target wirers
    target = getattr(args, "target", None)
    if target:
        if target not in ALL_WIRERS:
            print(red(f"Unknown setup target: {target}"))
            print(f"Available: {', '.join(ALL_WIRERS)}")
            return 1
        wirers: dict = {target: ALL_WIRERS[target](**_wirer_kwargs(args, target))}
        skip_present_check = True   # explicit target = wire it even if not detected on PATH
    else:
        wirers = {name: cls(**_wirer_kwargs(args, name)) for name, cls in ALL_WIRERS.items()}
        skip_present_check = False

    print()
    print(bold("Sibyl Memory Plugin setup"))
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
        ans = input(
            f"Wire which? {choices}, [a]ll, [n]one (default: all): "
        ).strip().lower()
        if ans in ("n", "none"):
            print(dim("Skipping all."))
            print()
            return 0
        elif ans in ("", "a", "all"):
            pass
        else:
            picked = [n for n, w in detected.items() if w.initial == ans[:1]]
            if not picked:
                print(red(f"No match for '{ans}'. Aborting."))
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
        if o.status == "wired":
            any_wired = True

    # Post-wire verification: confirm MCP server actually boots
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

    return 0 if all(o.status != "error" for o in outcomes) and not any_verify_fail else 2
