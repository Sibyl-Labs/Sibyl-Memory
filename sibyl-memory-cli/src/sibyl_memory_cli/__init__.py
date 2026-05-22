"""sibyl-memory-cli — Command-line interface for the Sibyl Memory Plugin.

Entry point: `sibyl` (installed via [project.scripts] in pyproject).

Commands:
    sibyl init          activate the plugin — opens browser SIWE flow, writes ~/.sibyl-memory/credentials.json
    sibyl upgrade       open the upgrade flow — stake $SIBYL or subscribe in USDC
    sibyl status        show current tier, DB size, expiry, account
    sibyl health        provider self-check (mirrors SibylMemoryProvider.health())

Browser pages live at sibyllabs.org/plugin/{activate,upgrade}.
All HTTP calls target https://api.sibyllabs.org/api/plugin/*.
"""
from .cli import main

# Single-sourced from installed metadata so wheel + code can't drift
# (C3 audit fix v0.1.2). Same pattern as sibyl-memory-hermes v0.3.0+.
from importlib.metadata import PackageNotFoundError, version as _pkg_version
try:
    __version__ = _pkg_version("sibyl-memory-cli")
except PackageNotFoundError:  # pragma: no cover - source-tree dev only
    __version__ = "0.0.0+source"

__all__ = ["main", "__version__"]
