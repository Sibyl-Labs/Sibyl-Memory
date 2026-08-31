# Changelog

All notable changes to `sibyl-memory-client` are recorded here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows [SemVer](https://semver.org/).

## [Unreleased]

### Added

Write-time normalization for the search shadow (branch `lang-core-normalize`,
2026-08-30). One versioned normalizer, `shadow.NORMALIZER_VERSION = 1`, replaces
the query-time rescue layers stripped below. It renders the stored text at WRITE
time and the query at READ time from the same code.

- **`normalize_py` / `normalize_select`** — the rendering: `fold` (unchanged)
  plus boundary normalization, every punctuation and whitespace character
  collapsed to a single space, plus one leading and one trailing pad. The stored
  shadow text is now word-boundary clean, so a term can be matched at a WORD
  START; previously a word inside a JSON body was usually preceded by `"` or `:`
  and only a free-floating substring match was possible. Injected at the four
  per-tier expressions `_entity_txt` / `_state_txt` / `_reference_txt` /
  `_journal_txt`, shared verbatim by the triggers and the backfill, so both write
  paths render byte-identically. The rendering is staged through nested
  subqueries because SQLite's trigger parser overflows at about 27 nested
  function calls and the chain needs 51.
- **`normalize_token` / `normalize_terms`** — the ending rule, recovered from the
  removed D2L block with its tuned parameters intact (min 5, drop 3, floor 5) and
  two new guards: never truncate a token carrying a digit, and never truncate a
  token from an unspaced script (Han, Kana, Hangul, Thai, Lao, Khmer, Myanmar,
  Tibetan), which is what protects the CJK tail matching the shadow exists for.
- **`shadow_search(..., normalize=True)`** — the normalized pass. One probe per
  content term, always, in a fixed order, with no early stop and no selectivity
  re-ordering; rows are then scored by `sum(len(term) / df(term))` over the terms
  they cover, and the top-scoring rows are returned, ordered by whole-word
  exactness, then word-start matches, then BM25. Terms the ending rule did not
  shorten carry no score: that single rule, and no stoplist, is what stops 'and'
  or 'gdzie' from scoring every row in the store.
- **Twin masking closed** (2026-08-07 Finding 2). A single-token query whose
  token the normalizer shortened consults the shadow even on a NON-empty strict
  head, and appends. This is deliberately narrower than the 0.7.0 F2
  unconditional append it replaces: it cannot fire on a multi-word query at all,
  and it cannot fire on a token the ending rule leaves alone.
- **Order change below an empty strict head.** The normalized shadow now runs
  BEFORE the relaxed variants. Both are rescues, but the shadow answers the whole
  query while the relaxed ladder's last resort answers one token of it. The
  relaxed ladder still runs, unchanged, whenever the shadow has nothing.
- **Two bug fixes found on the way.** A short non-ASCII token was a hard AND
  requirement on every shadow candidate, so an accented 2-char Polish function
  word ('są') silently zeroed the fallback; only unspaced scripts keep that
  status now. And the journal cap `max(1, limit // 4)` was applied per pass, so
  strict and shadow caps could stack; it is now enforced across the whole result.

### Changed

- `_SHADOW_MARKER` 4 -> 5. Marker 4 is the fold-only rendering, marker 5 is
  NORMALIZER_VERSION 1; PRAGMA user_version is the on-disk stamp, so every
  existing store drops, recreates and re-backfills its shadow on the next open.
- `apply_shadow_migration` DROPs the ten maintenance triggers before recreating
  them. `CREATE TRIGGER IF NOT EXISTS` alone would have left a pre-existing store
  running the OLD rendering in its triggers while the backfill wrote the new one.
- `normalize_terms` returns `(term, anchored, raw)` triples.

### Measured effect

Same battery, same 300-entity store, all figures from our own runs
(`lang-core-rebuild/results/normalized.json`, three-way table in `delta3.txt`):

- `client.search()`: Polish matched recall 16/16 (baseline 0.7.0 was 15/16,
  stripped 8/16), Polish natural 19/19 (19/19, 11/19), Polish noise rows 17
  (57, 25). English unchanged at 16/16 and 14/14 on every path.
- Default path (`multi_record_search` and MCP): Polish matched recall 16/16
  (15/16, 8/16), Polish natural 13/19 (13/19, 5/19), answerable Polish queries
  returning nothing 6 (6, 20).
- Injection battery zero rows on all three paths, no injection token with any
  corpus support.
- Storage: the normalized rendering REPLACES the folded one, no second copy. On
  the 300-entity fixture the shadow text grows by 600 characters (the two pad
  characters per row; the boundary substitution is 1:1 in length) and the DB
  shrinks by 4096 bytes, one page, because punctuation trigrams give way to a
  smaller space-trigram vocabulary. Entity writes cost about 20 percent more
  (3.79 ms vs 3.12 ms per write) and a 300-row backfill 29.8 ms vs 16.0 ms.

### Changed (revision 2, after independent adversarial review 2026-08-30)

The first cut of this work was reviewed independently and returned
PROMOTE-WITH-FIXES with 2 blockers, both English regressions the battery could not
see. Full disposition in `stage2-report.md` §10.

- **The shadow no longer runs before the relaxed ladder.** Reverted to the 0.5.0
  order. The ladder searches with FTS5 porter, which reaches `story` from
  `stories`; the shadow only does substring on a fixed-length truncation, and
  `stori` is not a substring of `story` but is one of `historic`. Running the
  shadow first therefore answered with an unrelated row and LOST the correct one,
  for -y/-ies plurals and for every digit-bearing identifier (never shortened, so
  never a shadow content term, while CORE-11 exists to give it last-resort
  recall). One narrow exception remains: a last-resort single-token head that
  fills the caller's entire limit is overridden by the shadow, because filling the
  cap from one token means that token matched indiscriminately. A last-resort head
  that does NOT saturate keeps its rows and its rank 1, and the shadow's rows are
  appended after it.
- **The single-token consult requires the raw token.** `shadow_search` gained
  `require_raw`, set only by the consult: an appended row must carry one of the
  query's tokens verbatim, not merely its stem. Without it the consult fired on
  most ordinary English nouns, because the ending rule truncates every 6-to-8
  character token to five characters; `contract` reached control, contrast,
  contribution, contrary and contralto, `df[contract]` went 1 to 7, and the
  deflated idf pushed a correct row under `multi_record`'s coverage floor and off
  the default path. `df[contract]` is now 2, equal to released 0.7.0.
- **`shadow._heal` stamps the marker**, and the migration fast path additionally
  samples one shadow row and requires the normalizer's pad. `_heal` is a second
  writer of the rendering, so an old client healing a new store used to leave the
  new marker standing over the old rendering, and the new client trusted its fast
  path forever. The marker constant moved to `shadow.py`, next to the rendering it
  stamps, and is now 6.
- **The boundary set is complete.** 18 typographic characters were missing,
  including the Polish quotation pair and NBSP, so a word wrapped in them had no
  word start and the exactness tie-break scored 0 for it. `_BOUNDARY_CHARS` is now
  49 entries, the chain 69 replacements, six staged subqueries.
- **The re-backfill raises `busy_timeout` to 120 s for its duration.** The forced
  re-migration is O(rows) under one write lock (measured elsewhere at 27 s for
  100k rows) and a concurrent open died at the 5 s default above roughly 25k rows.
  A mitigation, not a fix; it needs a release note.
- Docstring and annotation corrections: `normalize_py` no longer claims
  idempotence, `_content_terms` is annotated for the three-tuples it handles.

Battery re-measured on all three builds after extending it with the three English
shapes it was missing (-y/-ies plurals, identifier-plus-word, five-character
prefix families) and two injection queries whose tokens have corpus support: 88
queries, up from 75. `client.search()` Polish 16/16 matched and 19/19 natural with
15 noise rows (baseline 15/16, 19/19, 55; stripped 8/16, 10/19, 26). English
25/25 natural and 16/16 matched on every build; on the DEFAULT path English is
byte-identical to the stripped build. Client suite 407 passed / 11 skipped.

### Removed

Query-time multilingual rescue layers stripped out of `MemoryClient.search()`
(operator directive, branch `lang-core-strip`, 2026-08-30). Four external
evaluation cycles accreted these layers at QUERY time, each compensating for the
previous one's side effects. They are being removed so the replacement can be
built at WRITE time on a clean base. This is a deliberate, measured recall
regression in Polish, not a bug fix.

- **F2 unconditional shadow append.** The folded-trigram shadow no longer runs on
  every query with its hits appended after the head. It is once again a
  ZERO-HIT-ONLY fallback, exactly as in 0.5.0: reached only when the strict pass
  and every relaxed variant returned nothing. This restores the 0.5.0 behaviour
  the 2026-08-07 report filed as Finding 2 (a weak English primary hit again
  hides a same-fact row in another language).
- **D2L coverage-gated stem rescue, and its probe ladder in full.** Deleted:
  `_stem_token`, `_stem_truncated_query`, `_head_searchable_text`,
  `_uncovered_stem_tokens`, and the constants `_STEM_MIN_TOKEN`, `_STEM_DROP`,
  `_STEM_FLOOR`, `_GATE_ROW_BYTES`. Deleted from `search()`: the coverage gate,
  the fully-stemmed probe, the selectivity-ordered probe loop (N3) and its
  no-early-stop behaviour (N3').
- **N2 relaxed-single holdback and backfill.** Existed only to reserve cap
  headroom for the D2L rescue; with the rescue gone it has nothing to reserve for.

`search()` is now exactly: strict head, relaxed variants on zero, zero-hit shadow
fallback. Nothing else.

### Kept (explicitly not part of this removal)

- `shadow.py` in full: the trigram shadow table, its triggers, backfill and
  diacritic folding (`fold_py`). This is the 0.5.0 foundation and the intended
  delivery vehicle for the write-time replacement.
- `multi_record.py` in full: the abstention side is unchanged — N4 any-df
  function-word drop, N5 negation abstention, the `df == 0` content-shaped
  abstention, the anchor/coverage gates, and the N1' diagnostics channel.
- The MCP fence/scrub stack and every other search behaviour.

### Measured effect

In-house battery, 300-entity store, 32 matched PL/EN queries + 35 natural
questions + 8 injection-shaped queries, all figures from our own runs
(`lang-core-rebuild/results/`):

- Polish `client.search()` matched recall 15/16 to 8/16; natural battery
  19/19 to 11/19. English unchanged at 16/16 and 14/14 on every path.
- The DEFAULT path moved just as far as the SDK path (PL matched recall
  15/16 to 8/16, natural 13/19 to 5/19) even though none of its own layers were
  touched. `multi_record_search` computes `df[t]` and gathers Stage-1 candidates
  with `client.search(t)`, so removing the rescue drives PL tokens to `df == 0`,
  where the kept content-shaped abstention zeroes the whole query.
- Precision improved: English `client.search()` noise rows 39 to 0; Polish
  57 to 25. One query ('aktualizacja cennika hurtowego') went from 20 rows with
  19 noise rows to 0 rows, which is the N7 regression disappearing along with
  the ladder that caused it.
- The injection-shaped battery returns zero rows on all three paths before and
  after, unchanged.

### Tests

Suites that pinned the removed semantics are SKIPPED with the reason
`removed 2026-08-30 lang-core-strip (operator directive)`, not deleted, so the
behaviour they asserted stays on the record:
`test_covgate_stem_2026_08_12.py`, `test_probe_selectivity_2026_08_16.py` and
`test_capfill_rescue_2026_08_16.py` at module level; two tests in
`test_shadow_append_2026_08_12.py` individually. Client suite: 340 passed before,
327 passed / 13 skipped after, zero failures. mcp, hermes and langgraph suites
unchanged.

## [0.7.0] - 2026-08-22

Multi-language search, part 4 (Kravento / Bilbo Polish evaluation, closing out
what 0.6.1 left open). Independent adversarial re-verification of the 0.6.1
release (cryptoxdylan, external contributor) found the N-series only partly
closed the default-MCP-path defect class: N2/N3 held, but a nonzero-df
function word could still anchor/pollute scoring (N4), a dropped negation word
silently answered the opposite of the query (N5, newly discovered), and the
df=0 abstention rule itself remained unchanged with only its lexicon grown
(N1'). This release closes N4 and N5, and ships a diagnostics channel for N1'
after a coverage-ratio alternative was implemented and measured unsafe (see
Fixed below). `search()` and `multi_record_search()` output shapes are
backward-compatible except where a caller was relying on N5's prior (silently
wrong) negation behavior — see Changed. New optional `diagnostics` kwarg on
`multi_record_search` is additive; every existing caller is unaffected.

### Fixed

- **N4 — a nonzero-df function word could anchor the ranking and crowd out the
  genuine match.** N1 (0.6.1) dropped a function-shaped token only when it had
  zero corpus support. A function word that happened to have SOME support
  elsewhere by pure substring (`'our'` matching inside `'c-our-ier'`) was kept,
  and because it was rare (low df) it could become the anchor term and sit in
  the idf denominator, scoring a spurious match (`courier-pickups`) higher than
  the genuine one (`warehouse-*`, matching `'warehouses'`) — measured missing
  `COVERAGE_THRESHOLD` by 0.005 in the reported case. A function-shaped token
  is now dropped at ANY df, not only df==0, provided at least one content
  token survives (an all-function query is left untouched, byte-identical to
  0.6.1). `'where are our warehouses'`: 1 wrong result → 3 correct results.
- **N3' — the D2L rescue ladder still stopped at the first appending probe
  when it shouldn't have.** N3 (0.6.1) ordered probes by selectivity but kept
  the original "stop after the first append" discipline. For a query naming
  TWO concepts that both have answers (`'reklamacji magazynie'`), stopping
  after the first discarded a row that had already been fetched and paid for.
  The ladder now continues while `len(out) < cap` instead of breaking at the
  first append; fan-out is unchanged (all probes are fetched up front).
- **Coverage computation no longer assumes every matched token survived the
  drop step.** `cov = sum(idf.get(t, 0.0) for t in e["m"]) / total` — a
  candidate that matched ONLY a since-dropped function/negation token now
  scores 0 coverage instead of raising `KeyError` or (pre-fix) riding that
  token's idf into relevance.

### Added

- **N5 — negation-word policy.** Dropping a negation word (`'not'`, `'nie'`,
  `'nicht'`/`'kein'`...) as a function word left the query answered as if it
  were never negated (`'contract not approved'` → the record saying it WAS
  approved). Full-text search has no negation handling either way, so this is
  a policy decision, not a quality regression: `NEGATION_POLICY` (module
  constant, default `"abstain"`) makes a dropped negation word abstain the
  query (`[]`) instead of silently answering the opposite. `"ignore"`
  preserves the pre-N5 behavior as an explicit opt-out. Verified against the
  full suite with zero regressions.
- **N1' diagnostics channel.** A coverage-ratio alternative to the df==0
  abstention rule ("abstain only when supported-token coverage falls below a
  fraction of the query") was implemented and measured, then rejected on
  evidence: the paraphrase class and the abstention class collide at
  identical coverage ratios with opposite required outcomes (e.g. 0.667 for
  both an answerable multi-word question and an unanswerable
  short-discriminator query) — `df` cannot distinguish an unsupported
  CONNECTIVE VERB from an unsupported DISCRIMINATOR without a signal this
  module does not have (morphology/POS). `DF0_ABSTAIN_POLICY` is recorded as
  a documented sentinel (`"any"`, the only supported value) rather than
  shipped as a second, unsafe code path. What ships instead: an optional
  `diagnostics: dict | None` kwarg on `multi_record_search`, populated with
  `abstained`, `abstained_on`, `dropped_function`, `negation_dropped`, and
  `coverage` — additive, zero extra searches, zero precision cost. `count: 0`
  stops being indistinguishable from "nothing is stored"; a caller reading
  `abstained_on` can retry `tiers="entity"` with the one word to drop instead.

### Changed

- Default runtime behavior for a query containing a negation word that gets
  dropped as a function word: previously answered with the record asserting
  the opposite (silently wrong); now abstains (`[]`) unless `NEGATION_POLICY`
  is explicitly set to `"ignore"`. This is the one behavior change in this
  release that isn't purely additive — flagged here per SemVer minor
  (real query results can change for negated inputs, everything else is
  either a precision improvement or additive).

### Provenance

cryptoxdylan (external contributor) independently reproduced both the
2026-08-07 and 2026-08-13 Kravento/Bilbo reports as pytest regression suites
against clean PyPI installs, confirmed F1/F2/F3/N2/N3 closed, and reproduced
N4/N1'/N3'/N5 live on 0.6.1 (black-box, freshly-spawned MCP process included).
He also flagged a packaging hazard closed in this release cycle (see mcp/
hermes CHANGELOGs): `sibyl-memory-mcp` and `sibyl-memory-hermes` floored on
`sibyl-memory-client>=0.5.0`, so an MCP- or Hermes-only `pip install -U` was a
silent no-op once `sibyl-memory-cli`'s tighter floor was the only thing
actually pulling a newer client. He built and emailed a working patch with a
339/343-passing test run; that attachment did not survive the Gmail-attachment
retrieval path intact (gzip CRC mismatch, confirmed corrupt against two
independent decode paths in the same session this shipped). The fix above is
SIBYL's independent reimplementation against his detailed written analysis,
verified against the scenarios he reproduced rather than his exact bytes.

## [0.6.1] - 2026-08-16

Multi-language search, part 3 (Kravento / Bilbo Polish evaluation, follow-up to
0.6.0). Three targeted recall fixes on the retrieve-then-verify + rescue path.
`search()` and `multi_record_search()` output shapes are unchanged and every
change is strictly append-only / additive-at-cap, so this stays a
backward-compatible release: no migration, no downstream (`mcp`/`hermes`/
`langgraph`/`cli`) code change required. Regression gate held: existing client
suite green; the SDK `client.search()` PL/EN twin battery stays **PL 16/16,
EN 16/16** at 32 / 300 / 3000 entities. The headline of this release is that the
agent-**default** MCP path (`memory_search` with `tiers` omitted →
`multi_record_search`) now reaches that same parity on natural question-shaped
queries: a faithful default-path battery (16 parallel PL + 16 EN facts, Polish
stored inflected and queried in a DIFFERENT inflection) moved **PL 9/16 → 16/16
and EN 15/16 → 16/16** — every prior miss was a zero-df function word the lexicon
did not recognise (N1 / Finding B), not a content-shaped abstention.

### Fixed

- **N1 — question-shaped queries abstained on the default (multi-record) path.**
  `multi_record_search` abstains (`return []`) the moment any significant term has
  zero corpus support, so a discriminating absence (`rejected`, an injection
  token) correctly collapses the query. But the module stoplist `_STOP` was 23
  English words with **no interrogatives**, so a zero-support *function* word
  (`kiedy`, `gdzie`, `jest`, `when`, `who`, `how`) survived tokenization and
  collapsed the *whole* query — the agent-default MCP path (`memory_search` with
  `tiers` omitted) and the Hermes `sibyl_search` rail both returned nothing for a
  natural question. The df=0 gate now classifies the zero-df token against a
  **curated function-word lexicon ONLY** (`_DF0_FUNCTION`: EN interrogatives /
  auxiliaries / modals / pronouns + declined PL copula/pronoun paradigms with
  their ASCII de-diacritic twins + compact DE/FR/ES/CZ sets): a listed
  **function-shaped** token is **dropped** (it carried no corpus signal by
  construction), while **anything not in the lexicon** (a content word of any
  length, a ticker / codename / brand code, a digit-bearing identifier, any
  non-ASCII content token) still **hard-abstains**. The drop happens before
  idf / min_df / anchor computation, so the coverage denominator excludes the
  dropped token (`kiedy jest inwentaryzacja` scores coverage 1.0 on
  `inwentaryzacja`) and the injection / `rejected` abstention contract is intact
  (`co0001 nonexistenttokenzzzq report` still returns `[]`).

  **Finding B (adversarial-panel expansion).** The first cut of `_DF0_FUNCTION`
  carried only a compact interrogative/copula set, so natural PL/other-language
  questions still zeroed on the default path when they used an inflected function
  form the lexicon missed (the fusional być paradigm is the worst offender:
  a future/present/past *person* the store never carries — `będą`, `jesteśmy`,
  `będę` — collapsed the whole query). The lexicon is now widened to the
  high-frequency function inventory: the **full być paradigm** (present / future /
  past / conditional, incl. ASCII de-diacritic twins), the **completed
  interrogative/relative family** (`kim`, `czym`, `jacy`, `skąd`, `dokąd`, the
  declined `który`/`jaki`/`czyj` forms), common **PL prepositions, conjunctions,
  particles, and pronouns/possessives**, plus the obvious missing high-frequency
  **EN / DE / FR / ES / CZ** function words. HARD RULE held throughout — every
  entry is a genuine function word safe to drop when absent; known content
  collisions were deliberately excluded (`bez`=lilac, `ten`/`nas`/`nią`, the
  number words `one`/`ten`, `mine`/`can`/`may`, DE `die`/`war`/`man`/`hat`, FR
  `car`/`par`/`son`/`ton`, ES `son`/`con`/`sin`/`era`, CZ `byt`). Faithful
  default-path battery (16 PL + 16 EN, question-shaped, tiers-omitted):
  **PL 9/16 → 16/16, EN 15/16 → 16/16**; SDK `client.search()` unchanged at
  **16/16 / 16/16**.

  Classification is **lexicon-only, by deliberate design.** An interim revision
  additionally dropped any `<=4`-char ASCII-alpha zero-df token; that length net
  was reverted before release because length is not a function-vs-content signal.
  It swept in exactly the short discriminators an entity/company store is queried
  by (`acme`, `acer`, `weth`, `usdc`, `aero`, `visa`, `ford`, `meta`, `ikea`,
  `sol`, 3-4-letter names) and, for an *absent* such term, dropped-then-collapsed
  the query into a cross-entity firehose (`acme report` returned 10 unrelated
  reports) instead of abstaining; it also let arbitrary short garbage tokens
  `continue` past the df=0 early-abort, reopening the CORE-6/MH-3 per-token fanout
  bound (`return []` on the first content-shaped absence is restored, so a 24
  short-garbage-token query issues one `client.search()`, not 24). The safe
  direction is preserved: an **unlisted** function word (any language, any length)
  falls through to hard-abstain — over-abstain, never over-recall. **Known limit:**
  because the classifier is a curated lexicon and Polish/German are fusional, this
  is an ongoing lexical-coverage burden, not a solved problem — inflected forms
  outside the lexicon (rare oblique cases, tense/person conjugations) still
  collapse a question to `[]`; widen `_DF0_FUNCTION` to cover them as they surface.
- **N2 — a cap-filling relaxed single-token head suppressed the F2/D2L rescue.**
  When strict AND missed and the relaxed *single-token* last resort filled the cap
  with rows sharing one common token, the `if len(out) < cap` guard skipped both
  the F2 shadow and the D2L stem rescue, and an inflected target that lost the
  FTS-rank lottery for a head slot never surfaced (19 junk rows FOUND the target,
  20 junk rows LOST it — exactly cap arithmetic at limit=20). `search()` now holds
  back a small tail reserve (`max(1, cap//4)`) of the relaxed single-token head so
  the rescue stages run, then **backfills** the held rows after the rescue: when
  the ladder rescues nothing the output is byte-identical to before; when it
  rescues K rows the result is truncated-head + K rescue rows + backfilled held
  rows, still exactly `cap`, still dedup-safe. **Strict-head invariant preserved:**
  the holdback can only fire when `_search_strict` returned `[]` (the sole path
  into the relaxed loop), so a non-empty strict head is never trimmed or
  reordered.
- **N3 — the stem-rescue ladder probed by length, not selectivity.** The D2L
  ladder ordered uncovered stems longest-first and stopped at the first that
  appended, so a saturated high-frequency stem (`aktualiza`, 20 rows) beat a
  discriminating one (`cenni`, 1 row incl. the target) purely because it was
  longer — the target was never probed. The ladder now pre-fetches the uncovered
  stems and orders them by **measured selectivity** (fewest hits = most
  discriminating), with stem length as the tie-break, then stops at the first
  probe that appends. The pinned stop-at-first-append discipline and length
  tie-break output on ties are unchanged
  (`test_covgate_stem::test_ladder_longest_first_and_stops` green verbatim); only
  *which* probe wins when hit counts differ.

  **Panel Finding A (regression fix, must-not-ship).** An interim cut bounded the
  fetch by slicing the probe SELECTION to the `_STEM_PROBE_MAX=8` **longest**
  uncovered stems before measuring selectivity. That truncated the candidate set:
  a query with >8 uncovered stems whose only reachable target sits on a stem past
  position 8 (in longest-first order) never probed it — a recall regression versus
  0.6.0, which probed **every** uncovered stem. The slice (and the constant) were
  removed: the ladder now fetches **all** uncovered stems, so no reachable target
  is dropped, and orders them by selectivity as above. Fan-out is bounded exactly
  as in 0.6.0 — by the uncovered-stem count, which equals the query's match-token
  count — with no NEW cost exposure: the default MCP path reaches `search()` with
  single-token queries (≤1 uncovered stem each), and `multi_record_search` bounds
  its own caller upstream via `_MAX_FANOUT_TOKENS`. Guarded by
  `test_probe_selectivity::test_no_truncation_beyond_probe_cap` (a 9-uncovered-stem
  query whose most-selective probe is the shortest stem — dropped by the old
  `[:8]` slice, surfaced by the fix).

## [0.6.0] - 2026-08-12

Multi-language search, part 2 (Kravento / Bilbo Polish evaluation). On the same
corpus, code, and store the Polish recall was 81% against English 100% — a gap
that is entirely linguistic, not a bug. Two search-path changes (F2 + D2L) close
it. Official harness (32-query PL/EN twin battery, LIMIT 10): **Polish recall
8/16 → 16/16, English recall stays 16/16**; a 6-query realistic PL multi-token
extension battery goes **4/6 → 6/6**. `search()` output shape is unchanged and
every change is strictly append-only, so this is a backward-compatible minor
release: no migration, no downstream (`mcp`/`hermes`/`langgraph`/`cli`) code
change required.

Also aligns stale `2 MB` free-tier-cap references (comments, docstrings,
README) to the current `5 MB` cap (raised in 0.5.0); no behavior change.

### Fixed

- **F2 — the folded-trigram shadow was suppressed by any non-empty primary hit.**
  `search()` fired the v0.5.0 shadow only on a total zero-hit (`if not hits:`),
  so a single weak/English strict hit hid same-fact rows in other languages —
  e.g. `search("packshot")` returned the English `packshots` row and skipped the
  Polish `packshoty` row; the failure worsens as the store fills. The shadow now
  runs **unconditionally** and its hits are **appended** after the primary
  (strict/relaxed) hits, deduped on the `(tier, category, key)` identity triple
  and capped at `limit`. **Append-only invariant:** the primary head is never
  reordered or dropped — the shadow can only extend the tail — so English recall
  and existing ranking cannot regress. (harness: Polish recall 8 → 9/16 from F2
  alone; the packshot class is fixed.)

### Added

- **D2L — coverage-gated stem rescue with a rescue ladder.** Fusional languages
  inflect by REPLACING endings (`reklamacj-a/-e/-i` share the stem `reklamac`),
  so porter token-equality (strict) and the trigram substring test (raw shadow)
  BOTH fail for an inflected query. D2L turns ending-replacement into the
  substring problem the shadow already solves, appended after the F2 head:
  - **Coverage gate.** Only query tokens whose stem is *not* already
    substring-covered by the assembled head trigger stem work. A query porter or
    the raw shadow already answered runs no stem probe at all — this is what
    keeps the pass off the English path at scale (stem probes measured 32/32 →
    7/32 per query battery-wide).
  - **Rescue ladder.** When some token is uncovered, the fully-stemmed query
    (all tokens stemmed, AND-ed) is tried first; if it appends nothing the
    uncovered stems are probed as single tokens, **longest-first, stopping at the
    first that appends**. This rescues realistic multi-token PL queries whose
    full-stemmed AND matches nothing (e.g. `status reklamacji` → the
    `reklamacja` row), the class an unconditional single stem pass hard-misses.
  - **Parameters:** `_STEM_MIN_TOKEN=5`, `_STEM_DROP=3`, `_STEM_FLOOR=5`, digit
    tokens exempt (`q3`/`v2`/`k8` stay exact). Crude fixed-length truncation is
    deliberate — there is no Polish stemmer in the stdlib, and *because* it is
    crude it survives stem-internal palatalization a rule-based stemmer would
    diverge on (`wysyłka`/`wysyłce` both keep the `wysył` prefix). `drop=3`
    covers the 2–3 char ending classes of Polish/Czech/Russian declension
    (`drop=2` misses `-ach` locatives such as `magazynach`); `floor=5` keeps
    stems long enough to avoid cross-lemma collisions at scale.
  - Applied ONLY in the append-only rescue path of `search()`, gated on
    coverage — never on the strict path — so the primary index and its ranking
    are untouched, and every probe goes through `_shadow_fallback` (errors
    contained to `[]`).

  **Measured trade-off (official 32-query battery vs F2-only):** mean precision
  0.980 → **0.9479**, precision@1 0.960 → **0.9688** (up), fp_total 1 → **4**.
  The two coverage-gated FPs D2L removes relative to an unconditional stem pass
  are exactly the avoidable class (same-stem noise appended to a query the head
  already answered). **Two documented residuals:**
  1. Shadow/stem appends are skipped once the head fills to `limit`, so the
     old zero-hit-suppression shape reappears exactly at limit-saturation —
     inherent to append-only + cap.
  2. Same-stem SIBLING rows are not appended when the queried concept already
     surfaced in the head (the coverage gate suppresses them). This is
     consistent with the battery's own false-positive convention (the same
     sibling-append event is scored as noise elsewhere in the battery); full
     sibling-vs-noise separation is semantic and requires F3.

### Roadmap (not in this release)

- **F3 — substring/trigram is structurally mismatched with fusional morphology.**
  Polish query-is-substring-of-stored held 0/7 vs English 7/7. The true fix is
  language-aware **lemmatization at index time** (a pluggable per-language
  analyzer writing lemmas into the shadow) plus relevance scoring to separate
  same-lemma siblings from same-stem noise. Out of scope here.

## [0.5.0] - 2026-08-06

Multi-language search. The default (linker) search path was effectively
Latin-ASCII-only: a 100-language write+query sweep passed **21/100**. This
release takes it to **100/100** (measured; deterministic dataset — one
native-script write+query probe per language). Absorbs PR #25 (0.4.20).

### Fixed

- **Non-ASCII and non-Latin queries silently returned zero results on the
  default (untiered) search path.** Reported via Discord as "Polish diacritics
  break full-text search" (`search("Bełżyce")` → 0 hits). The fault was never
  FTS5 (`porter unicode61` folds decomposable diacritics correctly and a direct
  `entities_fts MATCH` returns the rows); it was the query-side linker plus an
  index that cannot see inside an unbroken token. Five distinct mechanisms:
  - **M1 — ASCII-only linker regex.** `_significant_tokens` tokenized with
    `[A-Za-z0-9]+`, shattering any non-ASCII word into index-absent fragments
    (`Bełżyce → ['yce']`) or none at all (Cyrillic/CJK/Greek/Arabic → `[]`), and
    the linker abstains as soon as one token has df=0. (This is PR #25's fix.)
  - **M2 — ASCII-calibrated length filter.** The `len(t) > 2` floor dropped
    2-char CJK/Hangul words (the norm in those scripts) and Brahmic combining-mark
    fragments, emptying the token list → unconditional abstain. Now applied to the
    ASCII path ONLY; short non-ASCII tokens are kept.
  - **M3 — lowercase-before-split.** `query.lower()` ran before tokenization, and
    `'İstanbul'.lower()` emits `i` + combining U+0307, which then split. Now we
    split first and case-fold per token only when the fold is length-preserving.
  - **M4 — glued/single-token runs (index side).** `unicode61` indexes an
    unbroken letter run as ONE token, so `MATCH '北京'` never hits `北京烤鸭`; same
    shape for Thai fragment glue and Zulu/Bantu locative compounds. No query-side
    fix can reach inside a token — this needs substring matching.
  - **M5 — non-decomposable fold gap.** `ł ß ø æ đ ı œ þ ð` have no canonical
    decomposition, so no `remove_diacritics` setting folds them; `Belzyce` cannot
    find a stored `Bełżyce` without an explicit fold map on both sides.

  Staged, measured recovery on the 100-language harness:
  **21 (baseline) → 69 (PR #25 `\w+`) → 87 (script-aware linker, M1+M2+M3) →
  100 (folded-trigram shadow, M4+M5).**

### Added

- **Script-aware `multi_record._significant_tokens` (M1+M2+M3).** Splits before
  case-folding, keeps short non-ASCII tokens, and preserves **byte-identical
  ASCII behaviour** (the pre-0.5.0 stopword + `len>2` + lower token stream is
  unchanged). Supersedes PR #25's one-line `\w+` change.
- **Folded-trigram search shadow (`shadow.py`, schema v4; M4+M5).** A single
  standalone FTS5 `trigram` table (`search_shadow`) holding a FOLDED rendering of
  all searchable text across the four tiers (entity/state/reference/journal),
  maintained by DB-side triggers, consulted ONLY as a **zero-hit fallback** in
  `MemoryClient.search()`. Gives substring semantics (matches inside CJK/Thai/
  Bantu/compound tokens — and partial-word/typo'd-suffix queries in any language
  as a free side effect) plus an explicit fold map that closes the
  non-decomposable class in both directions (`Belzyce` ↔ `Bełżyce`).
  - **Strictly additive → benchmark-safe by construction.** The fallback fires
    only where today's answer is `[]`; a non-empty strict result is returned
    untouched, so ranking/recall of existing hits (English recall, LongMemEval)
    cannot regress. `prefix=True` searches are unaffected.
  - **No native dependency.** `trigram` is built into SQLite; the shadow keeps the
    plugin shipping pure-Python wheels. `remove_diacritics 1` is used on SQLite
    ≥ 3.45 and a bare `trigram` below it (runtime-selected; a DB moved across that
    boundary self-heals).
  - **Correct under old clients.** The triggers are DB-resident SQL, so a 0.4.x
    client writing to a migrated DB keeps the shadow in sync without knowing it
    exists; old clients simply never query it.

### Changed

- **Schema v3 → v4 migration** (`storage.py`): creates `search_shadow` + its
  triggers and backfills all four tiers, stamped in `PRAGMA user_version`
  (`_SHADOW_MARKER = 4`) by the same crash-atomic machinery as the FTS-rebuild
  marker — a crash anywhere rolls the whole transaction back and the next open
  retries. The fast path returns only when the shape is v3, the marker is ≥ 4,
  the shadow table is present, **and all 10 shadow triggers exist** (F1), so a
  dropped/corrupt shadow — including an out-of-band drop of a single maintenance
  trigger — is rebuilt on the next open. The shadow is derived state: base-table
  data is never touched and it is always rebuildable.
- PR #25's pinned `xfail` (`Belzyce` → `Bełżyce`) now passes as a normal test
  (its `strict=True` marker is removed).

### Hardening (post-review, 2026-08-06)

- **F1 — migration fast path requires the full trigger set, not just the table.**
  The v4 fast-path precondition previously checked only that the `search_shadow`
  TABLE existed. An out-of-band drop of any of the 10 maintenance triggers left
  the table present but silently un-maintained (writes stopped propagating to the
  shadow), risking a stale/false-positive fallback hit. The fast path now also
  requires the shadow trigger count to be exactly 10 (cheap `sqlite_master`
  lookup, `shadow.shadow_triggers_complete`); on mismatch it falls through to the
  idempotent `apply_shadow_migration`, which recreates every trigger and
  re-backfills — mirroring how the v3 FTS triggers self-heal on each open.
  Regression test drops one trigger out-of-band and asserts the count returns to
  10 and the shadow is consistent again.
- **F3 — one undecodable row no longer voids the fallback.** In
  `shadow.shadow_search`, the per-row `_shape_hit` join back to the base table is
  now wrapped so a single row with an undecodable JSON body (corrupt row, partial
  write, manual edit) is SKIPPED rather than raising out of the whole fallback and
  losing every other valid hit — consistent with the §4.2 "a broken shadow must
  never take down search" containment stance.

### Rollback

The shadow is additive and reversible. To restore exact v3 behaviour (base data
is never affected):

```sql
DROP TRIGGER IF EXISTS entities_ai_shadow;
DROP TRIGGER IF EXISTS entities_au_shadow;
DROP TRIGGER IF EXISTS entities_ad_shadow;
DROP TRIGGER IF EXISTS state_documents_ai_shadow;
DROP TRIGGER IF EXISTS state_documents_au_shadow;
DROP TRIGGER IF EXISTS state_documents_ad_shadow;
DROP TRIGGER IF EXISTS reference_documents_ai_shadow;
DROP TRIGGER IF EXISTS reference_documents_au_shadow;
DROP TRIGGER IF EXISTS reference_documents_ad_shadow;
DROP TRIGGER IF EXISTS journal_events_ai_shadow;
DROP TABLE IF EXISTS search_shadow;
PRAGMA user_version = 3;
```

### Notes / scope

- **Free-tier cap raised 2 MiB → 5 MiB (operator directive, spec §6):** the
  folded copy + trigram index roughly **doubles** the on-disk footprint (measured
  ~2.27× on an English-heavy corpus), which would have made free users reach the
  old 2 MiB cap roughly 2× sooner. Rather than ship that regression, the default
  free storage cap is raised to **5 MiB (5,242,880 bytes = `5 * 1024 * 1024`)** so
  a free user keeps roughly the same effective headroom they had before the shadow
  existed. The client constants `_capcheck.FREE_TIER_CAP_BYTES` and
  `lint.DEFAULT_SOFT_CAP_BYTES` / `TIER_SOFT_CAPS["free"]` now default to 5 MiB,
  and the user-facing cap messages say "5 MB". **Deploy-time parity (outside this
  repo):** the server `pricing.js` default `cap_free_bytes` and the server
  `sibyl_plugin.config.cap_free_bytes` must also be set to `5242880` so the SDK
  and server agree on the cap.
- **Honest scope of 100/100:** one native-script write+query probe per language.
  It does NOT claim full linguistic quality — no CJK/Thai word segmentation
  (substring, not semantic), no cross-script/romanization (`Beijing` will not find
  `北京`), no non-English stemming. Those remain for a future ICU/embedding tier.
- Impact on existing queries is nil by construction: English/ASCII queries take
  the identical pre-0.5.0 path and the fallback is never reached when the primary
  index returns anything.

## [0.4.19] - 2026-07-05

Super-patch: recovery + adjudication of the remaining Fable 10-lens audit
findings (`plugin-hardening-superpatch-plan-2026-07-05.md`), covering
`storage.py`, `client.py`, `learning.py`, `_capcheck.py`, and `_heartbeat.py`.
These fixes compose with the FREE-tier account-level cap aggregation shipped
in 0.4.18 rather than regressing it.

### Fixed
- **Per-thread SQLite connection registry leaked one fd per dead thread (Real
  #2).** `Storage` tracked opened connections in a plain list so `close()`
  could reap connections opened by other threads, but nothing pruned an
  entry once its owning thread exited — a long-lived `Storage` under Hermes
  (fresh thread per turn) accumulated one open connection per finished
  thread until the process hit `EMFILE`. The registry now holds
  `(weakref-to-owning-thread, conn)` pairs; every new registration sweeps
  and closes entries whose owning thread is dead or exited. A cached
  per-thread connection is also liveness-probed (`total_changes` read) on
  reuse, so a handle closed by another thread's `close()` call is detected
  and transparently reopened instead of raising.
- **FTS5 v2->v3 migration was not crash-atomic (Real #3).** The old migration
  ran as three separately-committed steps (drop / recreate / rebuild) with
  no marker; a crash between the drop and the rebuild committing left a
  v3-shaped but empty FTS index, and the shape check then read that as
  "already migrated" — search returned nothing forever. The rebuild now
  stamps a marker (`PRAGMA user_version = 3`) in the same transaction as
  the FTS rebuild, so the marker exists iff the rebuild committed; on open,
  a v3-shaped store whose marker is unset is rebuilt from the intact base
  tables before use. **Note:** this means every existing healthy database
  does a one-time, idempotent FTS rebuild on its first open under 0.4.19
  (the marker was never set by any prior version) — this is expected and
  safe, not a sign of corruption.
- **`TierCache.store()` used a fixed `<name>.tmp` name (Real #5).** Two
  concurrent cap checks — Hermes opens a fresh thread per turn, and
  multiple processes can share `~/.sibyl-memory` — could unlink each
  other's in-flight temp file and crash `os.replace` with a
  `FileNotFoundError`, failing the caller's memory write. `store()` now
  uses `tempfile.mkstemp()` for a unique 0600 temp name in the same
  directory, and a persist failure (disk full, permissions, a lost rename
  race) degrades to "skip caching" — logged, never raised into the
  caller's write path.
- **`accept_proposal`/`reject_proposal` had no in-transaction cap recheck
  or concurrent-review guard (Hardening #8).** Two callers could both pass
  the pre-transaction pending-status check and both commit an accept, and
  accepting a large proposal had no cap enforcement inside the write
  transaction that stages the new `reference_documents` row. Both methods
  now call the cap gate's local (no-network) `check_total_local` with the
  in-transaction logical size before committing, and the `UPDATE` is
  guarded with `AND status = 'pending'`; a `rowcount == 0` (already
  reviewed by a concurrent call) raises `ValidationError` and rolls back
  the whole transaction, including the staged reference-doc write. Skipped
  only for cap gates that don't implement `check_total_local` (advanced/
  estimate-only test doubles) — the production `CapGate` always does.
- **In-transaction CAP-2 recheck could fail open on a 0-byte size read
  (Hardening #16).** `_maybe_recheck_cap` used `storage.logical_size_bytes`
  directly, which returns 0 on any internal error; since this recheck only
  ever runs with a write already staged, a 0 is never a real post-write
  footprint — it means the measurement was unavailable, and passing it to
  `check_total_local(0)` would trivially clear the cap. A 0/failed read now
  falls back to the cap gate's own WAL-inclusive `db_size_fn` (the same
  account-level aggregate the pre-write check uses) before gating, so the
  cap is enforced instead of silently bypassed.
- **CAP-2 absolute-total check swapped shared instance state (Hardening
  #13).** `check_total` previously monkey-patched `self._db_size_fn` with a
  lambda and restored it in a `finally` — not thread-safe: two concurrent
  callers could observe each other's swapped size fn (a crossed total) or
  leave a patched fn behind if the restore was skipped. The absolute size
  is now threaded through as an explicit keyword argument, removing the
  shared mutable state entirely.
- **A failed `COMMIT` poisoned the persistent per-thread connection
  (Hardening #14).** Only the pre-commit path had rollback-on-error
  handling; a `COMMIT` failure itself (disk full, I/O error) left the
  connection mid-transaction, so the next write on that thread raised
  "cannot start a transaction within a transaction" for the rest of the
  session. `COMMIT` is now wrapped: on failure a guarded `ROLLBACK` returns
  the connection to autocommit (chained as `__context__`) and the original
  `COMMIT` error is re-raised.
- **Learner watermark could skip same-timestamp journal rows (Hardening
  #15).** `_last_watermark` cursored on `MAX(ts)`, so two events sharing a
  timestamp (or a backdated event) could be skipped on the next run. The
  learner now cursors on the monotonic journal `rowid`
  (`learning_runs.cursor_after_rowid`, added via an idempotent one-time
  `ALTER TABLE` on existing databases) with the timestamp kept only for
  readable logs; an explicit `since=` timestamp remains a valid escape
  hatch for a manual re-scan.
- **Search query string had no length ceiling (Hardening #9, subsumes
  duplicate finding R15).** `_sanitize_fts5_query` expands every token in
  the input into an ANDed, phrase-quoted term and MATCHes it across up to
  four FTS5 tiers; with no bound, a multi-megabyte / ~200k-token query
  became a ~200k-term MATCH executed four times — a CPU/memory DoS
  reachable from the client, MCP, and Hermes alike. Queries are now
  truncated to 4096 characters before any tokenization (best-effort
  truncate, not a raised error, since search is a read path); real
  natural-language queries are far under the ceiling and are never
  affected.
- **Co-occurrence learner had no bound on per-event tokens or tracked pairs
  (R13).** The co-occurrence detector built every 2-combination of an
  event's distinct tokens (O(tokens²) per event); a single pathological
  event could hold tens of thousands of unique strings and hang the run.
  `_extract_tokens` now caps distinct tokens per event at 64, and the
  detector caps total tracked pairs across a run at 100,000 (already-seen
  pairs keep accumulating hits; new pairs beyond the ceiling are dropped).
  Adjudicated low severity: the learner runs locally over the agent's own
  journal on a paid-tier feature, so the primary threat model is
  self-inflicted, not third-party.

### Security
- **Dict key names leaked verbatim to the Sibyl-routed summarizer prompt
  (Hardening #1).** `_redact_event_for_prompt`'s shape reducer preserved
  literal dict key names (`{"keys": sorted(...)}`) — content can hide in a
  key name as easily as in a value. Dict values are now reduced to a
  `{"key_count", "key_lens"}` shape descriptor (sorted lengths only, no
  literal text or ordering signal) via a shared `_key_shape` helper.
- **Hint redaction was a denylist, not an allowlist (Hardening #11).**
  `_redact_hints_for_prompt` only stripped four explicitly-named
  content-derived fields, so any future content-derived hint field would
  leak by default. Inverted to an allowlist: only known pure-shape/numeric
  fields (`hits`, `cadence_minutes`, `cov`, `confidence`) pass through
  as-is; `shared_keys` is shaped via `_key_shape` (Hardening #1, it carries
  key names); every other field is stubbed to a shape descriptor.
- **Usage heartbeat could leak the account bearer to a non-Sibyl host
  (Hardening #12).** The heartbeat URL is env-overridable
  (`SIBYL_MEMORY_HEARTBEAT_URL`); without a check, an injected override
  would still receive the `Authorization: Bearer` header. The bearer is
  now attached only when the resolved URL is `https` and its host is
  `sibyllabs.org` or a subdomain (checked via `urlparse().hostname`, not
  string matching, so a userinfo-spoofed URL like
  `https://api.sibyllabs.org@evil.com/` resolves to the real host and is
  rejected). Any other scheme/host still gets the heartbeat POST, just
  without the bearer.
- **`TierCache`'s symlink guard was dead code (Hardening #3).** `__init__`
  called `Path(path).expanduser().resolve()`, which follows a symlinked
  cache file before the later `is_symlink()` checks in `load()`/`store()`
  ever run, silently defeating the SEC-11 guard. Only the parent directory
  is resolved now; the cache file's final path component stays literal, so
  a symlinked cache path is detected and refused (never written through)
  while a relocated/containerized home is still canonicalized correctly.
- **Storage and cache directories could persist at a loose mode (Hardening
  #4).** `mkdir(mode=0o700)` is a no-op on an already-existing directory,
  so a pre-existing 0o755 `~/.sibyl-memory` or cache dir kept its umask-
  derived mode. Both `Storage.__init__` and `TierCache.__init__` now
  explicitly `chmod` the directory to `0o700` after `mkdir`, best-effort
  and guarded for chmod-less platforms.
- **WAL/SHM sidecar files had no symlink/hardlink guard (Hardening #10).**
  SQLite opens `<db>-wal`/`<db>-shm` at fixed paths beside the main file; a
  planted symlink there could redirect the write-ahead log (which holds
  committed rows before checkpoint) to an attacker-chosen file, and the
  perms-tightening chmod could retarget through it. `Storage` now rejects
  a symlinked or hardlinked sidecar before opening, and the perms-
  tightening pass uses `follow_symlinks=False` where the platform supports
  it (skipping entirely where it doesn't) so a sidecar planted after open
  is never chmod'd through.

### Changed
- Corrected the `_capcheck.py` module docstring to enumerate the real
  check-write payload (`account_id`, `session_token`, `current_size_bytes`,
  `proposed_delta_bytes`, and, when a signed claim is present,
  `credentials_signature` + `credentials_claim`); the prior "only
  (account_id, current_size_bytes, proposed_delta_bytes)" wording
  under-stated it (Contract PII, code half). The wire payload is unchanged;
  dropping the claim's `email`/`wallet` is the policy-gated follow-up.
- Packaging: added the `Repository` URL
  `https://github.com/Sibyl-Labs/Sibyl-Memory` to `[project.urls]`
  (previously omitted) (R27).

## [0.4.18] - 2026-07-05

### Fixed
- FREE-tier 2 MB cap now aggregates across every memory store the machine
  resolves, instead of being enforced per DB file (Discord report 2026-06-11:
  6.29 MB across 9 stores on one FREE account, each store individually under
  the cap). New `aggregate_db_size()` in `_capcheck.py` sums the SDK default
  store (`~/.sibyl-memory/memory.db`), the Hermes adapter store
  (`$HERMES_HOME/sibyl/memory.db`, `HERMES_HOME` defaulting to `~/.hermes`),
  every Hermes per-profile store (`$HERMES_HOME/sibyl/profiles/<p>/memory.db`),
  the `SIBYL_MEMORY_DB` override, and the active `db_path` — deduped by
  resolved path; missing/unreadable candidates contribute 0 and the walk
  never raises. Each candidate is sized WAL-inclusively via `db_size_bytes`
  (SQLite logical size, `page_count x page_size`), so the aggregate COMPOSES
  with CAP-1 (0.4.15) rather than regressing it — a plain per-file `st_size`
  sum would have under-counted data still sitting in a store's `-wal`
  journal. Free accounts already over the aggregate cap are blocked on their
  next write by design (the boundary check now sees the true account
  footprint); paid tiers are unaffected (uncapped). Regression tests cover
  both the sibling-store aggregation (two 1.5 MB stores -> 3 MB -> blocked,
  with the check-write payload reporting the aggregate) and the
  WAL-inclusive sizing (fails if the aggregate reverts to `st_size`); a new
  `tests/conftest.py` autouse fixture isolates HOME/USERPROFILE/HERMES_HOME
  and clears `SIBYL_MEMORY_DB` so the candidate walk can never leak a real
  local store into the suite.

## [0.4.17] - 2026-06-30

### Security
- Self-learning privacy contract enforced on the Sibyl-routed summarizer path
  (#14, B005). The `VeniceX402Summarizer` relays prompts through Sibyl Labs'
  inference proxy, so per the module contract "only the prompt summary leaves
  the device, never the underlying memory content." The prompt builder
  previously embedded full journal-event payloads (`events[:10]`) regardless of
  path. The Sibyl-routed path now redacts events to metadata only (keys /
  counts / timestamps — no raw content) before assembling the prompt. The BYOK
  path (`BYOKSummarizer`) is unchanged and keeps full fidelity: the user
  controls their own inference destination.
- Extended the same redaction to the `hints` dict on the Sibyl-routed path
  (multi-model audit follow-up, 2026-06-30). `hints` carried content-derived
  fields (`action_signature`/`pair`/`slug`/`title` — normalized first-N tokens
  of the raw `acted` string), which the initial #14 fix left serialized verbatim
  into the prompt. `_redact_hints_for_prompt` now reduces those fields to a shape
  stub on `redact=True`, while structural hints (`hits`, `cadence_minutes`,
  `cov`, `confidence`, `shared_keys`=key names) are preserved. The regression
  test was hardened to assert no `acted`-derived token survives in the prompt.

### Fixed
- Search fallback: short function words and contraction tails (`us`, `me`, `am`,
  `re`, `ll`, `ve`) are now excluded from the zero-hit single-token recovery
  step, so they can no longer trigger a spurious last-resort match now that the
  CORE-11 (0.4.15) `len>=2` floor admits short tokens. Strict search is
  unaffected (it keeps every token); this only tightens the relaxation step.
  Complements CORE-11's short-identifier recall (q3/v2/k8). Operator-directed,
  benchmark-validated (phrasing-invariance: in-contract recall held at 100%,
  zero new distractors).

### Hygiene
- `_heartbeat.py`: the telemetry `urlopen` call is now wrapped in a `with`
  context manager so the HTTP socket closes deterministically instead of
  waiting on GC (#15). Behavior unchanged.
- `client.validate_identifier`: the forbidden-control-character error message
  now reports the correct character index via `enumerate()` instead of
  `value.index(ch)`, which returned the first occurrence of the character
  rather than the position being scanned (#15). Message accuracy only;
  validation behavior unchanged.

## [0.4.15] - 2026-06-25

Pre-launch security audit hardening.

### Security
- Cap enforcement now counts the full footprint including the `-wal`/`-shm`
  sidecars (previously only the main DB file, so burst writes under-reported).
- The cap gates on the absolute resulting footprint, re-read inside the write
  transaction (CAP-2) rather than per-write estimate. The in-transaction recheck
  is LOCAL-ONLY (no network call under the write lock).
- Fail-open now fails CLOSED for a no-account / no-cache user (was: allowed up
  to 4x the cap when the verify endpoint was unreachable).
- A 401/403 from tier verification is treated as an authoritative "not entitled"
  and hard-denies; it is no longer classed as a retryable/transient code.
- `current_cap()` no longer honors a null-account "uncapped" cache (SEC-13).

### Fixed
- `json.loads` on every read path now raises a typed `StorageError` on a
  malformed stored row instead of a raw `JSONDecodeError`.
- Shared limit clamp on `list_entities`/`read_events`/`search`/`search_entities`
  (no unbounded or negative limits; `read_events(limit=-1)` is no longer
  unbounded).
- `set_tenant` validates the tenant id. `archive_entity` cap-check moved inside
  its transaction. Cross-thread connections are all closed in `close()`. A
  failing ROLLBACK no longer masks the original error. `multi_record` corpus
  count via `COUNT(*)` instead of a full-table scan. Short-identifier recall
  (q3/v2/k8) restored.

## [0.4.14] - 2026-06-19

### Fixed

- **Silent write loss under sustained load (CRITICAL; beta deadguy 2026-06-17,
  report 3.1).** When tier verification was unreachable (e.g. the check-write
  endpoint returning a rate-limit-shaped 401 under a heavy write burst) and there
  was no cached tier, the write was rejected with `TierVerificationError` -- and a
  caller that ignored ok/error lost the write silently. The check-write transport
  now does a bounded retry with backoff on transient codes (401/408/425/429/5xx),
  and a no-cache write whose verification is unreachable now FAILS OPEN (allows the
  write) up to a 4x safety ceiling, logging a warning, instead of dropping data.
  Durability is preserved during outages; the server reconciles tier/cap on the
  next reachable check. Past the ceiling it hard-blocks. Test: `tests/test_capcheck.py`.

### Added

- **Paraphrase zero-hit search fallback (beta deadguy 2026-06-14).** Natural-language
  queries miss under strict token-AND (+ Porter stem). `MemoryClient.search` now
  retries with relaxed variants (stopwords stripped, then rarest token) ONLY when
  the strict search returns nothing. Strictly additive: a non-empty strict result
  is returned untouched, and single-token / prefix queries (the `multi_record`
  path) never trigger it. Test: `tests/test_paraphrase_fallback_2026_06_19.py`.

- **Single-value size ceiling (red-team F5, 2026-06-17).** `_check_json` rejects a
  single serialized body over 1 MiB with a clear, recoverable error, so one
  oversized value can't flood agent context on recall/search.

- **Bounded learner scan (red-team F6, 2026-06-17).** `Learner._load_events` caps
  the per-run journal scan at 10k events (DoS backstop); the watermark advances so
  a large backlog drains across runs instead of spiking memory/CPU in one pass.

## [0.4.13] - 2026-06-16

### Added

- **Usage heartbeat (privacy-preserving).** Local-first memory operations never
  touch the network, so an account's request count under-reported real usage (a
  heavy user and a tire-kicker looked identical). The client now sends a
  debounced, fire-and-forget POST to `/api/plugin/heartbeat` carrying ONLY an
  aggregate operation COUNT -- no memory content, no query text, no PII beyond
  the `account_id` already held. Flushes every 15 ops or 10 min and once at
  process exit; no-op without an `account_id`; opt out with
  `SIBYL_MEMORY_TELEMETRY=0`. Never blocks or breaks a memory op; offline-safe.
  Closes the usage-visibility blind spot the beta reports surfaced (deadguy
  2026-06-14). Regression tests: `tests/test_heartbeat_2026_06_16.py` (7 cases).

### Documented

- **`forget`/archive is recoverable, not a hard delete.** Clarified that
  archiving moves an entity into `archived_entities` (recoverable, stored
  plaintext at rest) rather than destroying it; a hard-delete path is tracked
  separately. (big-patch PKG-11)

## [0.4.12] - 2026-06-11

### Fixed

- **`set_reference(key, body)` raised StorageError on a dict/list body**
  (beta report VRTX ISSUE-003, 2026-06-11). `body` now accepts a `str` or a
  JSON-serializable `dict`/`list`; mappings/sequences are coerced to canonical
  JSON (via the same `_check_json` guard used for metadata) before the INSERT.
  Any other type raises a typed `ValidationError` naming the `body` parameter
  instead of an opaque DB-layer failure. Regression test:
  `tests/test_set_reference_body_2026_06_11.py` (4 cases). (big-patch PKG-5)

## [0.4.11] - 2026-06-11

### Added

- **Cross-tenant search isolation regression test**
  (`tests/test_smoke.py::test_tenant_search_isolation`). Two tenants index
  near-identical "billing outage refund escalation ticket" vocabulary in the
  same database file; asserts `search_entities`, cross-tier `search()`, and
  `multi_record_search` each return only the calling tenant's rows (Discord
  2026-05-31 parallel-workflow report). Passes against current source: SQL
  `tenant_id` filtering holds on all three surfaces, so the reported sibling-
  case bleed is attributed to within-tenant topical ranking, addressed by the
  0.4.9 anchor-first resolver and 0.4.10 proximity re-rank. Tests only, no
  source change. (bugflow)

### Fixed

- **`search()` silently returned `[]` on unknown tier names.** Unknown values in
  `tiers` now raise `ValueError` (defense in depth behind the MCP-level
  whitelist; direct callers such as the Hermes provider inherit the fix).
  (bugflow)
- **Lint timestamp cutoffs were malformed and used deprecated
  `datetime.utcnow()`.** Python `%f` means microseconds (not SQLite's
  seconds-with-millis), so stale-entity and flagged-actor cutoffs rendered as
  `HH:MM:<microseconds>Z` with no seconds field, breaking the lexicographic
  comparison against stored `HH:MM:SS.sssZ` timestamps. Cutoffs now use
  `datetime.now(timezone.utc)` with an exactly aligned `%Y-%m-%dT%H:%M:%S.000Z`
  format. (bugflow)

## [0.4.10] - 2026-06-08

### Fixed

- **Multi-word search precision: "near-negative decoy" false positives** (chainriffs +
  KAPPA Discord reports against v0.4.2 / v0.4.4; triaged from the 2026-06-06 bug intake).
  The AND-of-tokens default (v0.4.2+) gives full recall but lets short rows that contain
  the query tokens in an unrelated context out-rank the real answer under BM25, which
  rewards term density over proximity (reported precision ~73% at recall 100%).
  `search()` and `search_entities()` now re-rank multi-word results by match tightness
  before the limit is applied: contiguous query phrase (bucket 0) > all tokens within a
  small window (bucket 1) > scattered tokens (bucket 2), with the existing BM25 `rank` as
  the in-bucket tiebreaker. No hit is dropped, so **recall is unchanged**: only the order
  changes. Single-token and `prefix=True` queries keep plain BM25 order, so
  `multi_record_search` (the anchor-first resolver, which only issues single-token
  searches) is unaffected. New module helpers `_match_tokens` / `_normalize_text` /
  `_proximity_bucket` / `_min_cover_span`; regression suite
  `tests/test_proximity_rerank_2026_06_08.py` (9 tests). Verified: scattered-decoy
  precision@1 0/6 -> 6/6 on the reproduction corpus, 119/119 suite green.

  Residual (out of scope, by design): a decoy that contains the *exact query phrase* is a
  genuine lexical-semantic collision, the documented graph-native / GNN-tier case, not
  resolvable by keyword ranking.

## [0.4.9] - 2026-06-06

### Fixed

- **Multi-record search recall/precision regression at scale (anchor-first hybrid resolver).**
  `multi_record_search` used a corpus-fraction selectivity cutoff
  (`round(0.15 * corpus_n)`) calibrated on a 24-record reconstruction. Past ~150
  records the cutoff lost meaning: almost every term read as "selective," so
  cross-cluster records cleared the gate and polluted results (tester Sylvain
  Runs 16/17, ~0.36 recall at 50-100 companies). The resolver is now anchor-first:
  anchor terms are the rarest tokens, defined RELATIVE to the rarest query term
  (`df <= ANCHOR_BAND * min_df`, scale-invariant). The gate is a HYBRID: a
  candidate survives if it is in the anchor's cluster (matches an anchor term) OR
  clears the high-coverage bar `ANCHOR_HYBRID_HI` (genuinely relevant despite
  lacking the rare anchor). A pure strict filter killed cross-cluster pollution
  but over-dropped natural-language evidence; the hybrid keeps both. Abstention
  (zero-support term) and the terminal/prep gates are unchanged. Validated two
  ways: (a) synthetic 480-record workflow A/B — full recall, 0 cross-cluster
  pollution vs the old code's 1,920 polluting hits over 120 queries (matches
  tester Runs 24-29); (b) real-data LongMemEval retrieval diagnostic — per-question
  (oracle) retrieval is not regressed (NEW >= OLD, +3.4pts), and in a combined-
  store contamination stress NEW cuts cross-question pollution ~29% for a small
  recall trade. Regression guard: `tests/test_anchor_resolver_2026_06_06.py`.

- **Cross-tier rank comparability.** `search()` BM25 ranks are not on a common
  scale across FTS tables (`journal_events_fts` is contentless). Added a tier
  tiebreaker so content tiers (entity/state/reference) sort before journal at equal
  rank, layered on the existing 0.4.7 journal cap. (tester email 19e7eb3096b4dae5)

### Added

- **`search_entities(category=...)`.** Optional exact-match category anchor on
  entity FTS, removing topical bleed across categories on multi-entity workloads
  (tester email 19e7e75af0b7780a). Backward compatible (defaults to all categories).

Sourced from Sylvain's beta Runs 24-29 + the bugflow batch dedup; this single
patch also supersedes ~20 already-fixed entries that had accumulated in the
bug-batch queue.

## [0.4.8] - 2026-06-04

### Fixed

- **Prefix-mode FTS5 crash on all-operator queries.** `_sanitize_fts5_query(prefix=True)`
  routed tokens through `_drop_fts5_operator_tokens`, whose keep-all fallback
  (`return kept or tokens`) re-introduced raw operator keywords when every token was an
  operator. The prefix path then appended `*`, producing invalid FTS5 (`OR*`, `AND*`,
  `NOT*`) that crashed the SQLite FTS5 parser with a syntax error. Prefix mode now
  hard-drops operator keywords with no fallback and returns an empty match for an
  all-operator query (no safe expansion exists). Non-prefix phrase mode is unchanged
  (quoted phrases keep `"OR"` literal and valid). Reported via the acerieus stress suite
  (LEARNING-SEARCH-PREFIX-OPERATOR-MUTATIONS-STAY-LITERAL, 2026-06-01). Found + verified
  by bugflow; operator-approved.

## [0.4.7] - 2026-06-02

Bundled bug-fix release from beta/UserSignal reports (sylvain, acerieus, cryptoxdylan), triaged + adversarially verified via bugflow.

### Security

- **Cap-enforcement bypass via a forged tier cache (SEC-13).** A local user could
  write `~/.sibyl-memory/tier_cache.json` with `account_id: null` and
  `cap_bytes: null`. For a pre-activation/free user (whose runtime `account_id`
  is also `None`), this matched the cache fast-path and returned "uncapped",
  letting an oversized write bypass the free-tier cap entirely offline. The
  uncapped fast-path now requires a real `account_id`; a null-account uncapped
  claim is distrusted and falls through to credentials-hint + server
  enforcement. A legitimately uncapped tier always carries an `account_id`.
- **Hardlink / symlink DB-path redirect across profiles (SEC-12).**
  `Storage.__init__` opened the SQLite DB after `Path.resolve()` (which follows
  symlinks) with no link guard, and `is_symlink()` is `False` for hardlinks. A
  symlinked db path or a hardlinked `memory.db` (`st_nlink > 1`) could redirect
  one profile's writes/reads into another profile's database at the SQLite
  layer. `__init__` now refuses a symlinked (final-component) or hardlinked DB
  file, raising `StorageError`. The check is on the db file only, not parent
  dirs, so symlinked / relocated home directories still work.

### Fixed

- **Search quality: journal entries drowned out real results.** On mixed-keyword
  queries, long journal entries (sharing common terms like "project",
  "research", "decision") dominated 50-80% of `search()` hits and buried
  entities / state / reference. The journal tier is now capped at one quarter of
  the global limit; the structured tiers keep the rest. The global rank-sort +
  limit still applies.

## [0.4.6] - 2026-06-01

### Fixed

- **A negative `limit` could broaden search instead of narrowing it.**
  `search()` and `search_entities()` passed `limit` straight into SQLite
  `LIMIT ?`, where `LIMIT -1` means unbounded, so `limit=-1` returned more
  rows rather than fewer. Both methods now clamp `limit` with `max(0, limit)`
  so an invalid negative limit can never broaden results.

## [0.4.5] - 2026-05-30

Adversarial QA remediation (Acer stress-test suite): two findings + a review hardening.

### Fixed

- **FTS5 corruption containment (high).** A poisoned/desynced external-content FTS5 index threw an uncontained `StorageError` out of `search()` / `search_entities()`, crashing the caller. Search now self-heals the index (`'rebuild'` from the intact base table) and retries once; contains to `[]` if unhealable (e.g. contentless journal FTS). A single poisoned row can no longer crash a search. New `_fts_query` helper routes every FTS query site; `_heal_fts` performs the rebuild.
- **Primitive entity/state bodies rejected (contract).** `set_entity` / `set_state` declared `body: dict | list` but silently accepted JSON primitives, so a bare string/number persisted and broke downstream consumers that assume structured bodies. They now raise `ValidationError`. `reference_documents` free-text `str` bodies are unaffected.

### Changed

- Corruption containment keys on the exception *class*, not a message substring (corruption surfaces under varied messages: "vtable constructor failed", "database disk image is malformed", ...). `ProgrammingError` is re-raised so a genuine code/binding bug is never masked as empty results.

Regression coverage: `tests/test_acer_stress_2026_05_30.py` (7 tests). 96/96 suite green.

### Added (Terminal B — multi-record retrieval, tester Run15)

- **`multi_record.py` — `multi_record_search(client, query, ...)`.** Two-stage
  retrieve-then-verify search for workflow / linked-record queries (whose answer
  spans several related records). Per-token recall, then verify gates: abstain on
  zero-support terms, drop purely-preparatory records on terminal-state queries,
  require a rare/selective term match, IDF-coverage rank. Drop-in for a single
  `search()` call (same hit shape); `recall()` unchanged. Fixes the tester Run15
  multi-record-miss class (bench 10/10 vs 4/10 single-pass). Uses only the public
  `MemoryClient` surface. NOTE: gate constants are bench-tuned on a 24-record
  reconstruction, not yet generalized — validate at scale or gate behind a flag
  before publish.

## [0.4.4] - 2026-05-28

Beta-tester bug-report remediation (chainriffs Discord + KAPPA rounds 3/4).

### Fixed

- **FTS5 search: uppercase operator keywords poisoned recall.** A
  natural-language query containing `AND` / `OR` / `NOT` / `NEAR`
  (e.g. `"auth AND db"`, `"cache NEAR eviction"`) had each token
  phrase-quoted into a *required literal* term, so a matched row had to
  literally contain the word "AND"/"NEAR" — recall silently collapsed to
  ~0 hits. These keywords are now dropped during tokenization so the
  remaining terms AND together (the natural intent). A query that is
  *only* operator keywords keeps them as literals so searching for the
  word "and" still resolves. (`_drop_fts5_operator_tokens`.)

### Security

- **Identifier validation: path-traversal + metacharacter defense-in-depth**
  (KAPPA #3 PARTIAL). `validate_identifier` now rejects the `..` traversal
  marker and the shell/redirection/quote metacharacters `< > | ; " \``. SQL
  was already parameterized; this guards downstream non-parameterized
  consumers (filesystem export, CLI display, logs). Apostrophe is
  deliberately allowed (legit in name-shaped keys). Bare `/` and `\` remain
  allowed per the v0.4.0 contract — rejecting raw separators is a contract
  change flagged for team decision.

## [0.4.3] - 2026-05-26

### Fixed

- **Cross-tier timestamp precision mismatch.** `_utc_now_iso()` produced
  6-digit microsecond timestamps (`45.525358Z`) while every SQL DEFAULT
  used SQLite's 3-digit milliseconds (`45.525Z`). The width difference
  broke lexicographic sorting across tiers: `'Z'` (0x5A) > `'3'` (0x33)
  at position 24, so a journal event written 0.358 ms after an entity
  update would sort *before* it in any `ORDER BY ts` merge. Now truncated
  to 3-digit milliseconds to match SQLite output. Affects journal_events,
  revenue_events, error_events, learning_runs.completed_at, and
  skill_proposals.reviewed_at. Existing rows retain their original
  precision (cosmetic, sort-correct within their own tier). Reported by
  external tester smoke test on sibyl-memory-mcp 0.1.2.

## [0.4.2] - 2026-05-22

`_sanitize_fts5_query` default mode flipped from phrase-match to
AND-of-tokens. Pre-0.4.2, multi-word natural-language queries were wrapped
as FTS5 phrases: required exact word sequence: so
`client.search("H&M tops bought")` returned 0 hits even when the haystack
contained all three words. Surfaced by the LongMemEval 50-Q benchmark on
2026-05-22 as the dominant default-UX gap for Hermes-plugin users (every
natural-language query against the plugin's search returned 0 hits).

### Changed

- `_sanitize_fts5_query(raw, *, prefix=False, as_phrase=False)`: new
  default behaviour: tokenize input into alphanumeric + underscore tokens,
  wrap each as a single-term phrase, join with spaces. FTS5 treats
  space-joined terms as implicit AND, so every token must appear in the
  matched row (in any order). Callers that need phrase-match semantics
  must now pass `as_phrase=True` explicitly.
- Empty / all-symbol input still falls back to phrase-wrapping rather than
  returning an empty match string: preserves prior safety posture.

### Added

- `tests/test_search_default_mode.py`: 8 regression tests pinning the
  new default behaviour, including end-to-end multi-word recall against
  live SQLite + FTS5 storage.

### Migration

- Callers who relied on phrase-match (rare: would have needed exact
  word sequences in stored content): pass `as_phrase=True`.
- Most callers see strictly better recall on natural-language queries with
  no code change.

## [0.4.1] - 2026-05-19

Auth-redesign wave 1 step 15: forward-compat with the server's bearer
model. `/api/plugin/check-write` accepts `Authorization: Bearer <token>`
headers in addition to the existing `session_token` body field. This
release sends both: body field for older servers, header for the new
protocol. The server populates device credentials at bind time,
so legacy `session_token`-as-bearer credentials still resolve.

### Changed

- `_capcheck.py:_default_check_write_fn` sends
  `Authorization: Bearer <token>` header on every check-write call.
  Token source priority: `payload["bearer_token"]` (server-issued in
  credentials.json schema_version >= 3) → `payload["session_token"]`
  (v1 backward compat). No behavior change against current production
  server. Companion: api-sibyllabs accepts both paths.

## [0.4.0] - 2026-05-18

KAPPA external-tester remediation release. Independent third-party install
test (KAPPA, peer Tulip-referred) against the v0.3.3 family surfaced one
blocker that broke `sibyl-memory-mcp` on PyPI plus four secondary findings.
This release lands the engine-side fixes. Companion releases:
`sibyl-memory-mcp` v0.1.2, `sibyl-memory-hermes` v0.3.2, `sibyl-memory-cli`
v0.1.3.

### Fixed

- **KAPPA-BLOCKER**. `CapExceededError` and `TierVerificationError`
  relocated from `_capcheck.py` to `exceptions.py` so they are importable
  from the canonical `sibyl_memory_client.exceptions` submodule path. The
  v0.3.3 family had them defined and re-exported only at the top-level
  package; the `.exceptions` submodule path (which `sibyl-memory-mcp`
  imports from) raised `ImportError`. `_capcheck.py` now imports them back
  for full backwards compatibility with anyone reaching into the private
  module.
- **KAPPA-RED**. `~/.sibyl-memory/memory.db` now chmod 0600 after the
  schema apply (was inheriting umask, typically 0644). WAL + SHM sidecar
  files also tightened to 0600 if present. Idempotent + non-fatal on
  chmod failure. Closes the file-perm gap KAPPA observed on a multi-user
  / CI / shared-dev-box install.
- **KAPPA-YELLOW**. `set_entity`, `set_state`, and `set_reference` now
  validate user-supplied identifiers (category, name, key) before write.
  Rejects: non-string, empty, control characters / null bytes, length
  > 1024. Raises `ValidationError` with a recovery hint. Read paths are
  unchanged: already-stored bad identifiers remain accessible so users
  can introspect and migrate. New module-level helper
  `validate_identifier(value, *, field_name)`.
- **KAPPA-YELLOW**. `search()` and `search_entities()` no longer silently
  swallow `sqlite3.OperationalError` into empty results. The error is now
  classified by `_classify_fts5_error()`:
  - schema-missing (`"no such table"`) returns empty (defense against
    partial schema state on very old DBs);
  - FTS5 syntax error (`"fts5"`, `"malformed match"`, `"syntax error near"`,
    `"no such column"`) raises `ValidationError` with the original cause
    chained;
  - anything else raises `StorageError` with the original cause chained.

### Added

- `validate_identifier(value, *, field_name)`: public helper for
  validating user-supplied identifiers consistently across the SDK.
- `_classify_fts5_error(err)`: internal helper for translating FTS5
  `OperationalError` into the appropriate exception type.

### Notes

- The 2 MB free-tier cap (KAPPA's product question) is NOT changed in this
  release. Operator decision to be made separately on whether to raise
  the cap or document the intent more explicitly.
- Existing 53/53 client tests pass unchanged. New tests covering the
  KAPPA-attributed fixes added in `tests/test_smoke.py`.

---

## [0.3.3] - 2026-05-18

Audit-remediation release. v0.3.0 pre-ship audit (2026-05-18T05:05Z) surfaced
10 critical findings across four lanes; this release lands the engine-side
fixes. Companion releases: `sibyl-memory-hermes` v0.3.1, `sibyl-memory-cli`
v0.1.2, `sibyl-memory-mcp` v0.1.1.

### Added

- `MemoryClient.search(query, *, limit=20, prefix=False, tiers=None)` -
  cross-tier FTS5 search over entities + state + reference + journal. Each
  hit is tier-tagged with `{tier, key, category, body, snippet, rank, ts}`.
  Pass `tiers=("entity", "state")` to restrict scope. The marketing claim of
  "FTS5 across all tiers" is now actually true.
- FTS5 query sanitization: every user-supplied query is wrapped as a single
  quoted FTS5 phrase before MATCH. Column-filter syntax (`name:foo`,
  `rowid:*`, etc.) can no longer escape into the FTS5 parser. Empty queries
  short-circuit to empty result (no SQL error leak).
- `_sanitize_fts5_query(raw, *, prefix=False)` helper exposed for callers
  building their own FTS5 queries.

### Changed (schema v3 migration)

- **Schema bumped to v3.** All four searchable tiers (entities, state,
  reference, journal) now have FTS5 indexes:
  - entities_fts → external-content (was standalone with body duplication)
  - state_documents_fts → NEW, external-content
  - reference_documents_fts → external-content (was standalone, never
    exposed in the public SDK)
  - journal_events_fts → NEW, contentless, payload = evaluated || acted ||
    forward || extra concatenated
- v2 → v3 migration runs automatically on first open. Detects v2's
  standalone entities_fts shape, drops it and the old reference_documents_fts,
  recreates in external-content form, and rebuilds the FTS5 indexes from
  the existing base-table data. No application data lost. ~50ms per 10k
  entities on first open after upgrade; idempotent thereafter.
- FTS5 disk footprint reduced ~50% on body-dominated tenants (v2 stored
  the entity body twice; v3 stores it once in the base table).
- FTS5 update trigger pattern fixed: was O(N) DELETE-by-UNINDEXED-column;
  now O(log N) external-content delete-by-rowid.
- `search_entities()` updated to join via rowid (the external-content
  primary key) instead of entity_id.
- `search_entities()` now returns empty list on malformed FTS5 queries
  rather than raising. Previously `client.search_entities('"')` would
  surface a `sqlite3.OperationalError` wrapped as `StorageError` with the
  full db_path interpolated into the message.

### Security

- **SEC-2**. Atomic 0600-at-create for `TierCache.store`. Previously
  used `write_text(...)` then `os.chmod(..., 0o600)`, leaving a
  world-readable window between syscalls every cache write. Now opens with
  `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0o600` set at creation
  time. No race window.
- **SEC-3**. FTS5 query sanitization on every MATCH path. Prevents
  FTS5 injection / DoS via malformed queries.
- **SEC-3**. `StorageError` messages no longer echo the absolute
  `db_path` or full SQLite error text. Original exception is chained via
  `from e` for debugging; user-visible message stays generic.
- **SEC-9**. `TierVerificationError` no longer echoes the server-side
  `error` body string in the user-visible message: strips to a generic
  "Retry shortly" pointer to avoid leaking internal server detail into
  user logs.
- **SEC-11**. `TierCache.load` refuses to follow symlinks. A
  low-privilege attacker who once had write to `~/.sibyl-memory` cannot
  redirect the cache to `/dev/null` or another file via symlink.

### Fixed

- **C2**. `__version__` no longer hardcoded. Now sourced from
  `importlib.metadata.version("sibyl-memory-client")` with the same
  `+source` fallback pattern as sibyl-memory-hermes v0.3.0. The wheel and
  the in-Python `__version__` can no longer drift (v0.3.2 published with
  `__init__.py` saying "0.3.1").
- HTTP User-Agent in `_default_check_write_fn` now built from
  `__version__` instead of hardcoded `"sibyl-memory-client/0.3.0"`. Server
  telemetry will accurately reflect the installed version.
- `from e` chaining added to `_default_check_write_fn`'s `HTTPError` and
  `URLError`/`TimeoutError`/`OSError` handlers so the original cause is
  preserved through `TierVerificationError`.

### Hygiene

- Dropped unused `Iterable` and `ConflictError` imports from `client.py`
  (DC1/DC2). Both remain in `__all__` via re-export.

## [0.3.2] - 2026-05-16

Audit-remediation release. Companion to api-sibyllabs payment-rail fixes
and the post-audit shipping pass. Closes T1-3, T1-4, T2-3 from the
2026-05-16 audit pass (full report: `memory/research/` + email
msg_id 19e33139dfc3e4d4).

### Changed

- **T1-3. `archive_entity` now goes through CapGate**. The audit found
  that `MemoryClient.archive_entity` bypassed the cap check, letting a
  free user at 1.9 MB archive their largest entities (body copied into
  archived_entities, doubling footprint) to keep writing past 2 MB. The
  method now reads the entity body first to size the proposed insert
  (`body + name + category + reason + 200B overhead`), then calls
  `self._cap_gate.check(proposed_delta_bytes=delta)` before the write
  transaction. NotFoundError still raised before any cap-gate side effect.
- **T1-3. `Learner.accept_proposal` now accepts an optional `cap_gate`**.
  `Learner.__init__` gains a `cap_gate: Any = None` parameter. When
  non-None, `accept_proposal` calls `cap_gate.check(proposed_delta_bytes=...)`
  before inserting the `reference_documents` row (skill body can be
  kilobytes). The convenience entry `MemoryClient.learner()` threads
  the client's CapGate through automatically. Direct-import callers can
  override `cap_gate=None` explicitly for tests.
- **T2-3. `_default_check_write_fn` no longer forges fake decisions on
  HTTP error**. Previously a transient 502 response synthesized
  `{ok: False, tier: "free"}` and the caller cached it as authoritative,
  locking a paid user out for up to 7 days. Now raises
  `TierVerificationError` on any HTTP error: the offline-grace path in
  `_refresh_and_check` decides whether to honor a recent cache or hard-cap.
- **T1-4. TierCacheEntry gains `server_expires_at` + `cache_token` fields**.
  `server_expires_at` is the server-supplied subscription expiry parsed
  from the `expires_at` field on the `/check-write` response. The cache
  is now honored only while `now < min(checked_at + grace_seconds,
  server_expires_at)`, which prevents the multi-grace-period attack
  where a user blackholes the network to keep using their cached paid
  tier past actual subscription expiry. Authoritative end-of-validity
  comes from the server's record, not from a refresh-able local timer.
  `cache_token` stores the credentials.signature as a defense-in-depth
  link between cache and credentials identity (sent on subsequent
  cap-checks for tamper telemetry).
- **TierCache.load/store round-trip the new fields**. Backwards
  compatible with v0.3.1 cache files (missing fields default to None).

### Schema

- TierCache file schema bumped (implicitly v2). v1 caches load fine
  with `server_expires_at=None` and `cache_token=None`; next successful
  `/check-write` upgrades them.

### Tests

- 53/53 unchanged, all green. The cap-gate addition in `archive_entity`
  fires under the default 2 MB cap on test data well below that
  threshold: no test changes needed.

### Notes for downstream

- `sibyl-memory-hermes` v0.2.2 ships in lockstep (narrows `recall()`
  exception handling to `NotFoundError` only, T2-2 fix). Earlier
  hermes versions still work; the bug they had was over-aggressive
  exception swallowing, harmless to the cap-gate plumbing.

## [0.3.1] - 2026-05-16

Tamper-evidence release. Companion to api-sibyllabs HMAC signing.

### Added

- `MemoryClient.__init__` and `MemoryClient.local()` accept two new
  optional kwargs: `credentials_claim` (dict of the canonical signed
  fields) and `credentials_signature` (hex HMAC). Both default to None
  for backwards compatibility with unsigned v0.3.0 credentials.
- `CapGate` accepts the same two kwargs and, when both are present,
  attaches them to every `/check-write` POST body. The server uses
  them to verify the signature and log `credentials_tamper_suspected`
  telemetry on mismatch. The cap-gate decision itself is unaffected -
  authoritative tier always comes from the database via
  `effectiveAccess`.

### Schema

- Credentials JSON schema v2 (server-issued 2026-05-16+): adds
  `signature` (HMAC-SHA256 hex, 64 chars) and `signed_at` (ISO ts).
  Old schema v1 credentials still load and work; the client just
  sends an unsigned request and the server skips the tamper check.

### Tests

- 53/53 unchanged, all green. The signing path is purely additive.

## [0.3.0] - 2026-05-15

Hard-cap enforcement release. Operator directive 2026-05-15: "how do
we hard-limit free users to the 2Mb size? and ensure they can't
circumvent this" → Level 1 (hard write cap) + Level 2 (signed
credentials.json, deferred) + server-authoritative tier check at the
boundary. Locked in: 7-day grace cache, hard cap on by default.

### Added

- **`_capcheck.py` module** with the cap-enforcement primitives:
  - `CapGate.check(proposed_delta_bytes)`: three fast paths plus one
    slow server-refresh path. Most writes never phone home. The slow
    path only fires when (a) a free-tier user is about to push past
    2 MB or (b) the local tier cache has expired.
  - `TierCache`: file-backed at `~/.sibyl-memory/tier_cache.json`,
    mode 0600, atomic write, JSON shape `{ account_id, tier,
    checked_at, cap_bytes }`. Honored as fresh for 7 days; honored
    for an extended 14-day grace if the user is offline.
  - `CapExceededError` (code `CAP_EXCEEDED`): carries `upgrade_url`.
  - `TierVerificationError`: raised only when the user is at the cap,
    offline, AND has no valid grace cache. Distinct from CAP_EXCEEDED
    so callers can route the two error states differently.
  - `_default_check_write_fn`: pure stdlib urllib transport. The
    default endpoint is `https://api.sibyllabs.org/api/plugin/check-write`.
    Replaceable for tests via the `check_fn` constructor kwarg.
  - Constants `FREE_TIER_CAP_BYTES = 2 * 1024 * 1024` and
    `GRACE_PERIOD_SECONDS = 7 * 24 * 60 * 60`.

- **`MemoryClient` cap wiring** (additive, non-breaking):
  - `__init__` and `local()` accept `account_id`, `session_token`,
    `tier`, and an optional `cap_gate` override.
  - Every write path (`set_entity`, `write_event`, `set_state`,
    `set_reference`) calls `self._cap_gate.check(proposed_delta_bytes=...)`
    with a JSON-byte-length estimate. Reads are never gated.
  - Pre-activation users (no `account_id`) get a strict local 2 MB cap
    with no server check possible: by design.

### Tests

- 13 new tests in `tests/test_capcheck.py` covering: under-cap (no
  server call), at-cap server says no, server upgrades a stale-cached
  user, paid-cache short-circuits server, stale paid cache triggers
  refresh, offline-at-cap with grace cache passes, offline-at-cap
  with no cache raises, pre-activation under/at cap, e2e MemoryClient
  free/paid, cache file mode is 0600, `invalidate_cache()` works.
  Full suite 53/53 green.

### Notes for downstream

- `sibyl-memory-hermes` v0.2.0 plumbs `account_id` and `session_token`
  through to the client. Earlier hermes versions still work but
  pre-activation users hit the strict local 2 MB cap.
- The Level 2 HMAC-signed `credentials.json` design is in
  `memory/research/2026-05-15-hard-cap-enforcement.md` (deferred until
  `PLUGIN_CREDENTIAL_SIGNING_KEY` is provisioned in Doppler/Vercel).

## [0.2.0] - 2026-05-15

Self-learning + memory-linting release. Operator directive 2026-05-15:
"add a self-learning cron + function to the memory deployment so the
memory learns and creates skills from things in the session just as you
do. could we also do memory linter?"

### Schema

- **v2 migration**: adds two tables. Idempotent. v1 databases auto-upgrade on next open.
  - `skill_proposals`: review queue for detected skills. Columns: id, tenant_id, created_at, pattern_kind, proposed_slug, proposed_title, proposed_body, evidence (JSON), confidence (REAL 0..1), summarizer, status (pending/accepted/rejected/superseded), reviewed_at, review_note, accepted_doc_key. UNIQUE indexes on (tenant_id, status, created_at) and (tenant_id, proposed_slug).
  - `learning_runs`: watermark log so detectors don't rescan ground they covered. Columns: id, tenant_id, started_at, completed_at, summarizer, events_scanned, proposals_made, cursor_after_ts, notes.

### Added

- **`learning.py` module** with the full self-learning loop:
  - `Learner` class: scans journal_events since last watermark, runs four pattern detectors, dedupes by slug, persists top-N proposals.
  - Four deterministic detectors: `repeated_action`, `structural_similarity`, `co_occurrence`, `temporal_routine`.
  - Three pluggable summarizer backends (per operator design directive 2026-05-15):
    - `LocalDeterministicSummarizer` (free tier default): pure SQL + Python templates, zero network.
    - `BYOKSummarizer` (paid tier opt-in): user supplies their own inference callable, SDK never holds the key.
    - `VeniceX402Summarizer` (paid tier hosted). Venice-routed via x402 against the user's pre-funded plugin balance. Endpoint design at `memory/research/2026-05-15-self-learning-design.md`.
  - Review queue API: `list_proposals`, `get_proposal`, `accept_proposal` (writes `reference_documents` row under `skill/<slug>` key with provenance metadata), `reject_proposal`.
  - Both LLM-backed summarizers gracefully fall back to local-deterministic output when the inference callable raises.

- **`lint.py` module**: local memory linter mirroring `scripts/memory-lint.mjs`:
  - `Linter` class with 9 checks across three severity tiers (critical / warning / info): schema-version, invalid-json-entity, invalid-json-state, invalid-json-journal, duplicate-entity, empty-reference, stale-entity, journal-without-acts, db-soft-cap, fts-rowcount-mismatch, flagged-actors-fresh.
  - `LintReport` dataclass with `to_dict()` (JSON-serializable) + `to_ascii()` (single-block boxed report for CLI).
  - Tunable thresholds: `soft_cap_bytes` (default 10 MB per operator decision), `stale_days` (default 90), `flag_recency_days` (default 30).

- **`MemoryClient` API surface (additive)**:
  - `client.learner(**kwargs)`: construct a tenant-bound Learner.
  - `client.learn()`: convenience: one-shot Learner.run() returning a LearningRunReport.
  - `client.list_skill_proposals(status='pending', limit=50)`.
  - `client.accept_skill_proposal(id, note=None)`.
  - `client.reject_skill_proposal(id, note=None)`.
  - `client.lint(**kwargs)`: returns a LintReport.

- **Public exports** (`__init__.py`): added `Learner`, `SkillProposal`, `LearningRunReport`, `Summarizer`, `LocalDeterministicSummarizer`, `BYOKSummarizer`, `VeniceX402Summarizer`, `Linter`, `LintReport`, `Finding`.

### Tests

- 22 new tests across two files:
  - `tests/test_learning.py`: 12 tests: schema migration v2, no-event runs, repeated-action detection, watermark dedup, structural-similarity detection, accept/reject lifecycle, BYOK invocation, Venice/x402 fallback on failure, multi-tenant isolation.
  - `tests/test_lint.py`: 10 tests: clean-DB baseline, duplicate-entity, empty-reference, stale-entity, journal-without-acts, soft-cap, ASCII report rendering, dict serialization, severity buckets, multi-tenant isolation.
- Total package coverage: 10 (existing smoke) + 12 (learning) + 10 (lint) = **32 tests, all green**.

### Compatibility

- v0.1.0 databases auto-upgrade to v2 on first open via existing idempotent `_ensure_schema()` path: no manual migration needed.
- `sibyl-memory-hermes` v0.1.0 is binary-compatible with v0.2.0 of this SDK (provider surface unchanged). Hermes-provider tests updated to expect schema_version=2.
- Local-first promise unchanged: free tier remains zero-network. BYOK / Venice routes are paid-tier opt-in only and the CLI gate enforces tier checks upstream.

### Notes for CLI integration (sibyl-labs-cli, next)

The CLI package will expose:
- `sibyl learn` → runs `client.learn()`.
- `sibyl learn review` → interactive walk of `client.list_skill_proposals()` with y/n/edit prompts.
- `sibyl lint` → runs `client.lint()`, prints `to_ascii()`, exits non-zero if `critical_count > 0`.
- Optional cron install during `sibyl init` (Linux/macOS cron, Windows Task Scheduler) for daily learn + lint.

## [0.1.0] - 2026-05-15

Initial release.

- SQLite + FTS5 port of the canonical `sibyl_memory.*` Postgres schema (10 base tables + 2 FTS5 virtuals + version table).
- `MemoryClient` public API with polymorphic constructor: `MemoryClient.local(path)`.
- Five-tier model: entities (WARM) / state_documents (HOT) / journal_events (COLD) / reference_documents (REFERENCE) / archived_entities (ARCHIVE) / flagged_actors (FLAGGED).
- Multi-tenant isolation via `tenant_id` column.
- `Storage` low-level wrapper with per-instance thread-local connection cache, WAL mode, foreign_keys=ON, busy_timeout=5000ms.
- Typed exception hierarchy (`SibylMemoryError` + subclasses).
- 10 smoke tests, all green.
- Zero runtime dependencies, MIT, Python 3.10+.
