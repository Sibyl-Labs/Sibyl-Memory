"""Unicode query tokenization in the multi-record linker (Discord ticket 2026-08-04).

Reported as "Polish diacritics break full-text search": ``search("Bełżyce")``
returned 0 hits while the ASCII spelling returned several. The reported cause
(FTS5 not folding diacritics) was wrong — ``porter unicode61`` folds
ż ó ę ą ś ć ń ö ä ü é ñ č correctly, and a direct ``entities_fts MATCH`` on the
accented spelling returns the right rows.

The real cause was one layer up. ``_significant_tokens`` tokenized with the
ASCII-only class ``[A-Za-z0-9]+``, so any word containing a non-ASCII letter
shattered into fragments that exist nowhere in the index::

    Bełżyce      -> ['yce']
    Gedenkstätte -> ['gedenkst', 'tte']

``multi_record_search`` abstains (``return []``) as soon as one token has df=0,
so a single shattered word silently zeroed the whole cross-tier result. Because
the linker is only active on the default path, passing ``tiers=`` explicitly
bypassed it and worked — which is what made this look like a tokenizer bug.

Blast radius was wider than the report: every non-Latin script (Cyrillic, CJK,
Greek, Arabic) produced NO tokens at all and returned [] unconditionally.
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import _significant_tokens, multi_record_search


# --------------------------------------------------------------------------
# Unit: the tokenizer itself
# --------------------------------------------------------------------------

def test_accented_words_survive_tokenization_whole():
    """Non-ASCII letters must not split a word into index-absent fragments."""
    assert _significant_tokens("Bełżyce") == ["bełżyce"]
    assert _significant_tokens("Gedenkstätte") == ["gedenkstätte"]
    assert _significant_tokens("Kraków") == ["kraków"]
    assert _significant_tokens("Saubachstraße") == ["saubachstraße"]


def test_non_latin_scripts_produce_tokens():
    """Cyrillic / CJK / Greek / Arabic previously yielded [] -> unconditional abstain."""
    assert _significant_tokens("Москва") == ["москва"]
    assert _significant_tokens("Αθήνα") == ["αθήνα"]
    assert _significant_tokens("القاهرة") == ["القاهرة"]


def test_ascii_tokenization_unchanged():
    """No-regression: ASCII behaviour, stopword drop and the len>2 filter all hold."""
    assert _significant_tokens("billing handled by alice") == ["billing", "handled", "alice"]
    assert _significant_tokens("H&M tops bought") == ["tops", "bought"]  # 'H','M' too short
    assert _significant_tokens("") == []


# --------------------------------------------------------------------------
# Integration: the reported symptom, through the linker.
#
# NB: exercise multi_record_search directly. MemoryClient.search() does NOT
# route through the linker (it goes to _search_strict, whose sanitizer is
# already Unicode-safe) and was never affected. The linker is reached from
# sibyl-memory-mcp server.py (untiered memory_search) and the Hermes provider,
# which is the path users actually hit.
# --------------------------------------------------------------------------

def _client(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    c.set_entity("places", "belzyce-office",
                 {"tag": "belzyce", "address": "Bełżyce, Lublin, Poland"})
    c.set_entity("places", "dachau-memorial",
                 {"note": 'bus 726 towards "Saubachstraße", stop "KZ-Gedenkstätte"'})
    c.set_entity("places", "moscow-office", {"address": "Москва, Тверская"})
    return c


def test_accented_query_finds_accented_record(tmp_path):
    """The ticket's headline case: accented query on the default (linker) path."""
    c = _client(tmp_path)
    hits = multi_record_search(c, "Bełżyce", limit=10)
    assert any(h.get("key") == "belzyce-office" for h in hits), hits


def test_german_accented_query_finds_record(tmp_path):
    """German was affected too, contrary to the report's 'German is fine'."""
    c = _client(tmp_path)
    hits = multi_record_search(c, "Gedenkstätte", limit=10)
    assert any(h.get("key") == "dachau-memorial" for h in hits), hits


def test_cyrillic_query_finds_record(tmp_path):
    """Non-Latin scripts previously tokenized to [] -> unconditional abstention."""
    c = _client(tmp_path)
    hits = multi_record_search(c, "Москва", limit=10)
    assert any(h.get("key") == "moscow-office" for h in hits), hits


def test_explicit_tiers_path_still_works(tmp_path):
    """The documented workaround (bypasses the linker) must keep working."""
    c = _client(tmp_path)
    hits = c.search("Gedenkstätte", limit=10, tiers=("entity",))
    assert any(h.get("key") == "dachau-memorial" for h in hits), hits


def test_ascii_recall_not_regressed(tmp_path):
    """Folding-eligible diacritics resolve from the ASCII spelling (unicode61)."""
    c = _client(tmp_path)
    hits = multi_record_search(c, "Gedenkstatte", limit=10)
    assert any(h.get("key") == "dachau-memorial" for h in hits), hits


def test_multiword_ascii_linker_not_regressed(tmp_path):
    """No-regression on the linker's normal ASCII path."""
    c = _client(tmp_path)
    hits = multi_record_search(c, "Lublin Poland address", limit=10)
    assert any(h.get("key") == "belzyce-office" for h in hits), hits


# --------------------------------------------------------------------------
# Known limitation, pinned so the follow-up fix trips this test loudly.
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="ł/ß/ø/æ/đ/ı have no canonical decomposition, so no unicode61 "
           "remove_diacritics setting folds them (checked =1 and =2). Needs an "
           "explicit fold map applied at write AND query time plus a reindex. "
           "When that lands this XPASSes — delete the marker.",
)
def test_ascii_query_finds_l_stroke_record(tmp_path):
    """`Belzyce` should find `Bełżyce` — the reporter's second, valid finding."""
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    c.set_entity("places", "only-accented", {"address": "Bełżyce, Lublin"})
    hits = multi_record_search(c, "Belzyce", limit=10)
    assert any(h.get("key") == "only-accented" for h in hits), hits
