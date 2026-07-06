# sibyl-memory-langgraph

A [LangGraph](https://langchain-ai.github.io/langgraph/) `BaseStore` backed by
[Sibyl Memory](https://sibyllabs.org) — durable, long-term, cross-thread memory
for your agents on SQLite + FTS5. No vector database, no embeddings.

```python
from sibyl_memory_langgraph import SibylStore
from langgraph.graph import StateGraph

store = SibylStore()  # ~/.sibyl-memory/memory.db, free tier
graph = StateGraph(State, store=store)
```

Direct use:

```python
store.put(("memories", "u1"), "fact1", {"text": "prefers dark mode"})
item = store.get(("memories", "u1"), "fact1")
hits = store.search(("memories",), query="dark mode")          # lexical, subtree
names = store.list_namespaces(prefix=("memories",))
```

## Mapping

| LangGraph | Sibyl Memory |
|-----------|--------------|
| `namespace` tuple | `category` (`"/".join(namespace)`) |
| `key` | entity `name` |
| `value` dict | entity `body` (JSON) |

## Scope

- Long-term **Store** only (not a checkpointer).
- `search` is **lexical FTS5**, not vector similarity.
- `PutOp.index` and `PutOp.ttl` are accepted and ignored (no embedding index, no TTL).
- Namespace elements must be non-empty and contain no `/` or `..`.

## Identity

`SibylStore()` with no explicit `client` or `tenant_id` binds to the **activated
account**: it reads `~/.sibyl-memory/credentials.json` (written by `sibyl init`,
looked up next to the DB file) and resolves the tenant via the canonical ladder

```
credentials.tenant_id  ->  credentials.account_id  ->  DEFAULT_TENANT
```

`DEFAULT_TENANT` is used only when no credentials are present (un-activated). The
credentials file is symlink-guarded — a symlinked `credentials.json` is treated
as absent rather than followed. Pass `tenant_id="..."` to override, or
`client=my_memory_client` to use that client's tenant as-is.

## Local-first & telemetry

Memory reads and writes are **fully local** — a SQLite database in
`~/.sibyl-memory/`, no network round-trip for any store operation. This adapter
inherits the same posture as the underlying `sibyl-memory-client`:

- **Un-activated (no credentials): zero network.** Nothing leaves the machine.
- **Activated (account credentials present):** the client may send a
  privacy-preserving, **debounced usage heartbeat** — an aggregate operation
  **count** only, never memory content, query text, entity names, or PII beyond
  the `account_id` — plus the cap-verification ping that lets paid tiers exceed
  the free-tier local cap. Both are fire-and-forget and offline-safe.
- **Opt out entirely** with the environment variable `SIBYL_MEMORY_TELEMETRY=0`.

MIT. Built by Sibyl Labs, LLC.
