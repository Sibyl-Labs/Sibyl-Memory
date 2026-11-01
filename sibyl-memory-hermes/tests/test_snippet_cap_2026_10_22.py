"""2.D D3 (2026-10-22): the Hermes search-hit snippet mirrors the MCP 600-char cap."""
from sibyl_memory_hermes._hermes_plugin import adapter


def test_snippet_capped_at_600_body_at_1500():
    out = adapter._truncate_hit_body({"body": "b" * 3000, "snippet": "s" * 3000})
    assert len(out["snippet"]) == adapter._SEARCH_HIT_SNIPPET_MAX + 1
    assert len(out["body"]) == adapter._SEARCH_HIT_BODY_MAX + 1
    assert out["truncated"] is True
