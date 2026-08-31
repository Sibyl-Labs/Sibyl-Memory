# Sibyl Memory 0.8.0 family: release notes

`sibyl-memory-client` 0.8.0 · `sibyl-memory-mcp` 0.2.0 · `sibyl-memory-cli` 0.4.0 ·
`sibyl-memory-hermes` 0.4.0 · `sibyl-memory-langgraph` 0.2.0

Branch `lang-core-verdict`. Every number below comes from a run recorded in this
repository. Nothing here is projected, rounded up, or inferred.

---

## What this release is

The query-time multilingual rescue layers that four evaluation cycles accreted in
`MemoryClient.search()` were removed and replaced by one versioned write-time
normalizer, and every search that returns zero rows now returns a machine-readable
cause alongside it.

**It improves the INDEX layer.** Stored text is rendered word-boundary clean at
write time, so a term can match at a word START rather than only as a free
substring, which is what inflected and non-spaced languages need.

**It does not solve default-path natural-language recall.** The first call on the
default path answers 32 of 155 natural paraphrases, the same as before. The other
123 now say why, and 99 of them are recoverable by a caller that reads the verdict
and runs the taught two-retry loop. The 29 negation-phrased answerable questions
still return nothing on the default path; they now report `negation_abstain` or
`abstained_on`. `NEGATION_POLICY` is unchanged and answering a negated question
correctly still needs negation handling this engine does not have.

Search behaviour is byte-identical to the previous build (`ef98f5b`) on every
quality field of the full battery across three configurations. This release adds
explanation, not retrieval.

---

## Read these before you upgrade

### 1. The two-retry bound is agent-side and unenforced

The verdict tells an agent that one word in its query blocked everything, and the
tool docstring, the Hermes tool schema and the Hermes system-prompt block all
teach: drop that word, call again, at most twice, then stop.

Nothing in the server enforces it. The server has no memory of a caller's previous
query and cannot count retries without becoming stateful.

An agent that ignores the bound and keeps stripping words reaches the
corpus-supported residue of its own query. Measured: 6 rows across the two
supported-class injection queries, worst case 7 rounds.

Two things make that acceptable, and both are measured:

- A query built from vocabulary the store does not contain returns zero rows at
  any number of retries. Verified unbounded on all eight zero-support injection
  queries.
- The residue an unbounded loop reaches is **byte-identical to what one ordinary
  call returns with no loop at all**. `memory_search('contract')` returns the same
  3 rows the unbounded loop grinds down to. The loop grants no access the caller
  did not already have, and the advice this replaces (`retry with tiers="entity"`)
  returned those rows for the FULL unmodified injection query in a single step.

So the bound is a quality control, not an access boundary: it stops the loop from
answering a shorter question than the user asked.

**The appearance hazard.** An operator reading a non-compliant agent's transcript
will see rows returned after an injection-shaped prompt. Those rows are the answer
to `contract`. They are not the answer to the injection, and no injected word
appears in any returned body.

### 2. First open after upgrade re-backfills the shadow, O(rows), under one lock

The stored rendering changed, so `_SHADOW_MARKER` was bumped. Every existing store
drops, recreates and re-backfills its search shadow on the next open. The marker
is in the code, so **the bump triggers this fleet-wide on upgrade**, not per-store
on demand.

Measured: **1.65 s at 10k rows, 27 s at 100k rows.**

It runs under one `BEGIN IMMEDIATE` write lock. A second process opening the same
store during the rebuild blocks. That lock-out is fixed (`busy_timeout` is widened
to 180 s for the whole span of schema-apply plus migration, which is exactly the
window a concurrent opener can be blocked for, then restored so the ordinary read
path keeps failing fast). The O(rows) shape itself is not removed.

Plan for it on large stores: the first open is the slow one, every open after it
is O(1).

### 3. `empty_store` probe cost

`empty_store` is probed, never inferred, and only on the zero path.

- **Zero extra `COUNT(*)` on any store that has entities.** The engine reuses the
  entities count it already takes for IDF weighting; a non-zero one proves the
  store is non-empty and the probe is skipped.
- **Four extra `COUNT(*)` per zero on a store holding only journal / state /
  reference rows.** That is the case where the probe is load-bearing: such a store
  answers queries and must be reported `abstained_on`, never `empty_store`.
- Measured at 1 `COUNT(*)` per zero-result MCP call, the same as the previous
  build. Not measured above 1400 entities.

### 4. OPEN: the corroborated-class append is unbounded

On `client.search()` and on the MCP `tiers=` route, rows covering two or more of
the query's terms are appended without a budget. Rows covering exactly one term
are budgeted to the query's content-term count; rows covering two or more are not.

Consequence: boilerplate carrying two of the query's terms can append up to the
caller's limit. Measured worst case on the battery: 19 appended rows where 0.7.0
produces none.

This is reported, not decided. A per-coverage-level budget was built and measured
and is **not shippable**: it costs 35 answer-bearing rows across 7 LongMemEval
questions, because the distributions overlap (a legitimate level needs 12 rows at
1 term, 9 at 2 and 7 at 4, while the sweep has 30 at 3), so no monotone
query-shape budget separates them and a tie-group bound fails identically.

The **default path is unaffected**. Behaviour is pinned by
`test_corroborated_class_is_NOT_bounded_open_finding`, so a future change to it is
a deliberate change, not a drift.

### 5. SQLite floor: 3.34, verified and now enforced

The package carried two different floors, neither of them run: `shadow.py`
documented 3.34, `storage.py`'s recovery message told users to check for "3.38+
for json_valid", and the stage-2 trigger shape (the rendering is staged through
nested subqueries that read `new.` INSIDE a subquery) had only ever executed on
3.45.1. `CREATE TRIGGER` failing outright was the failure mode nobody had ruled
out.

**Verified.** SQLite was built from the amalgamation at **3.34.1** and **3.44.2**
(FTS5 + JSON1) and statically linked into a scratch interpreter:

| version | trigger shape | shadow rows vs 3.45.1 | client suite |
|---|---|---|---|
| 3.34.1 | creates and fires on all four tiers | byte-identical | 476 passed, 12 skipped |
| 3.44.2 | creates and fires on all four tiers | byte-identical | 476 passed, 12 skipped |
| 3.45.1 | creates and fires on all four tiers | reference | 477 passed, 11 skipped |

`_STAGE_OPS`, the nesting-depth constant tuned against the trigger parser's
ceiling on 3.45.1, is inside the ceiling on 3.34.1 and 3.44.2 as well.

**Enforced.** `shadow.SQLITE_MIN_VERSION = (3, 34, 0)` is checked by
`assert_sqlite_supported()` at the top of `Storage._ensure_schema`, before the
schema apply and therefore before any migration write. Below it, a `SchemaError`
names the minimum, the found version and the reason. Previously the failure
surfaced from inside the v4 migration as "error in tokenizer constructor", which
named neither. The contradictory "3.38+" claim is deleted and a test asserts no
string in `storage.py` states a SQLite version.

**3.45 is a feature boundary, not a floor.** `remove_diacritics` reached the
`trigram` tokenizer in 3.45. Below it the shadow folds the non-decomposables in
`FOLD_MAP` (l-stroke, sharp s, o-slash, ae, d-stroke, dotless i, oe, thorn, eth)
but not decomposable diacritics, because that fold is the tokenizer's job. So
`Belzyce` does not find `Bełżyce` on 3.34 through 3.44. The store opens, writes,
migrates and searches, and the primary porter-unicode61 index is unaffected. That
one test is gated on the boundary with the version in its skip reason; a companion
test covering the non-decomposable half runs on every supported version.

Check what you are on:

```
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

### 6. Two LongMemEval residuals at limit 10

`e982271f` and `gpt4_f420262c` are real losses at limit 10 and clean at limit 25.

- `e982271f` drops the session recording which venue was recommended LAST and
  keeps an interchangeable venue card containing the gold string. Raising the
  limit to 11, 12, 13 or 15 does not recover its row, so this needs a RANKING
  change, not a holdback.
- `gpt4_f420262c` drops the EARLIEST session on an earliest-to-latest ordering
  question. It closes at limit 11 via a holdback, which is a head override and is
  excluded by the constraint this work ran under.

Both are recorded as known, deferred, with reasons.

### 7. `tiers=` is a documented bypass, not a recovery

The previous MCP docstring taught `tiers="entity"` as the escape hatch from an
abstention. That advice is gone from the source.

`tiers=` has no abstention gate, and therefore no injection precision gate, which
is what the abstention gate IS. It remains supported and documented as the linker
bypass with its tradeoff stated. It is not the recovery for an `abstained_on`.
The recovery for an `abstained_on` is: drop the one token named in
`verdict.tokens[0]`, call again with every gate still armed, at most twice.

### 8. Docker-image users must rebuild the image

A self-built `sibyl-memory-mcp` Docker image pins its dependencies at image build
time. **A `pip install -U` on the host does not reach inside the image.** Rebuild
the image to pick up 0.2.0 and the client 0.8.0 underneath it.

This is the same class of hazard the 0.1.14 dependency-floor raise addressed: an
MCP-only install path that is not going through the CLI's tighter floor can sit on
older code while reporting a clean install.

### 9. The deprecated `diagnostics=` dict

`multi_record_search(..., diagnostics=<dict>)` still populates, with identical
keys and values on **every exit that previously wrote anything**. There is exactly
one observable difference: the `no significant tokens` exit now writes the five
standard keys where the previous build left the dict empty.

Prefer `result.verdict`. An optional channel is one a caller can forget, and
forgetting it is the whole defect this release closes: the MCP server never passed
`diagnostics=` at all, so the channel was unreachable from the surface that
matters most.

---

## Upgrade order

Client first, then the packages that depend on it. The dependency floors make a
partial upgrade fail loudly rather than silently mix an old client with new
adapters:

| package | version | floors |
|---|---|---|
| `sibyl-memory-client` | 0.8.0 | (none) |
| `sibyl-memory-hermes` | 0.4.0 | client >= 0.8.0 |
| `sibyl-memory-mcp` | 0.2.0 | client >= 0.8.0, hermes >= 0.4.0 |
| `sibyl-memory-cli` | 0.4.0 | client >= 0.8.0, hermes >= 0.4.0, mcp >= 0.2.0 (extra) |
| `sibyl-memory-langgraph` | 0.2.0 | client >= 0.8.0 |

Note that `pip install -U <one package>` uses pip's only-if-needed upgrade
strategy, which leaves an already-satisfying older sibling in place. The floors
above are what turn that into a resolvable upgrade rather than a silent no-op.

---

## Response-shape changes

- **MCP** `memory_search`: the response gains an additive `verdict` object on
  every call, including every zero. Nothing was removed and no field changed
  meaning. `count` and `verdict.returned` are reconciled after bounding, so they
  cannot disagree.
- **Hermes** `sibyl_search`: returns `{"results": [...], "verdict": {...}}`.
  `search_multi_record` and `search` return a `SearchResults` list subclass;
  `len`, iteration, indexing, `== []`, `json.dumps`, `isinstance(x, list)`,
  copy/deepcopy/pickle and slicing all behave as before (a slice correctly drops
  the verdict).
- **CLI** `sibyl memory search`: the zero path prints a plain-language cause plus
  a machine-readable `cause: <code>` line. The success path prints no extra
  output.
- **langgraph** `SibylStore`: `BaseStore.search` still returns
  `list[SearchItem]`; the canonical verdict of the most recent query-backed search
  is exposed as `SibylStore.last_search_verdict`, and an empty FTS result is
  logged at DEBUG with its cause.

## What a verdict may carry

Numbers, enum values, and tokens from the caller's own query. Never stored-record
content: no body text, no snippet, no entity name, no category, no key. Token
strings are capped at 64 characters and 8 per list field, and the envelope key
order is fixed and JSON-serialisable, so the verdict composes with the MCP output
fence rather than becoming a second unfenced channel out of the store. A canary
sweep planting verdict-shaped content in bodies, entity keys, categories, nested
fields and three non-entity tiers, across four SDK entry points, the MCP tool over
real spawned stdio on both routes, and the Hermes provider, found **0 leaks**.
