"""N1 (2026-08-16): multi_record_search's df=0 abstention must distinguish
FUNCTION-shaped zero-df tokens (interrogatives / auxiliaries / conjunctions in
EN + PL + DE/FR/ES/CZ) from CONTENT-shaped ones ("rejected", injection tokens).

Before: _STOP had 23 English words and no interrogatives, so a single zero-support
function word ("kiedy", "when", "gdzie") collapsed the WHOLE query to [] via the
Stage-1 `if df[t] == 0: return []` gate — the agent-default MCP path returned
nothing for question-shaped queries. After: function-shaped zero-df tokens are
dropped (they carried no corpus signal by construction) and only content-shaped
zero-df tokens still hard-abstain (the injection / "rejected" contract is intact).
"""
from __future__ import annotations

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search, _df0_droppable


# --------------------------------------------------------------------------
# unit: the df=0 classifier
# --------------------------------------------------------------------------

def test_df0_droppable_function_words_true():
    for tok in ("kiedy", "gdzie", "jest", "when", "how", "wann", "welche"):
        assert _df0_droppable(tok) is True, tok


def test_df0_droppable_content_words_false():
    # content-shaped absences must still hard-abstain
    for tok in ("rejected", "denied", "nonexistenttokenzzzq", "odrzucone"):
        assert _df0_droppable(tok) is False, tok


def test_df0_droppable_identifiers_and_nonascii_false():
    assert _df0_droppable("q3") is False          # digit-bearing identifier
    assert _df0_droppable("北京") is False          # non-ASCII (2-char CJK is a real unit)


# --------------------------------------------------------------------------
# integration: multi_record_search recovers question-shaped queries
# --------------------------------------------------------------------------

def _seed(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t1")
    c.set_entity("ops", "inwentaryzacja",
                 {"text": "inwentaryzacja magazynu zaplanowana na piatek"})
    return c


def test_question_query_surfaces_target(tmp_path):
    c = _seed(tmp_path)
    # 'kiedy' + 'jest' are zero-df function words -> dropped; 'inwentaryzacja'
    # carries the query and the target is surfaced (was [] pre-N1).
    res = multi_record_search(c, "kiedy jest inwentaryzacja", limit=10)
    assert res, "question-shaped query abstained (N1 regression)"
    assert "inwentaryzacja" in {h.get("key") for h in res}


def test_drop_only_at_df_zero(tmp_path):
    """When a function word actually has corpus support (df>0) it is NOT dropped —
    the drop happens only at df=0. The target still surfaces."""
    c = _seed(tmp_path)
    c.set_entity("notes", "plan", {"text": "kiedy zaczynamy prace w biurze"})
    # now 'kiedy' has df>0, so it participates normally (no drop path taken)
    res = multi_record_search(c, "kiedy inwentaryzacja", limit=10)
    assert "inwentaryzacja" in {h.get("key") for h in res}


# --------------------------------------------------------------------------
# Stage-2 gates intact: a dropped function word does not resurrect pollution
# --------------------------------------------------------------------------

_TYPES = {
    "report":  "report revenue forecast quarterly",
    "email":   "email thread followup correspondence",
    "journal": "journal meeting notes minutes",
    "bug":     "bug ticket error defect",
}


def _build_corpus(c, n):
    for i in range(n):
        anchor = f"co{i:04d}"
        g = i % max(1, n // 12)
        topics = f"topic{g}alpha topic{g}beta topic{g}gamma"
        for t, tt in _TYPES.items():
            c.set_entity(t, f"{t}-{i}",
                         {"text": f"{anchor} {topics} {t} {tt} project status update"})


def test_dropped_function_word_does_not_pollute(tmp_path):
    """Prefixing a zero-df function word to a single-cluster query must return
    ONLY the anchor cluster — the anchor/coverage precision gate is unchanged and
    the dropped token adds no candidates (re-runs the pollution assertion)."""
    n = 40
    c = MemoryClient.local(tmp_path / "scale.db", tenant_id="scale")
    _build_corpus(c, n)
    g = 7 % max(1, n // 12)
    res = multi_record_search(
        c, f"kiedy co0007 topic{g}alpha topic{g}beta topic{g}gamma", limit=20)
    assert res, "expected the anchor cluster"
    for h in res:
        assert "co0007" in (h.get("body") or {}).get("text", ""), "leaked a non-anchor record"


def test_content_shaped_zero_df_still_abstains(tmp_path):
    """The pinned abstention contract: a content-shaped zero-df term collapses the
    whole query to [] (injection / 'rejected' class), even alongside a function
    word that would otherwise be dropped."""
    c = MemoryClient.local(tmp_path / "ab.db", tenant_id="scale")
    _build_corpus(c, 20)
    assert multi_record_search(c, "co0001 nonexistenttokenzzzq report", limit=10) == []
    # function word present but a content-shaped absence still abstains
    assert multi_record_search(c, "kiedy co0001 nonexistenttokenzzzq", limit=10) == []


# --------------------------------------------------------------------------
# N1 hardening (panel P0/P1, 2026-08-16): a short ABSENT content discriminator
# (ticker / codename / 3-4-letter name / brand code) is NOT function-shaped and
# must still hard-abstain — the reverted <=4-char ASCII length net used to drop
# it and firehose cross-entity records.
# --------------------------------------------------------------------------

def test_df0_droppable_short_content_words_false():
    """Short brand/company/ticker codes are content, not function words — the
    length net that swept them in was reverted; the lexicon must reject them."""
    for tok in ("acme", "acer", "weth", "usdc", "sol", "aero", "visa", "ford",
                "meta", "ikea", "ping", "raj", "base", "kate", "erik", "sui",
                "avax", "barn", "cena", "xqvk"):
        assert _df0_droppable(tok) is False, tok


def test_df0_droppable_inflected_function_words_true():
    """Finding 4: declined PL copula/pronoun forms (incl. non-ASCII and >4 char)
    and EN modals are dropped via explicit lexicon entries, not a length net."""
    for tok in ("będą", "beda", "były", "byly", "którym", "ktorym", "jakich",
                "jaką", "będziemy", "shall", "might", "must"):
        assert _df0_droppable(tok) is True, tok


def test_short_absent_proper_noun_abstains(tmp_path):
    """Pinned regression for P0/P1: an ABSENT short proper-noun/code as the
    query's discriminator collapses the query to [] instead of returning a
    firehose of records about other entities. Pre-fix: 'acme report' -> 10 rows."""
    c = MemoryClient.local(tmp_path / "pn.db", tenant_id="pn")
    for i in range(20):
        c.set_entity("report", f"report-{i}",
                     {"text": f"co{i:04d} quarterly report revenue forecast status update"})
    # 'acme' has df=0 and is a content discriminator, not a function word.
    assert multi_record_search(c, "acme report", limit=10) == []
    # governance corpus: 'aero' absent -> must not surface generic governance rows
    c2 = MemoryClient.local(tmp_path / "gov.db", tenant_id="gov")
    c2.set_entity("gov", "g1", {"text": "governance proposal vote scheduled for the treasury multisig"})
    c2.set_entity("gov", "g2", {"text": "governance vote passed for the treasury allocation change"})
    c2.set_entity("gov", "g3", {"text": "quarterly vote on office snack budget governance committee"})
    assert multi_record_search(c2, "aero governance vote", limit=10) == []


def test_short_garbage_query_does_not_fanout(tmp_path):
    """Finding 3: a stream of short (<=4-char) garbage tokens — the exact class the
    reverted length net declared droppable, letting each `continue` past the df=0
    early-abort and issue one FTS5 search apiece (up to _MAX_FANOUT_TOKENS=24) —
    must once again early-abort on the FIRST content-shaped absence. With the
    length net gone client.search() runs ONCE, not 24x (CORE-6/MH-3 bound)."""
    c = MemoryClient.local(tmp_path / "fan.db", tenant_id="fan")
    c.set_entity("ops", "inwentaryzacja", {"text": "inwentaryzacja magazynu zaplanowana na piatek"})
    calls = {"n": 0}
    real = c.search
    def _counting(q, **kw):
        calls["n"] += 1
        return real(q, **kw)
    c.search = _counting
    # 24 unique 4-char ASCII-alpha nonsense tokens; none is a lexicon function word
    garbage = [a + b + c2 + d
               for a in "zwq" for b in "xkv" for c2 in "pmt" for d in "gh"][:24]
    assert multi_record_search(c, " ".join(garbage), limit=10) == []
    assert calls["n"] == 1, f"expected 1 client.search() call (early abort), got {calls['n']}"
