"""`sibyl setup`: auto-detect agent frameworks and wire SIBYL as memory provider.

Maximum-efficiency onboarding command. Single-command path for the user:

    pip install sibyl-memory-cli
    sibyl setup          # auto-detects Hermes + Claude Code + Codex, prompts per stack, wires

Three wirers in v0.3.7:
  - HermesWirer:     install-plugin + edit $HERMES_HOME/config.yaml (memory.provider)
  - ClaudeCodeWirer: edit ~/.claude.json (mcpServers.sibyl-memory)
  - CodexWirer:      edit ~/.codex/config.toml ([mcp_servers.sibyl_memory])

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
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

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

        # 1. Install plugin if missing
        if not state["plugin_installed"]:
            if dry_run:
                pass  # report at the end
            else:
                try:
                    self._install_plugin()
                except Exception as e:
                    return WireOutcome(
                        self.name, "error",
                        f"install-plugin failed: {type(e).__name__}: {e}",
                    )

        # 2. Already wired? no-op
        if state["wired_with_sibyl"] and state["plugin_installed"]:
            return WireOutcome(
                self.name, "already",
                f"Hermes already has SIBYL as memory provider in {self.config_path}",
            )

        # 3. Existing non-SIBYL provider? confirm or refuse
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

        # 4. Dry-run report
        if dry_run:
            actions = []
            if not state["plugin_installed"]:
                actions.append(f"install plugin at {self.plugin_dir}")
            actions.append(f"set memory.provider=sibyl in {self.config_path}")
            return WireOutcome(self.name, "dry-run", "Would: " + "; ".join(actions))

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
        install(hermes_home=str(self.hermes_home))

    def _backup_config(self) -> Optional[Path]:
        if not self.config_path.exists():
            return None
        backup = self.config_path.with_suffix(".yaml.bak")
        shutil.copy2(self.config_path, backup)
        return backup

    def _write_config_with_sibyl(self, yaml) -> None:
        cfg: dict = {}
        if self.config_path.exists():
            raw = self.config_path.read_text(encoding="utf-8")
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                cfg = loaded
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

    def __init__(self, *, settings_path: Optional[Union[str, Path]] = None):
        # v0.3.7 (2026-05-22): default flipped from ~/.claude/settings.json to
        # ~/.claude.json. Recent Claude Code reads MCP server config from the
        # user-level ~/.claude.json — writing to ~/.claude/settings.json was a
        # silent-failure path (wirer reported success but MCP server didn't
        # appear in Claude Code). Override with --claude-settings if your
        # install uses a non-standard location.
        self.settings_path = (
            Path(settings_path).expanduser() if settings_path
            else Path.home() / ".claude.json"
        )

    def is_present(self) -> bool:
        if self.settings_path.exists():
            return True
        if shutil.which("claude"):
            return True
        return False

    def current_state(self) -> dict:
        settings_exists = self.settings_path.exists()
        mcp_servers: dict = {}
        sibyl_block: Optional[dict] = None
        if settings_exists:
            try:
                cfg = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    raw_servers = cfg.get("mcpServers", {})
                    if isinstance(raw_servers, dict):
                        mcp_servers = raw_servers
                        sibyl_block = mcp_servers.get("sibyl-memory")
            except Exception:
                pass
        return {
            "settings_path": str(self.settings_path),
            "settings_exists": settings_exists,
            "mcp_servers_count": len(mcp_servers),
            "sibyl_mcp": sibyl_block,
            "wired_with_sibyl": sibyl_block == self.SIBYL_MCP_BLOCK,
        }

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()

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
            return WireOutcome(
                self.name, "dry-run",
                f"Would {verb} mcpServers.sibyl-memory in {self.settings_path}",
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
        backup = self.settings_path.with_suffix(".json.bak")
        shutil.copy2(self.settings_path, backup)
        return backup

    def _write_settings_with_sibyl(self) -> None:
        cfg: dict = {}
        if self.settings_path.exists():
            try:
                loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg = loaded
            except Exception:
                cfg = {}
        if not isinstance(cfg.get("mcpServers"), dict):
            cfg["mcpServers"] = {}
        cfg["mcpServers"]["sibyl-memory"] = self.SIBYL_MCP_BLOCK
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.settings_path)


# ----------------------------------------------------------------------
# CodexWirer — edits ~/.codex/config.toml
# ----------------------------------------------------------------------

class CodexWirer:
    """Auto-wire Codex CLI's MCP server registry at ~/.codex/config.toml.

    Codex reads server defs from a TOML table named `[mcp_servers.<name>]`.
    We use `[mcp_servers.sibyl_memory]` (snake_case is TOML-idiomatic; the
    Claude Code side uses `sibyl-memory` because JSON keys allow hyphens
    and Anthropic docs use that form).

    Idempotency is text-based rather than full TOML round-trip, so we
    preserve operator hand-edits and comments in the rest of the file
    instead of reformatting on every run. The cost: we won't catch
    pathological TOML edge cases (e.g., comments mid-key). For the
    canonical Codex config layout this is fine.
    """
    name = "codex"
    display_name = "Codex CLI"
    initial = "x"  # 'c' is taken by Claude Code; 'x' from codeX

    SIBYL_TOML_BLOCK = '[mcp_servers.sibyl_memory]\ncommand = "sibyl-memory-mcp"\n'
    BLOCK_HEADER_RE = re.compile(
        r'^\s*\[\s*mcp_servers\s*\.\s*sibyl_memory\s*\]\s*$', re.MULTILINE
    )
    NEXT_TABLE_RE = re.compile(r'^\s*\[', re.MULTILINE)
    SIBYL_COMMAND_RE = re.compile(
        r'^\s*command\s*=\s*"sibyl-memory-mcp"\s*$', re.MULTILINE
    )

    def __init__(self, *, config_path: Optional[Union[str, Path]] = None):
        self.config_path = (
            Path(config_path).expanduser() if config_path
            else Path.home() / ".codex" / "config.toml"
        )

    def is_present(self) -> bool:
        if self.config_path.exists():
            return True
        if shutil.which("codex"):
            return True
        return False

    def _read(self) -> str:
        if not self.config_path.exists():
            return ""
        try:
            return self.config_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _extract_block(self, content: str) -> Optional[str]:
        m = self.BLOCK_HEADER_RE.search(content)
        if not m:
            return None
        # Find next [...] table header after our match (start-of-line "[").
        next_tbl = self.NEXT_TABLE_RE.search(content, m.end())
        end = next_tbl.start() if next_tbl else len(content)
        return content[m.start():end]

    def current_state(self) -> dict:
        content = self._read()
        block = self._extract_block(content)
        sibyl_block_present = block is not None
        wired_with_sibyl = bool(block and self.SIBYL_COMMAND_RE.search(block))
        return {
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "sibyl_block_present": sibyl_block_present,
            "wired_with_sibyl": wired_with_sibyl,
        }

    def wire(self, *, force: bool = False, dry_run: bool = False,
             prompt_fn: Optional[Callable[..., str]] = None) -> WireOutcome:
        state = self.current_state()

        if state["wired_with_sibyl"]:
            return WireOutcome(
                self.name, "already",
                f"Codex CLI already has [mcp_servers.sibyl_memory] block in {self.config_path}",
            )

        if state["sibyl_block_present"] and not force:
            if prompt_fn is None:
                return WireOutcome(
                    self.name, "skipped",
                    "Existing [mcp_servers.sibyl_memory] differs. Use --force to overwrite.",
                )
            ans = prompt_fn(
                "Codex has [mcp_servers.sibyl_memory] but command differs. Update?",
                default="N",
            )
            if ans != "y":
                return WireOutcome(self.name, "skipped", "TOML block update declined.")

        if dry_run:
            verb = "replace" if state["sibyl_block_present"] else "append"
            return WireOutcome(
                self.name, "dry-run",
                f"Would {verb} [mcp_servers.sibyl_memory] in {self.config_path}",
            )

        backup = self._backup_config()
        try:
            self._write_config_with_sibyl(replace_existing=state["sibyl_block_present"])
        except Exception as e:
            return WireOutcome(
                self.name, "error",
                f"config write failed: {type(e).__name__}: {e}",
                backup_path=backup,
            )
        return WireOutcome(
            self.name, "wired",
            f"Added [mcp_servers.sibyl_memory] to {self.config_path}",
            backup_path=backup,
        )

    def _backup_config(self) -> Optional[Path]:
        if not self.config_path.exists():
            return None
        backup = self.config_path.with_suffix(".toml.bak")
        shutil.copy2(self.config_path, backup)
        return backup

    def _write_config_with_sibyl(self, *, replace_existing: bool) -> None:
        content = self._read()

        if replace_existing:
            existing = self._extract_block(content) or ""
            # Rstrip the existing block to a single newline before splice so
            # we don't accumulate blank lines on repeat replaces.
            stripped = existing.rstrip("\n")
            new_content = content.replace(stripped, self.SIBYL_TOML_BLOCK.rstrip("\n"), 1)
            # Ensure file ends with a newline
            if not new_content.endswith("\n"):
                new_content += "\n"
        else:
            # Append: one blank line of separator if file is non-empty
            if not content:
                new_content = self.SIBYL_TOML_BLOCK
            else:
                # Normalize trailing whitespace to exactly "\n\n"
                trimmed = content.rstrip("\n") + "\n\n"
                new_content = trimmed + self.SIBYL_TOML_BLOCK

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".toml.tmp")
        tmp.write_text(new_content, encoding="utf-8")
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
    if name == "codex" and getattr(args, "codex_config", None):
        kw["config_path"] = args.codex_config
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
            loc = st.get("hermes_home") or st.get("settings_path") or st.get("config_path")
            print(f"  {w.display_name}: {loc}")
        print()
        print(dim("To override detection, point setup at a custom path:"))
        print(f"  {cyan('sibyl setup --hermes-home /custom/path')}")
        print(f"  {cyan('sibyl setup --claude-settings /custom/.claude.json')}")
        print(f"  {cyan('sibyl setup --codex-config /custom/config.toml')}")
        print()
        return 0

    # Detection summary
    print(dim("Detected:"))
    for name, w in detected.items():
        st = w.current_state()
        loc = st.get("hermes_home") or st.get("settings_path") or st.get("config_path")
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
        # State keys per wirer: hermes → memory_provider; claude-code →
        # sibyl_mcp; codex → sibyl_block_present.
        if (
            not args.yes
            and not st.get("wired_with_sibyl")
            and not st.get("memory_provider")
            and not st.get("sibyl_mcp")
            and not st.get("sibyl_block_present")
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
    print()

    if any_wired:
        print(green("Restart your agent(s) to load the new memory provider."))
        print()

    return 0 if all(o.status != "error" for o in outcomes) else 2
