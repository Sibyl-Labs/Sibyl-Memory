"""F1 (Kravento PL eval 2026-08-12): `sibyl memory` reads the ACTIVATED tenant.

cmd_memory used to open the store with MemoryClient.local(path=...) and no
tenant_id, so it always read DEFAULT_TENANT while the MCP server writes under the
account's real tenant (Contract T ladder: tenant_id -> account_id ->
DEFAULT_TENANT). An activated account therefore saw "(no entities)" for a healthy
store. cmd_memory now resolves the tenant from credentials.json exactly like the
MCP server, so `sibyl memory list/search/recall` sees what the MCP wrote.
"""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_cli import cli

REAL_TENANT = "3f9a2b44-0000-4000-8000-00000000abcd"


def _write_creds(tmp_path: Path, creds: dict) -> Path:
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    return p


def _seed(db: Path, tenant_id: str) -> None:
    c = MemoryClient.local(path=db, tenant_id=tenant_id)
    c.set_entity("partner", "Blocktronics", {"stage": "active", "note": "token forensics suite"})
    c.set_entity("partner", "Reppo", {"stage": "negotiation"})
    c.storage.close()


def test_memory_reads_activated_tenant(tmp_path, capsys):
    """Acceptance repro: write via a real tenant, then the CLI memory command sees
    it — across list, search and recall."""
    db = tmp_path / "memory.db"
    cred = _write_creds(tmp_path, {"tenant_id": REAL_TENANT, "account_id": "acc-1", "tier": "free"})
    _seed(db, REAL_TENANT)

    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Blocktronics" in out and "Reppo" in out

    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "search", "forensics"])
    out = capsys.readouterr().out
    assert rc == 0 and "Blocktronics" in out

    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "recall", "partner", "Blocktronics"])
    out = capsys.readouterr().out
    assert rc == 0 and "forensics" in out


def test_memory_account_id_fallback(tmp_path, capsys):
    """Ladder parity with the MCP: account_id-only credentials -> CLI reads rows
    written under tenant_id=<account_id>."""
    db = tmp_path / "memory.db"
    cred = _write_creds(tmp_path, {"account_id": "acc-only-42", "tier": "free"})
    _seed(db, "acc-only-42")

    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Blocktronics" in out and "Reppo" in out


def test_memory_default_tenant_without_creds(tmp_path, capsys):
    """Legacy behavior preserved: --credentials pointing at an absent path -> rows
    written under DEFAULT_TENANT are still visible."""
    db = tmp_path / "memory.db"
    _seed(db, DEFAULT_TENANT)
    absent = tmp_path / "nope" / "credentials.json"

    rc = cli.main(["--credentials", str(absent), "--db", str(db), "memory", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Blocktronics" in out and "Reppo" in out


def test_memory_activated_tenant_not_polluted_by_default(tmp_path, capsys):
    """Proves the read actually MOVED tenants: with activated credentials present,
    rows written under DEFAULT_TENANT are NOT listed."""
    db = tmp_path / "memory.db"
    cred = _write_creds(tmp_path, {"tenant_id": REAL_TENANT, "account_id": "acc-1", "tier": "free"})
    # seed ONLY under DEFAULT_TENANT; the activated tenant is empty
    _seed(db, DEFAULT_TENANT)

    rc = cli.main(["--credentials", str(cred), "--db", str(db), "memory", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Blocktronics" not in out and "Reppo" not in out
    assert "(no entities)" in out
