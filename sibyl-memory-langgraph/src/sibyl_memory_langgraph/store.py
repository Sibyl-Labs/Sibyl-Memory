"""
SibylStore — LangGraph BaseStore backed by Sibyl Memory.

Maps LangGraph's (namespace, key, value) model to Sibyl Memory's
(category, name, body) entities. Namespace tuples become category
strings, keys become entity names, and values become JSON bodies.

LangGraph namespace tuples like ("user", "123") are joined with "/"
to form the Sibyl Memory category: "user/123".
"""
import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langgraph.store.base import BaseStore, GetOp, PutOp, SearchOp, DeleteOp, OpResult, Item


def _namespace_to_category(namespace: Optional[Tuple[str, ...]]) -> str:
    """Convert a LangGraph namespace tuple to a Sibyl Memory category string."""
    if not namespace:
        return "_langgraph/global"
    return "/".join(str(n) for n in namespace)


def _category_to_namespace(category: str) -> Tuple[str, ...]:
    """Convert a Sibyl Memory category string back to a LangGraph namespace tuple."""
    if category.startswith("_langgraph/"):
        return ()
    return tuple(category.split("/"))


class SibylStore(BaseStore):
    """LangGraph BaseStore backed by Sibyl Memory's SQLite + FTS5 engine.

    This store provides:
    - Persistent cross-thread memory (survives process restarts)
    - Full-text search via FTS5 (for semantic-ish queries)
    - Automatic cap management (free-tier 2 MB, paid uncapped)
    - WAL mode for concurrent reads during writes

    Example:
        store = SibylStore()

        # Store a value
        store.put(("user", "123"), "name", {"value": "Alice"})

        # Retrieve it
        item = store.get(("user", "123"), "name")
        print(item.value)  # {"value": "Alice"}

        # Search across namespace
        items = store.search(("user",), query="Alice")

        # Delete
        store.delete(("user", "123"), "name")
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        tenant_id: str = "langgraph",
    ):
        """Initialize the SibylStore.

        Args:
            db_path: Path to the SQLite database. Defaults to ~/.sibyl-memory/memory.db
            tenant_id: Tenant ID for multi-tenancy. Defaults to "langgraph".
        """
        from sibyl_memory_client.client import MemoryClient
        self._client = MemoryClient.local(db_path=db_path, tenant_id=tenant_id)

    def batch(self, ops: Sequence[GetOp | PutOp | SearchOp | DeleteOp]) -> List[OpResult]:
        """Execute a batch of operations."""
        results = []
        for op in ops:
            if isinstance(op, GetOp):
                item = self.get(op.namespace, op.key, refresh=op.refresh)
                results.append(OpResult(op=op, value=item))
            elif isinstance(op, PutOp):
                self.put(op.namespace, op.key, op.value, op.index)
                results.append(OpResult(op=op))
            elif isinstance(op, SearchOp):
                items = self.search(
                    op.namespace,
                    query=op.query,
                    filter=op.filter,
                    limit=op.limit,
                    offset=op.offset,
                )
                results.append(OpResult(op=op, value=items))
            elif isinstance(op, DeleteOp):
                self.delete(op.namespace, op.key)
                results.append(OpResult(op=op))
        return results

    def get(
        self,
        namespace: Tuple[str, ...],
        key: str,
        *,
        refresh: Optional[bool] = None,
    ) -> Optional[Item]:
        """Get a single item by namespace + key."""
        category = _namespace_to_category(namespace)
        try:
            entity = self._client.recall(category, key)
            value = json.loads(entity.get("body", "{}")) if entity.get("body") else {}
            return Item(
                value=value,
                key=key,
                namespace=namespace,
                created_at=entity.get("created_at", ""),
                updated_at=entity.get("updated_at", ""),
            )
        except Exception:
            return None

    def put(
        self,
        namespace: Tuple[str, ...],
        key: str,
        value: Optional[Dict[str, Any]],
        index: Optional[Sequence[int | str]] = None,
    ) -> None:
        """Store a value at namespace + key."""
        if value is None:
            self.delete(namespace, key)
            return
        category = _namespace_to_category(namespace)
        body_json = json.dumps(value, ensure_ascii=False)
        self._client.set_entity(
            category=category,
            name=key,
            body={"value": value},
            tags=[f"langgraph:{namespace[0]}"] if namespace else ["langgraph:global"],
        )

    def search(
        self,
        namespace: Optional[Tuple[str, ...]] = None,
        *,
        query: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Item]:
        """Search for items matching a query within a namespace prefix."""
        category = _namespace_to_category(namespace) if namespace else None
        
        if query:
            # Use FTS5 search
            results = self._client.search(
                query=query,
                category=category,
                limit=limit,
            )
        elif category:
            # List all items in category
            results = self._client.list(category=category, limit=limit)
        else:
            # List everything
            results = self._client.list(limit=limit)

        items = []
        for entity in results:
            value = json.loads(entity.get("body", "{}")) if entity.get("body") else {}
            ns = _category_to_namespace(entity.get("category", ""))
            items.append(Item(
                value=value,
                key=entity.get("name", ""),
                namespace=ns,
                created_at=entity.get("created_at", ""),
                updated_at=entity.get("updated_at", ""),
            ))
        return items[offset:offset + limit]

    def delete(self, namespace: Tuple[str, ...], key: str) -> None:
        """Delete a single item."""
        category = _namespace_to_category(namespace)
        self._client.delete(category, key)
