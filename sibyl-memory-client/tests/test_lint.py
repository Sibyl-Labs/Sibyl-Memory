"""Smoke tests for sibyl_memory_client.lint."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sibyl_memory_client import (
    Finding,
    LintReport,
    Linter,
    MemoryClient,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path: Path) -> MemoryClient:
    # Memory linter is paid-tier only. Tests run as a lifetime-tier user.
    return MemoryClient.local(str(tmp_path / "memory.db"), tier="lifetime")


# ----------------------------------------------------------------------
# Baseline
# ----------------------------------------------------------------------
def test_lint_clean_db_has_no_critical(client: MemoryClient) -> None:
    report = client.lint()
    assert isinstance(report, LintReport)
    assert report.ok is True
    assert report.schema_version >= 2
    assert report.critical == []
    # Counts all present
    assert "entities" in report.counts
    assert "skill_proposals" in report.counts


def test_lint_includes_db_path_and_size(client: MemoryClient) -> None:
    client.set_entity("project", "atlas", {"status": "active"})
    report = client.lint()
    assert report.db_path.endswith("memory.db")
    assert report.db_size_bytes > 0


def test_lint_to_ascii_renders(client: MemoryClient) -> None:
    report = client.lint()
    rendered = report.to_ascii()
    assert "SIBYL MEMORY · LINT REPORT" in rendered
    assert "schema v" in rendered
    assert "critical" in rendered


# ----------------------------------------------------------------------
# Specific checks
# ----------------------------------------------------------------------
def test_duplicate_entity_finding(client: MemoryClient) -> None:
    client.set_entity("project", "atlas", {"x": 1})
    client.set_entity("product", "atlas", {"y": 2})  # same name, different category
    report = client.lint()
    msgs = [f.check for f in report.findings]
    assert "duplicate-entity" in msgs


def test_empty_reference_finding(client: MemoryClient) -> None:
    # Insert an empty reference doc directly via storage to bypass SDK validation
    with client.storage.transaction() as conn:
        conn.execute(
            "INSERT INTO reference_documents (tenant_id, doc_key, body) "
            "VALUES (?, ?, '')",
            (client.get_tenant(), "skill/empty-test"),
        )
    report = client.lint()
    assert any(f.check == "empty-reference" for f in report.findings)


def test_stale_entity_finding(client: MemoryClient) -> None:
    # Force-write an entity with an ancient updated_at via direct SQL
    client.set_entity("project", "ancient", {"created": True})
    with client.storage.transaction() as conn:
        conn.execute(
            "UPDATE entities SET updated_at = '2020-01-01T00:00:00.000Z' "
            "WHERE tenant_id = ? AND name = 'ancient'",
            (client.get_tenant(),),
        )
    report = client.lint()
    assert any(f.check == "stale-entity" for f in report.findings)


def test_journal_without_acts_finding(client: MemoryClient) -> None:
    # write_event refuses None for everything; insert directly
    from sibyl_memory_client.storage import new_id
    with client.storage.transaction() as conn:
        conn.execute(
            "INSERT INTO journal_events (id, tenant_id, ts) VALUES (?, ?, ?)",
            (new_id(), client.get_tenant(), "2026-05-15T17:30:00.000Z"),
        )
    report = client.lint()
    assert any(f.check == "journal-without-acts" for f in report.findings)


def test_soft_cap_critical_threshold(client: MemoryClient) -> None:
    # Write enough rows to push the DB well above any tiny cap we set.
    for i in range(20):
        client.set_entity("project", f"p{i}", {"i": i, "payload": "x" * 200})
    # Run with a 2 KB cap: well below the actual DB size after writes
    report = client.lint(soft_cap_bytes=2 * 1024)
    matches = [f for f in report.findings if f.check == "db-soft-cap"]
    assert matches, f"expected db-soft-cap finding; got {[f.check for f in report.findings]}"
    assert matches[0].severity in ("warning", "critical")


def test_findings_severity_buckets(client: MemoryClient) -> None:
    client.set_entity("project", "atlas", {})
    client.set_entity("person", "atlas", {})  # duplicate name -> warning
    report = client.lint(soft_cap_bytes=4 * 1024)  # very tiny -> warning or critical
    # Buckets resolve correctly
    assert isinstance(report.critical, list)
    assert isinstance(report.warnings, list)
    assert isinstance(report.info, list)
    total = len(report.critical) + len(report.warnings) + len(report.info)
    assert total == len(report.findings)


def test_lint_to_dict_serializes(client: MemoryClient) -> None:
    report = client.lint()
    d = report.to_dict()
    assert "findings" in d
    assert "counts" in d
    assert "ok" in d
    assert "schema_version" in d
    assert isinstance(d["findings"], list)


# ----------------------------------------------------------------------
# Multi-tenant isolation
# ----------------------------------------------------------------------
def test_lint_is_tenant_scoped(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    alice = MemoryClient.local(str(db), tenant_id="alice", tier="lifetime")
    bob = MemoryClient.local(str(db), tenant_id="bob", tier="lifetime")

    # Only alice creates a duplicate-name pair
    alice.set_entity("project", "atlas", {})
    alice.set_entity("product", "atlas", {})

    alice_report = alice.lint()
    bob_report = bob.lint()

    assert any(f.check == "duplicate-entity" for f in alice_report.findings)
    assert not any(f.check == "duplicate-entity" for f in bob_report.findings)


# ----------------------------------------------------------------------
# Tier gating: free tier blocked, paid tier allowed
# ----------------------------------------------------------------------
def test_free_tier_cannot_lint(tmp_path: Path) -> None:
    from sibyl_memory_client import TierGateError
    free = MemoryClient.local(str(tmp_path / "f.db"))  # default tier="free"
    with pytest.raises(TierGateError) as exc:
        free.lint()
    assert exc.value.feature == "memory linter"
    assert exc.value.current_tier == "free"
    assert "sibyllabs.org" in exc.value.upgrade_url


def test_paid_tiers_can_lint(tmp_path: Path) -> None:
    for tier in ("sync", "team", "lifetime", "stake", "enterprise"):
        c = MemoryClient.local(str(tmp_path / f"{tier}.db"), tier=tier)
        report = c.lint()
        assert report.ok or report.warnings  # runs without raising


def test_free_tier_status_visible_without_gate(tmp_path: Path) -> None:
    """Free-tier users CAN see their cap status (for upgrade-prompt UX) without
    being able to call lint() itself."""
    free = MemoryClient.local(str(tmp_path / "f.db"))
    status = free.free_tier_status()
    assert status["tier"] == "free"
    assert status["soft_cap_bytes"] == 2 * 1024 * 1024
    assert "upgrade_url" in status
    assert status["uncapped"] is False


def test_paid_tier_status_shows_uncapped(tmp_path: Path) -> None:
    paid = MemoryClient.local(str(tmp_path / "p.db"), tier="lifetime")
    status = paid.free_tier_status()
    assert status["tier"] == "lifetime"
    assert status["uncapped"] is True
    assert status["soft_cap_bytes"] is None
