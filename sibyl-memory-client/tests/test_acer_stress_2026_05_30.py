"""Regression tests promoted from Acer's adversarial stress-test suite (2026-05-30).

Three findings on sibyl-memory-client 0.4.4:
  - BUG-RAW-ENTITY-PRIMITIVE-BODY (medium): set_entity accepted a primitive body
  - BUG-RAW-STATE-PRIMITIVE-BODY  (medium): set_state accepted a primitive body
  - CHAOS-POISONED-EXTERNAL-FTS   (high) : a poisoned external-content FTS row
                                            crashed search() with StorageError

Source: https://sibyl-memory-stress-test.vercel.app/runs/all-live
"""
import sqlite3
import pytest
from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import ValidationError, StorageError


# --- contract: structured-body enforcement (entity + state) -----------------

def test_set_entity_rejects_primitive_body(tmp_path):
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    for bad in ("bad", 7, 3.14, True, None):
        with pytest.raises(ValidationError):
            c.set_entity("bounty", "primitive", bad)


def test_set_state_rejects_primitive_body(tmp_path):
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    for bad in ("bad", 7, 3.14, True, None):
        with pytest.raises(ValidationError):
            c.set_state("primitive_state", bad)


def test_structured_bodies_still_accepted(tmp_path):
    # The contract is dict|list — both must still pass (guard against over-correction).
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    c.set_entity("notes", "dict_body", {"k": "v"})
    c.set_entity("notes", "list_body", [1, 2, 3])
    c.set_state("dict_state", {"a": 1})
    c.set_state("list_state", ["x"])
    assert c.get_entity("notes", "dict_body")["body"] == {"k": "v"}
    assert c.get_entity("notes", "list_body")["body"] == [1, 2, 3]


# --- chaos: poisoned external-content FTS index must not crash search --------

def _poison_entities_fts(db_path):
    raw = sqlite3.connect(db_path)
    seg_ids = [r[0] for r in raw.execute(
        "SELECT id FROM entities_fts_data WHERE id >= 2").fetchall()]
    for sid in seg_ids:
        raw.execute("UPDATE entities_fts_data SET block = ? WHERE id = ?",
                    (b"\xff\x00\xde\xad\xbe\xef" * 8, sid))
    raw.commit(); raw.close()
    return seg_ids


def test_poisoned_external_fts_does_not_crash_search(tmp_path):
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa-sandbox")
    c.set_entity("notes", "fox", {"text": "the quick brown fox jumps over the lazy dog"})
    assert len(c.search("fox")) >= 1  # baseline

    seg_ids = _poison_entities_fts(str(db))
    assert seg_ids, "expected external-content FTS segment rows to corrupt"

    # Must NOT raise. Self-heal (rebuild from intact base table) is best-case;
    # an empty list is acceptable containment. A StorageError crash is the bug.
    hits = c.search("fox")
    assert isinstance(hits, list)


def test_poisoned_fts_self_heals_from_base_table(tmp_path):
    # When the base table is intact, containment should rebuild and recover hits.
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa-sandbox")
    c.set_entity("notes", "fox", {"text": "the quick brown fox jumps"})
    _poison_entities_fts(str(db))
    hits = c.search("fox")
    assert any(h["key"] == "fox" for h in hits), "rebuild should recover the hit"


def test_search_entities_contains_poisoned_fts(tmp_path):
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa-sandbox")
    c.set_entity("notes", "fox", {"text": "the quick brown fox"})
    _poison_entities_fts(str(db))
    # search_entities() shares the containment path — must not crash either.
    assert isinstance(c.search_entities("fox"), list)


# --- review hardening: corruption is contained, but code bugs still surface ---

def test_fts_query_reraises_programming_error(tmp_path):
    """A binding/SQL bug (ProgrammingError) must NOT be swallowed as []."""
    import sqlite3
    from sibyl_memory_client.client import _fts_query
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa-sandbox")
    with c._storage.connection() as conn:
        with pytest.raises(sqlite3.ProgrammingError):
            # binding-count mismatch: 1 placeholder, 0 params
            _fts_query(conn, "SELECT 1 WHERE 1 = ?", (), "entities_fts")
