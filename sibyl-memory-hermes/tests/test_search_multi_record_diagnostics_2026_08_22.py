"""N1' diagnostics passthrough (2026-08-18 Kravento PL eval, part 4).

SibylMemoryProvider.search_multi_record accepts an optional `diagnostics`
dict, forwarded straight to sibyl_memory_client.multi_record.multi_record_search.
Additive: omitting it (the existing call shape) is unaffected.
"""
from __future__ import annotations

from pathlib import Path

from sibyl_memory_hermes import SibylMemoryProvider


def _provider(tmp_path: Path) -> SibylMemoryProvider:
    return SibylMemoryProvider(db_path=str(tmp_path / "diag.db"), autoload_credentials=False)


def test_diagnostics_omitted_is_unaffected(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    p.remember("ops", "inwentaryzacja",
               {"text": "inwentaryzacja magazynu zaplanowana na piatek"})
    hits = p.search_multi_record("kiedy jest inwentaryzacja")
    assert any(h.get("key") == "inwentaryzacja" for h in hits)


def test_diagnostics_dict_is_populated_on_abstention(tmp_path: Path) -> None:
    p = _provider(tmp_path)
    p.remember("order", "co0001-order", {"text": "co0001 order shipped confirmation"})
    d: dict = {}
    hits = p.search_multi_record("co0001 nonexistenttokenzzzq report", diagnostics=d)
    assert hits == []
    assert d["abstained"] is True
    assert d["abstained_on"] == ["nonexistenttokenzzzq"]
