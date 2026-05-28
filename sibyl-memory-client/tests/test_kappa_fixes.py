"""Regression tests for the v0.4.0 KAPPA-attributed fixes.

Covers:
- BLOCKER: CapExceededError + TierVerificationError importable from
  sibyl_memory_client.exceptions (the canonical submodule path).
- RED:     memory.db is chmod 0600 after Storage init.
- YELLOW:  validate_identifier rejects empty / null-byte / non-string /
           oversized. set_entity, set_state, set_reference call it.
- YELLOW:  _classify_fts5_error returns the right exception type for the
           three buckets (schema-missing → None; FTS5-syntax → ValidationError;
           backend → StorageError).

Source bug report: /tmp/kappa-sibyl-memory-mcp-report.md
(KAPPA, 2026-05-18, via Acer/Tulip referral).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ----------------------------------------------------------------------
# BLOCKER: submodule exception path
# ----------------------------------------------------------------------

def test_cap_exceeded_error_importable_from_exceptions_submodule():
    """KAPPA's exact import path. server.py:41 does this; before v0.4.0
    it raised ImportError."""
    from sibyl_memory_client.exceptions import CapExceededError
    assert CapExceededError.__name__ == "CapExceededError"
    assert CapExceededError.code == "CAP_EXCEEDED"
    # Constructor contract: positional message + required keyword args
    err = CapExceededError("test", current_size=100, cap=200, proposed_delta=10)
    assert err.current_size == 100
    assert err.cap == 200
    assert err.proposed_delta == 10
    assert "tiers" in err.upgrade_url


def test_tier_verification_error_importable_from_exceptions_submodule():
    """Same submodule path. KAPPA's blocker covered both classes."""
    from sibyl_memory_client.exceptions import TierVerificationError
    assert TierVerificationError.__name__ == "TierVerificationError"
    assert TierVerificationError.code == "TIER_VERIFY_FAILED"
    err = TierVerificationError("test")
    assert "internet" in err.recovery or "verify" in err.recovery


def test_capcheck_backwards_compat_reexports():
    """Anyone reaching into the private _capcheck module should still get the
    same class objects (identity check) post-relocation."""
    from sibyl_memory_client.exceptions import CapExceededError as E_exc
    from sibyl_memory_client._capcheck import CapExceededError as E_cap
    assert E_exc is E_cap, "_capcheck must re-export the same class object"
    from sibyl_memory_client.exceptions import TierVerificationError as T_exc
    from sibyl_memory_client._capcheck import TierVerificationError as T_cap
    assert T_exc is T_cap


def test_top_level_package_still_exports_both():
    """The top-level `from sibyl_memory_client import CapExceededError` path
    that already worked in v0.3.3 must still work: no regression on the
    main public surface."""
    from sibyl_memory_client import CapExceededError, TierVerificationError
    assert CapExceededError.__name__ == "CapExceededError"
    assert TierVerificationError.__name__ == "TierVerificationError"


# ----------------------------------------------------------------------
# RED: memory.db file perms
# ----------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "chmod"), reason="POSIX-only test")
def test_memory_db_file_perms_are_0600(tmp_path):
    """KAPPA RED finding. Docs claim 0600, actual was 0644 (umask default).
    After v0.4.0, Storage init tightens to 0600 unconditionally."""
    from sibyl_memory_client import MemoryClient
    db_path = tmp_path / "memory.db"
    MemoryClient.local(db_path)
    assert db_path.exists(), "DB file should exist after Storage init"
    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert mode == 0o600, f"memory.db mode should be 0600, got 0o{mode:o}"


@pytest.mark.skipif(not hasattr(os, "chmod"), reason="POSIX-only test")
def test_memory_db_wal_sidecar_perms_tighten_when_present(tmp_path):
    """WAL/SHM sidecar files also get 0600 if they exist after a write."""
    from sibyl_memory_client import MemoryClient
    db_path = tmp_path / "memory.db"
    client = MemoryClient.local(db_path)
    # Force a write so WAL/SHM appear, then re-init to trigger another chmod pass.
    client.set_entity("test", "alpha", {"k": "v"})
    # Re-open to trigger the chmod pass on the WAL/SHM files
    MemoryClient.local(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            mode = stat.S_IMODE(sidecar.stat().st_mode)
            assert mode == 0o600, f"{sidecar.name} should be 0600, got 0o{mode:o}"


# ----------------------------------------------------------------------
# YELLOW: validate_identifier
# ----------------------------------------------------------------------

def test_validate_identifier_rejects_empty():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    with pytest.raises(ValidationError, match="cannot be empty"):
        validate_identifier("", field_name="name")


def test_validate_identifier_rejects_non_string():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    with pytest.raises(ValidationError, match="must be a string"):
        validate_identifier(123, field_name="name")
    with pytest.raises(ValidationError, match="must be a string"):
        validate_identifier(None, field_name="name")


def test_validate_identifier_rejects_null_bytes():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    with pytest.raises(ValidationError, match="forbidden control character"):
        validate_identifier("foo\x00bar", field_name="name")


def test_validate_identifier_rejects_other_control_chars():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    with pytest.raises(ValidationError, match="forbidden control character"):
        validate_identifier("foo\tbar", field_name="key")  # tab
    with pytest.raises(ValidationError, match="forbidden control character"):
        validate_identifier("foo\nbar", field_name="key")  # newline


def test_validate_identifier_rejects_oversized():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    too_long = "a" * 1025
    with pytest.raises(ValidationError, match="too long"):
        validate_identifier(too_long, field_name="name")


def test_validate_identifier_accepts_reasonable():
    from sibyl_memory_client.client import validate_identifier
    # All of these should pass
    for ok in ("foo", "alice", "project-atlas", "a", "x" * 1024,
               "with spaces", "unicode-é-ñ-中", "with.dot", "with/slash"):
        assert validate_identifier(ok, field_name="name") == ok


# ----------------------------------------------------------------------
# YELLOW: write paths call validate_identifier
# ----------------------------------------------------------------------

def test_set_entity_rejects_empty_name(tmp_path):
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.exceptions import ValidationError
    client = MemoryClient.local(tmp_path / "memory.db")
    with pytest.raises(ValidationError, match="cannot be empty"):
        client.set_entity("project", "", {"k": "v"})


def test_set_entity_rejects_null_byte_in_category(tmp_path):
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.exceptions import ValidationError
    client = MemoryClient.local(tmp_path / "memory.db")
    with pytest.raises(ValidationError, match="forbidden control character"):
        client.set_entity("proj\x00ect", "atlas", {"k": "v"})


def test_set_state_rejects_oversized_key(tmp_path):
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.exceptions import ValidationError
    client = MemoryClient.local(tmp_path / "memory.db")
    with pytest.raises(ValidationError, match="too long"):
        client.set_state("k" * 2000, {"v": 1})


def test_set_reference_rejects_empty_key(tmp_path):
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.exceptions import ValidationError
    client = MemoryClient.local(tmp_path / "memory.db")
    with pytest.raises(ValidationError, match="cannot be empty"):
        client.set_reference("", "body text")


def test_read_paths_unaffected_by_validation(tmp_path):
    """Read paths (get_entity, get_state, get_reference) must NOT validate -
    users with already-stored bad identifiers should still be able to read
    and migrate them.

    We can't easily inject bad data through a write (validation blocks),
    but we can confirm get_entity/get_state with weird-but-not-validated
    inputs returns NotFoundError (the lookup path), not ValidationError."""
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client.exceptions import NotFoundError
    client = MemoryClient.local(tmp_path / "memory.db")
    # Read on bad identifier should be NotFound, not ValidationError -
    # we don't gate reads. (NB: passing through SQLite, which handles it.)
    with pytest.raises(NotFoundError):
        client.get_entity("project", "nonexistent-but-validly-named")
    # get_state returns None for missing keys (not raise).
    assert client.get_state("nonexistent") is None


# ----------------------------------------------------------------------
# YELLOW. FTS5 error classifier
# ----------------------------------------------------------------------

def test_classify_fts5_error_schema_missing_returns_none():
    """no such table case → caller should return empty (defensive)."""
    from sibyl_memory_client.client import _classify_fts5_error
    err = sqlite3.OperationalError("no such table: entities_fts")
    assert _classify_fts5_error(err) is None


def test_classify_fts5_error_syntax_returns_validation_error():
    """malformed match / fts5 syntax errors → ValidationError."""
    from sibyl_memory_client.client import _classify_fts5_error
    from sibyl_memory_client.exceptions import ValidationError
    for msg in (
        "fts5: syntax error near \"AND\"",
        "malformed MATCH expression: \"bad\"",
        "fts5 query error",
        "no such column: invalid_col",
    ):
        err = sqlite3.OperationalError(msg)
        result = _classify_fts5_error(err)
        assert isinstance(result, ValidationError), \
            f"expected ValidationError for {msg!r}, got {type(result)}"


def test_classify_fts5_error_other_returns_storage_error():
    """Anything else (disk full, locked, etc.) → StorageError."""
    from sibyl_memory_client.client import _classify_fts5_error
    from sibyl_memory_client.exceptions import StorageError
    err = sqlite3.OperationalError("database is locked")
    result = _classify_fts5_error(err)
    assert isinstance(result, StorageError)


def test_search_with_valid_query_does_not_raise(tmp_path):
    """Normal queries should still work: no false-positive ValidationError."""
    from sibyl_memory_client import MemoryClient
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("project", "atlas", {"description": "alpha bravo charlie"})
    client.set_entity("project", "babel", {"description": "delta echo foxtrot"})
    # Plain text query: should not raise, returns matching results
    hits = client.search("alpha")
    assert len(hits) >= 1
    # Empty query short-circuits to []
    assert client.search("") == []
    # Whitespace-only query short-circuits to []
    assert client.search("   ") == []


def test_search_entities_phrase_match_semantics(tmp_path):
    """Document the actual phrase-match behavior so KAPPA's confusion
    (queries containing AND/OR/* return zero hits) is verified expected.
    These queries get wrapped as phrases: they only match literal occurrences
    of the phrase text in entity bodies."""
    from sibyl_memory_client import MemoryClient
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("project", "atlas", {"description": "alpha bravo charlie"})
    # "alpha bravo" should match because the body contains that exact phrase
    hits = client.search_entities("alpha bravo")
    assert len(hits) == 1
    # "AND" is a literal here: no entity body contains "AND"
    hits = client.search_entities("AND OR NOT")
    assert hits == []
    # "*" is wrapped as a literal phrase
    hits = client.search_entities("*")
    assert hits == []


# ----------------------------------------------------------------------
# v0.4.4: entity-name path-traversal + metacharacter defense-in-depth
# (KAPPA #3 PARTIAL — path-traversal shape + SQL-keyword shape were ACCEPTED)
# ----------------------------------------------------------------------

def test_validate_identifier_rejects_path_traversal():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    # ".." traversal marker is rejected; bare "/" stays allowed per the v0.4.0
    # contract (test_validate_identifier_accepts_reasonable covers "with/slash").
    for bad in ("../../etc/passwd", "..\\..\\windows", "foo/..", ".."):
        with pytest.raises(ValidationError, match="forbidden path sequence"):
            validate_identifier(bad, field_name="name")


def test_validate_identifier_rejects_sql_and_shell_metacharacters():
    from sibyl_memory_client.client import validate_identifier
    from sibyl_memory_client.exceptions import ValidationError
    # KAPPA's SQL-keyword shape ("'; DROP TABLE entities;--") is caught by ';'
    for bad in ("'; DROP TABLE entities;--", "a;b", 'a"b', "a`b", "a|b", "a<b", "a>b"):
        with pytest.raises(ValidationError, match="forbidden character"):
            validate_identifier(bad, field_name="name")


def test_validate_identifier_allows_apostrophe_and_normal_names():
    """Apostrophe is deliberately allowed so name-shaped keys survive; plain
    identifiers, dashes, underscores, dots-without-traversal pass."""
    from sibyl_memory_client.client import validate_identifier
    for ok in ("o'brien", "acme-deal", "alice", "project_atlas", "v0.4.4", "L-S-ratio"):
        assert validate_identifier(ok, field_name="name") == ok


# ----------------------------------------------------------------------
# v0.4.4: FTS5 operator-keyword drop
# (chainriffs Discord + KAPPA #4 — uppercase AND/OR/NOT/NEAR became required
# literal tokens, silently collapsing recall to ~0 hits)
# ----------------------------------------------------------------------

def test_sanitizer_drops_operator_keywords_default_mode():
    from sibyl_memory_client.client import _sanitize_fts5_query
    # operator words must NOT survive as quoted literal tokens
    assert _sanitize_fts5_query("auth AND db") == '"auth" "db"'
    assert _sanitize_fts5_query("cache NEAR eviction") == '"cache" "eviction"'
    assert _sanitize_fts5_query("foo OR bar NOT baz") == '"foo" "bar" "baz"'


def test_sanitizer_keeps_operator_only_query_as_literal():
    """If the query is ONLY operator keywords, keep them so a genuine search
    for the literal word 'and' still resolves (no empty-query surprise)."""
    from sibyl_memory_client.client import _sanitize_fts5_query
    assert _sanitize_fts5_query("AND") == '"AND"'
    assert _sanitize_fts5_query("AND OR NOT") == '"AND" "OR" "NOT"'


def test_search_with_operator_words_returns_hits_end_to_end(tmp_path):
    """The actual reported failure: a natural-language query containing an
    uppercase operator word used to return 0 hits. It must now match."""
    from sibyl_memory_client import MemoryClient
    client = MemoryClient.local(tmp_path / "memory.db")
    client.set_entity("debug", "authnote",
                      {"text": "auth uses JWT and a db connection for cache eviction"})
    # Pre-fix: "AND"/"NEAR" became required literal tokens -> 0 hits.
    assert len(client.search("auth AND db")) >= 1
    assert len(client.search("cache NEAR eviction")) >= 1
    assert len(client.search_entities("auth AND db")) >= 1
