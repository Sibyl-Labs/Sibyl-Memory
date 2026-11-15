"""Shared helpers for the 2026-11-13 harness tests (DeepSeek Harness wirer,
doctor harness checks, backups under the plugin home, Codex stale path).

``isolate_path`` gives a test an isolated PATH: a temp bin directory holding
only a fake ``sibyl-memory-mcp``, then /usr/bin and /bin. ``shutil.which`` is
wrapped so the harness binaries (claude, codex, cursor, cursor-agent, hermes,
dsh) are never found, whatever is installed on the machine running the suite:
the real ``claude`` is on PATH on the build box, and the harness section of
doctor would otherwise depend on it. Nothing here runs a harness binary."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

HIDDEN_BINARIES = ("claude", "codex", "cursor", "cursor-agent", "hermes", "dsh")
SCRUBBED_ENV = ("DSH_HOME", "SIBYL_MEMORY_MCP_OUTPUT_TOKENS", "SIBYL_MEMORY_RANKED_MIN_TOKENS",
                "CLAUDE_CONFIG_DIR", "SIBYL_MEMORY_INSTRUCTIONS")

_REAL_WHICH = shutil.which


def isolate_path(monkeypatch, tmp_path: Path) -> Path:
    bindir = tmp_path / "isolated-bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "sibyl-memory-mcp"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")

    def which(cmd, mode=os.F_OK | os.X_OK, path=None):
        if os.path.basename(str(cmd)) in HIDDEN_BINARIES:
            return None
        return _REAL_WHICH(cmd, mode=mode, path=path)

    monkeypatch.setattr(shutil, "which", which)
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    return fake


def backups_dir(harness: str, sub: str = "") -> Path:
    d = Path.home() / ".sibyl-memory" / "backups" / harness
    return d / sub if sub else d


def leftovers(directory: Path) -> list:
    """Temp files or .bak files of ours left in a harness directory."""
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir()
                  if p.name.endswith((".tmp", ".sibyl-tmp", ".bak")))
