"""Sibyl memory plugin. MemoryProvider adapter for sibyl-memory-hermes.

Developed by SIBYL, Sibyl Labs LLC. MIT licensed.

Bridges the Hermes v0.13 MemoryProvider ABC to the framework-agnostic
SibylMemoryProvider exposed by the `sibyl-memory-hermes` SDK package.

Why an adapter exists:
    sibyl-memory-hermes ships a rich, LangChain-flavored surface
    (save_context/load_context/remember/recall/search/set_state/...) but
    does NOT implement Hermes' MemoryProvider ABC. This module is the
    thin wrapper that exposes the SDK to Hermes' plugin loader.

Install location:
    Drop this directory at one of:
      $HERMES_HOME/plugins/sibyl/     (user install)
      <site-packages>/plugins/memory/sibyl/   (bundled install)
    Then activate via config.yaml:
      memory:
        provider: sibyl

Configuration:
    Credentials live in ~/.sibyl-memory/credentials.json (managed by the
    `sibyl init` CLI). This adapter does not duplicate that: it lets the
    SDK auto-load credentials. The only Hermes-side option is `db_path`,
    which defaults to <HERMES_HOME>/sibyl/memory.db so each profile has
    its own database.

v0.3.1 hardening (audit-remediation):
    - Hermes ABC + tool_error imports are guarded: module imports cleanly
      outside Hermes (tests, dry-run tooling). Off-Hermes the adapter
      degrades to a no-op MemoryProvider base; the tool dispatcher still
      works for offline validation.
    - sync_turn daemon uses retry-on-busy with backoff + WARNING log on
      final drop (was: silent log-and-drop).
    - shutdown sets a stop flag the daemon checks before issuing slow
      writes, so 10-second join-on-shutdown doesn't drop in-flight turns.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from hashlib import blake2b
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Hermes-side imports (guarded so the module loads off-Hermes for tests)
# ---------------------------------------------------------------------------
try:
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]
    from tools.registry import tool_error  # type: ignore[import-not-found]
    _HERMES_AVAILABLE = True
except ImportError:
    # Off-Hermes (test runner, dry-run, generic Python). Provide a no-op
    # base + a tool_error stub that returns the same JSON shape Hermes
    # would. The bundled module stays importable.
    _HERMES_AVAILABLE = False

    class MemoryProvider:  # type: ignore[no-redef]
        """Standalone fallback base when hermes-agent isn't installed."""
        pass

    def tool_error(msg: str) -> str:  # type: ignore[misc]
        return json.dumps({"error": msg})


logger = logging.getLogger(__name__)

# Timeouts + sizes: all named constants, no magic numbers in dispatch logic.
_SYNC_JOIN_TIMEOUT = 5.0          # wait this long for previous sync_turn write
_SHUTDOWN_JOIN_TIMEOUT = 10.0     # wait this long on shutdown
_MIN_QUERY_LEN = 10               # skip tiny prefetch queries (noise)
_PREFETCH_LIMIT = 5               # how many search hits to inject
_MAX_PREFETCH_CHARS = 6000        # trim prefetch block
_DEFAULT_SEARCH_LIMIT = 10        # sibyl_search default limit
_DEFAULT_LIST_LIMIT = 50          # sibyl_list default limit
_BUSY_RETRY_ATTEMPTS = 3          # sync_turn retry-on-busy attempts
_BUSY_RETRY_BACKOFF = 0.2         # base seconds between retries


def _hermes_home() -> Path:
    """Resolve $HERMES_HOME at call time (profiles can rebind it)."""
    from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    return get_hermes_home()


def _stable_key(content: str, prefix: str = "") -> str:
    """Deterministic short id for on_memory_write mirroring.

    blake2b keeps the value stable across runs so add+remove on the same
    content actually targets the same entity name.
    """
    h = blake2b(content.encode("utf-8", errors="replace"), digest_size=6).hexdigest()
    return f"{prefix}{h}" if prefix else h


# ---------------------------------------------------------------------------
# Tool schemas. OpenAI function-calling shape
# ---------------------------------------------------------------------------

REMEMBER_SCHEMA = {
    "name": "sibyl_remember",
    "description": (
        "Upsert a structured fact into Sibyl's warm-entity tier. Use for "
        "anything worth remembering across sessions: project decisions, user "
        "preferences, API quirks, conventions. (category, name) is the unique "
        "key: re-calling with the same pair overwrites."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Logical grouping, e.g. 'project', 'user', 'pattern', 'decision'.",
            },
            "name": {
                "type": "string",
                "description": "Short identifier unique within the category.",
            },
            "body": {
                "type": "object",
                "description": "JSON body describing the entity. Free-form dict.",
            },
            "status": {
                "type": "string",
                "description": "Optional lifecycle status (e.g. 'active', 'draft').",
            },
        },
        "required": ["category", "name", "body"],
    },
}

RECALL_SCHEMA = {
    "name": "sibyl_recall",
    "description": (
        "Look up a single entity by (category, name). Returns the entity row "
        "(or null if absent) shaped {id, tenant_id, category, name, status, "
        "body, created_at, updated_at}: the user data is under .body. Use "
        "when you know exactly what to fetch; use sibyl_search for fuzzy/keyword lookup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Category the entity lives under."},
            "name":     {"type": "string", "description": "Entity name within the category."},
        },
        "required": ["category", "name"],
    },
}

SEARCH_SCHEMA = {
    "name": "sibyl_search",
    "description": (
        "FTS5 full-text search across ALL Sibyl tiers (entities + state + "
        "reference + journal) for this tenant. Each hit carries a `tier` tag "
        "so you know where the match came from. Returns ranked matches. Use "
        "whenever you want past context but don't know the exact (category, name)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query. User input is sanitized as a single FTS5 phrase: column-filter syntax (name:foo) is treated as literal text."},
            "limit": {
                "type": "integer",
                "description": f"Max results (default {_DEFAULT_SEARCH_LIMIT}).",
                "default": _DEFAULT_SEARCH_LIMIT,
            },
        },
        "required": ["query"],
    },
}

LIST_SCHEMA = {
    "name": "sibyl_list",
    "description": (
        "List entities, optionally filtered by category and/or status. "
        "Use for browsing what's been remembered rather than recalling a "
        "specific item."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional: restrict to this category.",
            },
            "status": {
                "type": "string",
                "description": "Optional: restrict to entities with this status.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max entries to return (default {_DEFAULT_LIST_LIMIT}).",
                "default": _DEFAULT_LIST_LIMIT,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SibylAdapter(MemoryProvider):
    """Hermes MemoryProvider that delegates to sibyl-memory-hermes."""

    def __init__(self) -> None:
        self._sibyl = None  # type: ignore[assignment]  # set in initialize()
        self._session_id: str = ""
        self._hermes_home: Path | None = None
        self._agent_context: str = "primary"
        self._profile: str = "default"
        self._db_path: Path | None = None
        self._sync_thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        self._shutting_down = False  # P-C2 fix: skip slow paths during shutdown

    # -- mandatory ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "sibyl"

    def is_available(self) -> bool:
        """Cheap local check: no network, no DB open."""
        try:
            import sibyl_memory_hermes  # noqa: F401
            return True
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        from sibyl_memory_hermes import SibylMemoryProvider

        self._session_id = session_id
        hermes_home_raw = kwargs.get("hermes_home") or str(_hermes_home())
        self._hermes_home = Path(hermes_home_raw)
        self._agent_context = kwargs.get("agent_context", "primary") or "primary"
        self._profile = self._resolve_profile(kwargs)
        self._shutting_down = False

        # Per-profile DB so multiple Hermes profiles that share one HERMES_HOME
        # do not collapse into a single store. Hermes' get_hermes_home() falls
        # back to ~/.hermes whenever HERMES_HOME is unset (and warns it causes
        # cross-profile corruption), so keying the DB off hermes_home alone is
        # not enough: we also fold in the resolved profile. The default profile
        # keeps the legacy path so existing single-profile installs need no
        # migration; non-default profiles get an isolated DB under profiles/<name>/.
        sibyl_dir = self._hermes_home / "sibyl"
        if self._profile and self._profile != "default":
            db_dir = sibyl_dir / "profiles" / self._safe_profile(self._profile)
        else:
            db_dir = sibyl_dir
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "memory.db"
        self._db_path = db_path

        # autoload_credentials=True picks up ~/.sibyl-memory/credentials.json
        # (created by `sibyl init`). require_credentials=False so we degrade
        # to DEFAULT_TENANT pre-activation rather than crash on first run.
        self._sibyl = SibylMemoryProvider(
            db_path=db_path,
            autoload_credentials=True,
            require_credentials=False,
        )
        logger.info("Sibyl memory initialized: db=%s session=%s profile=%s",
                    db_path, session_id, self._profile)

    @staticmethod
    def _safe_profile(name: str) -> str:
        """Filesystem-safe profile directory name (no traversal, no separators)."""
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")
        return safe or "default"

    def _resolve_profile(self, kwargs: dict[str, Any]) -> str:
        """Resolve the active Hermes profile for per-profile DB scoping.

        Priority:
          1. ``agent_identity`` kwarg — the ABC-sanctioned per-profile hook.
          2. The on-disk ``active_profile`` file Hermes itself uses (checked
             under the active HERMES_HOME first, then ~/.hermes). This is the
             reliable signal when the spawner did not propagate HERMES_HOME,
             which is exactly the case that otherwise collapses every profile
             into the default DB.
          3. ``"default"``.
        """
        ident = (kwargs.get("agent_identity") or "").strip()
        if ident:
            return ident
        candidates = []
        if self._hermes_home is not None:
            candidates.append(self._hermes_home / "active_profile")
        candidates.append(Path.home() / ".hermes" / "active_profile")
        for f in candidates:
            try:
                if f.exists():
                    val = f.read_text().strip()
                    if val:
                        return val
            except OSError:
                pass
        return "default"

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [REMEMBER_SCHEMA, RECALL_SCHEMA, SEARCH_SCHEMA, LIST_SCHEMA]

    # -- recommended overrides ---------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Sibyl Memory\n"
            "Active. Local SQLite-backed structured memory with four searchable "
            "tiers (warm entities, hot state, cold journal, reference docs).\n"
            "- sibyl_remember(category, name, body): store a fact\n"
            "- sibyl_recall(category, name): look up a known fact (returns {body, ...} row)\n"
            "- sibyl_search(query): FTS5 search across ALL tiers; hits are tier-tagged. "
            "Query is treated as AND-of-tokens by default (every word in the query must "
            "appear in the matched row, in any order). For consecutive-phrase match, wrap "
            "the input in double-quotes (e.g. query='\"Christopher Nolan\"').\n"
            "- sibyl_list(category?, status?): browse what's remembered"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._sibyl or not query or len(query.strip()) < _MIN_QUERY_LEN:
            return ""
        # Multi-strategy prefetch: try the full query first (the SDK default
        # is AND-of-tokens as of sibyl-memory-client v0.4.2, so multi-word
        # natural queries DO hit), then top up with per-significant-token
        # searches if recall is thin. This matches the behaviour the LongMemEval
        # 50-Q benchmark on 2026-05-22 showed gives competitive recall.
        clean = query.strip()[:1000]
        merged: dict[tuple[str, str | None], dict[str, Any]] = {}

        def _absorb(hits):
            for h in hits:
                k = (h.get("tier"), h.get("key"))
                r = h.get("rank", 0.0)
                if k in merged:
                    merged[k]["match_count"] += 1
                    if r < merged[k]["best_rank"]:
                        merged[k]["best_rank"] = r
                else:
                    merged[k] = {"hit": h, "match_count": 1, "best_rank": r}

        try:
            _absorb(self._sibyl.search(clean, limit=_PREFETCH_LIMIT))
        except Exception as e:
            logger.debug("Sibyl prefetch primary search failed: %s", e)

        # Per-token top-up. Skip stopwords + short tokens to avoid noise.
        if len(merged) < _PREFETCH_LIMIT:
            stop = {
                "the","a","an","and","or","but","is","are","was","were","be","do","did",
                "does","have","has","had","i","you","he","she","it","we","they","my","your",
                "what","which","who","whom","when","where","why","how","to","of","in","on",
                "at","for","with","this","that","these","those","not","can","will","would",
                "should","could","may","might","just","also","all","any","some","more","most",
            }
            import re as _re
            tokens = _re.findall(r"[A-Za-z0-9&]+(?:['-][A-Za-z0-9&]+)*", clean.lower())
            tokens = [t for t in tokens if len(t) >= 3 and t not in stop]
            for tok in tokens[:5]:  # cap to keep prefetch cheap
                try:
                    _absorb(self._sibyl.search(tok, limit=_PREFETCH_LIMIT))
                except Exception:
                    pass
                if len(merged) >= _PREFETCH_LIMIT * 2:
                    break

        if not merged:
            return ""
        # Rank by per-key match count desc, then best (most negative) FTS5 rank
        ranked = sorted(merged.values(),
                        key=lambda x: (-x["match_count"], x["best_rank"]))
        hits = [x["hit"] for x in ranked[:_PREFETCH_LIMIT]]
        lines = ["## Sibyl Memory: relevant context"]
        for hit in hits:
            tier = hit.get("tier", "?")
            category = hit.get("category", "")
            key = hit.get("key") or hit.get("name") or "?"
            body = hit.get("body")
            body_repr = json.dumps(body, ensure_ascii=False, default=str) if body else ""
            if len(body_repr) > 400:
                body_repr = body_repr[:400] + "…"
            label = f"{category}/{key}" if category else f"{tier}:{key}"
            lines.append(f"- [{label}] {body_repr}")
        block = "\n".join(lines)
        return block[:_MAX_PREFETCH_CHARS]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Sibyl is local SQLite: prefetch() runs synchronously and is fast.
        # Nothing to queue.
        pass

    def sync_turn(self, user_content: str, assistant_content: str,
                  *, session_id: str = "") -> None:
        """Append the turn to the cold journal in a daemon thread.

        v0.3.1 (audit P-C1, P-C2):
        - Retry-on-busy with backoff (was: silent log-and-drop on first failure)
        - WARNING log on final drop after retries exhausted
        - Skip the slow cap-gate path during shutdown
        - Serializes consecutive writes by joining the previous thread first
          (mirrors the byterover/honcho pattern)
        """
        if not self._sibyl:
            return
        if self._agent_context != "primary":
            # Cron/subagent contexts: don't journal (would corrupt the user's
            # representation as the ABC docstring warns).
            return
        if not user_content and not assistant_content:
            return

        sid = session_id or self._session_id
        sibyl = self._sibyl

        def _write() -> None:
            attempts = _BUSY_RETRY_ATTEMPTS
            for attempt in range(1, attempts + 1):
                if self._shutting_down:
                    logger.warning(
                        "Sibyl sync_turn skipping write during shutdown (session=%s)", sid)
                    return
                try:
                    sibyl.save_context(
                        inputs={"user": user_content, "session_id": sid},
                        outputs={"assistant": assistant_content},
                    )
                    return  # success
                except Exception as e:
                    if attempt < attempts:
                        # Exponential backoff for SQLITE_BUSY / transient errors
                        time.sleep(_BUSY_RETRY_BACKOFF * (2 ** (attempt - 1)))
                        continue
                    # Final attempt failed: escalate from debug to warning
                    # so users see drops in production logs.
                    logger.warning(
                        "Sibyl sync_turn dropped a journal turn after %d attempts: %s",
                        attempts, type(e).__name__,
                    )

        with self._sync_lock:
            if self._sync_thread and self._sync_thread.is_alive():
                self._sync_thread.join(timeout=_SYNC_JOIN_TIMEOUT)
            t = threading.Thread(target=_write, daemon=True, name="sibyl-sync")
            self._sync_thread = t
            t.start()

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if not self._sibyl:
            return tool_error("Sibyl provider not initialized")

        try:
            if tool_name == "sibyl_remember":
                category = args.get("category")
                name = args.get("name")
                body = args.get("body")
                if not category or not name or body is None:
                    return tool_error("category, name, and body are required")
                status = args.get("status")
                result = self._sibyl.remember(category, name, body, status=status)
                return json.dumps({"ok": True, "entity": result}, default=str)

            if tool_name == "sibyl_recall":
                category = args.get("category")
                name = args.get("name")
                if not category or not name:
                    return tool_error("category and name are required")
                result = self._sibyl.recall(category, name)
                return json.dumps({"entity": result}, default=str)

            if tool_name == "sibyl_search":
                query = args.get("query")
                if not query:
                    return tool_error("query is required")
                limit = int(args.get("limit") or _DEFAULT_SEARCH_LIMIT)
                # Run15 multi-record fix (Terminal B): workflow queries spanning
                # several linked records surface them all (retrieve-then-verify).
                # See provider.search_multi_record / sibyl_memory_client.multi_record.
                hits = self._sibyl.search_multi_record(query, limit=limit)
                return json.dumps({"results": hits}, default=str)

            if tool_name == "sibyl_list":
                category = args.get("category")
                status = args.get("status")
                limit = int(args.get("limit") or _DEFAULT_LIST_LIMIT)
                rows = self._sibyl.list(category=category, status=status, limit=limit)
                return json.dumps({"entities": rows}, default=str)

            return tool_error(f"Unknown tool: {tool_name}")

        except Exception as e:
            logger.exception("Sibyl tool %s failed", tool_name)
            # SEC-10 hardening: send only the exception class name back to the
            # agent. str(e) could echo entity bodies / args that contained
            # sensitive content. The full exception is in the local log.
            return tool_error(f"{type(e).__name__}")

    def shutdown(self) -> None:
        # P-C2 fix: set the stop flag BEFORE joining so in-flight write loops
        # see it on their next iteration and exit without issuing a slow
        # cap-gate refresh.
        self._shutting_down = True
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT)

    # -- optional hooks ----------------------------------------------------

    def on_session_switch(self, new_session_id: str, *,
                          parent_session_id: str = "",
                          reset: bool = False, **kwargs: Any) -> None:
        # Sibyl doesn't cache per-session resources; just update the id we
        # stamp onto journal events.
        self._session_id = new_session_id

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Flush soon-to-be-discarded turns to the journal."""
        if not self._sibyl or not messages:
            return ""

        # Pair user+assistant messages in order; capture the last ~10 pairs.
        pairs: list[tuple[str, str]] = []
        pending_user: str | None = None
        for msg in messages[-20:]:
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                pending_user = content
            elif role == "assistant" and pending_user is not None:
                pairs.append((pending_user, content))
                pending_user = None

        if not pairs:
            return ""

        sibyl = self._sibyl
        sid = self._session_id

        def _flush() -> None:
            for user_c, asst_c in pairs:
                if self._shutting_down:
                    return
                try:
                    sibyl.save_context(
                        inputs={"user": user_c, "session_id": sid,
                                "reason": "pre_compress"},
                        outputs={"assistant": asst_c},
                    )
                except Exception as e:
                    logger.debug("Sibyl pre_compress flush failed: %s", e)

        threading.Thread(target=_flush, daemon=True, name="sibyl-flush").start()
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs: Any) -> None:
        if not self._sibyl:
            return
        try:
            self._sibyl.save_context(
                inputs={"delegated_task": task, "child_sid": child_session_id,
                        "session_id": self._session_id},
                outputs={"child_result": result},
            )
        except Exception as e:
            logger.debug("Sibyl on_delegation failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: dict[str, Any] | None = None) -> None:
        """Mirror built-in `memory` tool writes into Sibyl's warm tier.

        Accepts metadata even though we treat it as informational only -
        ignoring the kwarg would TypeError under strict callers.
        """
        if not self._sibyl or not content:
            return
        name = _stable_key(content)
        try:
            if action in ("add", "replace"):
                self._sibyl.remember(
                    category=target,
                    name=name,
                    body={"content": content, "metadata": metadata or {}},
                )
            elif action == "remove":
                self._sibyl.forget(category=target, name=name)
        except Exception as e:
            logger.debug("Sibyl on_memory_write (%s/%s) failed: %s", action, target, e)

    # -- config ------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        """No Hermes-side config: prerequisite is the `sibyl init` CLI.

        Sibyl manages its own credentials and identity outside Hermes:
        running `sibyl init` writes ~/.sibyl-memory/credentials.json,
        which the SDK auto-loads at construction time. We deliberately
        return [] here so `hermes memory setup` does NOT double-prompt
        for credentials that already live in the Sibyl native file -
        running both flows would diverge tenant ids and confuse users.

        If a future version needs Hermes-side overrides (alt db_path,
        explicit tenant_id for testing), add them here as non-secret
        fields: keep secrets in credentials.json so there's one
        source of truth.
        """
        return []

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        # Nothing to persist: values are read live from credentials.json and
        # constructor args. This stays a no-op until a hermes-side config
        # file is actually needed.
        return


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Register Sibyl as a memory provider plugin."""
    ctx.register_memory_provider(SibylAdapter())
