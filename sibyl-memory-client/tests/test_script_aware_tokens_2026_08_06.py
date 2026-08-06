"""Script-aware ``_significant_tokens`` (v0.5.0 multi-language search, spec §4.1).

Supersedes PR #25's one-line ``\\w+`` change with the script-aware form that
closes three residual mechanisms #25 left broken while preserving byte-identical
ASCII behaviour:

  M1 non-ASCII split  — keep the accented/foreign word whole.
  M2 length filter    — the ``len(t) > 2`` floor is ASCII-only; 2-char CJK/Hangul
                        words and Brahmic combining-mark fragments (<=2 chars) are
                        real units and are kept, instead of being dropped ->
                        ``toks == []`` -> unconditional abstain.
  M3 case-fold order  — split BEFORE case-folding, so the U+0130 dotted-I class
                        ('İstanbul'.lower() emits i + U+0307) is not shattered.

The ASCII-invariance parametrization is the load-bearing no-regression guard: the
pure-ASCII token stream must be identical to the #25 / 0.4.19 behaviour.
"""
from __future__ import annotations

import re

import pytest

from sibyl_memory_client.multi_record import _STOP, _significant_tokens


# ---------- reference implementation of the pre-0.5.0 ASCII token stream --------
# PR #25 / 0.4.19: ``\w+`` (or [A-Za-z0-9]+) over query.lower(), len>2, stopwords.
# For PURE-ASCII input all three (baseline, #25, 0.5.0) agree; this reproduces
# that contract so the parametrized invariance test pins it exactly.
def _ascii_reference(query: str) -> list[str]:
    return [t for t in re.findall(r"\w+", query.lower())
            if len(t) > 2 and t not in _STOP]


# --------------------------------------------------------------------------
# M1 — non-ASCII words survive whole (no index-absent fragments)
# --------------------------------------------------------------------------

def test_m1_accented_latin_survives_whole():
    assert _significant_tokens("Bełżyce") == ["bełżyce"]
    assert _significant_tokens("Gedenkstätte") == ["gedenkstätte"]
    assert _significant_tokens("Straße") == ["straße"]


def test_m1_fully_non_latin_scripts_produce_tokens():
    # Cyrillic / Greek / Arabic — safe 1:1 case fold, so lowered.
    assert _significant_tokens("Москва") == ["москва"]
    assert _significant_tokens("Αθήνα") == ["αθήνα"]
    # Arabic has no case, unchanged.
    assert _significant_tokens("القاهرة") == ["القاهرة"]


# --------------------------------------------------------------------------
# M2 — short non-ASCII tokens are kept (CJK 2-char words, Brahmic fragments)
# --------------------------------------------------------------------------

def test_m2_cjk_two_char_word_kept():
    # 北京 (Beijing) is a 2-char word; the ASCII len>2 floor must NOT drop it.
    assert _significant_tokens("北京") == ["北京"]
    # a longer CJK run stays a single \w token.
    assert _significant_tokens("北京烤鸭") == ["北京烤鸭"]


def test_m2_hangul_two_char_word_kept():
    assert _significant_tokens("서울") == ["서울"]


def test_m2_brahmic_fragments_kept():
    # Python \w does not match Mn/Mc combining marks, so Devanagari/Bengali/Tamil
    # words fragment on the combining marks. Every surviving fragment (incl. the
    # <=2-char ones) must be kept, not filtered out.
    for word in ("दिल्ली", "কলকাতা", "சென்னை"):
        toks = _significant_tokens(word)
        assert toks, f"{word!r} produced no tokens (would abstain)"
        # fragments are the raw non-ASCII pieces \w+ found, in order
        assert toks == re.findall(r"\w+", word)


def test_m2_thai_fragments_kept():
    toks = _significant_tokens("ขอนแก่น")
    assert toks, "Thai query produced no tokens"
    assert toks == re.findall(r"\w+", "ขอนแก่น")


# --------------------------------------------------------------------------
# M3 — split BEFORE case-folding (no U+0130 i̇ artifacts)
# --------------------------------------------------------------------------

def test_m3_dotted_capital_i_not_shattered():
    # 'İstanbul'.lower() -> 'i̇stanbul' (i + U+0307), which \w+ would then split
    # into ['i', 'stanbul']. Splitting first keeps it one token; because the
    # lower() changes length we keep the RAW token (FTS5 case-folds downstream).
    toks = _significant_tokens("İstanbul")
    assert toks == ["İstanbul"]
    # no combining-dot artifact and no 'stanbul'-only fragment leaked through
    assert "̇" not in "".join(toks)
    assert toks != ["i", "stanbul"]


def test_m3_safe_fold_still_lowercased():
    # A non-ASCII token whose lower() is length-preserving IS lowered (Cyrillic).
    assert _significant_tokens("МОСКВА") == ["москва"]


# --------------------------------------------------------------------------
# ASCII invariance — the no-regression contract (parametrized)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "",
    "billing handled by alice",
    "H&M tops bought",
    "the final report was sent",
    "q3 revenue v2 k8 s3",
    "o'brien signed the decision",
    "UPPER Case MiXeD tokens",
    "with/slash and-hyphen under_score",
    "a an the of to  (all stopwords + short)",
    "Follow-up: rejected injection attempt denied",
])
def test_ascii_invariance_matches_reference(query):
    """Pure-ASCII queries produce the EXACT pre-0.5.0 token stream."""
    assert _significant_tokens(query) == _ascii_reference(query)


def test_ascii_stopwords_and_length_floor_hold():
    # explicit spot-checks of the ASCII contract that must not drift
    assert _significant_tokens("the a an and or") == []          # all stopwords
    assert _significant_tokens("go up in it") == []              # all <=2 / stopword
    assert _significant_tokens("cat dog fox") == ["cat", "dog", "fox"]


# --------------------------------------------------------------------------
# Mixed-script queries — ASCII and non-ASCII tokens coexist per-token
# --------------------------------------------------------------------------

def test_mixed_script_query_tokenizes_per_token():
    # 'Beijing 北京 office' -> ascii path for beijing/office (len>2, lowered),
    # non-ascii path keeps 北京.
    assert _significant_tokens("Beijing 北京 office") == ["beijing", "北京", "office"]
    # short ASCII still dropped even alongside kept short non-ASCII
    assert _significant_tokens("北京 is a city") == ["北京", "city"]
