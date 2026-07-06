"""SibylMemoryProvider: framework-agnostic Sibyl Memory SDK class.

DESIGN NOTES
============

Pure-Python SDK class. NOT a Hermes plugin on its own. The Hermes plugin
contract is satisfied by a thin adapter at `_hermes_plugin/adapter.py`
that delegates to this class. The split is intentional:

  - This class can be used by any orchestration (LangChain, LlamaIndex,
    custom Python, the sibyl-memory-mcp server, direct callers).
  - The Hermes adapter handles Hermes-specific lifecycle (initialize,
    sync_turn, get_tool_schemas, etc.) and is installed via the
    `sibyl-memory-hermes install-plugin` console script.

Prior versions (v0.2.x) attempted conditional inheritance from Hermes'
ABC at import time, but the import path was wrong (`hermes_agent.memory`
vs the actual `agent.memory_provider`), so the soft-bind silently failed
on every install. v0.3.0 removes the conditional inheritance entirely -
the adapter handles all Hermes glue. See packages/sibyl-memory-hermes/
CHANGELOG.md for the full ratification of this architectural shift.

The provider routes operations onto the correct memory tier:

    ┌─────────────────────────────────────────────────────────────┐
    │   intent                  │ tier         │ storage call       │
    ├─────────────────────────────────────────────────────────────┤
    │   "save the conversation" │ COLD journal │ write_event(...)   │
    │   "remember this fact"    │ WARM entity  │ set_entity(...)    │
    │   "current state"         │ HOT state    │ set_state(...)     │
    │   "lookup runbook"        │ REFERENCE    │ set_reference(...) │
    │   "archive stale entity"  │ ARCHIVE      │ archive_entity     │
    │   "search by content"     │ FTS5         │ search_entities    │
    └─────────────────────────────────────────────────────────────┘

The split is intentional. Vector-DB-only providers collapse all of the above
onto similarity search, which loses structure. Sibyl Memory preserves it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
from sibyl_memory_client.exceptions import NotFoundError
from sibyl_memory_client.storage import db_size_bytes

from .credentials import (
    DEFAULT_CRED_PATH,
    DEFAULT_DB_PATH,
    Credentials,
    CredentialsNotFoundError,
    load_credentials,
)


def _coerce_body(body: Any) -> Any:
    """Coerce a primitive body into a structured container (Coerce-on-Adapter).

    sibyl-memory-client enforces dict/list entity + state bodies. An agent
    calling ``sibyl_remember(..., body="a fact")`` or passing a bare
    number/bool/None is a natural mistake; the adapter wraps primitives as
    ``{"value": body}`` rather than letting the client reject the write.
    dict/list bodies pass through untouched. On recall the payload comes back
    under the ``"value"`` key. This keeps the storage contract structured
    (downstream tools can assume a container) while keeping the agent-facing
    surface forgiving.
    """
    if isinstance(body, (dict, list)):
        return body
    return {"value": body}


class SibylMemoryProvider:
    """Hermes Agent memory provider backed by sibyl-memory-client.

    Args:
        db_path:        path to the local SQLite database. Defaults to
                        ~/.sibyl-memory/memory.db (the path `sibyl init`
                        creates).
        tenant_id:      explicit tenant override. If None, credentials.json
                        is loaded and tenant resolves via the canonical ladder
                        tenant_id -> account_id -> DEFAULT_TENANT; DEFAULT_TENANT
                        is used only when credentials are genuinely absent.
        credentials_path: override for credentials.json discovery.
        require_credentials: if True, raise CredentialsNotFoundError when
                        the file is missing. Default False: degrade to
                        DEFAULT_TENANT so callers can run pre-activation.
        autoload_credentials: if True (default), read credentials.json on
                        construction and apply tenant_id from it.
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        tenant_id: str | None = None,
        credentials_path: str | Path = DEFAULT_CRED_PATH,
        require_credentials: bool = False,
        autoload_credentials: bool = True,
    ) -> None:
        # Resolve tenant: explicit > credentials (tenant_id > account_id) > default
        resolved_tenant = tenant_id
        creds: Credentials | None = None

        if resolved_tenant is None and autoload_credentials:
            try:
                creds = load_credentials(credentials_path)
                # Contract T (super-patch 2026-07-05): ONE canonical tenant
                # ladder shared by every surface (client / mcp / hermes /
                # langgraph) -- tenant_id -> account_id -> DEFAULT_TENANT.
                # An activated user whose credentials.json carries an account
                # but a missing-or-empty tenant_id (legacy schema-v1 files, or
                # a present-but-empty tenant field the loader does not mirror)
                # must resolve to their OWN account, never the shared
                # DEFAULT_TENANT constant. `or` collapses both the absent and
                # the present-but-empty cases; DEFAULT_TENANT is reached only
                # when credentials are genuinely absent (the except arms below).
                resolved_tenant = (
                    creds.tenant_id or creds.account_id or DEFAULT_TENANT
                )
            except CredentialsNotFoundError:
                if require_credentials:
                    raise
                resolved_tenant = DEFAULT_TENANT
            except (OSError, ValueError):
                if require_credentials:
                    raise
                resolved_tenant = DEFAULT_TENANT

        if resolved_tenant is None:
            resolved_tenant = DEFAULT_TENANT

        self._credentials = creds
        # Plumb account_id, session_token, tier, and the HMAC-signed
        # credentials claim through to the client so the cap gate can:
        #   1. verify free-tier writes against the authoritative server when
        #      the local DB approaches 2 MB (v0.3.0 behavior), and
        #   2. include the credentials_signature + claim in cap-check
        #      requests so the server can detect local credentials.json
        #      tampering and log it as telemetry (v0.3.1+).
        client_tier = creds.tier if creds else "free"
        client_account_id = creds.account_id if creds else None
        client_session_token = creds.session_token if creds else None
        # Build the canonical signed-claim object that matches the server's
        # SIGNING_FIELDS shape. Order doesn't matter on the JSON wire -
        # the server canonicalizes by field name.
        client_claim = None
        client_signature = None
        if creds and creds.signature:
            client_signature = creds.signature
            client_claim = {
                "account_id": creds.account_id,
                "tenant_id": creds.tenant_id,
                "tier": creds.tier,
                # Contract PII (super-patch 2026-07-05): email/wallet stay on the
                # wire. Dropping them is POLICY-GATED on a backend re-sign over
                # server-stored PII (plan §3/§6) -- removing them here first would
                # break server-side signature verification. Do NOT strip until
                # the backend signing set is PII-free.
                "email": creds.email,
                "wallet": creds.wallet,
                "issued_at": creds.issued_at,
                "schema_version": creds.schema_version,
            }
        self._client = MemoryClient.local(
            db_path,
            tenant_id=resolved_tenant,
            tier=client_tier,
            account_id=client_account_id,
            session_token=client_session_token,
            credentials_claim=client_claim,
            credentials_signature=client_signature,
        )

        # v0.3.0: no conditional super().__init__(): class is no longer
        # an ABC subclass. Hermes binding lives in the bundled adapter.

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def client(self) -> MemoryClient:
        """The underlying MemoryClient. Use for advanced operations not
        covered by the provider surface."""
        return self._client

    @property
    def credentials(self) -> Credentials | None:
        return self._credentials

    @property
    def tenant_id(self) -> str:
        return self._client.get_tenant()

    @property
    def hermes_bound(self) -> bool:
        """Deprecated since v0.3.0. The Hermes plugin contract is now
        satisfied by the bundled adapter (`_hermes_plugin/adapter.py`),
        not by this class's inheritance. Always returns False.

        v0.3.1: emits ``DeprecationWarning`` on read so users see the
        signal before v0.4 removal.

        Removed in v0.4.0.
        """
        import warnings
        warnings.warn(
            "SibylMemoryProvider.hermes_bound is deprecated and always "
            "returns False since v0.3.0. The Hermes plugin contract is "
            "now satisfied by the bundled adapter at _hermes_plugin/"
            "adapter.py. Property will be removed in v0.4.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return False

    # ==================================================================
    # HERMES-STYLE PROVIDER SURFACE
    # ==================================================================
    # The Hermes v0.10.0 memory contract uses save_context / load_context
    # for the per-turn agent memory loop. We map these onto the journal
    # (COLD) tier: every turn is an event in the agent's session log.
    #
    # remember() / recall() / forget() are the higher-level fact-store
    # operations that map onto entities (WARM tier).
    # ==================================================================

    def save_context(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        *,
        ts: str | None = None,
    ) -> str:
        """Persist a single turn (inputs + outputs) to the journal.

        Returns the journal event id."""
        return self._client.write_event(
            evaluated=inputs,
            acted=outputs,
            ts=ts,
        )

    def load_context(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent N turns from the journal."""
        return self._client.read_events(limit=limit)

    def clear_context(self) -> None:
        """No-op for now: journal events are append-only by design.

        If a caller genuinely wants to wipe the journal, they should drop
        the database file. This method exists for Hermes contract
        compatibility."""
        return None

    # ------------------------------------------------------------------
    # Fact store (WARM tier)
    # ------------------------------------------------------------------
    def remember(
        self,
        category: str,
        name: str,
        body: dict[str, Any] | list[Any],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Upsert an entity. Single source of truth per (tenant, category, name).

        Primitive bodies are coerced to ``{"value": body}`` (Coerce-on-Adapter)
        so the client's dict/list contract never rejects an agent's write.
        """
        return self._client.set_entity(category, name, _coerce_body(body), status=status)

    def recall(self, category: str, name: str) -> dict[str, Any] | None:
        """Look up a single entity by (category, name).

        Returns: a row dict shaped ``{id, tenant_id, category, name, status,
        body, created_at, updated_at}`` where ``body`` is the user-supplied
        JSON payload, or ``None`` if no matching entity exists.

        Note: the return shape is the full row wrapper, not just the body
        dict. To get the user payload only, use ``recall(...).["body"]``.
        State and reference tier reads (``get_state``, ``get_reference``)
        return slimmer ``{body, updated_at}`` shapes: that asymmetry is
        intentional (entities carry more provenance) and documented here
        per audit H2.

        Raises:
            StorageError: backend (SQLite) failure
            TenantError: misconfigured tenant_id
            SchemaError: DB schema mismatch

        T2-2 fix: previously caught bare ``Exception``, which swallowed
        StorageError / TenantError / SchemaError as "not found". That
        masked underlying-storage failures end-to-end. Now narrows to
        NotFoundError only: every other exception propagates so the
        caller can surface or retry.
        """
        try:
            return self._client.get_entity(category, name)
        except NotFoundError:
            return None

    def list(  # noqa: A003. Hermes-compatible name
        self,
        category: str | None = None,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._client.list_entities(category=category, status=status, limit=limit)

    def forget(self, category: str, name: str) -> bool:
        """Delete an entity. Returns True if a row was deleted, False if
        the entity didn't exist (no-op).

        Raises:
            StorageError: backend failure
            TenantError: misconfigured tenant_id

        Does NOT raise on missing entity: returns False instead (audit H3).
        """
        return self._client.delete_entity(category, name)

    def archive(
        self,
        category: str,
        name: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Move an entity to the archive tier.

        Returns: dict shaped ``{archived_id, original_id}`` referencing the
        new archive row.

        Raises:
            NotFoundError: no such (category, name) entity exists
            CapExceededError: archive would push the DB past the free-tier cap
            StorageError: backend failure

        Unlike ``forget``, ``archive`` is strict: missing entities raise
        NotFoundError rather than no-oping (audit H3).
        """
        return self._client.archive_entity(category, name, reason=reason)

    # ------------------------------------------------------------------
    # State documents (HOT tier)
    # ------------------------------------------------------------------
    def set_state(self, key: str, body: dict[str, Any] | list[Any]) -> None:
        """Set a state-tier document. ``body`` should be a dict or list
        (JSON-serializable container). A primitive is coerced to
        ``{"value": body}`` (Coerce-on-Adapter), e.g. ``set_state("seq", 42)``
        stores ``{"value": 42}``.

        Raises:
            ValidationError: body not JSON-serializable
            CapExceededError: write would push past the free-tier cap
            StorageError: backend failure
        """
        self._client.set_state(key, _coerce_body(body))

    def get_state(self, key: str) -> dict[str, Any] | None:
        """Read a state-tier document.

        Returns: dict shaped ``{body, updated_at}`` (the user payload is
        under ``body``), or ``None`` if no such key exists.

        Raises:
            StorageError: backend failure
        """
        return self._client.get_state(key)

    # ------------------------------------------------------------------
    # Reference docs (REFERENCE tier)
    # ------------------------------------------------------------------
    def set_reference(
        self,
        key: str,
        body: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set a reference-tier document.

        Note: reference bodies are plain ``str`` (markdown, runbooks,
        notes), not dict: intentionally different from entity / state
        which take dict|list bodies. Use the ``metadata`` kwarg for any
        structured side-data.

        Raises:
            ValidationError: metadata not JSON-serializable
            CapExceededError: write would push past the free-tier cap
            StorageError: backend failure
        """
        self._client.set_reference(key, body, metadata=metadata)

    def get_reference(self, key: str) -> dict[str, Any] | None:
        """Read a reference-tier document.

        Returns: dict shaped ``{body, metadata, updated_at}`` (body is
        the raw string), or ``None`` if no such key exists.

        Raises:
            StorageError: backend failure
        """
        return self._client.get_reference(key)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, query: str, *, limit: int = 20,
               prefix: bool = False,
               tiers: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Cross-tier FTS5 full-text search across all four searchable tiers.

        v0.3.1: search now spans entities + state + reference + journal
        (was: entities only: the marketing claim of "search across all
        tiers" was not yet true in v0.3.0).

        Returns: list of tier-tagged hits, each shaped::

            {
              "tier":  "entity" | "state" | "reference" | "journal",
              "key":   <entity name | state key | doc_key | journal id>,
              "category": <entity category or None>,
              "body":  <JSON-decoded payload (str for reference tier)>,
              "snippet": <FTS5 snippet with [highlight] markers>,
              "rank":  <FTS5 rank, lower is better>,
              "ts":    <ISO timestamp>
            }

        Hits sorted globally by FTS5 rank. ``limit`` applies to the
        combined union (not per tier). Pass ``tiers=("entity",)`` to
        restrict scope. ``prefix=True`` enables prefix matching on the
        last token.

        Query is sanitized as a single FTS5 phrase: column-filter
        syntax (``name:foo``) is treated as literal text. Empty / invalid
        queries return ``[]``.

        For warm-entity-only search returning full entity rows, use
        ``client.search_entities()`` directly.

        Raises:
            StorageError: backend failure
        """
        return self._client.search(query, limit=limit, prefix=prefix, tiers=tiers)

    def search_multi_record(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Two-stage retrieve-then-verify search for workflow / linked-record
        queries (whose answer spans several related records, e.g. feedback + bug +
        journal). Surfaces all the linked records instead of only the single
        strongest keyword match (tester Run15 fix).

        Same hit shape as ``search()``. For exact single-entity lookups use
        ``recall()``. Backed by ``sibyl_memory_client.multi_record``.
        """
        from sibyl_memory_client.multi_record import multi_record_search
        return multi_record_search(self._client, query, limit=limit)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Return a small diagnostic dict: used by `sibyl status`."""
        db_path = self._client.storage.db_path
        return {
            "ok": True,
            "schema_version": self._client.schema_version(),
            "db_path": str(db_path),
            # Audit #13: report the WAL-inclusive logical size used by the cap
            # gate (db_size_bytes), not the bare main-file st_size, so the number
            # shown here matches what the free-tier cap actually measures.
            "db_size_bytes": db_size_bytes(db_path) if db_path.exists() else 0,
            "tenant_id": self.tenant_id,
            "hermes_bound": False,  # v0.3.0: adapter owns Hermes binding
            "tier": self._credentials.tier if self._credentials else "free",
            "email": self._credentials.email if self._credentials else None,
        }

    # ------------------------------------------------------------------
    # repr
    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"SibylMemoryProvider(db={self._client.storage.db_path}, "
            f"tenant={self.tenant_id!r})"
        )
