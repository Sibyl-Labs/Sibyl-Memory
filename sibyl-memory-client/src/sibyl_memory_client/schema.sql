-- sibyl-memory-client SQLite schema v1
--
-- Port of the canonical sibyl_memory.* Postgres schema (scripts/sibyl-memory-schema.sql,
-- applied to Neon 2026-05-01) to SQLite for the local-first plugin v1.
--
-- Dialect translations:
--   UUID                → TEXT (Python uuid.uuid4() at write-time)
--   JSONB               → TEXT with CHECK(json_valid(col)) using SQLite json1
--   TIMESTAMPTZ + now() → TEXT ISO 8601 UTC via strftime('%Y-%m-%dT%H:%M:%fZ','now')
--   gin jsonb_path_ops  → SQLite json_extract expression indexes where useful
--   NUMERIC             → REAL (sufficient precision for plugin v1 use)
--   tsvector            → FTS5 virtual tables for text search
--
-- Multi-tenant: every table carries tenant_id. Local-first means typically
-- one tenant per machine, but the schema accepts N tenants (paid Team-tier
-- federation forward-compatible).
--
-- Idempotent. Apply via CREATE TABLE IF NOT EXISTS. Schema version recorded
-- in sibyl_memory_schema_version for future migrations.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================================
-- WARM tier: entities (single source of truth per rule 43)
-- ============================================================================
CREATE TABLE IF NOT EXISTS entities (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  category    TEXT NOT NULL,
  name        TEXT NOT NULL,
  status      TEXT,
  body        TEXT NOT NULL CHECK (json_valid(body)),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (tenant_id, category, name)
);

CREATE INDEX IF NOT EXISTS entities_tenant_cat_status
  ON entities (tenant_id, category, status);
CREATE INDEX IF NOT EXISTS entities_updated_at
  ON entities (tenant_id, updated_at DESC);

-- ============================================================================
-- Cross-references: typed relations between entities
-- ============================================================================
CREATE TABLE IF NOT EXISTS entity_relations (
  id            TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  from_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  to_id         TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  metadata      TEXT CHECK (metadata IS NULL OR json_valid(metadata)),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS entity_relations_from
  ON entity_relations (tenant_id, from_id, relation_type);
CREATE INDEX IF NOT EXISTS entity_relations_to
  ON entity_relations (tenant_id, to_id, relation_type);

-- ============================================================================
-- HOT tier: state documents (treasury, priorities, session, index analogs)
-- ============================================================================
CREATE TABLE IF NOT EXISTS state_documents (
  tenant_id    TEXT NOT NULL,
  document_key TEXT NOT NULL,
  body         TEXT NOT NULL CHECK (json_valid(body)),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (tenant_id, document_key)
);

-- ============================================================================
-- COLD tier: append-only journal of events
-- ============================================================================
CREATE TABLE IF NOT EXISTS journal_events (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  ts         TEXT NOT NULL,
  evaluated  TEXT CHECK (evaluated IS NULL OR json_valid(evaluated)),
  acted      TEXT CHECK (acted IS NULL OR json_valid(acted)),
  forward    TEXT CHECK (forward IS NULL OR json_valid(forward)),
  extra      TEXT CHECK (extra IS NULL OR json_valid(extra))
);

CREATE INDEX IF NOT EXISTS journal_events_tenant_ts
  ON journal_events (tenant_id, ts DESC);

-- ============================================================================
-- COLD tier: revenue events with optional entity ref
-- ============================================================================
CREATE TABLE IF NOT EXISTS revenue_events (
  id                  TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  ts                  TEXT NOT NULL,
  event_type          TEXT,
  gross_usd           REAL,
  operator_share_usd  REAL,
  source              TEXT,
  tx                  TEXT,
  entity_id           TEXT REFERENCES entities(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS revenue_events_tenant_ts
  ON revenue_events (tenant_id, ts DESC);

-- ============================================================================
-- COLD tier: error events
-- ============================================================================
CREATE TABLE IF NOT EXISTS error_events (
  id         TEXT PRIMARY KEY,
  tenant_id  TEXT NOT NULL,
  ts         TEXT NOT NULL,
  code       TEXT,
  message    TEXT,
  context    TEXT CHECK (context IS NULL OR json_valid(context))
);

CREATE INDEX IF NOT EXISTS error_events_tenant_ts
  ON error_events (tenant_id, ts DESC);

-- ============================================================================
-- REFERENCE tier: static documents (markdown bodies, lookup-only)
-- ============================================================================
CREATE TABLE IF NOT EXISTS reference_documents (
  tenant_id   TEXT NOT NULL,
  doc_key     TEXT NOT NULL,
  body        TEXT,
  metadata    TEXT CHECK (metadata IS NULL OR json_valid(metadata)),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (tenant_id, doc_key)
);

-- ============================================================================
-- ARCHIVE tier: frozen entities (out of working set, retrievable)
-- ============================================================================
CREATE TABLE IF NOT EXISTS archived_entities (
  id                  TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  original_entity_id  TEXT,
  category            TEXT,
  name                TEXT,
  body                TEXT CHECK (body IS NULL OR json_valid(body)),
  archived_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  archive_reason      TEXT
);

CREATE INDEX IF NOT EXISTS archived_entities_tenant_cat
  ON archived_entities (tenant_id, category, name);

-- ============================================================================
-- FLAGGED tier: actors flagged for social-engineering / fraud (rule 13/14/15)
-- ============================================================================
CREATE TABLE IF NOT EXISTS flagged_actors (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  actor_handle    TEXT,
  actor_address   TEXT,
  flagged_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  reason          TEXT,
  evidence        TEXT CHECK (evidence IS NULL OR json_valid(evidence))
);

CREATE INDEX IF NOT EXISTS flagged_actors_tenant
  ON flagged_actors (tenant_id);

-- ============================================================================
-- FTS5 virtual tables for full-text retrieval (the tsvector port)
-- ============================================================================
-- v3 (2026-05-18): all FTS5 tables now use external-content (or contentless
-- for journal). Body lives in the base table, FTS5 stores only the index.
-- Triggers fire transparently. Disk footprint stays flat (vs v2's 2x dup).
-- Cross-tier search lands here: entities + state + reference + journal.
-- v2 → v3 migration is handled in storage.py:_migrate_to_v3.

-- ENTITIES: external-content FTS5 over entities table
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
  name, category, body, tenant_id UNINDEXED,
  content='entities', content_rowid='rowid',
  tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS entities_ai_fts
AFTER INSERT ON entities BEGIN
  INSERT INTO entities_fts(rowid, name, category, body, tenant_id)
  VALUES (new.rowid, new.name, new.category, new.body, new.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad_fts
AFTER DELETE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, name, category, body, tenant_id)
  VALUES ('delete', old.rowid, old.name, old.category, old.body, old.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS entities_au_fts
AFTER UPDATE ON entities BEGIN
  INSERT INTO entities_fts(entities_fts, rowid, name, category, body, tenant_id)
  VALUES ('delete', old.rowid, old.name, old.category, old.body, old.tenant_id);
  INSERT INTO entities_fts(rowid, name, category, body, tenant_id)
  VALUES (new.rowid, new.name, new.category, new.body, new.tenant_id);
END;

-- STATE: external-content FTS5 over state_documents
CREATE VIRTUAL TABLE IF NOT EXISTS state_documents_fts USING fts5(
  document_key, body, tenant_id UNINDEXED,
  content='state_documents', content_rowid='rowid',
  tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS state_documents_ai_fts
AFTER INSERT ON state_documents BEGIN
  INSERT INTO state_documents_fts(rowid, document_key, body, tenant_id)
  VALUES (new.rowid, new.document_key, new.body, new.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS state_documents_ad_fts
AFTER DELETE ON state_documents BEGIN
  INSERT INTO state_documents_fts(state_documents_fts, rowid, document_key, body, tenant_id)
  VALUES ('delete', old.rowid, old.document_key, old.body, old.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS state_documents_au_fts
AFTER UPDATE ON state_documents BEGIN
  INSERT INTO state_documents_fts(state_documents_fts, rowid, document_key, body, tenant_id)
  VALUES ('delete', old.rowid, old.document_key, old.body, old.tenant_id);
  INSERT INTO state_documents_fts(rowid, document_key, body, tenant_id)
  VALUES (new.rowid, new.document_key, new.body, new.tenant_id);
END;

-- REFERENCE: external-content FTS5 over reference_documents
CREATE VIRTUAL TABLE IF NOT EXISTS reference_documents_fts USING fts5(
  doc_key, body, tenant_id UNINDEXED,
  content='reference_documents', content_rowid='rowid',
  tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS reference_ai_fts
AFTER INSERT ON reference_documents BEGIN
  INSERT INTO reference_documents_fts(rowid, doc_key, body, tenant_id)
  VALUES (new.rowid, new.doc_key, new.body, new.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS reference_ad_fts
AFTER DELETE ON reference_documents BEGIN
  INSERT INTO reference_documents_fts(reference_documents_fts, rowid, doc_key, body, tenant_id)
  VALUES ('delete', old.rowid, old.doc_key, old.body, old.tenant_id);
END;

CREATE TRIGGER IF NOT EXISTS reference_au_fts
AFTER UPDATE ON reference_documents BEGIN
  INSERT INTO reference_documents_fts(reference_documents_fts, rowid, doc_key, body, tenant_id)
  VALUES ('delete', old.rowid, old.doc_key, old.body, old.tenant_id);
  INSERT INTO reference_documents_fts(rowid, doc_key, body, tenant_id)
  VALUES (new.rowid, new.doc_key, new.body, new.tenant_id);
END;

-- JOURNAL: standalone FTS5 over journal_events (concatenated payload).
-- Standalone (not external-content) because journal_events has 4 separate
-- JSON payload columns we want searchable as one concatenated field, and
-- there's no single base-table column we could external-content against.
-- Acceptable cost: journal is append-only (no updates), so the body
-- duplication doesn't compound on edits like it would on warm entities.
CREATE VIRTUAL TABLE IF NOT EXISTS journal_events_fts USING fts5(
  ts UNINDEXED, payload, tenant_id UNINDEXED, event_id UNINDEXED,
  tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS journal_events_ai_fts
AFTER INSERT ON journal_events BEGIN
  INSERT INTO journal_events_fts(rowid, ts, payload, tenant_id, event_id)
  VALUES (
    new.rowid, new.ts,
    COALESCE(new.evaluated, '') || ' ' || COALESCE(new.acted, '') || ' ' ||
      COALESCE(new.forward, '') || ' ' || COALESCE(new.extra, ''),
    new.tenant_id, new.id
  );
END;

-- ============================================================================
-- Schema version tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS sibyl_memory_schema_version (
  version       INTEGER PRIMARY KEY,
  applied_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  description   TEXT
);

INSERT OR IGNORE INTO sibyl_memory_schema_version (version, description)
VALUES (1, 'sibyl-memory-client v1. SQLite port of sibyl_memory.* Postgres schema. 10 tables (entities, entity_relations, state_documents, journal_events, revenue_events, error_events, reference_documents, archived_entities, flagged_actors, schema_version) + 2 FTS5 virtual tables. Local-first plugin foundation.');

-- ============================================================================
-- Schema v2 — self-learning skill proposals (review queue)
-- ============================================================================
-- The Learner module scans journal_events for repeating patterns and writes
-- proposed skill documents here. The user reviews via `sibyl learn review`
-- and either accepts (which writes to reference_documents under skill/<slug>)
-- or rejects. Idempotent; safe to re-apply against a v1 database.
CREATE TABLE IF NOT EXISTS skill_proposals (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

  -- detector output
  pattern_kind    TEXT NOT NULL,   -- 'repeated_action' / 'structural_similarity' / 'temporal_routine' / 'co_occurrence'
  proposed_slug   TEXT NOT NULL,   -- the reference_documents.doc_key it would land under (skill/<slug>)
  proposed_title  TEXT,            -- one-line human-readable title
  proposed_body   TEXT NOT NULL,   -- the actual skill body (markdown text)

  -- evidence + provenance
  evidence        TEXT NOT NULL CHECK (json_valid(evidence)),  -- list of source journal_event ids + snippets
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  summarizer      TEXT NOT NULL,   -- 'local-deterministic' / 'byok-anthropic' / 'byok-openai' / 'venice-x402' / etc.

  -- review state
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'superseded')),
  reviewed_at     TEXT,
  review_note     TEXT,

  -- when accepted, points at the reference_documents row that was created
  accepted_doc_key TEXT
);

CREATE INDEX IF NOT EXISTS skill_proposals_tenant_status
  ON skill_proposals (tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS skill_proposals_slug
  ON skill_proposals (tenant_id, proposed_slug);

-- ============================================================================
-- Schema v2 — learning run log (so detectors don't re-scan ground they covered)
-- ============================================================================
CREATE TABLE IF NOT EXISTS learning_runs (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  completed_at    TEXT,
  summarizer      TEXT NOT NULL,
  events_scanned  INTEGER NOT NULL DEFAULT 0,
  proposals_made  INTEGER NOT NULL DEFAULT 0,
  cursor_after_ts TEXT,             -- watermark — newest journal ts processed
  notes           TEXT
);

CREATE INDEX IF NOT EXISTS learning_runs_tenant
  ON learning_runs (tenant_id, started_at DESC);

INSERT OR IGNORE INTO sibyl_memory_schema_version (version, description)
VALUES (2, 'sibyl-memory-client v2. Adds skill_proposals (self-learning review queue) and learning_runs (detector watermark log). Idempotent migration; v1 databases auto-upgrade on first open. Free tier uses local-deterministic summarizer; paid tier can opt into BYOK or Venice/x402-routed summarization.');

INSERT OR IGNORE INTO sibyl_memory_schema_version (version, description)
VALUES (3, 'sibyl-memory-client v3. External-content FTS5 across entities + state_documents + reference_documents + contentless FTS5 over journal_events. Fixes the v0.3.0 "search covers warm entities only" bug. Eliminates body duplication (v2 stored body twice — base table + FTS5). v2 to v3 migration handled in storage.py:_migrate_to_v3: drops the standalone FTS5 tables and rebuilds in external-content shape from existing base-table data. No data loss.');
