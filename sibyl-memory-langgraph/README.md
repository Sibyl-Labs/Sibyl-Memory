# sibyl-memory-langgraph

[![PyPI](https://img.shields.io/pypi/v/sibyl-memory-langgraph?label=pypi&color=8a6a2a)](https://pypi.org/project/sibyl-memory-langgraph/)
[![License: MIT](https://img.shields.io/badge/license-MIT-15110a.svg)](./LICENSE)

A [LangGraph](https://langchain-ai.github.io/langgraph/) `BaseStore` backed by
[Sibyl Memory](https://sibyllabs.org) — durable, long-term, cross-thread memory
for your agents on SQLite + FTS5. No vector database, no embeddings.

## Install

```bash
pip install sibyl-memory-langgraph
```

Part of the [Sibyl Memory](https://github.com/Sibyl-Labs/Sibyl-Memory) plugin
family. See the [CHANGELOG](./CHANGELOG.md) for release notes.

```python
from sibyl_memory_langgraph import SibylStore
from langgraph.graph import StateGraph

store = SibylStore()  # ~/.sibyl-memory/memory.db; needs `sibyl init` once
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

`DEFAULT_TENANT` is a constant for tests and for data written before accounts
existed; that data moves into the account tenant the first time a signed-in
client uses the store (client 0.9.0, local only), or with `sibyl memory adopt`. The credentials file is symlink-guarded: a symlinked `credentials.json`
is treated as absent rather than followed. Pass `tenant_id="..."` to override, or
`client=my_memory_client` to use that client's tenant as-is.

## Account required (0.3.0)

Every store operation needs an activated account (`sibyl init`) holding a valid,
server-signed entitlement for this computer. Without one, `SibylStore()` still
constructs, but `put`, `get`, `search`, `delete`, `list_namespaces` (and
`batch` / `abatch`) raise `sibyl_memory_client.EntitlementError`. A framework
store propagates it, and LangGraph surfaces it to the graph. `e.code` says why
(`no_account`, `signed_out`, `expired_offline`, ...) and the message names the
one action (usually `sibyl init`). `tenant_id=` (including `DEFAULT_TENANT`)
chooses the tenant, never the access. The gate reads the `credentials.json`
next to the DB file when there is one, else `~/.sibyl-memory/credentials.json`.

For an agent driving the graph, the exception carries the handoff (sibyl-memory-client
0.9.0): `e.next` is the exact command to run next (`sibyl init`, `sibyl refresh`,
`sibyl init --force`, ...; or `done`), and `e.human_action` is the sentence to relay to
the person, or `None` when nobody has to act. An uncaught traceback shows the same two
lines at the end of `str(e)` (`ask your human: ...` when a person must act, then
`next: ...`, always last); `e.summary` is the message without them.

```python
from sibyl_memory_client import EntitlementError

try:
    store.put(("notes",), "k", {"v": 1})
except EntitlementError as e:
    if e.human_action:
        print("ask your human:", e.human_action)
    print("next:", e.next)
```

Embedding or testing: pass your own client, for example one built with the
client's test gate:

```python
from sibyl_memory_client import MemoryClient
from sibyl_memory_client.testing import NullEntitlementGate

store = SibylStore(client=MemoryClient.local("/tmp/t.db", entitlement_gate=NullEntitlementGate()))
```

## Local-first & telemetry

Memory reads and writes are **fully local**: a SQLite database in
`~/.sibyl-memory/`, and memory content never leaves the machine. This adapter
inherits the posture of the underlying `sibyl-memory-client`, which talks to
`api.sibyllabs.org` for these things only:

- activation (`sibyl init`);
- a daily account check-in that renews this computer's access (account id,
  this computer's sign-in token, plugin version, OS family; no memory content);
- the size check when a free account nears its cap (account id, sign-in
  token, the database's byte size and the proposed delta, and this computer's
  signed access token: account id, tier, cap, device hash, account flags; no
  personal data);
- a debounced usage heartbeat: an aggregate operation **count** only, never
  memory content, query text or entity names, sent with this computer's
  signed access token (the same fields as above; no personal data). Opt out
  with `SIBYL_MEMORY_TELEMETRY=0`.

Your local data is always readable with `sibyl memory list|search|recall`,
with or without an account.

MIT. Built by Sibyl Labs, LLC.

## Save and debloat

The plugin's save and debloat procedures belong to agent sessions (the MCP server, the Hermes
adapter, the Claude Code hooks), not to a framework store, so this package has no lifecycle of
its own. A LangGraph agent that also uses the MCP server gets the `save` and `debloat` prompts
there. The Pro automatic save (by active time) and the skill proposals ride the MCP server and
the Hermes adapter too; `SibylStore` never asks for a save.

Uninstalling the CLI: run `sibyl skills uninstall` before `pip uninstall sibyl-memory-cli`; it
removes the Claude Code hooks that call the `sibyl` command (they exit quietly once it is gone,
but stay in `~/.claude/settings.json` until removed).
