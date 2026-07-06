"""SibylStore — a LangGraph BaseStore backed by Sibyl Memory.

Long-term (cross-thread) memory for LangGraph agents, backed by Sibyl Memory's
local SQLite + FTS5 engine. No vector database, no embeddings: retrieval is
deterministic lexical (FTS5).

Scope (deliberate):
  * Implements the long-term ``BaseStore`` surface (get / put / delete / search /
    list_namespaces) via ``batch`` / ``abatch``.
  * It is NOT a LangGraph checkpointer (short-term graph-state serialization is a
    different job and a poor fit for an entity/event schema).
  * ``search`` is lexical (FTS5), not vector similarity. ``PutOp.index`` and
    ``PutOp.ttl`` are accepted and ignored (no embedding index, no TTL expiry).

Mapping:
  LangGraph namespace tuple  ->  Sibyl category   ("/".join(namespace))
  LangGraph key              ->  Sibyl entity name
  LangGraph value (dict)     ->  Sibyl entity body (JSON)

Namespace elements must be non-empty strings containing no "/" and no ".."
(the join must stay unambiguous and the client rejects path-traversal). A
ValueError is raised for namespaces that cannot be represented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)

try:  # client exception surface
    from sibyl_memory_client import (
        DEFAULT_TENANT,
        MemoryClient,
        NotFoundError,
        ValidationError,
    )
except Exception as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "sibyl-memory-langgraph requires sibyl-memory-client. "
        "Install it with: pip install sibyl-memory-client"
    ) from exc

__all__ = ["SibylStore"]

_log = logging.getLogger(__name__)

_NS_SEP = "/"
# Candidate pool for browse / subtree search / namespace listing. The client
# clamps every read to MAX_LIMIT (10_000) and exposes no offset/cursor, so this
# is the most the adapter can enumerate in one pass. Beyond it, results are
# truncated AND a warning is logged (LangGraph return types carry no has_more).
# True unbounded enumeration needs a client-side cursor / distinct-category API.
_POOL = 10_000

# credentials.json lives beside memory.db in ~/.sibyl-memory/ (written by
# `sibyl init`). Contract T / Hardening #5: when no explicit client/tenant is
# given, the default store binds to the activated account instead of running
# identity-blind on DEFAULT_TENANT.
_CRED_FILENAME = "credentials.json"


def _resolve_tenant_from_creds(db_path: str) -> str | None:
    """Resolve the tenant for the DEFAULT (no-explicit-tenant) store.

    Reads the ``credentials.json`` that ``sibyl init`` writes next to the DB
    file and applies the ONE canonical tenant ladder shared by every plugin
    surface (Contract T)::

        creds.tenant_id -> creds.account_id -> DEFAULT_TENANT

    Returns the resolved tenant id, or ``None`` when credentials are genuinely
    absent/unreadable so the caller falls back to the client's DEFAULT_TENANT
    (i.e. DEFAULT_TENANT is used ONLY when creds are absent). Symlink-guarded
    (mirrors sibyl-memory-hermes ``load_credentials`` SEC-11): a symlinked
    credentials file is treated as absent rather than followed, so a stale/
    hostile link can never redirect identity resolution. Never raises — any
    error degrades to the un-activated default.
    """
    try:
        cred_path = Path(db_path).expanduser().parent / _CRED_FILENAME
        # Detect symlinks BEFORE resolve(): resolve() follows them silently.
        if cred_path.is_symlink() or not cred_path.exists():
            return None
        with cred_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return None
        tenant = raw.get("tenant_id")
        account = raw.get("account_id")
        # `or` collapses both absent (None) and present-but-empty ("") at each
        # rung, so a corrupt/blank id falls through to the next rung, never
        # binding an empty tenant.
        return (tenant or "") or (account or "") or DEFAULT_TENANT
    except (OSError, ValueError):  # unreadable / unparseable -> un-activated default
        return None


def _clamp_page(limit: Any, offset: Any, *, default_limit: int) -> tuple[int, int]:
    """Normalize a caller-supplied (limit, offset) to non-negative ints.

    Guards three sharp edges at once (R32/R33):
      * ``limit is None`` -> the op's documented default (never ``offset + None``
        arithmetic, which raised TypeError);
      * a NEGATIVE limit -> 0 (never a negative-index slice that would broaden
        the page to almost every row);
      * a negative/None offset -> 0.

    The limit is also capped at ``_POOL`` so a single request can never ask the
    client for more than it will ever return.
    """
    raw_limit = default_limit if limit is None else limit
    lim = min(max(0, int(raw_limit)), _POOL)
    off = max(0, int(offset or 0))
    return lim, off


def _validate_ns_element(el: Any) -> None:
    if not isinstance(el, str) or not el:
        raise ValueError(f"namespace elements must be non-empty strings (got {el!r})")
    if _NS_SEP in el:
        raise ValueError(f"namespace element may not contain {_NS_SEP!r}: {el!r}")
    if ".." in el:
        raise ValueError(f"namespace element may not contain '..': {el!r}")


def _ensure_ns_seq(namespace: Any, *, allow_empty: bool) -> tuple[str, ...]:
    # Reject a bare str/bytes (iterating yields characters) or any non-sequence:
    # the namespace must be an explicit tuple/list of strings, never coerced.
    if isinstance(namespace, (str, bytes)) or not isinstance(namespace, (tuple, list)):
        raise ValueError(
            f"namespace must be a tuple of strings, not {type(namespace).__name__}"
        )
    if not namespace and not allow_empty:
        raise ValueError("namespace must be a non-empty tuple")
    for el in namespace:
        _validate_ns_element(el)
    return tuple(namespace)


def _encode_namespace(namespace: Any) -> str:
    return _NS_SEP.join(_ensure_ns_seq(namespace, allow_empty=False))


def _validate_prefix(prefix: Any) -> tuple[str, ...]:
    # An empty prefix is legal (search-all); non-empty elements are validated so a
    # typo'd / path-shaped / mistyped prefix raises instead of silently matching nothing.
    return _ensure_ns_seq(prefix, allow_empty=True)


def _decode_namespace(category: str) -> tuple[str, ...]:
    return tuple(category.split(_NS_SEP))


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


_MAX_VALUE_DEPTH = 1000


def _ensure_string_keys(value: Any, path: str = "<value>") -> None:
    """Reject non-string dict keys (and pathologically deep values) before write.

    json.dumps would stringify int/float/bool/NaN keys, silently coercing types
    and (on collision with an existing string key) silently dropping a value, so
    non-string keys are rejected loudly. The walk is iterative with an explicit
    depth bound so neither this guard nor the client's JSON encoder can raise a
    bare RecursionError on a crafted ultra-deep value; over-deep raises a clean
    ValueError far above any realistic nesting (real memory values are shallow).
    """
    stack: list[tuple[Any, str, int]] = [(value, path, 0)]
    while stack:
        v, p, depth = stack.pop()
        if depth > _MAX_VALUE_DEPTH:
            raise ValueError(
                f"value nesting too deep at {p} (>{_MAX_VALUE_DEPTH} levels); "
                f"flatten the structure"
            )
        if isinstance(v, dict):
            for k, sub in v.items():
                if not isinstance(k, str):
                    raise ValueError(
                        f"value contains a non-string dict key at {p}: {k!r} "
                        f"({type(k).__name__}); memory values must use string keys"
                    )
                stack.append((sub, f"{p}.{k}", depth + 1))
        elif isinstance(v, (list, tuple)):
            for i, sub in enumerate(v):
                stack.append((sub, f"{p}[{i}]", depth + 1))


_ORDER_OPS = {
    "$gt": operator.gt,
    "$gte": operator.ge,
    "$lt": operator.lt,
    "$lte": operator.le,
}


def _apply_op(opname: str, actual: Any, operand: Any) -> bool:
    """Evaluate one filter operator against a stored value.

    Robustness (R16): neither an incomparable pair (``$gt`` of dict-vs-int) nor
    a non-iterable membership operand (``$in`` of an int) may crash the search
    with a raw ``TypeError`` — that would let a single malformed filter abort an
    otherwise valid batch. Order comparisons that raise ``TypeError`` are read as
    "does not match" (return False); ``$in``/``$nin`` validate the operand is a
    container up front and raise a CLEAN ``ValueError`` naming the operator
    otherwise. An unknown operator raises ``ValueError`` (mirrors the caller).
    """
    if opname == "$eq":
        return actual == operand
    if opname == "$ne":
        return actual != operand
    op_fn = _ORDER_OPS.get(opname)
    if op_fn is not None:
        # A missing field (actual is None) is excluded, never raises — this is
        # the documented, intentional divergence from InMemoryStore's float
        # coercion. An incomparable pair (dict vs int) raises TypeError under
        # Py3; treat it as "no match" rather than crashing.
        if actual is None:
            return False
        try:
            return op_fn(actual, operand)
        except TypeError:
            return False
    if opname in ("$in", "$nin"):
        if operand is None or not hasattr(operand, "__iter__"):
            raise ValueError(
                f"filter operator {opname} requires an iterable operand, "
                f"got {type(operand).__name__}"
            )
        try:
            contained = actual in operand
        except TypeError:
            # e.g. `5 in "abc"` — element/container type mismatch. No match.
            contained = False
        return contained if opname == "$in" else not contained
    raise ValueError(f"unsupported filter operator: {opname}")


def _match_filter(value: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    """Apply a LangGraph value-filter.

    Operators: $eq $ne $gt $gte $lt $lte $in $nin, plus implicit equality.
    Comparisons use native Python ordering (NOT float() coercion), so numeric
    strings compare lexically; an item missing the field is excluded (never
    raises). This intentionally diverges from InMemoryStore's float-coercing
    comparators (which raise on missing/non-numeric fields).

    A condition value that is a NON-EMPTY dict of ``$``-prefixed keys is treated
    as an operator map; anything else (including an EMPTY dict ``{}``) falls to
    the equality branch (R34), so ``{"f": {}}`` matches only rows where
    ``f == {}`` instead of vacuously matching every row.
    """
    if not flt:
        return True
    if not isinstance(flt, dict):
        raise ValueError(f"filter must be a dict or None, not {type(flt).__name__}")
    for field, cond in flt.items():
        actual = value.get(field) if isinstance(value, dict) else None
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for opname, operand in cond.items():
                if not _apply_op(opname, actual, operand):
                    return False
        else:
            if actual != cond:
                return False
    return True


class SibylStore(BaseStore):
    """LangGraph BaseStore backed by Sibyl Memory (local SQLite + FTS5).

    Usage::

        from sibyl_memory_langgraph import SibylStore
        store = SibylStore()                      # ~/.sibyl-memory/memory.db
        store.put(("memories", "u1"), "fact1", {"text": "prefers dark mode"})
        item = store.get(("memories", "u1"), "fact1")
        hits = store.search(("memories",), query="dark mode")

    Pass an existing client to share a connection / tier / tenant::

        store = SibylStore(client=my_memory_client)

    Identity: with no explicit ``client`` or ``tenant_id``, the default store
    binds to the ACTIVATED account — it reads ``credentials.json`` next to the
    DB file (written by ``sibyl init``) and resolves the tenant via the canonical
    ladder ``credentials.tenant_id -> credentials.account_id -> DEFAULT_TENANT``.
    DEFAULT_TENANT is used only when no credentials are present (un-activated).
    Passing ``tenant_id=`` overrides this; passing ``client=`` uses that client's
    tenant as-is.
    """

    supports_ttl = False

    def __init__(
        self,
        client: "MemoryClient | None" = None,
        *,
        path: str = "~/.sibyl-memory/memory.db",
        tier: str = "free",
        tenant_id: str | None = None,
        **client_kwargs: Any,
    ) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            kw: dict[str, Any] = {"tier": tier, **client_kwargs}
            if tenant_id is not None:
                kw["tenant_id"] = tenant_id
            else:
                # Contract T / Hardening #5: with no explicit tenant, bind the
                # default store to the ACTIVATED account (credentials.json beside
                # the DB) instead of running identity-blind on DEFAULT_TENANT.
                # A resolved value means creds were present; None means genuinely
                # un-activated, so we leave tenant_id unset and MemoryClient.local
                # applies DEFAULT_TENANT — the ladder's final rung.
                resolved = _resolve_tenant_from_creds(path)
                if resolved is not None:
                    kw["tenant_id"] = resolved
            self._client = MemoryClient.local(path, **kw)
            self._owns_client = True

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        storage = getattr(self._client, "storage", None)
        closer = getattr(storage, "close", None)
        if self._owns_client and callable(closer):
            closer()

    def __enter__(self) -> "SibylStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- the one required surface ---------------------------------------
    def batch(self, ops: Iterable[Any]) -> list[Any]:
        """Execute a batch of ops in order.

        Atomicity (R25): ``batch`` is **per-op best-effort**, not a single
        transaction — each ``PutOp`` commits independently through the client, so
        a raise partway through can leave an EARLIER prefix of the batch
        committed. To make the common failure modes all-or-nothing, every
        ``PutOp`` is fully VALIDATED up front (namespace shape, string key,
        JSON-serializable value) before ANY op executes; a malformed PutOp raises
        during pre-validation, so no write lands. A failure that only surfaces
        DURING execution (I/O error, cap-exceeded on the Nth write) can still
        leave the first N-1 writes applied — callers needing true transactional
        semantics should not rely on batch rollback.
        """
        ops = list(ops)
        # Pre-flight: fail the whole batch before writing anything if any PutOp
        # is malformed (R25). Non-Put ops (Get/Search/List) are side-effect-free
        # and validated when they run.
        for op in ops:
            if isinstance(op, PutOp):
                self._validate_put(op)
        results: list[Any] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._get(op))
            elif isinstance(op, PutOp):
                results.append(self._put(op))
            elif isinstance(op, SearchOp):
                results.append(self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._list_namespaces(op))
            else:  # pragma: no cover - defensive
                raise NotImplementedError(f"unsupported op: {type(op).__name__}")
        return results

    def _validate_put(self, op: PutOp) -> None:
        """Validate a PutOp without writing (R25 pre-flight).

        Runs exactly the adapter-level checks that ``_put`` (and the client's
        ``set_entity``) would raise on — namespace shape / traversal, a non-empty
        string key, string dict keys, and JSON-serializability — so a bad op in
        the middle of a batch is caught before any sibling op commits. A ``None``
        value is a delete and needs no body validation. Bad-key and
        non-serializable-value failures raise the client's typed
        ``ValidationError`` (matching what ``set_entity`` would raise on the same
        input) so the error contract is unchanged from the un-batched path.
        NOTE: NaN/Infinity floats are accepted here (``json.dumps`` allows them,
        as does the client) and are rejected downstream by the DB's json_valid
        CHECK — so a lone NaN put still surfaces the client's StorageError, not a
        false pre-flight pass turned corruption.
        """
        _encode_namespace(op.namespace)  # ns shape + path-traversal guard
        if not isinstance(op.key, str) or not op.key:
            raise ValidationError(
                f"PutOp key must be a non-empty string (got {op.key!r})"
            )
        if op.value is not None:
            _ensure_string_keys(op.value)  # reject non-string / over-deep keys
            try:
                json.dumps(op.value)  # same serializability verdict as the client
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"PutOp value for key {op.key!r} is not JSON-serializable: {exc}"
                ) from exc

    async def abatch(self, ops: Iterable[Any]) -> list[Any]:
        # The SQLite backend is synchronous; offload so we never block the loop.
        return await asyncio.get_event_loop().run_in_executor(None, self.batch, list(ops))

    # ---- op handlers -----------------------------------------------------
    def _get(self, op: GetOp) -> Item | None:
        category = _encode_namespace(op.namespace)
        try:
            row = self._client.get_entity(category, op.key)
        except NotFoundError:
            return None
        return self._to_item(row)

    def _put(self, op: PutOp) -> None:
        category = _encode_namespace(op.namespace)
        if op.value is None:
            self._client.delete_entity(category, op.key)
            return None
        _ensure_string_keys(op.value)
        self._client.set_entity(category, op.key, op.value)  # index/ttl: lexical store, ignored
        return None

    def _search(self, op: SearchOp) -> list[SearchItem]:
        prefix = _validate_prefix(() if op.namespace_prefix is None else op.namespace_prefix)
        # R32/R33: normalize limit/offset ONCE. A negative limit can no longer
        # produce a negative-index slice (which broadened the page to nearly all
        # rows), and limit=None no longer trips `offset + limit` arithmetic.
        lim, off = _clamp_page(op.limit, op.offset, default_limit=10)
        if lim == 0:
            return []
        want = min(off + lim, _POOL)
        if op.query:
            # R14 + Hardening #2: ONE FTS MATCH across ALL categories, then a
            # namespace-prefix post-filter — replacing the O(categories) loop
            # that issued a MATCH per category and buffered up to _POOL rows
            # EACH (worst case ~10^4 categories x 10^4 rows). When post-filtering
            # (a prefix and/or a value filter) we fetch the full pool so a
            # filter-passing row ranked deeper than `want` is not truncated away
            # before the filter runs; the client clamps every read to MAX_LIMIT
            # (=_POOL), so total rows materialized here is bounded by _POOL.
            cap = _POOL if (prefix or op.filter) else want
            rows = self._client.search_entities(op.query, limit=cap)
            if len(rows) >= _POOL:
                _log.warning(
                    "SibylStore search hit the %d-row FTS ceiling (client "
                    "MAX_LIMIT); results may be incomplete for very large stores. "
                    "A complete pass needs a client-side cursor (not yet "
                    "available).",
                    _POOL,
                )
            if prefix:
                rows = [
                    r for r in rows
                    if _decode_namespace(r["category"])[: len(prefix)] == prefix
                ]
        else:
            rows = [
                r for r in self._list_capped()
                if _decode_namespace(r["category"])[: len(prefix)] == prefix
            ]
        if op.filter:
            rows = [r for r in rows if _match_filter(r.get("body") or {}, op.filter)]
        rows = rows[off : off + lim]
        return [self._to_search_item(r) for r in rows]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        if op.max_depth is not None and op.max_depth < 0:
            raise ValueError(f"max_depth must be non-negative, got {op.max_depth}")
        # Match conditions run against the FULL namespace first, then truncate to
        # max_depth and de-duplicate (a deep prefix/suffix must be able to match
        # before truncation — mirrors langgraph InMemoryStore ordering).
        full: set[tuple[str, ...]] = set()
        for r in self._list_capped():
            full.add(_decode_namespace(r["category"]))
        conds = op.match_conditions or ()
        matched = [ns for ns in full if all(_ns_matches(ns, c) for c in conds)] if conds else list(full)
        if op.max_depth is not None:
            matched = [ns[: op.max_depth] for ns in matched]
        namespaces = sorted(set(matched))
        # R32/R33: same clamp as _search — a negative limit must NOT slice from
        # the end (which returned almost every namespace); limit=None must not
        # break `offset + limit`.
        lim, off = _clamp_page(op.limit, op.offset, default_limit=100)
        return namespaces[off : off + lim]

    # ---- helpers ---------------------------------------------------------
    def _list_capped(self, category: str | None = None) -> list[dict[str, Any]]:
        # Single bounded enumeration pass. The client clamps to MAX_LIMIT and has
        # no cursor, so warn (don't silently truncate) when the cap is reached.
        rows = self._client.list_entities(category=category, limit=_POOL)
        if len(rows) >= _POOL:
            _log.warning(
                "SibylStore enumeration hit the %d-row cap (client MAX_LIMIT); "
                "results may be incomplete. Stores larger than this need a "
                "client-side cursor (not yet available).",
                _POOL,
            )
        return rows

    def _to_item(self, row: dict[str, Any]) -> Item:
        return Item(
            value=row["body"] if row.get("body") is not None else {},
            key=row["name"],
            namespace=_decode_namespace(row["category"]),
            created_at=_parse_ts(row.get("created_at")),
            updated_at=_parse_ts(row.get("updated_at")),
        )

    def _to_search_item(self, row: dict[str, Any]) -> SearchItem:
        return SearchItem(
            namespace=_decode_namespace(row["category"]),
            key=row["name"],
            value=row["body"] if row.get("body") is not None else {},
            created_at=_parse_ts(row.get("created_at")),
            updated_at=_parse_ts(row.get("updated_at")),
            score=row.get("score"),
        )


def _wild_match(actual: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if len(actual) != len(pattern):
        return False
    return all(p == "*" or p == a for a, p in zip(actual, pattern))


def _ns_matches(ns: tuple[str, ...], cond: Any) -> bool:
    match_type = getattr(cond, "match_type", None)
    path = tuple(getattr(cond, "path", ()) or ())
    if not path:
        return True
    if match_type == "prefix":
        return len(ns) >= len(path) and _wild_match(ns[: len(path)], path)
    if match_type == "suffix":
        return len(ns) >= len(path) and _wild_match(ns[-len(path):], path)
    # R35: an unknown match_type must NOT fail open (the old `return True`
    # matched every namespace). Raise, mirroring _match_filter's unknown-operator
    # handling, so a typo'd/unsupported condition is loud instead of silently
    # returning the entire namespace set.
    raise ValueError(f"unsupported match_type: {match_type!r} (expected 'prefix' or 'suffix')")
