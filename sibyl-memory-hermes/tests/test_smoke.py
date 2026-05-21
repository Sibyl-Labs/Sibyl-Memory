"""Smoke tests for sibyl-memory-hermes.

These exercise the public provider surface. They run against a fresh
SQLite DB per test via pytest tmp_path fixtures. Hermes is NOT required
to be installed: the provider degrades gracefully.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_memory_hermes import (
    DEFAULT_DB_PATH,
    Credentials,
    CredentialsNotFoundError,
    SibylMemoryProvider,
    __version__,
    load_credentials,
)
from sibyl_memory_hermes.credentials import write_credentials


# ----------------------------------------------------------------------
# Module-level sanity
# ----------------------------------------------------------------------
def test_version_is_pep440() -> None:
    """__version__ must be PEP440 format and single-sourced from importlib.metadata (v0.3.0+)."""
    import re
    from importlib.metadata import version as _v

    # PEP440 minimum: N.N.N with optional pre/post/dev/local suffix.
    assert re.match(r"^\d+\.\d+\.\d+", __version__), f"non-PEP440 version: {__version__}"
    # When the package is installed (not a raw source tree), __version__ must
    # match what importlib.metadata returns. v0.3.0 fixed the drift bug where
    # __init__.py and the wheel could disagree.
    if not __version__.endswith("+source"):
        assert __version__ == _v("sibyl-memory-hermes")


def test_default_db_path_is_home_relative() -> None:
    assert DEFAULT_DB_PATH.startswith("~")


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------
def test_construct_explicit_path_default_tenant(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    provider = SibylMemoryProvider(db_path=str(db), autoload_credentials=False)
    assert provider.tenant_id == "00000000-0000-0000-0000-000000000001"
    # Schema is currently v3 (cross-tier FTS5 landed 2026-05-18). Assert >= 2
    # so the test survives future schema bumps without spurious breakage.
    assert provider.client.schema_version() >= 2
    assert db.exists()


def test_construct_explicit_tenant(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    provider = SibylMemoryProvider(
        db_path=str(db), tenant_id="alice@example.com", autoload_credentials=False
    )
    assert provider.tenant_id == "alice@example.com"


def test_construct_with_missing_credentials_degrades(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    cred = tmp_path / "no-creds.json"
    provider = SibylMemoryProvider(
        db_path=str(db),
        credentials_path=str(cred),
    )
    # Default tenant when creds absent
    assert provider.tenant_id == "00000000-0000-0000-0000-000000000001"
    assert provider.credentials is None


def test_construct_require_credentials_raises(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    cred = tmp_path / "no-creds.json"
    with pytest.raises(CredentialsNotFoundError):
        SibylMemoryProvider(
            db_path=str(db),
            credentials_path=str(cred),
            require_credentials=True,
        )


def test_construct_with_credentials_file(tmp_path: Path) -> None:
    db = tmp_path / "memory.db"
    cred_path = tmp_path / "credentials.json"
    creds = Credentials(
        account_id="acct-123",
        tenant_id="alice@example.com",
        tier="lifetime",
        email="alice@example.com",
        issued_at="2026-05-21T14:32:18Z",
    )
    write_credentials(creds, cred_path)

    provider = SibylMemoryProvider(
        db_path=str(db), credentials_path=str(cred_path)
    )
    assert provider.tenant_id == "alice@example.com"
    assert provider.credentials is not None
    assert provider.credentials.tier == "lifetime"


# ----------------------------------------------------------------------
# Hermes contract surface
# ----------------------------------------------------------------------
def test_save_and_load_context(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    ev_id = provider.save_context(
        inputs={"user": "what's the status?"},
        outputs={"agent": "all green"},
    )
    assert isinstance(ev_id, str)
    assert len(ev_id) >= 8

    events = provider.load_context(limit=10)
    assert len(events) == 1
    assert events[0]["evaluated"] == {"user": "what's the status?"}
    assert events[0]["acted"] == {"agent": "all green"}


def test_clear_context_is_noop(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.save_context({"q": "hi"}, {"r": "hello"})
    assert provider.clear_context() is None
    # journal still has the entry
    assert len(provider.load_context()) == 1


# ----------------------------------------------------------------------
# Fact store (entities)
# ----------------------------------------------------------------------
def test_remember_recall_forget(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )

    ent = provider.remember(
        "project", "atlas",
        {"status": "active", "owner": "jane"},
        status="active",
    )
    assert ent["category"] == "project"
    assert ent["name"] == "atlas"
    assert ent["body"]["status"] == "active"

    fetched = provider.recall("project", "atlas")
    assert fetched is not None
    assert fetched["body"]["owner"] == "jane"

    assert provider.recall("project", "nonexistent") is None

    assert provider.forget("project", "atlas") is True
    assert provider.recall("project", "atlas") is None


def test_list_entities(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.remember("project", "atlas", {"status": "active"}, status="active")
    provider.remember("project", "borealis", {"status": "stale"}, status="stale")
    provider.remember("person", "jane", {"role": "ops"})

    projects = provider.list(category="project")
    assert {p["name"] for p in projects} == {"atlas", "borealis"}

    active = provider.list(category="project", status="active")
    assert len(active) == 1
    assert active[0]["name"] == "atlas"


def test_archive_round_trip(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.remember("project", "dead-prototype", {"status": "abandoned"})
    result = provider.archive("project", "dead-prototype", reason="stale")
    assert "archived_id" in result
    # Active set no longer contains it
    assert provider.recall("project", "dead-prototype") is None


# ----------------------------------------------------------------------
# State / reference / search
# ----------------------------------------------------------------------
def test_state_documents(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.set_state("current-priorities", {"top": ["ship plugin"]})
    state = provider.get_state("current-priorities")
    assert state is not None
    assert state["body"]["top"] == ["ship plugin"]
    assert provider.get_state("nonexistent") is None


def test_reference_documents(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.set_reference(
        "voice-rules", "lowercase is fine. no em dashes.",
        metadata={"source": "SIBYL-VOICE.md"},
    )
    ref = provider.get_reference("voice-rules")
    assert ref is not None
    assert "em dashes" in ref["body"]
    assert ref["metadata"]["source"] == "SIBYL-VOICE.md"


def test_fts_search(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    provider.remember("project", "atlas", {"summary": "memory plugin shipping"})
    provider.remember("project", "borealis", {"summary": "audit dashboard"})
    provider.remember("person", "jane", {"role": "operator ops"})

    # v0.3.1: provider.search() now returns cross-tier hits with a `key`
    # field (was: entity rows with `name`). The shape is documented in
    # MemoryClient.search(): each hit is {tier, key, category, body, ...}.
    results = provider.search("memory")
    keys = {r["key"] for r in results if r["tier"] == "entity"}
    assert "atlas" in keys
    assert "borealis" not in keys


# ----------------------------------------------------------------------
# Multi-tenant isolation
# ----------------------------------------------------------------------
def test_multi_tenant_isolation(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    alice = SibylMemoryProvider(
        db_path=str(db), tenant_id="alice", autoload_credentials=False
    )
    bob = SibylMemoryProvider(
        db_path=str(db), tenant_id="bob", autoload_credentials=False
    )

    alice.remember("project", "atlas", {"owner": "alice"})
    bob.remember("project", "atlas", {"owner": "bob"})

    a = alice.recall("project", "atlas")
    b = bob.recall("project", "atlas")
    assert a is not None and b is not None
    assert a["body"]["owner"] == "alice"
    assert b["body"]["owner"] == "bob"

    # Neither tenant sees the other's entities
    assert len(alice.list(category="project")) == 1
    assert len(bob.list(category="project")) == 1


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------
def test_health(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    h = provider.health()
    assert h["ok"] is True
    # Schema is currently v3 (cross-tier FTS5 landed 2026-05-18)
    assert h["schema_version"] >= 2
    assert h["db_size_bytes"] >= 0
    assert h["tier"] == "free"
    # hermes_bound is deprecated since v0.3.0 and always False: the asymmetry
    # is the signal v0.4 cleanup is approaching. Tightened from bool-only check.
    assert h["hermes_bound"] is False


def test_provider_exposes_client(tmp_path: Path) -> None:
    provider = SibylMemoryProvider(
        db_path=str(tmp_path / "m.db"), autoload_credentials=False
    )
    # The underlying MemoryClient is accessible for advanced use
    assert provider.client is not None
    assert hasattr(provider.client, "set_entity")
    assert hasattr(provider.client, "write_event")


# ----------------------------------------------------------------------
# Credentials file plumbing
# ----------------------------------------------------------------------
def test_write_then_load_credentials(tmp_path: Path) -> None:
    cred_path = tmp_path / "credentials.json"
    creds_in = Credentials(
        account_id="abc-def",
        tenant_id="user@example.com",
        tier="sync",
        email="user@example.com",
        wallet="0xabc",
        issued_at="2026-05-21T14:32:18Z",
        schema_version=1,
    )
    write_credentials(creds_in, cred_path)

    # File is mode 0600
    assert oct(cred_path.stat().st_mode)[-3:] == "600"

    creds_out = load_credentials(cred_path)
    assert creds_out.tenant_id == "user@example.com"
    assert creds_out.tier == "sync"
    assert creds_out.wallet == "0xabc"


def test_load_credentials_missing(tmp_path: Path) -> None:
    with pytest.raises(CredentialsNotFoundError):
        load_credentials(tmp_path / "nope.json")


def test_load_credentials_missing_ids_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tier": "free"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_credentials(bad)
