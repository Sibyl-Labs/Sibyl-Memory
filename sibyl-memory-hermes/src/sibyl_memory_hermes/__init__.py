"""sibyl-memory-hermes — Sibyl Memory SDK + bundled Hermes plugin payload.

Public exports:
    SibylMemoryProvider     framework-agnostic Sibyl Memory SDK class
    DEFAULT_DB_PATH         ~/.sibyl-memory/memory.db
    DEFAULT_CRED_PATH       ~/.sibyl-memory/credentials.json
    load_credentials        helper for reading the activation credential file
    HermesMemoryError       base exception (re-exports SibylMemoryError)

ARCHITECTURE (v0.3.0+)
======================

This package ships two things:

  1. `SibylMemoryProvider` — a pure-Python SDK class. Framework-agnostic.
     Routes memory operations across the five Sibyl tiers (warm entities,
     hot state, cold journal, reference docs, archive). Can be called
     directly by any orchestration that wants a structured local memory
     backend.

  2. A bundled Hermes plugin payload (`_hermes_plugin/`) — a thin adapter
     implementing Hermes v0.13+ `MemoryProvider` ABC that delegates to
     `SibylMemoryProvider`. Installed into `$HERMES_HOME/plugins/sibyl/`
     by the `sibyl-memory-hermes install-plugin` console script.

Hermes' plugin loader uses filesystem discovery, NOT pip entry points
(verified against `plugins/memory/__init__.py` source 2026-05-17). A pip
install alone won't make Sibyl visible to Hermes — the install-plugin
script bridges that gap.

HERMES INSTALL FLOW
===================

    pip install sibyl-memory-hermes
    sibyl-memory-hermes install-plugin

    # then edit ~/.hermes/config.yaml:
    #   memory:
    #     provider: sibyl

    # (optional) bind your account to lift the 2 MB free-tier cap:
    pip install sibyl-memory-cli
    sibyl init

    hermes                         # sibyl_remember / recall / search / list
                                   # now available to the agent

DIRECT SDK USAGE (any Python orchestration)
===========================================

    from sibyl_memory_hermes import SibylMemoryProvider

    provider = SibylMemoryProvider()         # auto-loads credentials.json
    provider.remember("project", "atlas", {"status": "shipping v2 friday"})
    provider.recall("project", "atlas")
    provider.search("SAML", limit=10)

See https://docs.sibyllabs.org/memory/integrations for the full integration
matrix (Claude Code, Codex, Cursor, Continue, LangChain, LlamaIndex, custom).
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from sibyl_memory_client import SibylMemoryError

from .credentials import (
    DEFAULT_CRED_PATH,
    DEFAULT_DB_PATH,
    Credentials,
    CredentialsNotFoundError,
    load_credentials,
)
from .provider import SibylMemoryProvider

# Single-sourced from installed metadata. Fallback for editable / source-tree
# usage where metadata isn't populated yet.
try:
    __version__ = _pkg_version("sibyl-memory-hermes")
except PackageNotFoundError:  # pragma: no cover - source-tree dev only
    __version__ = "0.0.0+source"

# Backwards-compat alias for callers who want a Hermes-namespaced exception type
HermesMemoryError = SibylMemoryError

__all__ = [
    "SibylMemoryProvider",
    "Credentials",
    "CredentialsNotFoundError",
    "DEFAULT_DB_PATH",
    "DEFAULT_CRED_PATH",
    "load_credentials",
    "HermesMemoryError",
    "__version__",
]
