"""`sibyl-memory-hermes install-plugin`: installs the Sibyl adapter into Hermes.

Hermes' loader does NOT use pip entry points (verified against
plugins/memory/__init__.py source 2026-05-17). It scans the filesystem
for `__init__.py` files under two locations:

  - bundled:  <site-packages>/plugins/memory/<name>/__init__.py
  - user:     $HERMES_HOME/plugins/<name>/__init__.py       (note: no /memory/)

After `pip install sibyl-memory-hermes`, the user runs this script to drop
the bundled adapter into their HERMES_HOME. They then activate by setting
`memory.provider: sibyl` in their config.yaml.

Usage:
  sibyl-memory-hermes install-plugin
  sibyl-memory-hermes install-plugin --hermes-home /custom/path
  sibyl-memory-hermes install-plugin --force
  sibyl-memory-hermes install-plugin --dry-run
  sibyl-memory-hermes uninstall-plugin

v0.3.1 hardening (audit SEC-5):
  --force will not rmtree a target directory unless it looks like an
  actual prior Sibyl install (existing plugin.yaml with name: sibyl).
  Prevents accidental destruction of arbitrary user-writable trees
  via misconfigured HERMES_HOME.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

from . import _aesthetic as a
from ._banner import print_banner


def _hermes_home(override: str | None = None) -> Path:
    """Resolve the active HERMES_HOME directory.

    Precedence: CLI flag → $HERMES_HOME env var → ~/.hermes default.
    """
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".hermes"


def _plugin_dest(hermes_home: Path) -> Path:
    """User-install location for a Hermes memory provider plugin.

    Note: asymmetric vs the bundled location. Bundled providers live at
    <site-packages>/plugins/memory/<name>/, but user plugins live at
    $HERMES_HOME/plugins/<name>/ without the /memory/ segment. Confirmed
    against plugins/memory/__init__.py loader source.
    """
    return hermes_home / "plugins" / "sibyl"


def _payload_files() -> list[tuple[str, str]]:
    """Files to copy: (source_name_in_package, dest_name_in_plugin_dir).

    adapter.py is renamed to __init__.py at destination so Hermes' filesystem
    discovery (which looks for `<plugins>/<name>/__init__.py`) picks it up.
    v0.3.1: adapter.py imports the Hermes ABC under a try/except guard, so
    the source module is now importable in test / dry-run contexts where
    hermes-agent isn't installed.
    """
    return [
        ("adapter.py", "__init__.py"),
        ("plugin.yaml", "plugin.yaml"),
    ]


def _read_payload(filename: str) -> bytes:
    """Read a bundled file from the _hermes_plugin package."""
    return (resources.files("sibyl_memory_hermes._hermes_plugin") / filename).read_bytes()


def _looks_like_sibyl_install(dest: Path) -> bool:
    """SEC-5 sentinel check: dest must contain a recognizable prior Sibyl
    install before we'll rmtree it.

    Recognizes the install by `plugin.yaml` with `name: sibyl` in it (the
    canonical marker we ship). If the directory exists but doesn't match,
    we refuse --force rather than destroy possibly-unrelated content."""
    yaml_path = dest / "plugin.yaml"
    if not yaml_path.exists() or not yaml_path.is_file():
        return False
    try:
        content = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Loose match: yaml has `name: sibyl` somewhere near the top
    for line in content.splitlines()[:10]:
        stripped = line.strip().lower()
        if stripped.startswith("name:") and "sibyl" in stripped:
            return True
    return False


def install(hermes_home: Path, force: bool, dry_run: bool) -> int:
    dest = _plugin_dest(hermes_home)

    # ── HEAVY: install moment. Full SIBYL banner + section header. ──
    print_banner()
    print(a.section_header("install-plugin",
                          subtitle="hermes memory provider · drops adapter at $HERMES_HOME/plugins/sibyl/"))
    print()
    print(a.kv("Hermes home", str(hermes_home)))
    print(a.kv("Plugin dest", str(dest)))
    print()

    # SEC-5: refuse to follow symlinks for the dest path.
    if dest.exists() and dest.is_symlink():
        print(a.err_line(f"Refused: {dest} is a symlink."))
        print(a.dim("  Sibyl will not install through symlinks. Remove the symlink and rerun."))
        return 3

    if dest.exists() and any(dest.iterdir()):
        if not force:
            print(a.err_line(f"Refused: {dest} already exists and is not empty."))
            print(a.dim("  Use --force to overwrite. Existing files will be replaced."))
            return 2
        # SEC-5: sentinel check
        if not _looks_like_sibyl_install(dest):
            print(a.err_line(f"Refused: {dest} is not empty but does not contain a prior Sibyl install."))
            print(a.dim(f"  No plugin.yaml with `name: sibyl` found. Sibyl will not rmtree directories"))
            print(a.dim(f"  it does not recognize, even with --force. Remove manually if intentional."))
            return 4
        if not dry_run:
            print(a.warn_line(f"Removing existing plugin at {dest}"))
            shutil.rmtree(dest)
        else:
            print(a.dim(f"  [dry-run] would remove existing plugin at {dest}"))

    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    print(a.eyebrow("writing payload"))
    for src_name, dest_name in _payload_files():
        bytes_in = _read_payload(src_name)
        out = dest / dest_name
        if dry_run:
            print(f"  {a.dim('[dry-run]')} would write {a.color(str(out), a.INK)}  {a.dim(f'({len(bytes_in)} bytes)')}")
        else:
            out.write_bytes(bytes_in)
            print(f"  {a.ok(a.GLYPH_OK)} {a.color(str(out), a.INK)}  {a.dim(f'({len(bytes_in)} bytes)')}")

    if dry_run:
        print()
        print(a.warn_line("Dry run complete. No files modified."))
        return 0

    print()
    print(a.success_line("Plugin installed."))
    print()
    print(a.section_header("next steps", subtitle="three to go · then your agent has memory"))
    print()

    # Step 1: activate in config.yaml
    print(f"  {a.chip('1', palette='accent')}  {a.bold('Activate Sibyl in your Hermes config')}")
    print(f"      {a.color(str(hermes_home / 'config.yaml'), a.INK)}")
    print()
    print(f"        {a.color('memory:', a.ACCENT)}")
    print(f"          {a.color('provider:', a.ACCENT)} {a.color('sibyl', a.INK)}")
    print()

    # Step 2: bind account
    print(f"  {a.chip('2', palette='accent')}  {a.bold('Bind your account')}  {a.dim('(optional · lifts the 2 MB free-tier cap)')}")
    print(f"      {a.color('sibyl init', a.INK)}")
    print(a.dim("      three paths: desktop wallet · email + code · mobile wallet"))
    print(a.dim("      defer if you want: the plugin runs on a local default tenant without it"))
    print()

    # Step 3: start hermes
    print(f"  {a.chip('3', palette='accent')}  {a.bold('Start Hermes: your agent now has memory')}")
    print(a.dim("      tools available to the agent:"))
    for tool in ("sibyl_remember", "sibyl_recall", "sibyl_search", "sibyl_list"):
        print(f"        {a.color(a.GLYPH_BULLET, a.PULSE)} {a.color(tool, a.INK)}")
    print()

    print(a.divider(60))
    print(f"  {a.dim('uninstall later:')} {a.color('sibyl-memory-hermes uninstall-plugin', a.INK)}")
    print(f"  {a.dim('docs:')}             {a.color('docs.sibyllabs.org/memory/integrations', a.INK)}")
    print()
    return 0


def uninstall(hermes_home: Path, dry_run: bool) -> int:
    dest = _plugin_dest(hermes_home)
    # ── HEAVY: removal is also ceremonial: banner + section header.
    print_banner()
    print(a.section_header("uninstall-plugin",
                          subtitle="remove sibyl from this hermes install"))
    print()
    print(a.kv("Hermes home", str(hermes_home)))
    print(a.kv("Plugin dest", str(dest)))
    print()
    if not dest.exists():
        print(a.warn_line("Nothing to uninstall: plugin directory does not exist."))
        return 0
    if dest.is_symlink():
        print(a.err_line(f"Refused: {dest} is a symlink."))
        print(a.dim("  Sibyl will not rmtree through symlinks."))
        return 3
    if not _looks_like_sibyl_install(dest):
        print(a.err_line(f"Refused: {dest} is not recognized as a Sibyl install."))
        print(a.dim("  No plugin.yaml with `name: sibyl`. Remove manually if intentional."))
        return 4
    if dry_run:
        print(a.dim(f"  [dry-run] would remove {dest} (recursively)"))
        return 0
    shutil.rmtree(dest)
    print(a.success_line(f"Removed {dest}"))
    print()
    print(a.dim(f"  remember to remove `memory.provider: sibyl` from"))
    print(a.dim(f"  {hermes_home / 'config.yaml'}"))
    print(a.dim("  if it's still set, or Hermes will warn on startup."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sibyl-memory-hermes",
        description="Install the Sibyl memory provider plugin into Hermes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install-plugin", help="Install the Sibyl plugin into HERMES_HOME.")
    p_install.add_argument("--hermes-home", help="Override HERMES_HOME (defaults to env var or ~/.hermes).")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing Sibyl plugin directory (refuses non-Sibyl content).")
    p_install.add_argument("--dry-run", action="store_true", help="Show what would happen without writing.")

    p_uninstall = sub.add_parser("uninstall-plugin", help="Remove the Sibyl plugin from HERMES_HOME.")
    p_uninstall.add_argument("--hermes-home", help="Override HERMES_HOME (defaults to env var or ~/.hermes).")
    p_uninstall.add_argument("--dry-run", action="store_true", help="Show what would happen without writing.")

    args = parser.parse_args(argv)
    hermes_home = _hermes_home(args.hermes_home)

    if args.cmd == "install-plugin":
        return install(hermes_home, force=args.force, dry_run=args.dry_run)
    if args.cmd == "uninstall-plugin":
        return uninstall(hermes_home, dry_run=args.dry_run)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
