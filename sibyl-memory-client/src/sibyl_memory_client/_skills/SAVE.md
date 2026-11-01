---
name: sibyl-save
description: Session memory sync and skill extraction for the Sibyl Memory Plugin. Writes what happened this session into the plugin's local memory (entities, state, journal) through the plugin's own tools, then offers to turn repeated work into reusable skills. Use at the end of a session, after a major block of work, or when the plugin asks you to save (a save reminder or a Stop-hook continuation). Triggers - /sibyl-save, save session, sync memory, save to memory, /sibyl-save setup, /sibyl-save preview, /sibyl-save headless, /sibyl-save curate.
---
<!-- SIBYL-SKILL-WATERMARK-BEGIN
  origin:  sibyl-labs
  build:   2026-10-22T00:00:00Z
  release: v1.2.0
  file:    SAVE.md
  hash:    sha256:f539c7011b01b788980da2f2b7337e7193bcd3cca67f879f98e4c045fc27e981
  notice:  Part of the Sibyl Labs open skill collection. MIT licensed (see LICENSE). Free to use, modify, and redistribute with this notice preserved. Built by Sibyl Labs, LLC. Verify or report at sibyl@sibyllabs.org.
SIBYL-SKILL-WATERMARK-END -->

# /sibyl-save - Session Memory Sync + Skill Extraction (Sibyl Memory Plugin)

Part of the Sibyl Labs open skill collection. MIT licensed. Built by Sibyl Labs, LLC.
Adapted from github.com/Sibyl-Labs/sibyl-save v1.0.0 for the plugin (the changes are listed in `CHANGES.md` inside the plugin package).

A companion procedure for the **Sibyl Memory Plugin**. It captures everything that happened this session and writes it into the plugin's local memory database (`~/.sibyl-memory/memory.db`) through the plugin's own interface, then extracts reusable patterns into new skills.

It does not write raw memory files. All memory goes through the plugin so tiering, full-text indexing, and the free-tier cap are honored. Because the plugin exposes the same MCP tools on every harness it supports (Claude Code, Codex, and any MCP host; Hermes via the Python SDK), this one procedure is multi-harness by construction.

## Requirements

The Sibyl Memory Plugin, installed, signed in and wired:

```bash
pip install sibyl-memory-cli
sibyl init                 # sign in (an account is required); writes ~/.sibyl-memory/
sibyl setup claude-code    # or: codex | hermes  (wires the memory interface into your harness)
```

`sibyl setup` wires the harness AND installs this file (`~/.sibyl-memory/skills/SAVE.md`) with a short pointer for your harness (`/sibyl-save` in Claude Code, a marked block in Codex's global AGENTS.md plus a `sibyl-save` skill in Codex's skills folder, a skill in Hermes; any MCP host also gets it as the `save` prompt). The procedure is installed for you by `sibyl setup`; the clone-and-copy install from the public repository still works.

This procedure talks to the plugin through one of two surfaces:

- **MCP** (Claude Code, Codex, Cursor, any MCP host): the 15 `memory_*` tools below.
- **Python SDK** (Hermes or any Python agent): `sibyl_memory_client.MemoryClient` / `sibyl_memory_hermes.SibylMemoryProvider`.

### The plugin interface

| Purpose | MCP tool | Python SDK |
|---------|----------|------------|
| Save a fact / entity | `memory_remember(category, name, body)` | `client.set_entity(category, name, body)` |
| Recall an entity | `memory_recall(category, name)` | `client.get_entity(category, name)` |
| List entities | `memory_list(category, limit)` | `client.list_entities(category)` |
| Full-text search | `memory_search(query, limit, tiers, scope, with_links)` | `provider.search_multi_record(query, scope=...)` |
| Set a state doc | `memory_set_state(key, body)` | `client.set_state(key, body)` |
| Get a state doc | `memory_get_state(key)` | `client.get_state(key)` |
| Record a journal event | `memory_record_event(kind, body, category, name)` | `client.write_event(...)` |
| Archive / forget | `memory_forget(category, name, reason)` | `client.archive_entity(category, name, reason)` |
| Store a document | `memory_set_reference(key, body, metadata, links)` | `client.set_reference(key, body, metadata=..., links=...)` |
| Read a document (paged) | `memory_get_reference(key, offset, max_chars)` | `client.read_reference(key, offset=...)` |
| List documents | `memory_list_references(prefix, limit, cursor)` | `client.list_references(prefix=...)` |
| Delete a document | `memory_delete_reference(key)` (keys under `skill/` and `debloat/` belong to the plugin and need `force=true`; pass it only when the person asked) | `client.delete_reference(key)` |
| Link two nodes | `memory_link(from_node, to_node, relation, metadata)` | `client.link(src, dst, relation)` |
| Remove a link | `memory_unlink(from_node, to_node, relation)` | `client.unlink(src, dst, relation)` |
| Walk a node's links | `memory_neighbors(node, relation, direction, tier, ...)` | `client.neighbors(node, ...)` |

Examples below use the MCP names. On Hermes or a Python agent, use the SDK column (Hermes exposes `sibyl_remember`, `sibyl_recall`, `sibyl_search` (with `scope`), `sibyl_list`).

### Tier mapping

| Session content | Tier | Call |
|-----------------|------|------|
| Session pointer / summary / forward items | HOT (state) | `memory_set_state("session", ...)` |
| Ranked priorities | HOT (state) | `memory_set_state("priorities", ...)` |
| Session log entry | COLD (journal) | `memory_record_event("session_save", ...)` |
| Projects / people / products | WARM (entities) | `memory_remember(category, name, body, links)` |
| Transcripts, specs, threads, notes, long analyses | REFERENCE | `memory_set_reference("kind/slug", body, {title, date, source}, links)` |
| A fact that joins two nodes | LINK | `links=` on the write, or `memory_link(from_node, to_node, relation)` |
| Closed / dead items | ARCHIVE | `memory_forget(category, name, reason)` |
| This procedure's config + delta marker | HOT (state) | `memory_set_state("sibyl_save:config" / "sibyl_save:last_save", ...)` |
| Decisions left for the next session (headless) | HOT (state) | `memory_set_state("sibyl_save:pending", ...)` |

## Modes

- **run** (default): `/sibyl-save`, `save session`, `sync memory`. Write the session into the plugin, then offer skill extraction.
- **setup**: `/sibyl-save setup`, `/sibyl-save config`. Interactive. Build or edit the config (stored in the plugin at state key `sibyl_save:config`). Run once before first use.
- **preview** (dry run): `/sibyl-save preview`, `/sibyl-save --dry-run`. Run the full analysis and report every planned write. Call nothing that mutates the DB. Do not advance the delta marker.
- **headless**: `/sibyl-save headless`. Used when the plugin asks you to save (on Pro: the Stop hook continuation in Claude Code, a `notice` on a tool result in Cursor, Codex and other MCP hosts, a note in Hermes; once early, after 5 memory writes or a compaction, then about every hour of active work, not only when the session ends). Run Phase 1 fully, ask nothing, skip Phase 2, and queue anything that needs a person's decision (see "Mode: headless").
- **curate**: `/sibyl-save curate`. Go through the decisions a headless save queued (`sibyl_save:pending`) with the person, one at a time.

---

## Mode: setup (dynamic config)

Goal: write the config to the plugin at state key `sibyl_save:config`. Be interactive and minimal. Infer what you can; ask only what you cannot.

1. **Harness.** Detect or ask which harness this is (claude-code, codex, cursor, hermes, other). This drives where Phase 2 writes skill files.
2. **Skill output.** Where new skills get written for this harness. Default for Claude Code: `.claude/skills/<name>/SKILL.md`. For other harnesses, confirm the skill location and format. Store as `skill_output: {dir, format}`.
3. **Priorities.** State key for the ranked list (default `priorities`) and a cap. Default cap 15. Hard ceiling 30: never accept a value above 30. A list longer than 30 is a backlog, not a set of priorities.
4. **Entity categories.** Which WARM categories you track (default `project`, `person`, `product`). User adds or removes.
5. **Additional capture.** Optionally name extra state keys or entity categories the save should populate each run (a metrics state doc, a contacts category). This is the extension point.
6. **Dry-run default.** Toggle `dry_run_default` (default false).
7. **Write the config** via `memory_set_state("sibyl_save:config", {...})`. Do not touch the delta marker.

Config shape (all keys optional except `schema`):

```json
{
  "schema": "sibyl-save-config-v1",
  "harness": "claude-code",
  "skill_output": {"dir": ".claude/skills", "format": "claude-skill-md"},
  "session_state_key": "session",
  "priorities_state_key": "priorities",
  "priorities_cap": 15,
  "journal_event_kind": "session_save",
  "entity_categories": ["project", "person", "product"],
  "additional_state_keys": [],
  "additional_entity_categories": [],
  "dry_run_default": false
}
```

Re-running setup edits it in place.

---

## Mode: run

### Writing: which tier

- **Entity** (`memory_remember`): a thing you will name again: a person, company, project, product, ticket, place, decision. The body is a card, not a dump: name, aliases, role or kind, status, a one-line summary, and the few facts you would want in a search hit. Keep it under about 1 KB (if you are pasting, it is a document). Never store a document's text in an entity.
- **Reference** (`memory_set_reference`): anything you would read in full: a transcript, a spec, an email thread, meeting notes, a file's trimmed history, a long analysis. Key `kind/slug`; `metadata.title` always, `metadata.date` and `metadata.source` when known. Put the title and a one-line "about" at the head of the body so the search snippet is informative. Set `metadata.status` (`draft`, `proposed`, `approved`, `final`, `sent`) whenever the document has one: on a question about a final state ("what was approved"), search reads `metadata.status` first and drops documents whose status is preparatory; without it, it reads the head of the body for draft words and the whole body for a decision.
- **Link** (`links=` on either write, or `memory_link`): a fact that joins two nodes. Write the edge when you write the node, in the same call. Recommended relations: `works_at`, `member_of`, `owns`, `part_of`, `reports_to`, `source_of` (hub -> document), `mentions` (document -> entity), `about` (document -> entity/project), `supersedes`, `related_to`. Direction reads as a sentence: `person:Maya Patel --works_at--> company:Redwood`; `customer:Tethys --source_of--> reference:doc/fireflies/...`. Both ends must exist first, so write in this order: hubs (companies, projects, customers) first, then their members (people, tickets) with `links=` to the hub, then documents with `links=` to what they are about. `memory_link` takes `from_node` and `to_node` (not `from` / `to`); a wrong or missing argument name comes back as a `VALIDATION_ERROR` that names the parameter. A write with `links=` returns `links: {created, updated, unchanged}`: new edges, existing edges whose metadata changed, and identical re-asserts.
- **State** (`memory_set_state`): the current working set and directories (`session`, `priorities`, `directory:customers`). Overwritten, never appended.
- **Journal** (`memory_record_event`): what happened, with the entity it is about. The entity is the noun, the event is the verb.
- Hubs do not list their members in the body. Membership is links; `memory_neighbors` lists them, paginated, with the edge metadata (date, role, title) you wrote.
- **Forget keeps links.** `memory_forget` archives the entity and leaves its edges in place (they show the endpoint as archived). Re-creating the same category and name reconnects those edges, but each one written for the earlier entity is marked `stale: true` (the name is the same, the thing may not be: a new intern named like the former CFO); `memory_remember` says how many as `inherited_links` when it creates the entity. An edge whose other end was forgotten and not re-created is not stale: it shows that end as archived. Read `stale` before trusting an edge; keep the ones that still hold (writing one again with `memory_link` confirms it) and `memory_unlink` the rest.

### Reading: top-down

1. **Named thing known**: `memory_recall(category, name)`. If the category is unknown, `memory_search(name, scope="graph")` and take the hit with `match: "name"`. Then `memory_neighbors(node, relation=..., tier=...)` to walk (e.g. `tier="reference"` for the documents under a hub). No edges can mean a misspelled node: the result says whether the node you asked about exists (or is archived), so check that before concluding it has no links. Then `memory_get_reference(key)` for the document, paging with `offset` while `next_offset` is set.
2. **Nothing named**: `memory_search(question)` with the words the author would have used. Read each hit's `match`, `snippet`, `chars` and `links`. Recall the hub a hit links to before searching again; siblings of a good document are under the same hub. A result is held to about 22,000 tokens: a large `limit` (up to 50) first shortens and drops hit bodies (the `snippet` stays), then link samples, and only then trailing hits (`verdict.truncated`). For full text, read the record (`memory_recall` / `memory_get_reference`).
3. **A zero is a verdict, not a dead end**: `abstained_on` -> drop the named word and retry, at most twice; `gated` -> use more of the stored wording or one distinctive term; never use `tiers=` to escape an abstention.
4. Cite documents (reference keys), not hubs.

### Phase 1: Memory Sync (into the plugin DB)

#### Step 0 - Load config + delta
`memory_get_state("sibyl_save:config")`. If absent, switch to setup (in headless mode, use the defaults above and do not ask). `memory_get_state("sibyl_save:last_save")` for the delta anchor (what changed since the last save). If `dry_run_default` is true and no mode was given, run as preview.

#### Step 1 - Read current state
- `memory_get_state("session")` and `memory_get_state(<priorities key>)`
- `memory_list(category, limit)` for tracked categories, and `memory_recall` / `memory_search` for entities touched this session
- any additional state keys or categories named in config

#### Step 1.5 - Priorities check + reconcile
Read the priorities state doc and reconcile in place (write back with `memory_set_state`). Conditions and the action each requires:

- **COMPLETED item still listed**: drop it; record the closure via `memory_record_event("priority_closed", {...})`.
- **RANK_DRIFT / DUP_RANK** (ranks not contiguous 1..N, or a rank repeats): renumber to 1..N.
- **OVER_CAP** (more items than the cap): move the lowest-priority items to a backlog state doc until at or under cap.
- **STALE_ITEM** (>14 days untouched): surface it to the user (in headless mode, queue it).
- **TERMINAL entity** (the referenced entity is archived/dead): close the priority.
- **BROKEN entity ref** (points at no real entity): fix it to a real `category/name`.

Enforce the cap on every write (never exceed 30).

#### Step 2 - Analyze the session
Review the conversation since the delta anchor (or since session start on first save). Extract:

- **Entities touched** - projects, people, products created, read, or modified
- **Actions taken** - concrete things that happened, with identifiers
- **Decisions made** - strategic choices and their rationale
- **Forward items** - next-session actions, verb-first ("deploy...", "follow up with...", "verify...")
- **Warnings** - active risks, blockers, deadlines, near-misses
- **Lessons learned** - operational improvements (these feed Phase 2)
- **Additional captures** - data for any extra state keys / categories in config

#### Step 3 - Write into the plugin DB
In **preview** mode, call nothing that mutates. Report the planned writes (Step 4) and stop.

1. **Session state** - `memory_set_state("session", { last_session, summary, forward, entities_touched, warnings })`. Forward items are action references only, never balance snapshots or anything that goes stale.
2. **Journal event** - `memory_record_event("session_save", { evaluated, acted, forward, extra })`. One event per save (append-only by design).
3. **Priorities** - `memory_set_state(<priorities key>, { updated, cap, items })`, capped. Overflow to a backlog state doc.
4. **Entities** - for each changed entity, `memory_remember(category, name, body, links)`: a card (see "Writing: which tier"), with the links it gained this session in the same call. New entities are just new `memory_remember` calls; the plugin indexes them, so there is no separate registry to maintain. A long text produced this session (a spec, meeting notes, an analysis) is a document: `memory_set_reference("kind/slug", body, {title, date, source}, links=[{to: <its hub>, relation: "about"}])`, never an entity body.
5. **Closed / dead** - `memory_forget(category, name, reason)` (archives; links are kept, see "Forget keeps links").
6. **Additional captures** - apply each configured extra state key / category.
7. **Delta marker** - `memory_set_state("sibyl_save:last_save", { ts: <now, ISO 8601>, session_id: <this session's id if you know it>, writes_seen: <number of memory writes this save made> })`. Skip in preview. The plugin reads the same marker to know a save happened, so its reminders stop.

#### Step 3.5 - Keep your instruction files lean
If the plugin reported `context: watch` or `context: heavy` this session (a session-start line, a `notice`, or `sibyl status`), offer the debloat procedure once: on the free plan say "run `/sibyl-debloat` (or the `debloat` prompt) to move old history out of your instruction files"; on Pro say "the plugin will propose the trim at the next start". Never trim anything as part of a save. In headless mode, only mention it in the report.

#### Step 4 - Report
Concise summary: entities written, state docs updated, journal event id, priorities added/closed, additional captures, and forward items for next session. In preview mode, label every line "would" and confirm nothing was written.

---

### Phase 2: Skill Extraction (harness-native)

Skills are presented the same way regardless of harness: identify, propose, then create as a native skill file for the current harness. Skipped in headless mode (candidates are queued instead).

#### Step 5 - Identify reusable patterns
Scan the session for operations meeting ANY of these: **repeated** (more than once this session), **recurring** (likely again), **multi-step** (3+ sequential steps), **non-obvious** (a workaround worth keeping), **error-prone** (needs a checklist). For each candidate evaluate name (short imperative verb phrase), trigger, parameters, complexity (any dangerous steps), frequency.

#### Step 6 - Propose candidates
Present each as a one-liner with name, description, and estimated frequency. Then ask: "Create these? Or skip any?"

#### Step 7 - Create approved skills (harness-native)
Read `skill_output` from config to know where and in what format this harness expects skills. Defaults:

| Harness | Skill location | Format |
|---------|----------------|--------|
| claude-code | `.claude/skills/<name>/SKILL.md` | YAML frontmatter (name, description) + Markdown body |
| codex | `~/.codex/skills/<name>/SKILL.md` | YAML frontmatter (`name`: hyphen-case, at most 64 characters, same as the folder; `description`: at most 1024 characters, no angle brackets; no other keys than name, description, license, allowed-tools, metadata) + Markdown body |
| cursor | `.cursor/` command/rule (confirm in setup) | per Cursor convention |
| hermes | `~/.hermes/skills/<name>/SKILL.md` (confirm in setup) | YAML frontmatter (name, description) + Markdown body |
| other | from `skill_output.dir` | from `skill_output.format` |

For each approved skill, write the file at the configured location with trigger phrases, step-by-step instructions, parameter descriptions, safety checks for any dangerous step, and an example invocation. Report the path.

**Do NOT create skills that:** duplicate an existing skill (check the skill location first), are too session-specific to recur, wrap a single trivial command, or require editing files your operating rules protect (surface those, do not self-edit).

---

## Mode: headless

The plugin may end a session with an instruction like "run the save procedure in headless mode now". Then:

1. Run Phase 1 (Steps 0 to 3.5) fully and silently. Ask the person nothing. Use the config defaults when there is no config.
2. Skip Phase 2 (a headless save never creates a skill; on Pro the plugin also proposes candidate skills separately, from your journal, and you decide them in curate mode). Instead, put every candidate skill, every ambiguous entity (you could not tell which existing entity it belongs to, or whether it is new), and every stale priority into the pending-curation queue: `memory_set_state("sibyl_save:pending", { "updated": <now>, "items": [ {"kind": "candidate_skill" | "ambiguous_entity" | "stale_priority", "summary": "<one line>", "detail": {...}} ] })`. Merge with the items already queued; never drop an item someone has not decided.
3. Write the delta marker (Step 3 item 7).
4. Report in one or two lines (if the plugin named candidate skills this session, add that they wait for `/sibyl-save curate`), then stop. Do not start new work.

## Mode: curate

`memory_get_state("sibyl_save:pending")`. For each item, one at a time: show the one-line summary, ask what to do (create the skill, merge the entity into an existing one, keep it separate, close the priority, or skip), do it, and remove the item from the queue with `memory_set_state("sibyl_save:pending", {...})`. Stop when the queue is empty or the person says stop.

Then the plugin's own skill proposals (Pro), when you can run a terminal command: `sibyl skills proposals` lists them. For each, one at a time: show its title and why (`sibyl skills proposals show <id>`), ask, and only on a yes run `sibyl skills proposals accept <id>` (it writes the skill file for Claude Code, Codex and Hermes on this computer and keeps a reference copy in memory); on a no run `sibyl skills proposals reject <id> --note "<why>"` (not proposed again for 30 days). Never accept without the person's yes. Without a terminal, tell the person to run `sibyl skills proposals` themselves.

---

## Rules (non-negotiable)

- Never store live data (balances, prices, counts) as durable facts. Capture references, not snapshots.
- Never include secrets, tokens, passwords, keys or credentials in any memory body.
- Session forward items are action-oriented and verb-first.
- One journal event per save (append-only; never rewrite history).
- Priorities are capped (default 15, never above 30). Overflow to a backlog state doc.
- Entities are addressed by `category` + `name`; let the plugin be the index. No parallel registry.
- All memory writes go through the plugin interface. Never write `~/.sibyl-memory/` files or open `memory.db` directly: the client handles locking and the free-tier cap.
- Treat existing skills as read-only. Create new ones rather than editing in place unless explicitly asked.
- A save never trims or edits an instruction file. Trimming is the debloat procedure's job and needs its own explicit choice.

## License & provenance

MIT. Copyright (c) 2026 Sibyl Labs, LLC. The watermark comment at the top of this file is attribution and provenance, not a usage restriction; keep it when you copy or modify the file.
