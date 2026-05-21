"""End-to-end smoke test for sibyl-memory-client v0.1.0.

Exercises every public method against a fresh SQLite database in a temp
directory. Verifies schema applies, FTS5 triggers fire, JSON validation
holds, and the typed exceptions surface correctly.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

# Run from repo: PYTHONPATH=src python tests/test_smoke.py
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sibyl_memory_client import (
    DEFAULT_TENANT,
    MemoryClient,
    NotFoundError,
    ValidationError,
)


def test_schema_applies_idempotently(tmp_path):
    db = tmp_path / "memory.db"
    client = MemoryClient.local(db)
    # v2 is the current schema as of 2026-05-15 (added skill_proposals + learning_runs)
    assert client.schema_version() >= 2, "schema_version should be 2 after first open"
    # Re-open: no error
    client2 = MemoryClient.local(db)
    assert client2.schema_version() >= 2
    return "schema applies and re-applies idempotently"


def test_entity_roundtrip(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    body = {"status": "active", "members": ["a", "b"], "score": 9.5}
    written = client.set_entity("project", "atlas", body, status="active")
    assert written["category"] == "project"
    assert written["name"] == "atlas"
    assert written["status"] == "active"
    assert written["body"] == body
    assert written["tenant_id"] == DEFAULT_TENANT
    assert written["id"]  # UUID assigned

    read = client.get_entity("project", "atlas")
    assert read["body"] == body

    # Update via set_entity overwrites
    body["score"] = 9.7
    updated = client.set_entity("project", "atlas", body, status="active")
    assert updated["body"]["score"] == 9.7
    assert updated["id"] == written["id"], "update preserves entity id"
    return "entity roundtrip works (insert, read, update)"


def test_entity_listing_and_filtering(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("project", "alpha", {}, status="active")
    client.set_entity("project", "beta", {}, status="paused")
    client.set_entity("person", "alice", {}, status="active")

    all_projects = client.list_entities(category="project")
    assert len(all_projects) == 2, f"expected 2 projects, got {len(all_projects)}"

    active_projects = client.list_entities(category="project", status="active")
    assert len(active_projects) == 1
    assert active_projects[0]["name"] == "alpha"

    all_active = client.list_entities(status="active")
    assert len(all_active) == 2
    return f"list/filter works: {len(all_projects)} projects, {len(active_projects)} active project"


def test_journal_append_and_read(tmp_path):
    import time
    client = MemoryClient.local(tmp_path / "memory.db")
    client.write_event(
        evaluated=["option A", "option B"],
        acted=["chose A", "tx 0xabc"],
        forward=["follow up tomorrow"],
        extra={"session": "smoke", "n": 1},
    )
    time.sleep(0.001)  # guarantee microsecond ts separation across writes
    client.write_event(acted=["another event"])
    events = client.read_events(limit=10)
    assert len(events) == 2, f"expected 2 events, got {len(events)}"
    # Newest first (ts DESC, id DESC tiebreaker)
    assert events[0]["acted"] == ["another event"], f"events[0] acted = {events[0]['acted']}"
    assert events[1]["evaluated"] == ["option A", "option B"], f"events[1] evaluated = {events[1]['evaluated']}"
    return f"journal append+read works ({len(events)} events round-tripped)"


def test_state_documents(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    assert client.get_state("priorities") is None
    client.set_state("priorities", {"items": [1, 2, 3], "version": "v2"})
    got = client.get_state("priorities")
    assert got["body"]["items"] == [1, 2, 3]
    # Upsert
    client.set_state("priorities", {"items": [4, 5], "version": "v3"})
    got2 = client.get_state("priorities")
    assert got2["body"]["version"] == "v3"
    return "state_documents upsert + read works"


def test_reference_documents(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_reference(
        "voice-rules",
        "no em-dashes, no LLM tells, lowercase ok.",
        metadata={"applies_to": ["x", "ping", "email"]},
    )
    ref = client.get_reference("voice-rules")
    assert "em-dashes" in ref["body"]
    assert ref["metadata"]["applies_to"] == ["x", "ping", "email"]
    return "reference_documents (body + metadata) works"


def test_archive_flow(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("project", "dead-deal", {"note": "abandoned"})
    result = client.archive_entity("project", "dead-deal", reason="founder disappeared")
    assert "archived_id" in result
    # Entity should be gone from active set
    try:
        client.get_entity("project", "dead-deal")
        return "FAIL: archived entity still in active set"
    except NotFoundError:
        pass
    return "archive moves entity out of active set"


def test_fts_search(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("project", "atlas", {"description": "Distributed inference platform"})
    client.set_entity("project", "horizon", {"description": "On-chain prediction markets"})
    client.set_entity("person", "alice", {"role": "infrastructure engineer"})
    results = client.search_entities("inference")
    assert len(results) >= 1
    found_names = {r["name"] for r in results}
    assert "atlas" in found_names
    return f"FTS5 search works ({len(results)} hits for 'inference')"


def test_json_validation(tmp_path):
    client = MemoryClient.local(tmp_path / "memory.db")
    class Unserializable:
        pass
    try:
        client.set_entity("test", "broken", {"obj": Unserializable()})
        return "FAIL: should have raised ValidationError"
    except ValidationError:
        pass
    return "JSON validation rejects unserializable input"


def test_tenant_isolation(tmp_path):
    client_a = MemoryClient.local(tmp_path / "memory.db", tenant_id="tenant-a")
    client_b = MemoryClient.local(tmp_path / "memory.db", tenant_id="tenant-b")
    client_a.set_entity("project", "shared-name", {"owner": "a"})
    client_b.set_entity("project", "shared-name", {"owner": "b"})
    assert client_a.get_entity("project", "shared-name")["body"]["owner"] == "a"
    assert client_b.get_entity("project", "shared-name")["body"]["owner"] == "b"
    return "multi-tenant: same (category, name) isolated by tenant_id"


def main():
    tests = [
        test_schema_applies_idempotently,
        test_entity_roundtrip,
        test_entity_listing_and_filtering,
        test_journal_append_and_read,
        test_state_documents,
        test_reference_documents,
        test_archive_flow,
        test_fts_search,
        test_json_validation,
        test_tenant_isolation,
    ]
    passed = failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                msg = t(Path(td))
                print(f"  PASS  {t.__name__:48s}  {msg}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {t.__name__:48s}  {e}")
                failed += 1
            except Exception as e:
                print(f"  ERR   {t.__name__:48s}  {type(e).__name__}: {e}")
                failed += 1
    print()
    print(f"  {passed}/{len(tests)} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
