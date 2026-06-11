"""PKG-5 regression: set_reference accepts dict/list bodies (VRTX ISSUE-003).

Before 0.4.12, set_reference(key, {...}) reached the SQLite INSERT with a dict
bound as a parameter and raised StorageError. Now a dict/list body is coerced
to canonical JSON; an unsupported type raises a typed ValidationError naming the
body param before it can reach the DB.
"""
import json
import tempfile
from pathlib import Path

import pytest

from sibyl_memory_client.client import MemoryClient
from sibyl_memory_client.exceptions import ValidationError


def _client():
    d = tempfile.mkdtemp()
    return MemoryClient.local(Path(d) / "memory.db")


def test_set_reference_dict_body_coerced_to_json():
    c = _client()
    payload = {"b": 2, "a": 1, "nested": {"x": [1, 2, 3]}}
    c.set_reference("cfg/profile", payload)
    got = c.get_reference("cfg/profile")
    assert got is not None
    # Stored as canonical JSON (sorted keys); round-trips back to the dict.
    assert json.loads(got["body"]) == payload


def test_set_reference_list_body_coerced_to_json():
    c = _client()
    c.set_reference("cfg/list", [{"k": "v"}, 2, "three"])
    got = c.get_reference("cfg/list")
    assert json.loads(got["body"]) == [{"k": "v"}, 2, "three"]


def test_set_reference_str_body_unchanged():
    c = _client()
    c.set_reference("cfg/str", "plain text body")
    assert c.get_reference("cfg/str")["body"] == "plain text body"


def test_set_reference_bad_type_raises_typed_error():
    c = _client()
    with pytest.raises(ValidationError) as ei:
        c.set_reference("cfg/bad", object())  # type: ignore[arg-type]
    assert "body" in str(ei.value)
