# sibyl-memory-hermes

**Sibyl Memory SDK + bundled Hermes plugin payload. Local-first, SQLite-backed, structured-tier memory for Hermes v0.13+ (and any Python orchestration that wants direct SDK access).**

The package ships two things:
1. **`SibylMemoryProvider`** — a framework-agnostic SDK class. Call it directly from any Python code that wants structured local memory.
2. **A bundled Hermes plugin payload** — a thin adapter implementing Hermes v0.13's `MemoryProvider` ABC. Installed into `$HERMES_HOME/plugins/sibyl/` by the `sibyl-memory-hermes install-plugin` console script.

Memory content lives on the user's own machine, never on our servers. Built on [`sibyl-memory-client`](https://pypi.org/project/sibyl-memory-client/), the SDK foundation.

## Install (Hermes path)

Hermes' loader uses filesystem discovery, NOT pip entry points. A pip install alone won't make Sibyl visible to Hermes — the `install-plugin` console script bridges the gap.

```bash
pip install sibyl-memory-hermes
sibyl-memory-hermes install-plugin
```

Then edit `~/.hermes/config.yaml`:

```yaml
memory:
  provider: sibyl
```

Restart Hermes. Four tools become available to the agent:

- `sibyl_remember(category, name, body)` — store a structured fact
- `sibyl_recall(category, name)` — look up a known fact
- `sibyl_search(query)` — FTS5 search across **all four tiers** (entities, state, journal, reference); hits are tier-tagged
- `sibyl_list(category?, status?)` — browse what's remembered

Optional: lift the 2 MB free-tier cap by binding your account:

```bash
pip install sibyl-memory-cli
sibyl init
```

## Direct SDK use (any Python orchestration)

```python
from sibyl_memory_hermes import SibylMemoryProvider

provider = SibylMemoryProvider()         # auto-loads ~/.sibyl-memory/credentials.json
provider.remember("project", "atlas", {"status": "shipping v2 friday"})
provider.recall("project", "atlas")      # → {id, tenant_id, category, name, body, ...}
provider.set_state("active_branch", {"name": "v0.3.1"})
provider.save_context(
    inputs={"user": "what changed in v0.3.1?"},
    outputs={"assistant": "..."},
)
provider.search("v0.3.1")                # FTS5 across entities + state + reference + journal
```

## Why "local-first"?

Mem0, Zep, Honcho, and most other agent-memory products centralize user context on their servers. The Sibyl Memory Plugin keeps the data on the user's disk. Our cloud schema has no memory-content tables. Even with admin DB access we cannot read what users have written. That's the difference between *"we promise we don't"* and *"we structurally can't."*

| | Sibyl Memory Plugin | Typical hosted memory |
|---|---|---|
| Memory content lives | on user's disk | on vendor's servers |
| Query latency | local SQLite (sub-ms) | round-trip + vector search |
| Privacy claim | structurally enforced | policy-only |
| Free-tier cost to vendor | near-zero | scales with users |

## Architecture: five tiers, not one bucket

The provider routes operations onto the appropriate memory tier instead of dumping everything into a single vector store:

| Intent | Tier | Storage call |
|---|---|---|
| save the conversation turn | COLD journal | `save_context(inputs, outputs)` |
| remember a fact | WARM entity | `remember(category, name, body)` |
| current state | HOT state | `set_state(key, body)` |
| lookup a runbook | REFERENCE | `set_reference(key, body)` |
| archive stale entity | ARCHIVE | `archive(category, name)` |
| search by content | FTS5 cross-tier | `search(query)` → tier-tagged hits |

Different intents, different lookups, no embedding model required. FTS5 covers full-text search out of the box.

## Hermes contract

The Hermes plugin is implemented by a bundled adapter at `_hermes_plugin/adapter.py`. The adapter is copied into `$HERMES_HOME/plugins/sibyl/` by the `install-plugin` console script and is what Hermes' filesystem loader picks up. The adapter implements Hermes v0.13's `MemoryProvider` ABC and delegates every call to `SibylMemoryProvider`.

The SDK class itself (`SibylMemoryProvider`) is framework-agnostic — it does not inherit from any framework ABC. This is the v0.3.0 architecture shift. v0.2.x and earlier attempted soft-inheritance via a broken import path; that path was removed and the adapter pattern replaced it.

## Activation

Most users get here via the `sibyl init` CLI (from [`sibyl-memory-cli`](https://pypi.org/project/sibyl-memory-cli/)), which writes `~/.sibyl-memory/credentials.json` after browser authentication. The provider auto-detects this file on construction.

For pre-activation use (tests, internal tooling):

```python
from sibyl_memory_hermes import SibylMemoryProvider

provider = SibylMemoryProvider(
    db_path="/tmp/test-memory.db",
    tenant_id="test-user",
    autoload_credentials=False,
)
```

## Free tier

- 2 MB local soft cap (with server-authoritative tier verification at the cap boundary)
- Single device
- All five tiers (HOT/WARM/COLD/REFERENCE/ARCHIVE)
- FTS5 full-text search across entities + state + reference + journal
- Multi-tenant isolation

Paid tiers (Stake, Sync, Lifetime, Enterprise) unlock self-learning, the memory check-up, no cap, and (in build) cross-device encrypted sync. See [docs.sibyllabs.org/memory/tiers](https://docs.sibyllabs.org/memory/tiers).

## Documentation

- Full docs: [docs.sibyllabs.org/memory/](https://docs.sibyllabs.org/memory/)
- Hermes integration guide: [docs.sibyllabs.org/memory/integrations#hermes](https://docs.sibyllabs.org/memory/integrations#hermes)
- Install guide: [docs.sibyllabs.org/memory/install](https://docs.sibyllabs.org/memory/install)

## License

MIT. Package on PyPI: [pypi.org/project/sibyl-memory-hermes](https://pypi.org/project/sibyl-memory-hermes/).

## Citation

The Sibyl Memory Plugin holds #2 globally on the LongMemEval Oracle benchmark. The benchmark methodology and report are at [blog.sibylcap.com/longmemeval-v2](https://blog.sibylcap.com/longmemeval-v2).
