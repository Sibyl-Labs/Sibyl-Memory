# sibyl-memory-hermes

**Sibyl Memory SDK + bundled Hermes plugin payload. Local-first, SQLite-backed, structured-tier memory for Hermes v0.13+ (and any Python orchestration that wants direct SDK access).**

The package ships two things:
1. **`SibylMemoryProvider`**: a framework-agnostic SDK class. Call it directly from any Python code that wants structured local memory.
2. **A bundled Hermes plugin payload**: a thin adapter implementing Hermes v0.13's `MemoryProvider` ABC. Installed into `$HERMES_HOME/plugins/sibyl/` by the `sibyl-memory-hermes install-plugin` console script.

Memory content lives on the user's own machine, never on our servers. Built on [`sibyl-memory-client`](https://pypi.org/project/sibyl-memory-client/), the SDK foundation.

## Install (Hermes path)

Hermes' loader uses filesystem discovery, NOT pip entry points. A pip install alone won't make Sibyl visible to Hermes: the `install-plugin` console script bridges the gap.

```bash
pip install sibyl-memory-hermes
sibyl-memory-hermes install-plugin
```

Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is not supported.

Then wire it (needs `sibyl-memory-cli`):

```bash
pip install sibyl-memory-cli
sibyl setup hermes
```

`sibyl setup hermes` sets `memory.provider: sibyl` in `~/.hermes/config.yaml` for you. Restart Hermes. Four tools become available to the agent:

- `sibyl_remember(category, name, body)`: store a structured fact
- `sibyl_recall(category, name)`: look up a known fact
- `sibyl_search(query)`. FTS5 search across **all four tiers** (entities, state, journal, reference); hits are tier-tagged
- `sibyl_list(category?, status?)`: browse what's remembered

Required: sign in once (the memory tools need an account):

```bash
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

## Environment overrides

Read by the bundled Hermes adapter only. `SibylMemoryProvider` used directly (above) does not read it; pass `tenant_id=` instead.

| Var | Default | What it does |
|-----|---------|--------------|
| `SIBYL_TENANT_ID` | unset | Non-secret tenant override. When set to a non-empty value it becomes the active tenant and wins over the tenant in `credentials.json`. Blank or unset leaves tenant resolution untouched. This is an identifier, not a secret. |

Precedence: explicit `SIBYL_TENANT_ID` > `credentials.json` (`tenant_id` then `account_id`) > the shared default tenant. Set it when one machine or container needs to target a specific tenant without editing the credential file.

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

The Hermes plugin is implemented by a bundled adapter at `_hermes_plugin/adapter.py`. The adapter is copied into `$HERMES_HOME/plugins/sibyl/` by the `install-plugin` console script and is what Hermes' filesystem loader picks up. The adapter implements Hermes v0.13's `MemoryProvider` ABC and delegates every call to `SibylMemoryProvider`. Verified against `hermes-agent` through **0.19.0** (current): the adapter loads via the live ABC and instantiates cleanly.

The SDK class itself (`SibylMemoryProvider`) is framework-agnostic: it does not inherit from any framework ABC. This is the v0.3.0 architecture shift. v0.2.x and earlier attempted soft-inheritance via a broken import path; that path was removed and the adapter pattern replaced it.

## Activation (required since 0.5.0)

Run `sibyl init` (from [`sibyl-memory-cli`](https://pypi.org/project/sibyl-memory-cli/)) once. It writes `~/.sibyl-memory/credentials.json` and this computer's signed access file `~/.sibyl-memory/entitlement.json` after you sign in. The provider auto-detects both on construction.

Without an account the provider and the Hermes plugin still load (Hermes never crashes on it), but memory does not work until you sign in:

- the agent's `sibyl_*` tool calls return a tool error: "Sibyl Memory is not signed in on this computer. Run sibyl init in a terminal, then try again. Your local memory is kept.";
- turn syncing and prefetch are skipped quietly, with ONE warning in the log per process ("Sibyl memory is signed out: run sibyl init. Memory tools return an error until then.");
- direct `SibylMemoryProvider` methods raise `sibyl_memory_client.EntitlementError`.

`SIBYL_TENANT_ID` (and the `tenant_id=` argument) choose the tenant only; they never stand in for an account. Your local memory is never deleted or changed by the account gate, and `sibyl memory list|search|recall` reads it without an account.

For tests and internal tooling, inject the test gate from the client instead of relying on a missing credentials file:

```python
from sibyl_memory_client import MemoryClient
from sibyl_memory_client.testing import NullEntitlementGate

client = MemoryClient.local("/tmp/test-memory.db", tenant_id="test-user",
                            entitlement_gate=NullEntitlementGate())
```

> **Privacy disclosure.** Your memory content never leaves the machine. The plugin talks to `api.sibyllabs.org` for these things only: activation (`sibyl init`); a daily account check-in that renews this computer's access (account id, this computer's sign-in token, plugin version, OS family; no memory content); a size check when a free account nears its cap (account id, sign-in token, the database's byte size and the proposed delta); and, unless you opt out with `SIBYL_MEMORY_TELEMETRY=0`, an aggregate count of memory operations. The size check and the count also carry this computer's signed access token (account id, tier, cap, device hash, account flags; no personal data).

## Free tier

- 5 MB local soft cap (with server-authoritative tier verification at the cap boundary)
- Single device
- All five tiers (HOT/WARM/COLD/REFERENCE/ARCHIVE)
- FTS5 full-text search across entities + state + reference + journal
- Multi-tenant isolation
- Per-profile memory isolation: each Hermes profile gets its own DB automatically (`<HERMES_HOME>/sibyl/profiles/<name>/memory.db`), so specialist agents don't share or leak memory

Paid tiers (Pro, Elite) unlock self-learning, the memory check-up, and no cap. See [docs.sibyllabs.org/memory/tiers](https://docs.sibyllabs.org/memory/tiers).

## Documentation

- Full docs: [docs.sibyllabs.org/memory/](https://docs.sibyllabs.org/memory/)
- Hermes integration guide: [docs.sibyllabs.org/memory/integrations#hermes](https://docs.sibyllabs.org/memory/integrations#hermes)
- Install guide: [docs.sibyllabs.org/memory/install](https://docs.sibyllabs.org/memory/install)

## Save and debloat

**Save.** At the end of a session, or after a big block of work, your agent writes what happened
into Sibyl Memory through the plugin's own tools: the decisions made and why (journal), the
people, projects and products touched (entities), what to pick up next time and the open
warnings (session state), the ranked priorities, and it closes what died (archive). A delta
marker means a second save in the same day captures only what changed. It may offer to turn a
repeated pattern into a reusable skill; that is offered, never forced. Nothing in a save leaves
your machine.

**Debloat.** Sibyl measures the files your agent loads on every session start (`CLAUDE.md`,
`AGENTS.md`, `.cursorrules` and the rest of the scan set), deterministically (`chars/4`,
labeled `MEASURED~`), and when they are heavy, trims the accreted history out of them without
losing a byte: the history moves out and a one-line pointer stays. On the free plan the history
moves to a sibling `*-history` file. On Pro it is stored in Sibyl Memory as a reference document,
so it stays searchable, and the measurement and trim run by themselves under a policy you set
once.

| | Free (activated account) | Pro |
|---|---|---|
| The procedures in `~/.sibyl-memory/skills/` (`SAVE.md`, `DEBLOAT.md`), `/sibyl-save` and `/sibyl-debloat` in Claude Code, skills in Hermes and Codex, a block in Codex's `AGENTS.md`, the `save` and `debloat` MCP prompts | yes | yes |
| `sibyl save`, `sibyl debloat` (measure, plan, apply, undo) | yes | yes |
| Reminders (one quiet line; `sibyl reminders off`) | yes | replaced by a one-line report of what ran |
| Where trimmed history goes | a sibling `*-history` file | Sibyl Memory, plus a byte-verified backup |
| Automatic debloat | no | yes: `propose` by default; a file is trimmed by itself only after you run `sibyl debloat auto <file>` (that file at its path, never the same name elsewhere) |
| Automatic save by active time: once early (after 5 memory writes or a compaction), then about every 60 minutes of active work, only when there is something to save, at most 8 a session and 12 a day (`sibyl autosave every <30-240>` or `sibyl autosave off`). Claude Code: the Stop hook asks your agent to run the save procedure headless (a scripted `claude -p` run is never asked); Cursor, Codex and other MCP hosts: a `notice` on the next tool result; Hermes: a note in the next prefetch. A session capsule (counts only) at compaction and session end. Each automatic save is one extra model turn that reads and writes memory | no | yes |
| Skill proposals: Sibyl finds repeated work in your journal (locally, no network), names up to 3 at a session start at most once a day, and creates nothing without a yes (`/sibyl-save curate`, or `sibyl skills proposals accept <id>`, which writes the skill for Claude Code, Codex and Hermes) | no (`/sibyl-save` still offers skills when you run it) | yes |

The procedures are vendored, with attribution, from the public MIT repos
[sibyl-save](https://github.com/Sibyl-Labs/sibyl-save) and
[debloat](https://github.com/Sibyl-Labs/debloat); both stay standalone and account-free.

Uninstalling: run `sibyl skills uninstall` first. It removes the Claude Code hooks in
`~/.claude/settings.json`, the skill shims and the Codex block (files you edited are kept); then
`pip uninstall` the packages. The hooks call this package's `sibyl` command: if the package goes
first they exit quietly, but they stay in your settings until you reinstall and run
`sibyl skills uninstall`, or delete the five `sibyl hook` entries by hand.

Privacy: the lifecycle reads your instruction files to measure and trim them locally; no file
content and no conversation content ever leaves your machine.

Forks: a gate-stripped fork can run the lifecycle code locally (MIT allows it). It gains nothing
hosted, and its trims land in a store the official path cannot recognize as Pro. No file, flag
or environment variable turns the automation on for a free account.

In this adapter: the system prompt block coaches the save procedure (on Pro: run it at natural
end points without asking); `install-plugin` adds `sibyl-save` and `sibyl-debloat` to
`$HERMES_HOME/skills/`; on Pro the adapter runs the debloat plan at start and records a session
capsule at compaction and at shutdown (counts only, no conversation text).

## License

MIT. Package on PyPI: [pypi.org/project/sibyl-memory-hermes](https://pypi.org/project/sibyl-memory-hermes/).

## Citation

The Sibyl Memory Plugin holds #2 globally on the LongMemEval Oracle benchmark. The benchmark methodology and report are at [blog.sibylcap.com/longmemeval-v2](https://blog.sibylcap.com/longmemeval-v2).
