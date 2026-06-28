"""Tests for SibylStore — LangGraph BaseStore backed by Sibyl Memory."""
import json
import os
import tempfile

import pytest


@pytest.fixture
def store():
    """Create a temporary SibylStore for testing."""
    from sibyl_memory_langgraph import SibylStore

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        s = SibylStore(db_path=db_path, tenant_id="test")
        yield s


class TestSibylStore:
    def test_put_and_get(self, store):
        """Store a value and retrieve it."""
        store.put(("user", "123"), "name", {"value": "Alice"})
        item = store.get(("user", "123"), "name")
        assert item is not None
        assert item.value == {"value": "Alice"}
        assert item.key == "name"
        assert item.namespace == ("user", "123")

    def test_get_nonexistent(self, store):
        """Get a non-existent item returns None."""
        item = store.get(("user", "999"), "name")
        assert item is None

    def test_delete(self, store):
        """Store and then delete an item."""
        store.put(("user", "123"), "name", {"value": "Alice"})
        store.delete(("user", "123"), "name")
        item = store.get(("user", "123"), "name")
        assert item is None

    def test_put_none_deletes(self, store):
        """Putting None should delete the item."""
        store.put(("user", "123"), "name", {"value": "Alice"})
        store.put(("user", "123"), "name", None)
        item = store.get(("user", "123"), "name")
        assert item is None

    def test_search(self, store):
        """Search returns matching items."""
        store.put(("user", "1"), "name", {"value": "Alice"})
        store.put(("user", "2"), "name", {"value": "Bob"})
        store.put(("user", "3"), "email", {"value": "alice@example.com"})

        items = store.search(("user",), query="Alice")
        assert len(items) >= 1
        names = [i.value.get("value") for i in items]
        assert "Alice" in names

    def test_search_empty(self, store):
        """Search with no results returns empty list."""
        items = store.search(("user",), query="nonexistent_xyz")
        assert items == []

    def test_namespace_isolation(self, store):
        """Different namespaces don't bleed into each other."""
        store.put(("user", "1"), "name", {"value": "Alice"})
        store.put(("team", "1"), "name", {"value": "Engineering"})

        user_items = store.search(("user",))
        team_items = store.search(("team",))

        assert any(i.value.get("value") == "Alice" for i in user_items)
        assert not any(i.value.get("value") == "Alice" for i in team_items)

    def test_global_namespace(self, store):
        """Empty namespace maps to _langgraph/global."""
        store.put((), "config", {"model": "gpt-4"})
        item = store.get((), "config")
        assert item is not None
        assert item.value == {"model": "gpt-4"}

    def test_batch_operations(self, store):
        """Batch executes multiple ops."""
        from langgraph.store.base import GetOp, PutOp

        ops = [
            PutOp(namespace=("user", "1"), key="name", value={"value": "Alice"}),
            PutOp(namespace=("user", "2"), key="name", value={"value": "Bob"}),
        ]
        results = store.batch(ops)
        assert len(results) == 2

        get_ops = [
            GetOp(namespace=("user", "1"), key="name"),
            GetOp(namespace=("user", "2"), key="name"),
        ]
        results = store.batch(get_ops)
        assert len(results) == 2
        assert results[0].value.value == {"value": "Alice"}
        assert results[1].value.value == {"value": "Bob"}

    def test_overwrite(self, store):
        """Overwriting a key updates the value."""
        store.put(("user", "1"), "name", {"value": "Alice"})
        store.put(("user", "1"), "name", {"value": "Alice Updated"})
        item = store.get(("user", "1"), "name")
        assert item.value == {"value": "Alice Updated"}
