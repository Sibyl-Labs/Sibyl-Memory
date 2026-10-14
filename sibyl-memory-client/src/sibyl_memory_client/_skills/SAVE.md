---
name: sibyl-save
description: Session memory sync and skill extraction for the Sibyl Memory Plugin. Writes what happened this session into the plugin's local memory (entities, state, journal) through the plugin's own tools, then offers to turn repeated work into reusable skills. Use at the end of a session, after a major block of work, or when the plugin asks you to save (a save reminder or a Stop-hook continuation). Triggers - /sibyl-save, save session, sync memory, save to memory, /sibyl-save setup, /sibyl-save preview, /sibyl-save headless, /sibyl-save curate.
---
<!-- SIBYL-SKILL-WATERMARK-BEGIN
  origin:  sibyl-labs
  build:   2026-09-27T00:00:00Z
  release: v1.1.0
  file:    SAVE.md
  hash:    sha256:d1c18bf470af29d703d012b7cb82d9c987eef95dfccbfbf26c67ce281b9aa704
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

- **MCP** (Claude Code, Codex, Cursor, any MCP host): the 8 `memory_*` tools below.
- **Python SDK** (Hermes or any Python agent): `sibyl_memory_client.MemoryClient` / `sibyl_memory_hermes.SibylMemoryProvider`.

### The plugin interface

| Purpose | MCP tool | Python SDK |
|---------|----------|------------|
| Save a fact / entity | `memory_remember(category, name, body)` | `client.set_entity(category, name, body)` |
| Recall an entity | `memory_recall(category, name)` | `client.get_entity(category, name)` |
| List entities | `memory_list(category, limit)` | `client.list_entities(category)` |
| Full-text search | `memory_search(query, limit, tiers)` | `client.search(query)` |
| Set a state doc | `memory_set_state(key, body)` | `client.set_state(key, body)` |
| Get a state doc | `memory_get_state(key)` | `client.get_state(key)` |
| Record a journal event | `memory_record_event(kind, body, category, name)` | `client.write_event(...)` |
| Archive / forget | `memory_forget(category, name, reason)` | `client.archive_entity(category, name, reason)` |

Examples below use the MCP names. On Hermes or a Python agent, use the SDK column (Hermes exposes `sibyl_remember`, `sibyl_recall`, `sibyl_search`, `sibyl_list`).

### Tier mapping

| Session content | Tier | Call |
|-----------------|------|------|
| Session pointer / summary / forward items | HOT (state) | `memory_set_state("session", ...)` |
| Ranked priorities | HOT (state) | `memory_set_state("priorities", ...)` |
| Session log entry | COLD (journal) | `memory_record_event("session_save", ...)` |
| Projects / people / products | WARM (entities) | `memory_remember(category, name, body)` |
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
4. **Entities** - for each changed entity, `memory_remember(category, name, body)`. New entities are just new `memory_remember` calls; the plugin indexes them, so there is no separate registry to maintain.
5. **Closed / dead** - `memory_forget(category, name, reason)`.
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
