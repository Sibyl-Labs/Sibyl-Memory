"""LangGraph BaseStore backed by Sibyl Memory (SQLite + FTS5, no vector DB)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .store import SibylStore

# Read from the installed distribution so it cannot drift from pyproject.
try:
    __version__ = _pkg_version("sibyl-memory-langgraph")
except PackageNotFoundError:  # pragma: no cover - source-tree dev only
    __version__ = "0.0.0+source"
__all__ = ["SibylStore"]
