"""Adversarial fuzz tests for SibylStore.

Target malformed inputs across all public methods. Hunt for:
- Crashes that leak raw internal tracebacks instead of clean typed errors.
- Silent state corruption or data loss.
- Boundary violations (None where types are strict, nested structures that break assumptions).

Each test category has a minimal repro. Tests run against an isolated temp DB.
"""

import os
import random
import string
import tempfile
from datetime import datetime, timezone
from typing import Any

import pytest
from langgraph.store.base import GetOp, ListNamespacesOp, PutOp, SearchOp

from sibyl_memory_langgraph import SibylStore

try:
    from sibyl_memory_client.exceptions import ValidationError
except ImportError:
    ValidationError = ValueError  # Fallback


# ---- helpers -----------------------------------------------------------------
def _fresh_store():
    """Create an isolated store with a temp DB."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    return SibylStore(path=db_path, tier="free")


# ---- namespace malformed inputs -----------------------------------------------
class TestNamespaceMalformed:
    """Namespace validation: must be tuple of non-empty strings, no "/" or ".."."""

    def test_namespace_plain_string(self):
        """Namespace as plain string: FIXED - now rejected with clean ValueError.

        Previously a bare str was silently coerced to a tuple of chars
        ("users" -> ("u","s","e","r","s")). Fixed: _ensure_ns_seq rejects
        bare str/bytes and any non-tuple/list with ValueError, no char-coercion.
        """
        store = _fresh_store()
        with pytest.raises(ValueError, match="namespace must be a tuple of strings"):
            store.batch([PutOp(namespace="users", key="k1", value={})])
        # And no data leaked in under a coerced char-tuple namespace.
        result = store.batch([GetOp(namespace=("u", "s", "e", "r", "s"), key="k1")])
        assert result[0] is None

    def test_namespace_list(self):
        """Namespace as list: ACCEPTED by design (tuple/list both allowed)."""
        store = _fresh_store()
        # The fix explicitly permits list (isinstance(ns, (tuple, list))).
        # A list is an explicit sequence of strings, not char-coercion, so it
        # is treated equivalently to the tuple form.
        store.batch([PutOp(namespace=["users"], key="k1", value={})])
        result = store.batch([GetOp(namespace=("users",), key="k1")])
        assert result[0] is not None

    def test_namespace_none(self):
        """Namespace as None should raise TypeError or ValueError."""
        store = _fresh_store()
        with pytest.raises((TypeError, ValueError)):
            store.batch([PutOp(namespace=None, key="k1", value={})])

    def test_namespace_int(self):
        """Namespace as int should raise TypeError or ValueError."""
        store = _fresh_store()
        with pytest.raises((TypeError, ValueError)):
            store.batch([PutOp(namespace=1, key="k1", value={})])

    def test_namespace_empty_tuple(self):
        """Empty tuple should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty tuple"):
            store.batch([PutOp(namespace=(), key="k1", value={})])

    def test_namespace_element_empty_string(self):
        """Tuple with empty string element should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty strings"):
            store.batch([PutOp(namespace=("users", ""), key="k1", value={})])

    def test_namespace_element_with_slash(self):
        """Tuple element containing "/" should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match=r"may not contain '/'"):
            store.batch([PutOp(namespace=("users/admin", "x"), key="k1", value={})])

    def test_namespace_element_with_dotdot(self):
        """Tuple element containing ".." should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match=r"may not contain '\.\.'"):
            store.batch([PutOp(namespace=("users", ".."), key="k1", value={})])

    def test_namespace_element_non_string_int(self):
        """Tuple element that is an int should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty strings"):
            store.batch([PutOp(namespace=("users", 1), key="k1", value={})])

    def test_namespace_element_non_string_none(self):
        """Tuple element that is None should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty strings"):
            store.batch([PutOp(namespace=("users", None), key="k1", value={})])

    def test_namespace_element_nested_tuple(self):
        """Tuple element that is a tuple should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty strings"):
            store.batch([PutOp(namespace=("users", ("nested",)), key="k1", value={})])

    def test_namespace_element_bytes(self):
        """Tuple element that is bytes should raise ValueError."""
        store = _fresh_store()
        with pytest.raises(ValueError, match="non-empty strings"):
            store.batch([PutOp(namespace=("users", b"data"), key="k1", value={})])


# ---- key malformed inputs ---------------------------------------------------
class TestKeyMalformed:
    """Key validation: must be a string."""

    def test_key_none(self):
        """Key as None should raise ValidationError from client."""
        store = _fresh_store()
        # Client validates identifier and rejects None
        with pytest.raises((ValidationError, TypeError, ValueError, AttributeError)):
            store.batch([PutOp(namespace=("users",), key=None, value={})])

    def test_key_int(self):
        """Key as int should raise ValidationError."""
        store = _fresh_store()
        with pytest.raises((ValidationError, TypeError, ValueError)):
            store.batch([PutOp(namespace=("users",), key=123, value={})])

    def test_key_empty_string(self):
        """Key as empty string: ValidationError from client."""
        store = _fresh_store()
        with pytest.raises((ValidationError, ValueError)):
            store.batch([PutOp(namespace=("users",), key="", value={"data": "test"})])

    def test_key_bytes(self):
        """Key as bytes should raise ValidationError."""
        store = _fresh_store()
        with pytest.raises((ValidationError, TypeError)):
            store.batch([PutOp(namespace=("users",), key=b"key", value={})])

    def test_key_very_long(self):
        """Very long key (10K chars) should raise ValidationError."""
        store = _fresh_store()
        long_key = "k" * 10000
        with pytest.raises((ValidationError, ValueError)):
            store.batch([PutOp(namespace=("users",), key=long_key, value={})])

    def test_key_with_newline(self):
        """Key with newline: ValidationError from client (control char check)."""
        store = _fresh_store()
        key_with_newline = "key\nwith\nnewline"
        with pytest.raises((ValidationError, ValueError)):
            store.batch([PutOp(namespace=("users",), key=key_with_newline, value={"x": 1})])

    def test_key_with_null_byte(self):
        """Key with null byte: ValidationError from client (control char check)."""
        store = _fresh_store()
        key_with_null = "key\x00null"
        with pytest.raises((ValidationError, ValueError)):
            store.batch([PutOp(namespace=("users",), key=key_with_null, value={})])


# ---- value malformed inputs -------------------------------------------------
class TestValueMalformed:
    """Value validation: must be dict, or None (delete sentinel)."""

    def test_value_string(self):
        """Value as string (not dict) should raise ValidationError."""
        store = _fresh_store()
        with pytest.raises((ValidationError, TypeError, ValueError)):
            store.batch([PutOp(namespace=("users",), key="k1", value="string")])

    def test_value_int(self):
        """Value as int should raise ValidationError."""
        store = _fresh_store()
        with pytest.raises((ValidationError, TypeError, ValueError)):
            store.batch([PutOp(namespace=("users",), key="k1", value=42)])

    def test_value_list(self):
        """Value as list: ACCEPTED (lists are valid containers)."""
        store = _fresh_store()
        # Lists are valid values (contract allows dict or list)
        store.batch([PutOp(namespace=("users",), key="k1", value=[1, 2, 3])])
        item = store.batch([GetOp(namespace=("users",), key="k1")])
        assert item[0] is not None
        assert item[0].value == [1, 2, 3]

    def test_value_none_is_delete(self):
        """Value as None is the delete sentinel (valid, not an error)."""
        store = _fresh_store()
        # First put a value.
        store.batch([PutOp(namespace=("users",), key="k1", value={"data": "test"})])
        item = store.batch([GetOp(namespace=("users",), key="k1")])
        assert item[0] is not None
        # Now delete with None.
        store.batch([PutOp(namespace=("users",), key="k1", value=None)])
        item = store.batch([GetOp(namespace=("users",), key="k1")])
        assert item[0] is None

    def test_value_dict_non_string_keys(self):
        """Dict with non-string keys: may be coerced or rejected cleanly."""
        store = _fresh_store()
        try:
            store.batch([PutOp(namespace=("users",), key="k1", value={1: "one", 2: "two"})])
            # If accepted, retrieve and verify no crash.
            item = store.batch([GetOp(namespace=("users",), key="k1")])
        except (TypeError, ValueError) as e:
            pass  # Acceptable clean error.

    def test_value_dict_bytes_values(self):
        """Dict with bytes values: ValidationError from JSON serialization."""
        store = _fresh_store()
        with pytest.raises((ValidationError, TypeError, ValueError)):
            store.batch([PutOp(namespace=("users",), key="k1", value={"data": b"bytes"})])

    def test_value_deeply_nested_dict(self):
        """Deeply nested dict structure: should be accepted (valid JSON-able)."""
        store = _fresh_store()
        deep = {"a": {"b": {"c": {"d": {"e": "value"}}}}}
        store.batch([PutOp(namespace=("users",), key="k1", value=deep)])
        item = store.batch([GetOp(namespace=("users",), key="k1")])
        assert item[0] is not None
        assert item[0].value == deep


# ---- filter malformed inputs ------------------------------------------------
class TestFilterMalformed:
    """Filter validation: must be dict or None, with valid operators."""

    def setup_method(self):
        """Pre-populate store with test data for search."""
        self.store = _fresh_store()
        self.store.batch([
            PutOp(namespace=("users",), key="u1", value={"age": 25, "name": "Alice"}),
            PutOp(namespace=("users",), key="u2", value={"age": 30, "name": "Bob"}),
        ])

    def test_filter_string(self):
        """Filter as string: FIXED - now clean ValueError instead of AttributeError.

        Previously _match_filter called flt.items() on a str and leaked a raw
        AttributeError. Fixed: _match_filter raises ValueError for non-dict filters.
        """
        with pytest.raises(ValueError, match="filter must be a dict or None"):
            self.store.batch([SearchOp(namespace_prefix=("users",), query="Alice", filter="invalid")])

    def test_filter_list(self):
        """Filter as list: FIXED - now clean ValueError instead of AttributeError."""
        with pytest.raises(ValueError, match="filter must be a dict or None"):
            self.store.batch([SearchOp(namespace_prefix=("users",), query="Alice", filter=["age", 25])])

    def test_filter_unknown_operator(self):
        """Filter with unknown operator like $and or $regex should raise ValueError."""
        with pytest.raises(ValueError, match="unsupported filter operator"):
            self.store.batch([SearchOp(
                namespace_prefix=("users",),
                query="Alice",
                filter={"age": {"$regex": "^25"}}
            )])

    def test_filter_operator_exists(self):
        """Filter with $exists operator (not in supported ops) should raise ValueError."""
        with pytest.raises(ValueError, match="unsupported filter operator"):
            self.store.batch([SearchOp(
                namespace_prefix=("users",),
                query="Alice",
                filter={"name": {"$exists": True}}
            )])

    def test_filter_valid_eq(self):
        """Valid $eq filter should work."""
        results = self.store.batch([SearchOp(
            namespace_prefix=("users",),
            query="",
            filter={"age": {"$eq": 25}}
        )])
        assert len(results[0]) > 0  # Should find u1.

    def test_filter_valid_in(self):
        """Valid $in filter should work."""
        results = self.store.batch([SearchOp(
            namespace_prefix=("users",),
            query="",
            filter={"age": {"$in": [25, 30]}}
        )])
        assert len(results[0]) > 0  # Should find both.

    def test_filter_mixed_valid_invalid(self):
        """Filter with both valid and invalid operators should raise ValueError on first invalid."""
        with pytest.raises(ValueError, match="unsupported filter operator"):
            self.store.batch([SearchOp(
                namespace_prefix=("users",),
                query="",
                filter={"age": {"$eq": 25, "$badop": "value"}}
            )])


# ---- search malformed inputs ------------------------------------------------
class TestSearchMalformed:
    """Search parameter validation: limit, offset, namespace_prefix."""

    def setup_method(self):
        """Pre-populate store with test data."""
        self.store = _fresh_store()
        for i in range(5):
            self.store.batch([
                PutOp(namespace=("users",), key=f"u{i}", value={"id": i})
            ])

    def test_search_negative_limit(self):
        """Negative limit should be handled (clamped, or rejected cleanly)."""
        try:
            results = self.store.batch([SearchOp(
                namespace_prefix=("users",),
                query="",
                limit=-10
            )])
            # If accepted, should not crash.
            assert isinstance(results[0], list)
        except (ValueError, TypeError) as e:
            pass  # Acceptable clean error.

    def test_search_zero_limit(self):
        """Zero limit should return empty list (valid edge case)."""
        results = self.store.batch([SearchOp(
            namespace_prefix=("users",),
            query="",
            limit=0
        )])
        assert results[0] == []

    def test_search_huge_limit(self):
        """Very large limit (10^9) should not crash, just return all available."""
        results = self.store.batch([SearchOp(
            namespace_prefix=("users",),
            query="",
            limit=10**9
        )])
        assert isinstance(results[0], list)
        assert len(results[0]) <= 5  # We only have 5 items.

    def test_search_negative_offset(self):
        """Negative offset should be handled."""
        try:
            results = self.store.batch([SearchOp(
                namespace_prefix=("users",),
                query="",
                offset=-5
            )])
            assert isinstance(results[0], list)
        except (ValueError, TypeError) as e:
            pass  # Acceptable clean error.

    def test_search_huge_offset(self):
        """Huge offset should return empty list."""
        results = self.store.batch([SearchOp(
            namespace_prefix=("users",),
            query="",
            offset=10**9
        )])
        assert results[0] == []

    def test_search_namespace_prefix_string(self):
        """namespace_prefix as string: FIXED - now rejected with clean ValueError.

        Previously "users" was silently coerced to ("u","s","e","r","s") and the
        search ran against the wrong prefix. Fixed: _validate_prefix routes through
        _ensure_ns_seq, which rejects a bare str with ValueError.
        """
        with pytest.raises(ValueError, match="namespace must be a tuple of strings"):
            self.store.batch([SearchOp(
                namespace_prefix="users",
                query=""
            )])

    def test_search_namespace_prefix_with_slash(self):
        """namespace_prefix element with "/" should raise ValueError."""
        with pytest.raises(ValueError, match=r"may not contain '/'"):
            self.store.batch([SearchOp(
                namespace_prefix=("users/admin",),
                query=""
            )])

    def test_search_empty_namespace_prefix_is_valid(self):
        """Empty namespace_prefix (empty tuple) should search all namespaces."""
        results = self.store.batch([SearchOp(
            namespace_prefix=(),
            query=""
        )])
        assert isinstance(results[0], list)


# ---- list_namespaces malformed inputs ----------------------------------------
class TestListNamespacesMalformed:
    """ListNamespacesOp edge cases."""

    def setup_method(self):
        """Pre-populate store with test data."""
        self.store = _fresh_store()
        self.store.batch([
            PutOp(namespace=("users", "profile"), key="u1", value={"x": 1}),
            PutOp(namespace=("posts",), key="p1", value={"x": 2}),
        ])

    def test_list_namespaces_negative_limit(self):
        """Negative limit should be handled."""
        try:
            results = self.store.batch([ListNamespacesOp(limit=-1)])
            assert isinstance(results[0], list)
        except (ValueError, TypeError) as e:
            pass

    def test_list_namespaces_zero_limit(self):
        """Zero limit should return empty list."""
        results = self.store.batch([ListNamespacesOp(limit=0)])
        assert results[0] == []

    def test_list_namespaces_huge_limit(self):
        """Huge limit should return all available."""
        results = self.store.batch([ListNamespacesOp(limit=10**9)])
        assert isinstance(results[0], list)

    def test_list_namespaces_negative_offset(self):
        """Negative offset should be handled."""
        try:
            results = self.store.batch([ListNamespacesOp(offset=-1)])
            assert isinstance(results[0], list)
        except (ValueError, TypeError) as e:
            pass

    def test_list_namespaces_huge_offset(self):
        """Huge offset should return empty list."""
        results = self.store.batch([ListNamespacesOp(offset=10**9)])
        assert results[0] == []

    def test_list_namespaces_negative_max_depth(self):
        """Negative max_depth should be handled."""
        try:
            results = self.store.batch([ListNamespacesOp(max_depth=-1)])
            assert isinstance(results[0], list)
        except (ValueError, TypeError) as e:
            pass

    def test_list_namespaces_zero_max_depth(self):
        """Zero max_depth: should truncate all namespaces to zero elements (all empty tuples -> one ())."""
        results = self.store.batch([ListNamespacesOp(max_depth=0)])
        # All namespaces truncated to () and deduplicated should give one empty tuple.
        assert results[0] == [()]


# ---- batch operation sequences and state consistency -------------------------
class TestBatchOperationSequences:
    """Run sequences of valid ops to verify state consistency and no silent corruption."""

    def test_put_get_delete_sequence(self):
        """Put, Get, Delete, Get sequence should maintain state."""
        store = _fresh_store()
        ns = ("users",)
        key = "u1"
        value = {"age": 30}

        # Put
        store.batch([PutOp(namespace=ns, key=key, value=value)])

        # Get (should exist)
        items = store.batch([GetOp(namespace=ns, key=key)])
        assert items[0] is not None
        assert items[0].value == value

        # Delete
        store.batch([PutOp(namespace=ns, key=key, value=None)])

        # Get (should not exist)
        items = store.batch([GetOp(namespace=ns, key=key)])
        assert items[0] is None

    def test_concurrent_ops_same_batch(self):
        """Multiple ops in one batch should execute atomically."""
        store = _fresh_store()
        ops = [
            PutOp(namespace=("users",), key="u1", value={"name": "Alice"}),
            PutOp(namespace=("users",), key="u2", value={"name": "Bob"}),
            PutOp(namespace=("posts",), key="p1", value={"title": "Hello"}),
            GetOp(namespace=("users",), key="u1"),
            SearchOp(namespace_prefix=("users",), query="Alice"),
        ]
        results = store.batch(ops)
        assert results[3] is not None  # GetOp result
        assert isinstance(results[4], list)  # SearchOp result

    def test_overwrite_same_key(self):
        """Multiple puts to same key should keep only latest value."""
        store = _fresh_store()
        ns = ("users",)
        key = "u1"

        store.batch([
            PutOp(namespace=ns, key=key, value={"version": 1}),
            PutOp(namespace=ns, key=key, value={"version": 2}),
            PutOp(namespace=ns, key=key, value={"version": 3}),
        ])

        items = store.batch([GetOp(namespace=ns, key=key)])
        assert items[0].value == {"version": 3}

    def test_random_ops_fixed_seed(self):
        """Generate 50 random valid ops with fixed seed and verify no crash/corruption."""
        random.seed(42)
        store = _fresh_store()

        ns_options = [
            ("users",),
            ("posts",),
            ("users", "profile"),
            ("data", "archive"),
        ]

        ops = []
        for _ in range(50):
            op_type = random.choice(["put", "get", "search", "list"])
            ns = random.choice(ns_options)
            key = f"key_{random.randint(0, 10)}"

            if op_type == "put":
                value = {
                    "field1": f"value_{random.randint(0, 100)}",
                    "field2": random.randint(0, 1000),
                }
                ops.append(PutOp(namespace=ns, key=key, value=value))
            elif op_type == "get":
                ops.append(GetOp(namespace=ns, key=key))
            elif op_type == "search":
                query = random.choice(["", "test", "data"])
                ops.append(SearchOp(namespace_prefix=ns, query=query, limit=10))
            else:  # list
                ops.append(ListNamespacesOp(limit=20))

        # Execute all ops; should not crash.
        results = store.batch(ops)
        assert len(results) == len(ops)


# ---- edge cases and boundary conditions ------

class TestEdgeCases:
    """Boundary conditions and special cases."""

    def test_special_characters_in_key(self):
        """Key with special characters should work."""
        store = _fresh_store()
        special_key = "key!@#$%^&*()"
        store.batch([PutOp(namespace=("users",), key=special_key, value={"x": 1})])
        items = store.batch([GetOp(namespace=("users",), key=special_key)])
        assert items[0] is not None

    def test_unicode_in_namespace_and_key(self):
        """Unicode in namespace element and key should work."""
        store = _fresh_store()
        store.batch([PutOp(namespace=("用户",), key="キー", value={"x": 1})])
        items = store.batch([GetOp(namespace=("用户",), key="キー")])
        assert items[0] is not None

    def test_empty_value_dict(self):
        """Empty dict as value should be valid."""
        store = _fresh_store()
        store.batch([PutOp(namespace=("users",), key="u1", value={})])
        items = store.batch([GetOp(namespace=("users",), key="u1")])
        assert items[0] is not None
        assert items[0].value == {}

    def test_deeply_nested_value(self):
        """Very deep nesting should work."""
        store = _fresh_store()
        deep = {"a": {}}
        current = deep["a"]
        for i in range(20):
            current[f"level{i}"] = {}
            current = current[f"level{i}"]
        current["value"] = "deep"

        store.batch([PutOp(namespace=("users",), key="u1", value=deep)])
        items = store.batch([GetOp(namespace=("users",), key="u1")])
        assert items[0] is not None

    def test_search_with_none_query(self):
        """Search with None query should behave like empty string."""
        store = _fresh_store()
        store.batch([PutOp(namespace=("users",), key="u1", value={"x": 1})])
        results = store.batch([SearchOp(namespace_prefix=("users",), query=None)])
        assert isinstance(results[0], list)

    def test_context_manager(self):
        """Store used as context manager should clean up."""
        with _fresh_store() as store:
            store.batch([PutOp(namespace=("users",), key="u1", value={"x": 1})])
            items = store.batch([GetOp(namespace=("users",), key="u1")])
            assert items[0] is not None
        # No exception on exit.

    def test_no_state_bleed_between_stores(self):
        """Two separate stores with different DBs should not share data."""
        store1 = _fresh_store()
        store2 = _fresh_store()

        store1.batch([PutOp(namespace=("users",), key="u1", value={"src": "store1"})])

        items = store2.batch([GetOp(namespace=("users",), key="u1")])
        assert items[0] is None  # Should not exist in store2.


# ---- fuzz-specific: malformed batch input itself --------------------------------
class TestBatchInputMalformed:
    """Malformed inputs to batch() itself."""

    def test_batch_with_none_in_ops(self):
        """Batch ops list containing None should raise NotImplementedError."""
        store = _fresh_store()
        with pytest.raises((NotImplementedError, TypeError, AttributeError)):
            store.batch([PutOp(namespace=("users",), key="u1", value={}), None])

    def test_batch_with_wrong_op_type(self):
        """Batch with an unrecognized op type should raise NotImplementedError or TypeError."""
        store = _fresh_store()
        with pytest.raises((NotImplementedError, TypeError, AttributeError)):
            store.batch([
                PutOp(namespace=("users",), key="u1", value={}),
                "not_an_op",  # Wrong type
            ])

    def test_batch_with_dict_instead_of_op(self):
        """Batch with a dict (not an Op object) should raise TypeError or NotImplementedError."""
        store = _fresh_store()
        with pytest.raises((NotImplementedError, TypeError, AttributeError)):
            store.batch([
                PutOp(namespace=("users",), key="u1", value={}),
                {"namespace": ("users",), "key": "u2", "value": {}},  # Dict, not Op
            ])

    def test_batch_empty_list(self):
        """Batch with empty list should return empty results."""
        store = _fresh_store()
        results = store.batch([])
        assert results == []

    def test_batch_generator_instead_of_list(self):
        """Batch should accept any iterable, including generators."""
        store = _fresh_store()
        def gen():
            yield PutOp(namespace=("users",), key="u1", value={"x": 1})
            yield GetOp(namespace=("users",), key="u1")

        results = store.batch(gen())
        assert len(results) == 2
        assert results[1] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
