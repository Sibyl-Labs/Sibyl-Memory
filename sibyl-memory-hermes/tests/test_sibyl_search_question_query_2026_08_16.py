"""N1 (2026-08-16): the Hermes rail inherits the df=0 function-word fix.

adapter.handle_tool_call('sibyl_search', ...) -> provider.search_multi_record ->
multi_record_search, the SAME retrieve-then-verify path the MCP default uses. A
question-shaped query whose interrogative / copula tokens have zero corpus support
previously abstained to an empty envelope; after the N1 fix the target is
surfaced through the provider-backed store.
"""
from __future__ import annotations

import json
from pathlib import Path

from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter


def _make_initialized_adapter(tmp_path: Path) -> SibylAdapter:
    adapter = SibylAdapter()
    adapter._sibyl = SibylMemoryProvider(
        db_path=str(tmp_path / "adapter.db"),
        autoload_credentials=False,
    )
    adapter._session_id = "test-session"
    adapter._hermes_home = tmp_path
    return adapter


def test_question_query_surfaces_target_via_hermes(tmp_path: Path) -> None:
    adapter = _make_initialized_adapter(tmp_path)
    adapter._sibyl.remember(
        "ops", "inwentaryzacja",
        {"text": "inwentaryzacja magazynu zaplanowana na piatek"})

    out = json.loads(adapter.handle_tool_call(
        "sibyl_search", {"query": "kiedy jest inwentaryzacja"}))
    hits = out["results"]
    assert hits, "sibyl_search abstained on a question-shaped query (N1 regression)"
    assert any(h.get("key") == "inwentaryzacja" for h in hits), hits


def test_content_shaped_absence_still_abstains_via_hermes(tmp_path: Path) -> None:
    adapter = _make_initialized_adapter(tmp_path)
    adapter._sibyl.remember(
        "order", "co0001-order", {"text": "co0001 order shipped confirmation"})
    # 'nonexistenttokenzzzq' is content-shaped and unsupported -> abstain -> []
    out = json.loads(adapter.handle_tool_call(
        "sibyl_search", {"query": "co0001 nonexistenttokenzzzq report"}))
    assert out["results"] == []
