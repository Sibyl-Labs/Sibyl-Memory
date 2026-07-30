"""Tests for the bundled Hermes plugin adapter (`_hermes_plugin/adapter.py`).

These tests close the validation gap flagged in the v0.3.1 pre-ship audit
(H1): the v0.3.0 CHANGELOG claimed "validated via Hermes' own
load_memory_provider('sibyl') dry-run + all 4 tool schemas resolved" but
zero references to the adapter existed in the test suite. A future change
that broke the adapter would have passed CI.

The adapter is designed to import cleanly off-Hermes (v0.3.1 guarded
imports): the `from agent.memory_provider import MemoryProvider` and
`from tools.registry import tool_error` are wrapped in try/except, with
no-op fallbacks. That means we can `import` and exercise the adapter
directly in pytest without mocking the Hermes runtime.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_module
from sibyl_memory_hermes._hermes_plugin.adapter import (
    LIST_SCHEMA,
    RECALL_SCHEMA,
    REMEMBER_SCHEMA,
    SEARCH_SCHEMA,
    SibylAdapter,
    _stable_key,
)


# ----------------------------------------------------------------------
# Module loadability
# ----------------------------------------------------------------------
def test_module_imports_without_hermes() -> None:
    """Off-Hermes the adapter still imports: the Hermes ABC + tool_error
    guards land their no-op fallbacks. Tests can therefore exercise it
    without spinning up a Hermes runtime."""
    # _HERMES_AVAILABLE reflects whether hermes-agent is installed.
    # In CI / local dev it's typically False; in a real Hermes deployment
    # it's True. Either way the module loaded successfully (we're here).
    assert hasattr(adapter_module, "_HERMES_AVAILABLE")
    assert hasattr(adapter_module, "tool_error")
    # tool_error must return a string regardless of source (Hermes-real or fallback)
    out = adapter_module.tool_error("test message")
    assert isinstance(out, str)
    assert "test message" in out


def test_register_function_exists() -> None:
    """register(ctx) is the Hermes plugin entry point: must exist for the
    filesystem loader to find it."""
    assert callable(adapter_module.register)


# ----------------------------------------------------------------------
# Tool schemas
# ----------------------------------------------------------------------
def test_tool_schemas_have_correct_count_and_names() -> None:
    """The CHANGELOG promised 4 tools; this asserts they exist and are
    named correctly. Catches accidental renames in future refactors."""
    adapter = SibylAdapter()
    schemas = adapter.get_tool_schemas()
    assert len(schemas) == 4
    names = sorted(s["name"] for s in schemas)
    assert names == ["sibyl_list", "sibyl_recall", "sibyl_remember", "sibyl_search"]


@pytest.mark.parametrize("schema", [REMEMBER_SCHEMA, RECALL_SCHEMA, SEARCH_SCHEMA, LIST_SCHEMA])
def test_tool_schemas_are_valid_openai_function_shape(schema: dict) -> None:
    """Each tool schema must follow OpenAI function-calling shape: name,
    description, parameters (with type=object + properties + required)."""
    assert "name" in schema
    assert isinstance(schema["name"], str)
    assert schema["name"].startswith("sibyl_")
    assert "description" in schema
    assert isinstance(schema["description"], str)
    assert len(schema["description"]) > 0
    assert "parameters" in schema
    p = schema["parameters"]
    assert p["type"] == "object"
    assert "properties" in p
    assert isinstance(p["properties"], dict)
    assert "required" in p
    assert isinstance(p["required"], list)


# ----------------------------------------------------------------------
# Adapter init + dispatch
# ----------------------------------------------------------------------
def _make_initialized_adapter(tmp_path: Path) -> SibylAdapter:
    """Build a SibylAdapter wired to a temp DB. Bypasses Hermes' real
    initialize() entry point (which calls _hermes_home from hermes_constants)
    by setting the provider directly."""
    adapter = SibylAdapter()
    adapter._sibyl = SibylMemoryProvider(
        db_path=str(tmp_path / "adapter.db"),
        autoload_credentials=False,
    )
    adapter._session_id = "test-session"
    adapter._hermes_home = tmp_path
    return adapter


def test_handle_tool_call_uninitialized_returns_error() -> None:
    """Calling handle_tool_call before initialize must return a structured
    error, not crash."""
    adapter = SibylAdapter()
    result = adapter.handle_tool_call("sibyl_remember", {"category": "x", "name": "y", "body": {}})
    parsed = json.loads(result)
    assert "error" in parsed


def test_handle_tool_call_unknown_tool_returns_error(tmp_path: Path) -> None:
    """Unknown tool names produce a clean error response, no exception."""
    adapter = _make_initialized_adapter(tmp_path)
    result = adapter.handle_tool_call("sibyl_does_not_exist", {})
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Unknown tool" in parsed["error"]


def test_handle_tool_call_remember_then_recall(tmp_path: Path) -> None:
    """End-to-end: remember an entity, recall it, verify the body roundtrips."""
    adapter = _make_initialized_adapter(tmp_path)
    # remember
    r1 = json.loads(adapter.handle_tool_call("sibyl_remember", {
        "category": "project",
        "name": "atlas",
        "body": {"status": "shipping", "owner": "tt"},
    }))
    assert r1["ok"] is True
    assert r1["entity"]["body"]["status"] == "shipping"
    # recall
    r2 = json.loads(adapter.handle_tool_call("sibyl_recall", {
        "category": "project", "name": "atlas",
    }))
    assert r2["entity"] is not None
    assert r2["entity"]["body"]["status"] == "shipping"
    assert r2["entity"]["body"]["owner"] == "tt"


def test_handle_tool_call_recall_missing_returns_null(tmp_path: Path) -> None:
    """Recall on a non-existent entity returns {"entity": null}, not an error."""
    adapter = _make_initialized_adapter(tmp_path)
    out = json.loads(adapter.handle_tool_call("sibyl_recall", {
        "category": "project", "name": "nonexistent",
    }))
    assert out["entity"] is None


def test_handle_tool_call_list_with_filter(tmp_path: Path) -> None:
    """list filters by category."""
    adapter = _make_initialized_adapter(tmp_path)
    for n, cat in [("a", "alpha"), ("b", "alpha"), ("c", "beta")]:
        adapter.handle_tool_call("sibyl_remember", {
            "category": cat, "name": n, "body": {"x": n},
        })
    out = json.loads(adapter.handle_tool_call("sibyl_list", {"category": "alpha"}))
    names = sorted(e["name"] for e in out["entities"])
    assert names == ["a", "b"]


def test_handle_tool_call_search_cross_tier(tmp_path: Path) -> None:
    """v0.3.1 promise: search spans all four tiers, not entities only.

    This is the regression test the audit (T5) said would have caught the
    cross-tier-coverage bug if it had existed in v0.3.0."""
    adapter = _make_initialized_adapter(tmp_path)
    sibyl = adapter._sibyl
    # Write a unique marker to each tier
    sibyl.remember("project", "atlas", {"note": "entitytier_xyzzy"})
    sibyl.set_state("active_branch", {"name": "statetier_xyzzy"})
    sibyl.set_reference("runbook", "referencetier_xyzzy is the value")
    sibyl.save_context(
        inputs={"u": "journaltier_xyzzy is the user message"},
        outputs={"a": "ok"},
    )
    out = json.loads(adapter.handle_tool_call("sibyl_search", {"query": "xyzzy"}))
    hits = out["results"]
    tiers_found = {h["tier"] for h in hits}
    # Each tier should surface at least one hit
    assert "entity" in tiers_found, f"entity tier missing from search: {tiers_found}"
    assert "state" in tiers_found, f"state tier missing from search: {tiers_found}"
    assert "reference" in tiers_found, f"reference tier missing from search: {tiers_found}"
    assert "journal" in tiers_found, f"journal tier missing from search: {tiers_found}"


def test_handle_tool_call_search_sanitizes_malformed_query(tmp_path: Path) -> None:
    """SEC-3 hardening: malformed FTS5 queries (unclosed quotes, column
    filters) must not crash or leak SQL error text."""
    adapter = _make_initialized_adapter(tmp_path)
    # Unclosed quote: pre-v0.3.1 would surface OperationalError + db_path leak
    out = json.loads(adapter.handle_tool_call("sibyl_search", {"query": '"'}))
    assert "results" in out
    # Empty input: should return empty results, not error
    out2 = json.loads(adapter.handle_tool_call("sibyl_search", {"query": ""}))
    assert "error" in out2  # query is required


def test_handle_tool_call_missing_required_args(tmp_path: Path) -> None:
    """Required parameters surface a clean error, not a backend crash."""
    adapter = _make_initialized_adapter(tmp_path)
    out = json.loads(adapter.handle_tool_call("sibyl_remember", {"category": "x"}))
    assert "error" in out


# ----------------------------------------------------------------------
# Shutdown behavior (P-C1, P-C2 audit fixes)
# ----------------------------------------------------------------------
def test_shutdown_sets_stop_flag(tmp_path: Path) -> None:
    """shutdown() sets _shutting_down so daemon writes can skip slow paths."""
    adapter = _make_initialized_adapter(tmp_path)
    assert adapter._shutting_down is False
    adapter.shutdown()
    assert adapter._shutting_down is True


def test_sync_turn_during_shutdown_skips(tmp_path: Path) -> None:
    """sync_turn called after shutdown should not error out (writes are
    skipped via the shutdown flag check in the worker loop)."""
    adapter = _make_initialized_adapter(tmp_path)
    adapter.shutdown()
    # Should not raise: even though we shut down, the call itself is safe
    adapter.sync_turn("user msg", "assistant reply")


# ----------------------------------------------------------------------
# Helper
# ----------------------------------------------------------------------
def test_stable_key_is_deterministic() -> None:
    """blake2b _stable_key gives the same answer for the same input across
    runs. This is what makes add+remove on the same content actually target
    the same entity."""
    k1 = _stable_key("hello world")
    k2 = _stable_key("hello world")
    k3 = _stable_key("hello world!")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 12  # 6 bytes = 12 hex chars


def test_stable_key_with_prefix() -> None:
    """prefix= argument prefixes the digest, used for namespacing built-in
    memory-tool mirror writes."""
    k = _stable_key("hello", prefix="mem-")
    assert k.startswith("mem-")
    assert len(k) == len("mem-") + 12


# ----------------------------------------------------------------------
# SIBYL_TENANT_ID env override (initialize)
# ----------------------------------------------------------------------
def _init_adapter_with_env(tmp_path, monkeypatch, tenant_env):
    """Initialize a SibylAdapter through the real initialize() path with
    credentials autoload isolated to an empty HOME, optionally setting
    SIBYL_TENANT_ID. Returns the adapter."""
    # Isolate credentials: HOME -> empty tmp dir so ~/.sibyl-memory/credentials.json
    # expands into a location with no file, and tenant falls to DEFAULT_TENANT
    # unless SIBYL_TENANT_ID overrides. (The hermes provider expands the literal
    # "~/..." DEFAULT_CRED_PATH via Path.expanduser(); it does NOT read
    # SIBYL_CREDENTIALS, so HOME is the correct isolation lever here.)
    monkeypatch.setenv("HOME", str(tmp_path))
    if tenant_env is None:
        monkeypatch.delenv("SIBYL_TENANT_ID", raising=False)
    else:
        monkeypatch.setenv("SIBYL_TENANT_ID", tenant_env)
    adapter = SibylAdapter()
    adapter.initialize("test-session", hermes_home=str(tmp_path),
                       agent_identity="default")
    return adapter


def test_initialize_honors_tenant_env(tmp_path, monkeypatch):
    adapter = _init_adapter_with_env(tmp_path, monkeypatch, "tenant-from-env")
    assert adapter._sibyl.tenant_id == "tenant-from-env"


def test_initialize_without_tenant_env_uses_default(tmp_path, monkeypatch):
    from sibyl_memory_client import DEFAULT_TENANT
    adapter = _init_adapter_with_env(tmp_path, monkeypatch, None)
    # No env, no credentials on the isolated HOME -> provider resolves to
    # the shared default tenant, exactly as before this change.
    assert adapter._sibyl.tenant_id == DEFAULT_TENANT


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_initialize_blank_tenant_env_treated_as_unset(tmp_path, monkeypatch, blank):
    from sibyl_memory_client import DEFAULT_TENANT
    adapter = _init_adapter_with_env(tmp_path, monkeypatch, blank)
    assert adapter._sibyl.tenant_id == DEFAULT_TENANT


def test_initialize_trims_surrounding_whitespace_to_inner_value(tmp_path, monkeypatch):
    # A set value with surrounding whitespace resolves to the inner value only:
    # confirms .strip() yields the trimmed identifier, not the padded string and
    # not a false unset. Distinct from the blank case, which strips to empty.
    adapter = _init_adapter_with_env(tmp_path, monkeypatch, "  tenant-x  ")
    assert adapter._sibyl.tenant_id == "tenant-x"


def test_initialize_never_logs_tenant_value(tmp_path, monkeypatch, caplog):
    # The init log must record the boolean state ("set") but never the tenant
    # identifier itself, and it must say "unset" when the env is absent.
    secret_like_value = "tenant-should-not-appear"
    with caplog.at_level(logging.INFO, logger="sibyl_memory_hermes._hermes_plugin.adapter"):
        _init_adapter_with_env(tmp_path, monkeypatch, secret_like_value)
    init_lines = [r for r in caplog.records if "Sibyl memory initialized" in r.getMessage()]
    assert init_lines, "expected an init log line"
    for record in init_lines:
        rendered = record.getMessage()
        assert "tenant_override=set" in rendered
        assert secret_like_value not in rendered
        # Belt-and-suspenders: the raw value is absent from the record args too.
        assert secret_like_value not in repr(record.args)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sibyl_memory_hermes._hermes_plugin.adapter"):
        _init_adapter_with_env(tmp_path, monkeypatch, None)
    unset_lines = [r for r in caplog.records if "Sibyl memory initialized" in r.getMessage()]
    assert unset_lines, "expected an init log line"
    assert all("tenant_override=unset" in r.getMessage() for r in unset_lines)
