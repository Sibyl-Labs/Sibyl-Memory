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

v0.5.0 note: PR #25 (0.4.20) is absorbed into 0.5.0. Its one-line ``\\w+`` fix is
superseded by the script-aware ``_significant_tokens`` (spec §4.1), and its
pinned ``ł``-class ``xfail`` is now a NORMAL passing test — the folded-trigram
search shadow (shadow.py, spec §4.2) resolves ``Belzyce`` -> stored ``Bełżyce``
in both directions, so the strict xfail marker has been DELETED per spec §3/§8.
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import _significant_tokens, multi_record_search
from sibyl_memory_client.shadow import (
    SQLITE_FULL_FOLD_VERSION, sqlite_supports_full_fold)


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
# The former known limitation — the ł-class fold — now RESOLVED by the v0.5.0
# folded-trigram search shadow. #25 pinned this with a strict xfail; the marker
# is DELETED (spec §3/§8) and it runs as a normal passing test.
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not sqlite_supports_full_fold(),
    reason=("needs SQLite >= %s: 'Belzyce' -> 'Bełżyce' folds ł via FOLD_MAP but "
            "ż via the trigram tokenizer's remove_diacritics, which is 3.45+. "
            "The documented-degradation half is asserted below and runs "
            "everywhere." % ".".join(str(n) for n in SQLITE_FULL_FOLD_VERSION)))
def test_ascii_query_finds_l_stroke_record(tmp_path):
    """`Belzyce` should find `Bełżyce` — the reporter's second, valid finding.

    Resolved via the folded-trigram shadow: the stored ``Bełżyce`` is folded to
    ``belzyce`` in ``search_shadow``, so the ASCII query matches once the strict
    porter-unicode61 pass (which leaves ``ł`` unfolded) returns nothing.
    """
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    c.set_entity("places", "only-accented", {"address": "Bełżyce, Lublin"})
    hits = multi_record_search(c, "Belzyce", limit=10)
    assert any(h.get("key") == "only-accented" for h in hits), hits


def test_the_l_stroke_fold_alone_works_on_every_supported_sqlite(tmp_path):
    """The half of the fold that does NOT need the 3.45 tokenizer.

    ``Łomza`` carries only the non-decomposable ``Ł``, which FOLD_MAP handles on
    both sides at any supported version. Keeping this unconditional means the
    version gate above never becomes a place where the whole fold quietly stops
    being tested on an older SQLite.
    """
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    c.set_entity("places", "l-stroke-only", {"address": "Łomza, Podlasie"})
    hits = multi_record_search(c, "Lomza", limit=10)
    assert any(h.get("key") == "l-stroke-only" for h in hits), hits
