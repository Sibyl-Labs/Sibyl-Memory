"""2.D D3 (2026-10-22): search hits carry `chars`; the snippet has its own 600-char cap."""
from sibyl_memory_mcp import server


def test_search_hit_gets_chars_and_snippet_cap():
    body = "x" * 5000
    hit = {"tier": "reference", "key": "doc/a", "body": body, "snippet": "s" * 2000}
    out = server._cap_hit_body(hit)
    assert out["chars"] == 5000
    assert len(out["body"]) == server._SEARCH_HIT_BODY_MAX + 1      # + ellipsis
    assert len(out["snippet"]) == server._SEARCH_HIT_SNIPPET_MAX + 1
    assert out["truncated"] is True


def test_structured_body_chars_and_short_hit_untouched():
    hit = {"tier": "entity", "key": "a", "body": {"t": "hi"}, "snippet": "[hi]"}
    out = server._cap_hit_body(hit)
    assert out["chars"] == len('{"t": "hi"}')
    assert out["body"] == {"t": "hi"} and out["snippet"] == "[hi]"
    assert "truncated" not in out


def test_entity_rows_without_snippet_get_no_chars():
    out = server._cap_hit_body({"name": "a", "body": {"t": "x"}})
    assert "chars" not in out
