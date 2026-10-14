"""sibyl-memory-cli: command-line interface for the Sibyl Memory Plugin.

Entry point: `sibyl` (installed via [project.scripts] in pyproject).
`sibyl --help` lists every command; a few to start with:

    sibyl init          sign in (email + code, or a wallet) and write
                        ~/.sibyl-memory/credentials.json
    sibyl setup         wire Claude Code, Codex and Hermes
    sibyl status        show current tier, DB size, expiry, account
    sibyl upgrade       open the upgrade flow: Pro by card or USDC, or by
                        staking $SIBYL

Account calls go to https://api.sibyllabs.org (SIBYL_API_BASE overrides);
`sibyl update` also asks pypi.org for the latest releases.
"""
# Lazy (PEP 562): `main` and `__version__` resolve on first access, so the
# `sibyl hook <event>` fast path (_entry.py -> hooks.py) imports neither the
# whole CLI nor importlib.metadata. `from sibyl_memory_cli import main` and
# `sibyl_memory_cli.__version__` work as before.


def __getattr__(name: str):
    if name == "main":
        from .cli import main
        return main
    if name == "__version__":
        # Single-sourced from installed metadata so wheel + code can't drift
        # (C3 audit fix v0.1.2). Same pattern as sibyl-memory-hermes v0.3.0+.
        from importlib.metadata import PackageNotFoundError, version as _pkg_version
        try:
            v = _pkg_version("sibyl-memory-cli")
        except PackageNotFoundError:  # pragma: no cover - source-tree dev only
            v = "0.0.0+source"
        globals()["__version__"] = v
        return v
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main", "__version__"]
