# Vendored skill text: provenance and changes

The two procedure files in this directory are adapted copies of two public MIT repositories by
Sibyl Labs, LLC. They are vendored (copied with attribution), not dependencies: `sibyl-save` is
Markdown and `debloat` is a Node script, and the plugin must not require Node. The watermark block
at the top of each file is kept (the MIT notice is the only condition); its `hash` is recomputed
for the adapted text with the upstream algorithm (`scripts/watermark.mjs`: SHA-256 of the
frontmatter plus the body with the watermark removed and leading whitespace trimmed).

The upstream `_watermark-manifest.json` is not carried (it describes the upstream package); the
plugin's own `manifest.json` records the installed files and their checksums.

## SAVE.md

Upstream: github.com/Sibyl-Labs/sibyl-save `SKILL.md` v1.0.0, commit `499a3fe` (2026-06-17).

1. Frontmatter: `name: sibyl-save` kept; the description adds "save to memory", `/sibyl-save
   headless`, `/sibyl-save curate` and "when the plugin asks you to save (a save reminder or a
   Stop-hook continuation)". Watermark `file:` is `SAVE.md`, `release:` v1.1.0.
2. Requirements: `sibyl init` is the sign-in (an account is required since plugin cli 0.5.0);
   `sibyl setup` wires the harness and installs this file and its shims; the clone-and-copy
   install still works. The example config is inlined (the `examples/` file is not carried).
3. New **headless** mode: Phase 1 fully, ask nothing, skip Phase 2, queue candidate skills,
   ambiguous entities and stale priorities at state key `sibyl_save:pending`.
4. New **curate** mode: walk the queued decisions with the person.
5. Step 3 item 7 (delta marker) writes `{ts, session_id, writes_seen}`; the plugin's reminders
   read the same marker.
6. New Step 3.5 "Keep your instruction files lean" (the public README's advice turned into a
   step): offer the debloat procedure when the plugin reported context watch or heavy.
7. Rules: "never include secrets, tokens or credentials in a memory body" and "a save never
   trims or edits an instruction file" made explicit.
8. Phase 2 table gains a Hermes row (`~/.hermes/skills/<name>/SKILL.md`, the directory
   hermes-agent 0.19.0 documents).
9. (SD-10) Requirements: `sibyl setup` gives Codex "a marked block in Codex's global AGENTS.md
   plus a `sibyl-save` skill in Codex's skills folder" (it never wrote a custom prompt).
10. (SD-10, reworded R1) Headless mode: on Pro the plugin asks for a headless save once early
    (after 5 memory writes or a compaction), then about every hour of active work; in Claude
    Code through the Stop hook (which fires at every turn end), in Cursor, Codex and other MCP
    hosts through a `notice` on a tool result, in Hermes through a note. Not "at the end of a
    session".
11. (R1) Phase 2 table: the Codex row is `~/.codex/skills/<name>/SKILL.md` with the frontmatter
    rules Codex CLI 0.135.0's bundled `quick_validate.py` enforces (it has no `~/.codex/prompts`
    loader; verified from the installed binary). This was the known-stale upstream row.
12. (R1) Headless mode says a headless save never creates a skill and that the plugin proposes
    candidate skills separately on Pro; the report mentions them.
13. (R1) Curate mode also walks the plugin's skill proposals through `sibyl skills proposals`
    (show, accept on a yes only, reject with a note).

## DEBLOAT.md

Upstream: github.com/Sibyl-Labs/debloat `SKILL.md` v1.0.0, commit `f0bf26d` (2026-08-01). The
upstream file carries no watermark; the plugin copy adds the collection's standard block.

1. Frontmatter `name: sibyl-debloat` (never collides with a user's own `/debloat`); triggers add
   `/sibyl-debloat` and "when the plugin reports context watch or heavy".
2. Measurement is `sibyl debloat --json|--fyi` (a Python port of `context-hygiene.mjs` with the
   same numbers); Node is optional.
3. Surface: the instruction files the plugin's scan finds for the project and home; a project
   `.debloat.json` narrows or extends it (same shape as upstream); only files under the project
   or home are ever measured or trimmed.
4. Two trim destinations, tier-honest: free = sibling `*-history` file (upstream behaviour);
   Pro = memory (`memory_remember("context-history", ...)` for the agent-driven trim, a
   reference document for the plugin's own trim).
5. The interactive rules are kept word for word; `sibyl:keep` / `sibyl:immutable` regions named.
6. New section "What runs by itself on Pro" (the `propose` / `auto` / `off` policy).
7. (SD-10) That section says an `auto` choice is for one file at its path (not the same name in
   another project or in the home), and adds "Protect what is current but looks like history":
   wrap live sections the splitter would take for history (a "Log format" heading, an old dated
   procedure) in `sibyl:keep` / `sibyl:immutable` before putting a file on `auto`, and check
   with `sibyl debloat plan`.
