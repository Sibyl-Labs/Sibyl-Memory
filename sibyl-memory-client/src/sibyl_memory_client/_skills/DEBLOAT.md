---
name: sibyl-debloat
description: Measure and trim your AI agent's always-load context - the instruction and rule files it reads on every session start (CLAUDE.md, AGENTS.md, .cursorrules and the rest). Use when the user says '/sibyl-debloat', 'debloat', 'context bloat', 'how heavy is my context', 'trim startup context', 'context weight', 'my startup is bloated', or when the Sibyl Memory plugin reports context watch or heavy. Environment hygiene, not a gate - it measures and suggests; you choose what to trim.
---
<!-- SIBYL-SKILL-WATERMARK-BEGIN
  origin:  sibyl-labs
  build:   2026-09-27T00:00:00Z
  release: v1.1.0
  file:    DEBLOAT.md
  hash:    sha256:b26c5858be378ddab02a7123fb89895277a4beb2f20ad24e8a87186a72c32687
  notice:  Part of the Sibyl Labs open skill collection. MIT licensed (see LICENSE). Free to use, modify, and redistribute with this notice preserved. Built by Sibyl Labs, LLC. Verify or report at sibyl@sibyllabs.org.
SIBYL-SKILL-WATERMARK-END -->

# /sibyl-debloat - context hygiene for the always-load surface (Sibyl Memory Plugin)

Part of the Sibyl Labs open skill collection. MIT licensed. Built by Sibyl Labs, LLC.
Adapted from github.com/Sibyl-Labs/debloat v1.0.0 for the plugin (the changes are listed in `CHANGES.md` inside the plugin package).

Deterministically measures what your agent loads on EVERY session start (its instruction files and
rules) and, on demand, runs a trim pass you drive. Pure byte/token arithmetic (chars/4, labelled
`MEASURED~`), no LLM judgment, so the number cannot be rationalized away. Environment hygiene, not
a gate: it never blocks a session and never forces a trim. You choose what (if anything) to move.

## Measure

```
sibyl debloat --json     # machine-readable record (drives the trim picker)
sibyl debloat --fyi      # one line
sibyl debloat            # full report
```

These are the same numbers as `context-hygiene.mjs` from github.com/Sibyl-Labs/debloat; if you
have Node you can run that too. Nothing needs Node here.

## The surface

The plugin measures the instruction files its scan finds for this project and your home:
`CLAUDE.md`, `.claude/CLAUDE.md`, `AGENTS.md`, `.codex/AGENTS.md`, `MEMORY.md`, `memory.md`,
`.cursorrules`, `.cursor/rules/*`. A `.debloat.json` in the project root narrows or extends it,
same shape as the public tool:

```json
{
  "surface": ["CLAUDE.md", "AGENTS.md", ".claude/rules.md"],
  "thresholds": { "watch": 45000, "heavy": 55000 },
  "perFileSoft": { "CLAUDE.md": 6000 }
}
```

Only files under this project or your home are ever measured or trimmed. Weight labels: `light`
(< watch) / `watch` (>= watch, or a heavy file present) / `heavy` (>= heavy).

## Trim (interactive)

1. **Measure** - `sibyl debloat --json`. Parse `total_tok`, `weight`, `files[]` (per-file weight),
   `targets[]` (heavy files).
2. **Show** the weight + the heaviest files + the suggested targets. If `weight === 'light'` and
   no targets, report "light, nothing to trim" and stop.
3. **Ask** which target(s) to trim (never trim without an explicit choice).
4. **Extract** each chosen target:
   - Identify the accreted HISTORY (dated log prose, superseded notes, event history) versus the
     current live state.
   - Move that history out, then leave the source file with current state + a one-line pointer:
     - **Free plan:** append the history to a matching `*-history` file beside it (create if
       absent), for example `AGENTS.md` -> `AGENTS-history.md`. The pointer names that file.
     - **Pro:** store the history in Sibyl Memory so it stays searchable:
       `memory_remember("context-history", "<file>-<YYYY-MM-DD>", {"file": "<file>", "sections":
       ["<heading>", ...], "body": "<the history text, unchanged>"})`. The pointer names the key and
       says to recall it with `memory_search`.
   - **NEVER delete data.** Extraction = move + keep a pointer. Immutable or append-only records
     (logs, transaction/entry data) are off-limits: surface them, do not trim. Anything inside
     `<!-- sibyl:keep -->` or `<!-- sibyl:immutable -->` stays where it is.
5. **Re-measure** with `sibyl debloat --json` and report the before/after `total_tok` drop so you
   see it landed. Validate any JSON file you edit (`python -m json.tool <path>`).

`sibyl debloat plan` shows what the plugin's own deterministic splitter would move for each file,
and `sibyl debloat apply <file>` applies that plan after asking you (free: sibling `*-history`
file; Pro: a memory reference document), with a byte-verified backup under
`~/.sibyl-memory/debloat/backups/` and `sibyl debloat undo <file>` to restore it.

## What runs by itself on Pro

On Pro the plugin measures your instruction files at every start and builds the same deterministic
plan. The policy lives in `~/.sibyl-memory/debloat.json` and you set it once:

- `propose` (the default): it only tells you what it would move, one line, and nothing changes.
- `auto`: it applies the plan, but ONLY for the files you named with `sibyl debloat auto <file>`
  (it asks once and explains what that means). The choice is for that file at its path: a file with
  the same name in another project, or in your home, is not included. `sibyl debloat manual
  <file>` takes a file back.
- `off`: measure and report only.

Every automatic trim first makes a byte-verified backup, stores the history in memory as a
reference document (`debloat/<file>/<date>`), rewrites the file last, and reports one line. The
plugin only moves blocks it can classify deterministically: a `<!-- sibyl:history -->` region, a
heading like "History" or "Changelog", a dated section older than 30 days with no open item, or
everything below `<!-- sibyl:trim-below -->`. If that would move less than 10 % or more than 90 %
of a file, it moves nothing and points you here. So some trims can happen without you in the room,
but only in files you pre-authorised, and every one is reversible.

**Protect what is current but looks like history.** The splitter goes by headings and dates, not
by meaning, so a section that is live instruction but is named like a log, or carries an old date,
would be moved: a "Log format" or "Changelog conventions" section (its heading starts with a
history word), a "2026-01-10 deploy procedure" that is still the procedure, a decision record you
still follow. Wrap each such section in `<!-- sibyl:keep -->` ... `<!-- /sibyl:keep -->` (or
`<!-- sibyl:immutable -->` ... `<!-- /sibyl:immutable -->`) before you put a file on `auto`;
anything inside those markers is never treated as history. Run `sibyl debloat plan` to see which
sections a trim would move, and adjust the markers until only real history is listed.

## Rules

- Environment hygiene, never a gate. Non-blocking. You select; nothing is forced.
- Never delete data - extract accreted history and leave a pointer.
- Never trim immutable/append-only records or transaction data.
- Never trim a file outside this project or your home, or one that is not an instruction file.
- The token count is `chars/4` (`MEASURED~`), a deterministic proxy, not an exact tokenizer count.

## License & provenance

MIT. Copyright (c) 2026 Sibyl Labs, LLC. The watermark comment at the top of this file is
attribution and provenance, not a usage restriction; keep it when you copy or modify the file.
