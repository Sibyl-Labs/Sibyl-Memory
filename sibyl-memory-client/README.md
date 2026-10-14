# sibyl-memory-client

**Local-first agentic memory SDK. The foundation of the Sibyl Memory Plugin family.**

A small Python library that gives any AI agent durable memory across sessions, stored in a SQLite database on the user's own computer. No round-trip to anyone's cloud. Organized by what kind of thing it is, not one fuzzy similarity bucket.

```bash
pip install sibyl-memory-client
```

Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows through WSL2. Native Windows is not supported.

## Quickstart

Sign in once (an account is required since 0.9.0; see below):

```bash
pip install sibyl-memory-cli
sibyl init
```

```python
import json
from pathlib import Path

from sibyl_memory_client import MemoryClient

# Write into your account's tenant, the one the MCP server, Hermes and
# LangGraph read (credentials.json is written by `sibyl init`).
creds = json.loads(Path("~/.sibyl-memory/credentials.json").expanduser().read_text())
memory = MemoryClient.local("~/.sibyl-memory/memory.db",
                            tenant_id=creds.get("tenant_id") or creds["account_id"])

# Remember a fact (entity)
memory.set_entity("project", "atlas", {"status": "active", "owner": "jane"})

# Recall it
print(memory.get_entity("project", "atlas"))

# Record what happened (journal)
memory.write_event(acted=["deployed atlas v1.2 to staging"])

# Search across everything
results = memory.search_entities("atlas")
```

Without `tenant_id`, `MemoryClient.local(...)` writes to the shared default tenant. Rows there move into your account only once per account per store (see "Account required" below); anything written to the default tenant after that stays outside the account the other surfaces read until you run `sibyl memory adopt`.

## Why this exists

Most agent-memory products store everything on someone else's servers, treat every piece of information the same way, and quietly forget the important things when you need them most. This SDK solves all three:

- **Local-first.** Memory lives in a SQLite database in `~/.sibyl-memory/`. Memory content never leaves the machine.
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

### Forgetting vs deleting

`archive_entity(kind, name)` is **recoverable**: it moves the entity into the
`archived_entities` table (stored plaintext at rest), out of the active set but
still on disk. `delete_entity(kind, name)` is a **permanent hard delete** that
removes the row outright. Use archive to declutter, delete to truly forget.

## What's in it

- The full five-tier memory model and the API surface above.
- Cross-tier FTS5 search across entities, state, reference, and journal tiers.
- Multi-tenant isolation: one machine can hold separate memory for separate identities.
- Self-learning module (paid-tier): the agent watches your patterns and proposes reusable skills.
- Memory linter (paid-tier): a health check on the local database.
- Tier gating: free-tier callers get clear errors pointing at the upgrade page; paid-tier callers get full access.
- Uniform millisecond timestamp precision across all tiers (v0.4.3).

## Account required (0.9.0)

Every memory operation needs an activated account (`sibyl init`) holding a valid, server-signed entitlement for this computer. Without one, operations raise `EntitlementError` whose message names the one thing to do (usually `sibyl init`); `e.code` says why (`no_account`, `signed_out`, `expired_offline`, `setup_incomplete`, `under_review`, `account_deleted`, `invalid_entitlement`, `wrong_device`, `policy_read_only`, `policy_none`, `client_too_old`). Constructing a `MemoryClient` never raises for this.

- The entitlement is an Ed25519-signed token the client verifies offline against a public key pinned in the package. It renews itself about once a day and keeps working offline for up to 14 days.
- Revoking this computer (account page, `sibyl devices revoke`, `sibyl logout`) or deleting the account takes effect on its next check-in.
- A computer that holds credentials but has never received its signed access file (an upgrade from before 0.9.0) needs ONE successful check-in first; until then operations raise `setup_incomplete` ("connect to the internet once to finish setup"). There is no offline grace before that first check-in.
- Your local data is never deleted by the gate. `sibyl memory list|search|recall` reads it without an account.
- Memory written before sign-in (the pre-account default tenant) moves into the account the first time a signed-in client uses this store: one local SQLite transaction, nothing deleted, nothing sent anywhere, a key the account already has kept under `<key> (before sign-in)`, an audit row in the store. It happens automatically only if the store has never held another account; otherwise `sibyl memory adopt` (or `MemoryClient.adopt_pre_signin_memory(confirm_other_accounts=True)`) does it after a confirmation.
- There is no environment variable that turns the gate off. Test suites inject `sibyl_memory_client.testing.NullEntitlementGate()` at construction (`MemoryClient.local(path, entitlement_gate=NullEntitlementGate())`).

> **Privacy disclosure.** Your memory content never leaves the machine. The plugin talks to `api.sibyllabs.org` for these things only: activation (`sibyl init`); a daily account check-in that renews this computer's access (account id, this computer's sign-in token, plugin version, OS family; no memory content); a size check when a free account nears its cap (account id, sign-in token, the database's byte size and the proposed delta); and, unless you opt out with `SIBYL_MEMORY_TELEMETRY=0`, an aggregate count of memory operations. The size check and the count also carry this computer's signed access token (account id, tier, cap, device hash, account flags; no personal data). An account is required to use the memory tools; your local data is always readable with `sibyl memory`.

## Tier model

Free tier is generous on purpose. You can build real things with it. Paid plans add self-learning, the linter, and remove the 5 MB local cap. Full plan comparison at [docs.sibyllabs.org/memory/tiers](https://docs.sibyllabs.org/memory/tiers).

## Documentation

Full docs: [docs.sibyllabs.org/memory/](https://docs.sibyllabs.org/memory/).
Install guide: [docs.sibyllabs.org/memory/install](https://docs.sibyllabs.org/memory/install).

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

In this package: `sibyl_memory_client.skills` (the procedure text and its installer) and
`sibyl_memory_client.lifecycle` (measurement, the loss-free splitter, apply and undo, the policy,
the reminders, and `automation_allowed`, which trusts only a verified entitlement whose own tier
is paid).

## License

MIT. Published on PyPI at [pypi.org/project/sibyl-memory-client](https://pypi.org/project/sibyl-memory-client/).
