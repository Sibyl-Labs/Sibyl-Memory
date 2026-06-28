"""
Sibyl Memory — LangGraph Store integration.

Implements LangGraph's BaseStore interface backed by Sibyl Memory's
local SQLite + FTS5 engine. This gives LangGraph agents persistent,
cross-thread memory with full-text search — without needing a
separate vector database.

Usage:
    from sibyl_memory_langgraph import SibylStore

    store = SibylStore()  # uses default ~/.sibyl-memory/memory.db

    # Use as LangGraph's store parameter
    from langgraph.graph import StateGraph
    graph = StateGraph(State, store=store)

    # Or use directly
    store.put(("user", "123"), "name", {"value": "Alice"})
    store.get(("user", "123"), "name")
    store.search(("user",), query="Alice")
"""
from sibyl_memory_langgraph.store import SibylStore

__all__ = ["SibylStore"]
__version__ = "0.1.0"
