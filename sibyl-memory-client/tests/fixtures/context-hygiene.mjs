#!/usr/bin/env node
// context-hygiene.mjs - deterministic measurement of an AI agent's always-load context surface.
//
// "debloat" measures what your agent loads on EVERY session start (its instruction files, rules,
// and any always-read state files) and reports the weight plus the heaviest files, so you can keep
// the always-load surface light. Pure byte/token arithmetic (chars/4, labelled MEASURED~), no LLM
// judgment, so the number cannot be rationalized away.
//
// Environment hygiene, not a gate: it never blocks a session and never forces a trim. You decide.
//
// Modes:
//   --fyi     one quiet line (weight + heavy spots). Good for a session-start hook.
//   --json    machine-readable record (drives an interactive trim pass).
//   (default) full human report: file weights + suggested trim targets.
//
// Config (optional): a .debloat.json in the root, or --config <path>:
//   { "surface": ["CLAUDE.md", "AGENTS.md"],
//     "thresholds": { "watch": 45000, "heavy": 55000 },
//     "perFileSoft": { "CLAUDE.md": 6000 },
//     "journal": ".debloat/history.jsonl" }
// You can also list files on the CLI with --include <path> (repeatable).
//
// Always exits 0. Node 18+ (no dependencies).
// Usage: node context-hygiene.mjs [--fyi|--json] [--root <dir>] [--config <path>] [--include <path>]...

import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const flag = (n) => args.includes(n);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const opts = (n) => { const out = []; for (let i = 0; i < args.length; i++) if (args[i] === n && args[i + 1]) out.push(args[i + 1]); return out; };

const ROOT = path.resolve(opt('--root', process.cwd()));
const AS_JSON = flag('--json');
const AS_FYI = flag('--fyi');

// ---- config resolution -------------------------------------------------------------------
const DEFAULTS = {
  surface: ['CLAUDE.md', 'AGENTS.md'],
  thresholds: { watch: 45000, heavy: 55000 },
  perFileSoft: {},
  journal: null, // set to a path (relative to root or absolute) to enable append-only telemetry
};
function loadConfig() {
  const cfgPath = opt('--config', path.join(ROOT, '.debloat.json'));
  if (fs.existsSync(cfgPath)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
      return {
        ...DEFAULTS, ...parsed,
        thresholds: { ...DEFAULTS.thresholds, ...(parsed.thresholds || {}) },
        perFileSoft: { ...DEFAULTS.perFileSoft, ...(parsed.perFileSoft || {}) },
      };
    } catch (e) { console.error(`debloat: could not parse ${cfgPath}: ${e.message}`); }
  }
  return { ...DEFAULTS };
}
const CFG = loadConfig();

// surface = config surface + any --include, de-duplicated
const surfaceList = [...new Set([...(CFG.surface || []), ...opts('--include')])];

const tok = (bytes) => Math.round(bytes / 4); // deterministic approx (chars/4), labelled MEASURED~

// ---- MEASURE (deterministic) -------------------------------------------------------------
const files = [];
let totalBytes = 0;
for (const rel of surfaceList) {
  const abs = path.isAbsolute(rel) ? rel : path.join(ROOT, rel);
  let bytes = 0;
  try { const st = fs.statSync(abs); if (!st.isFile()) continue; bytes = st.size; } catch { continue; }
  totalBytes += bytes;
  const t = tok(bytes);
  const soft = CFG.perFileSoft[rel];
  files.push({ file: rel, bytes, tok: t, soft: soft ?? null, heavy: soft != null && t > soft });
}
const totalTok = tok(totalBytes);
const WATCH = CFG.thresholds.watch, HEAVY = CFG.thresholds.heavy;

// suggested trim targets (informational)
const targets = [];
for (const f of files) if (f.heavy) {
  targets.push({ kind: 'file_heavy', file: f.file, tok: f.tok, soft: f.soft,
    suggestion: `${f.file} is ${f.tok} tok (soft ${f.soft}). Move accreted history to a *-history file; keep current state + a pointer.` });
}
if (!targets.length && files.length && totalTok >= WATCH) {
  const biggest = [...files].sort((a, b) => b.tok - a.tok)[0];
  targets.push({ kind: 'file_largest', file: biggest.file, tok: biggest.tok, soft: biggest.soft,
    suggestion: `${biggest.file} is the heaviest at ${biggest.tok} tok. If it carries accreted history, extract it to a *-history file.` });
}

// weight label (informational, never blocks)
let weight = 'light';
if (totalTok >= HEAVY) weight = 'heavy';
else if (totalTok >= WATCH || targets.length) weight = 'watch';

const record = {
  ts: new Date().toISOString(), root: ROOT, total_tok: totalTok, total_bytes: totalBytes,
  ref: { watch: WATCH, heavy: HEAVY }, weight,
  files: files.map((f) => ({ file: f.file, tok: f.tok, soft: f.soft, heavy: f.heavy })),
  targets: targets.map((t) => ({ file: t.file, kind: t.kind, tok: t.tok, soft: t.soft, suggestion: t.suggestion })),
};

// ---- OBSERVE: optional append-only telemetry (best-effort, never throws) ------------------
if (CFG.journal) {
  try {
    const jp = path.isAbsolute(CFG.journal) ? CFG.journal : path.join(ROOT, CFG.journal);
    fs.mkdirSync(path.dirname(jp), { recursive: true });
    fs.appendFileSync(jp, JSON.stringify({ ts: record.ts, total_tok: totalTok, weight, targets: targets.length }) + '\n');
  } catch { /* telemetry is best-effort */ }
}

// ---- OUTPUT ------------------------------------------------------------------------------
if (AS_JSON) { console.log(JSON.stringify(record, null, 2)); process.exit(0); }

if (AS_FYI) {
  const heavyList = files.filter((f) => f.heavy).map((f) => `${f.file.split('/').pop()} ${f.tok}t`);
  const spots = heavyList.length ? ` | heavy: ${heavyList.join(', ')}` : '';
  const hint = weight === 'light' ? '' : ' - /debloat to trim';
  console.log(`context: ~${(totalTok / 1000).toFixed(1)}k tok always-load (${weight})${spots}${hint}`);
  process.exit(0);
}

const bar = '='.repeat(60);
console.log(bar);
console.log(`CONTEXT WEIGHT  ${record.ts}`);
console.log(bar);
if (!files.length) {
  console.log('  no always-load files found.');
  console.log(`  looked for: ${surfaceList.join(', ') || '(none)'}`);
  console.log('  set "surface" in .debloat.json or pass --include <file>.');
  console.log(bar);
  process.exit(0);
}
console.log(`  always-load surface: ${totalTok.toLocaleString()} tok  (~${(totalBytes / 1024).toFixed(0)} KB)   [MEASURED~ chars/4]`);
console.log(`  reference: light <${WATCH / 1000}k / watch <${HEAVY / 1000}k / heavy >=${HEAVY / 1000}k`);
console.log(`  weight: ${weight}`);
console.log('');
console.log('  file weights (heaviest first):');
for (const f of [...files].sort((a, b) => b.tok - a.tok)) {
  const mark = f.heavy ? '  <-- heavy (soft ' + f.soft + ')' : (f.soft ? '  (soft ' + f.soft + ')' : '');
  console.log(`    ${String(f.tok).padStart(6)} tok  ${f.file}${mark}`);
}
if (targets.length) {
  console.log('');
  console.log('  suggested trim targets (you choose what moves):');
  for (const t of targets) console.log(`    * ${t.suggestion}`);
}
console.log('');
console.log(weight === 'light'
  ? '  Light. Nothing to do.'
  : '  Trim: move accreted history out of the heavy files into *-history files, keep current state + a pointer.');
console.log(bar);
process.exit(0);
