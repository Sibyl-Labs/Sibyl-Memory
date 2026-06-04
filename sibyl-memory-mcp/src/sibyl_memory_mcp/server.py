"""MCP server exposing Sibyl Memory Plugin tools.

8 tools:
  - memory_remember        store an entity
  - memory_recall          read an entity by category+name
  - memory_search          FTS5 search across ALL tiers (entities + state + reference + journal)
  - memory_list            list entities, optionally filtered by category
  - memory_forget          archive an entity (preserved, removed from active set)
  - memory_set_state       write a HOT-tier state document
  - memory_get_state       read a HOT-tier state document
  - memory_record_event    append a COLD-tier journal event

All operations run against the local SQLite at ~/.sibyl-memory/memory.db.
The cap gate (free-tier 2 MB hard cap, paid-tier uncapped) is enforced
automatically by the underlying sibyl-memory-client SDK: the MCP server
just surfaces the typed errors back to the caller.

v0.1.1 hardening (audit-remediation):
  - MemoryClient cached at module scope, NOT reopened per call (audit P-H1).
    Invalidation: file-mtime watch of credentials.json so `sibyl upgrade`
    is picked up without a server restart.
  - memory_record_event signature fixed against actual write_event
    contract (audit C1). Previous signature called a non-existent positional
    form and every invocation raised TypeError.
  - memory_get_state unpacks the nested {body, updated_at} dict so the
    response shape has body=user_payload, not body={user_payload, ...}
    (audit H2 body double-meaning fix).
  - memory_list category parameter is now Optional (audit N3).
  - credentials.json reads honor the lstat / symlink check (audit SEC-4/11).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_client.exceptions import (
    CapExceededError,
    NotFoundError,
    TierGateError,
    TierVerificationError,
    ValidationError,
)

# Default install location matches the rest of the plugin ecosystem.
DEFAULT_DB_PATH = Path(os.environ.get(
    "SIBYL_MEMORY_DB",
    Path.home() / ".sibyl-memory" / "memory.db",
))
DEFAULT_CRED_PATH = Path(os.environ.get(
    "SIBYL_CREDENTIALS",
    Path.home() / ".sibyl-memory" / "credentials.json",
))


# ----------------------------------------------------------------------
# Credential loading (audit SEC-4, SEC-11)
# ----------------------------------------------------------------------

def _load_credentials() -> dict[str, Any]:
    """Read credentials.json if present. Missing file = pre-activation, free tier.

    v0.1.1 hardening:
      - Refuses to follow symlinks (SEC-11). If the file is a symlink,
        treat as absent: same behavior as the Hermes provider.
      - Treats any I/O / parse error as absent (existing behavior).
    """
    if not DEFAULT_CRED_PATH.exists():
        return {}
    if DEFAULT_CRED_PATH.is_symlink():
        return {}
    try:
        return json.loads(DEFAULT_CRED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ----------------------------------------------------------------------
# Cached MemoryClient (audit P-H1)
# ----------------------------------------------------------------------

_client_lock = threading.Lock()
_client_cache: dict[str, Any] = {
    "client": None,           # MemoryClient instance, lazily built
    "creds_mtime": None,      # mtime of credentials.json at last open
    "creds_path_exists": False,
}


def _credentials_mtime() -> float | None:
    """Return credentials.json mtime if present, else None.

    Used to detect `sibyl upgrade` having written new credentials so the
    cached MemoryClient can be rebuilt with the new tier."""
    try:
        if DEFAULT_CRED_PATH.exists() and not DEFAULT_CRED_PATH.is_symlink():
            return DEFAULT_CRED_PATH.stat().st_mtime
    except OSError:
        pass
    return None


def _open_client() -> MemoryClient:
    """Return a MemoryClient bound to the local DB + credentials.

    v0.1.1 (audit P-H1): cached at module scope. Previously rebuilt every
    tool call (reading schema.sql from disk + bootstrapping FTS5 vtables -
    10-50ms per call). Now invalidated only when credentials.json mtime
    changes, which is the only thing that should change tier behavior.
    """
    with _client_lock:
        cur_mtime = _credentials_mtime()
        cur_exists = DEFAULT_CRED_PATH.exists()
        client = _client_cache["client"]
        cached_mtime = _client_cache["creds_mtime"]
        cached_exists = _client_cache["creds_path_exists"]
        # Rebuild if no cached client, or credentials.json mtime changed,
        # or credentials.json appeared / disappeared (post-init / post-logout).
        if client is None or cur_mtime != cached_mtime or cur_exists != cached_exists:
            client = _build_client()
            _client_cache["client"] = client
            _client_cache["creds_mtime"] = cur_mtime
            _client_cache["creds_path_exists"] = cur_exists
        return client


def _build_client() -> MemoryClient:
    """Construct a fresh MemoryClient. Called only on cache miss.

    v0.1.3 (sylvain1550 / KAPPA first-use bug): when credentials.json is
    absent, ``creds`` is ``{}`` and ``creds.get("tenant_id")`` is ``None``.
    Passing ``tenant_id=None`` *explicitly* overrode the SDK's DEFAULT_TENANT
    default, so every write hit the ``entities.tenant_id NOT NULL`` constraint
    and failed with an opaque ``SQLite error: IntegrityError`` -- while reads
    and tool discovery still worked, making a broken install look healthy.
    Fall back to DEFAULT_TENANT so pre-activation free local mode writes
    succeed, matching sibyl-memory-hermes' provider behavior.
    """
    creds = _load_credentials()
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return MemoryClient.local(
        str(DEFAULT_DB_PATH),
        tenant_id=creds.get("tenant_id") or DEFAULT_TENANT,
        account_id=creds.get("account_id"),
        session_token=creds.get("session_token"),
        tier=creds.get("tier", "free"),
        credentials_claim={
            "account_id": creds.get("account_id"),
            "tenant_id": creds.get("tenant_id"),
            "tier": creds.get("tier"),
            "email": creds.get("email"),
            "wallet": creds.get("wallet"),
            "issued_at": creds.get("issued_at"),
            "schema_version": creds.get("schema_version", 1),
        } if creds.get("signature") else None,
        credentials_signature=creds.get("signature"),
    )


# ----------------------------------------------------------------------
# Error mapping
# ----------------------------------------------------------------------

def _err(e: Exception) -> dict[str, Any]:
    """Map SDK exception → structured error payload the agent can reason about."""
    cls = type(e).__name__
    payload = {"error": cls, "message": str(e)}
    if isinstance(e, CapExceededError):
        payload["code"] = "CAP_EXCEEDED"
        payload["recovery"] = "Run `sibyl upgrade` to lift the 2 MB free-tier cap."
        payload["upgrade_url"] = getattr(e, "upgrade_url", "https://sibyllabs.org/plugin/upgrade")
    elif isinstance(e, TierGateError):
        payload["code"] = "TIER_GATED"
        payload["recovery"] = "This feature requires a paid tier. Run `sibyl upgrade`."
    elif isinstance(e, TierVerificationError):
        payload["code"] = "TIER_VERIFICATION_FAILED"
        payload["recovery"] = "The server couldn't verify your tier. Check connectivity and try again."
    elif isinstance(e, NotFoundError):
        payload["code"] = "NOT_FOUND"
    elif isinstance(e, ValidationError):
        payload["code"] = "VALIDATION_ERROR"
    return payload


def _coerce_body(body: Any) -> Any:
    """Coerce a primitive body into a container (Coerce-on-Adapter).

    sibyl-memory-client enforces dict/list entity + state bodies. An MCP
    client (Claude Code / Codex / Cursor) calling memory_remember with a
    bare string/number/bool/None is a natural mistake; the server wraps it as
    ``{"value": body}`` rather than surfacing a VALIDATION_ERROR. dict/list
    bodies pass through untouched. Mirrors the hermes adapter's coercion so
    every adapter surface presents the same forgiving contract.
    """
    if isinstance(body, (dict, list)):
        return body
    return {"value": body}


# ----------------------------------------------------------------------
# Argument-validation leak guard (SEC-14)
# ----------------------------------------------------------------------

# The MCP SDK's Tool.run wraps a pydantic ValidationError as
#   ToolError("Error executing tool <name>: <... input_value='<raw value>' ...>")
# and that message reaches the wire as an isError result. If a caller fat-fingers
# a secret into a typed argument (e.g. limit="sk-live-..."), the secret is echoed
# back. This signature is the pydantic-on-arguments fingerprint (the arg model is
# named "<func>Arguments").
_VALIDATION_LEAK = re.compile(r"validation error.*Arguments", re.IGNORECASE | re.DOTALL)
_GENERIC_ARG_ERROR = (
    "Error executing tool: one or more arguments failed validation "
    "(wrong type or format). The offending value is not echoed back for safety."
)


def _scrub_call_tool_result(server_result: Any) -> Any:
    """Redact pydantic argument-validation detail from an error tool result.

    No-op for normal (non-error) results and for errors that are not the
    argument-validation kind.
    """
    try:
        ctr = getattr(server_result, "root", None)
        if ctr is None or not getattr(ctr, "isError", False):
            return server_result
        for block in (getattr(ctr, "content", None) or []):
            text = getattr(block, "text", None)
            if text and _VALIDATION_LEAK.search(text):
                block.text = _GENERIC_ARG_ERROR
    except Exception:
        # Never let the guard itself break tool dispatch.
        pass
    return server_result


def _install_validation_leak_guard(mcp: FastMCP) -> None:
    """Wrap the lowlevel CallToolRequest dispatch to scrub argument-validation
    leakage (SEC-14).

    We wrap ``mcp._mcp_server.request_handlers[CallToolRequest]`` — the handler
    actually invoked on every stdio/SSE call — NOT ``mcp.call_tool``. FastMCP
    binds ``self.call_tool`` into the lowlevel server at construction time, so
    reassigning the instance attribute afterwards is dead code on the real wire
    path; only wrapping the registered handler is effective.
    """
    try:
        from mcp.types import CallToolRequest
        low = mcp._mcp_server
        orig = low.request_handlers.get(CallToolRequest)
        if orig is None:
            return

        async def _guarded(req: Any) -> Any:
            return _scrub_call_tool_result(await orig(req))

        low.request_handlers[CallToolRequest] = _guarded
    except Exception:
        # If SDK internals shift, fail open (server still runs) rather than
        # crash on startup. Defense-in-depth on top of typed tool signatures.
        pass


# ----------------------------------------------------------------------
# Server build
# ----------------------------------------------------------------------

def build_server() -> FastMCP:
    """Build and return the MCP server. Tool names are prefixed with `memory_`."""
    mcp = FastMCP("sibyl-memory")

    @mcp.tool()
    def memory_remember(category: str, name: str, body: Any) -> dict[str, Any]:
        """Store an entity in long-term memory.

        Use for facts, project state, person profiles, anything the agent
        should remember across sessions. Idempotent on (category, name) -
        a second call with the same key updates the entry.

        Args:
            category: Logical grouping (e.g. "people", "projects", "facts").
            name: Unique-within-category identifier (e.g. "alice", "acme-deal").
            body: The entity body. A dict or list is stored as-is; a primitive
                (str/int/float/bool/None) is wrapped as {"value": <primitive>}
                so the client's structured-body contract is always satisfied.
        """
        try:
            client = _open_client()
            client.set_entity(category, name, _coerce_body(body))
            return {"ok": True, "category": category, "name": name}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_recall(category: str, name: str) -> dict[str, Any]:
        """Read an entity by exact (category, name) lookup.

        Returns: {ok: True, entity: {id, tenant_id, category, name, status,
        body, created_at, updated_at}} where `body` is the user-supplied
        payload. Or a NOT_FOUND error.
        """
        try:
            client = _open_client()
            return {"ok": True, "entity": client.get_entity(category, name)}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_search(query: str, limit: int = 10, tiers: str | None = None) -> dict[str, Any]:
        """Full-text search across ALL Sibyl tiers (entities + state +
        reference + journal).

        v0.1.1: spans all four searchable tiers. Each hit carries a `tier`
        tag so the agent knows where the match came from. Previously was
        entities-only: the v0.3.0 plugin family marketing claim of
        "search across all tiers" is now actually true.

        Query is sanitized as a single FTS5 phrase: column-filter syntax
        (`name:foo`, `rowid:*`) is treated as literal text and cannot
        break out into the FTS5 parser. Empty/invalid queries return [].

        Args:
            query: Search terms. User input is sanitized before MATCH.
            limit: Maximum results to return (default 10, max 50).
            tiers: Optional comma-separated tier filter. Valid values:
                "entity", "state", "reference", "journal". Example:
                "entity,state" restricts to those two tiers and bypasses
                the multi-record linker. Omit or pass null to search all
                tiers with the multi-record linker active.
        """
        try:
            client = _open_client()
            safe_limit = min(max(limit, 1), 50)
            if tiers:
                # Tier-filtered path: bypass multi_record_search (which has no
                # tiers param) and call client.search() directly. Lets callers
                # avoid journal-entry domination on generic-keyword queries.
                tier_tuple = tuple(t.strip() for t in tiers.split(",") if t.strip())
                results = client.search(query, limit=safe_limit, tiers=tier_tuple or None)
            else:
                # Run15 multi-record fix (Terminal B): route workflow search through
                # retrieve-then-verify so queries spanning several linked records surface
                # them all. Drop-in (same hit shape). See sibyl_memory_client/multi_record.py.
                from sibyl_memory_client.multi_record import multi_record_search
                results = multi_record_search(client, query, limit=safe_limit)
            return {"ok": True, "query": query, "count": len(results), "results": results}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_list(
        category: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List entities, optionally filtered by category. Most-recently-updated first.

        v0.1.1: `category` is now optional (audit N3: matches the SDK and
        Hermes adapter behavior). Pass it to filter; omit to list across
        all categories.

        Args:
            category: Optional category filter. Pass None or omit to list all.
            limit: Max entities to return (default 50, max 200).
        """
        try:
            client = _open_client()
            results = client.list_entities(category=category, limit=min(max(limit, 1), 200))
            return {"ok": True, "category": category, "count": len(results), "results": results}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_forget(category: str, name: str, reason: str | None = None) -> dict[str, Any]:
        """Archive an entity (not destroyed: moved to archived_entities).

        The body is preserved in the archive table for forensic recovery
        but no longer appears in recall/list/search. Pass a `reason` to
        record why; useful in audit reviews.
        """
        try:
            client = _open_client()
            client.archive_entity(category, name, reason=reason)
            return {"ok": True, "archived": {"category": category, "name": name}}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_set_state(key: str, body: Any) -> dict[str, Any]:
        """Write a HOT-tier state document.

        Use for ephemeral working state the agent updates frequently -
        current focus, in-flight task list, working draft. Faster than
        entity writes; one row per key, overwritten on each set.

        body: dict/list stored as-is; a primitive is wrapped as
        {"value": <primitive>} (Coerce-on-Adapter).
        """
        try:
            client = _open_client()
            client.set_state(key, _coerce_body(body))
            return {"ok": True, "key": key}
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_get_state(key: str) -> dict[str, Any]:
        """Read a HOT-tier state document by key.

        v0.1.1 (audit H2): response shape is now flat -
            {ok, key, body: <user payload>, updated_at: <iso ts>}
        Previously returned ``body`` = the full ``{body, updated_at}`` dict
        from the SDK, so "body" meant two different things at different
        nesting levels.
        """
        try:
            client = _open_client()
            doc = client.get_state(key)
            if doc is None:
                return {"ok": False, "code": "NOT_FOUND", "key": key}
            # Unpack the SDK's {body, updated_at} wrapper so the MCP response
            # uses `body` for the user payload only.
            return {
                "ok": True,
                "key": key,
                "body": doc.get("body"),
                "updated_at": doc.get("updated_at"),
            }
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def memory_record_event(
        kind: str,
        body: dict[str, Any],
        category: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Append a COLD-tier journal event.

        Use for things that happened: actions taken, decisions made,
        observations recorded. Append-only; never overwrites. Best paired
        with entities (the entity is the noun, the journal is the verb).

        v0.1.1 (audit C1): wired against the actual SDK signature
        ``write_event(*, evaluated, acted, forward, extra, ts)``. Previously
        called a positional form that doesn't exist and raised TypeError on
        every invocation. The high-level (kind, body, category, name)
        contract is preserved by translating into the SDK shape:
          - kind / body → `acted = {kind, body}`
          - category / name → `extra = {category, name}` (when supplied)

        Args:
            kind: Event class (e.g. "decision", "observation", "action").
            body: JSON-serializable event payload.
            category: Optional entity category this event is about.
            name: Optional entity name this event is about.
        """
        try:
            client = _open_client()
            acted = {"kind": kind, "body": body}
            extra = None
            if category is not None or name is not None:
                extra = {}
                if category is not None:
                    extra["category"] = category
                if name is not None:
                    extra["name"] = name
            event_id = client.write_event(acted=acted, extra=extra)
            return {"ok": True, "event_id": event_id, "kind": kind}
        except Exception as e:
            return _err(e)

    _install_validation_leak_guard(mcp)
    return mcp


def run_stdio() -> None:
    """Run the server on stdio transport (what Claude Code / Codex / Cursor expect)."""
    mcp = build_server()
    mcp.run()
