# sibyl-memory-langgraph

LangGraph [BaseStore](https://langchain-ai.github.io/langgraph/concepts/persistence/#store) integration backed by [Sibyl Memory](https://github.com/Sibyl-Labs/Sibyl-Memory)'s local SQLite + FTS5 engine.

## What this gives you

- **Persistent cross-thread memory** — agents remember across conversations
- **Full-text search** — FTS5-powered search across stored memories
- **Zero infrastructure** — no vector database, no external services, just SQLite
- **Automatic cap management** — free-tier 2 MB, paid uncapped

## Install

```bash
pip install sibyl-memory-langgraph
```

Requires `sibyl-memory-client` to be activated (`sibyl init`).

## Usage

### As LangGraph store parameter

```python
from sibyl_memory_langgraph import SibylStore
from langgraph.graph import StateGraph

store = SibylStore()

graph = StateGraph(State, store=store)
# ... build your graph ...
```

### Direct usage

```python
from sibyl_memory_langgraph import SibylStore

store = SibylStore()

# Store a value
store.put(("user", "123"), "name", {"value": "Alice"})

# Retrieve it
item = store.get(("user", "123"), "name")
print(item.value)  # {"value": "Alice"}

# Search across namespace
items = store.search(("user",), query="Alice")

# Delete
store.delete(("user", "123"), "name")
```

### Custom database path

```python
store = SibylStore(db_path="/path/to/memory.db", tenant_id="my-agent")
```

## How it works

LangGraph's `(namespace, key, value)` model maps to Sibyl Memory's `(category, name, body)`:

| LangGraph | Sibyl Memory |
|-----------|--------------|
| `namespace` | `category` (joined with "/") |
| `key` | `name` |
| `value` | `body` (JSON) |

Search queries are powered by FTS5 full-text search, giving you fast keyword matching without vector embeddings.

## License

MIT
