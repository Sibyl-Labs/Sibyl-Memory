"""`sibyl memory neighbors` (v6 relations, spec tiered-retrieval 2.B): read-only
listing of one node's edges, tenant-resolved like list/search/recall."""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_client import MemoryClient, entity_ref, reference_ref
from sibyl_memory_client.testing import NullEntitlementGate
from sibyl_memory_cli import cli

TENANT = "3f9a2b44-0000-4000-8000-00000000abcd"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "memory.db"
    cred = tmp_path / "credentials.json"
    cred.write_text(json.dumps({"tenant_id": TENANT, "account_id": "acc-1"}), encoding="utf-8")
    c = MemoryClient.local(path=db, tenant_id=TENANT, entitlement_gate=NullEntitlementGate())
    c.set_entity("company", "Redwood", {})
    c.set_entity("person", "Maya Patel", {})
    c.set_reference("doc/kickoff", "Kickoff")
    c.link(entity_ref("person", "Maya Patel"), entity_ref("company", "Redwood"), "works_at")
    c.link(entity_ref("company", "Redwood"), reference_ref("doc/kickoff"), "source_of")
    c.storage.close()
    return db, cred


def test_neighbors_lists_edges_both_directions(tmp_path, capsys) -> None:
    db, cred = _setup(tmp_path)
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "neighbors",
                   "--entity", "company", "Redwood"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "works_at" in out and "person/Maya Patel" in out
    assert "source_of" in out and "reference doc/kickoff" in out


def test_neighbors_filters_and_reference_node(tmp_path, capsys) -> None:
    db, cred = _setup(tmp_path)
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "neighbors",
                   "--reference", "doc/kickoff", "--direction", "out"])
    out = capsys.readouterr().out
    assert rc == 0 and "(no edges)" in out
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "neighbors",
                   "--entity", "company", "Redwood", "--tier", "reference"])
    out = capsys.readouterr().out
    assert rc == 0 and "source_of" in out and "works_at" not in out


def test_neighbors_bad_cursor_is_a_clean_error(tmp_path, capsys) -> None:
    db, cred = _setup(tmp_path)
    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "neighbors",
                   "--state", "nope", "--cursor", "garbage!!"])
    assert rc == 1
