# sibyl-memory-client

**Local-first agentic memory SDK. The foundation of the Sibyl Memory Plugin family.**

A small Python library that gives any AI agent durable memory across sessions, stored in a SQLite database on the user's own computer. No round-trip to anyone's cloud. Organized by what kind of thing it is, not one fuzzy similarity bucket.

```bash
pip install sibyl-memory-client
```

## Quickstart

```python
from sibyl_memory_client import MemoryClient

memory = MemoryClient.local("~/.sibyl-memory/memory.db")

# Remember a fact (entity)
memory.set_entity("project", "atlas", {"status": "active", "owner": "jane"})

# Recall it
print(memory.get_entity("project", "atlas"))

# Record what happened (journal)
memory.write_event(acted=["deployed atlas v1.2 to staging"])

# Search across everything
results = memory.search_entities("atlas")
```

## Why this exists

Most agent-memory products store everything on someone else's servers, treat every piece of information the same way, and quietly forget the important things when you need them most. This SDK solves all three:

- **Local-first.** Memory lives in a SQLite database in `~/.sibyl-memory/`. No cloud round-trip for any operation.
- **Organized by kind.** Five separate tiers: state, entities, journal, reference, archive: each recalled the way it should be recalled.
- **Benchmarked.** The Sibyl Memory Plugin (built on this SDK) sits at #2 globally on the LongMemEval Oracle benchmark when paired with Claude Opus 4.6. Methodology open at [blog.sibylcap.com/longmemeval-v2](https://blog.sibylcap.com/longmemeval-v2).

## The five tiers

| Intent | Tier | API |
|---|---|---|
| What you're working on right now | HOT state | `set_state(key, body)` / `get_state(key)` |
| Things the agent knows about | WARM entities | `set_entity(kind, name, body)` / `get_entity` |
| What happened, in time order | COLD journal | `write_event(...)` / `read_events(...)` |
| Documents you look up by name | REFERENCE | `set_reference(key, body)` / `get_reference` |
| Frozen things, kept but out of the way | ARCHIVE | `archive_entity(kind, name)` |
| Search across everything | FTS5 | `search_entities(query)` |

## What's in v0.2.x

- The full five-tier memory model and the API surface above.
- Multi-tenant isolation: one machine can hold separate memory for separate identities.
- Self-learning module (paid-tier): the agent watches your patterns and proposes reusable skills.
- Memory linter (paid-tier): a health check on the local database.
- Tier gating: free-tier callers get clear errors pointing at the upgrade page; paid-tier callers get full access.

## Tier model

Free tier is generous on purpose. You can build real things with it. Paid plans add self-learning, the linter, and remove the 2 MB local cap. Full plan comparison at [docs.sibyllabs.org/memory/tiers](https://docs.sibyllabs.org/memory/tiers).

## Documentation

Full docs: [docs.sibyllabs.org/memory/](https://docs.sibyllabs.org/memory/).
Install guide: [docs.sibyllabs.org/memory/install](https://docs.sibyllabs.org/memory/install).

## License

MIT. Published on PyPI at [pypi.org/project/sibyl-memory-client](https://pypi.org/project/sibyl-memory-client/).
