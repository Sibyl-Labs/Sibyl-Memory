"""N4 / N3' / N5 / N1'-diagnostics (2026-08-18 Kravento PL eval, independent
adversarial re-verification by cryptoxdylan against 0.6.1).

Provenance: cryptoxdylan reproduced these findings against the released 0.6.1
build (client), attached a working patch by email with full rationale and a
339/343-passing test run. That attachment did not survive the Gmail-attachment
retrieval path (gzip CRC mismatch, confirmed corrupt against two independent
decode paths in the same session this file was written). This file
independently reimplements and verifies the scenarios his email described in
detail rather than his exact bytes — see multi_record.py's module-level
comment for the full provenance note.

F1/F2/F3/N2/N3 (0.6.0/0.6.1) are covered by their own dated test files and are
unaffected by this patch; this file covers only what 0.6.1 left open.
"""
from __future__ import annotations

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search


# --------------------------------------------------------------------------
# N4 — a nonzero-df function word must not anchor/pollute idf scoring
# --------------------------------------------------------------------------

def _seed_warehouse_corpus(c):
    # 'our' (df=1) matches ONLY courier-pickups, by pure substring inside
    # 'courier'. 'warehouses' (df=3) is the genuine content token.
    c.set_entity("ops", "courier-pickups", {"text": "courier schedule for pickups this week"})
    c.set_entity("ops", "warehouse-staff", {"text": "warehouse staff rota and shift coverage"})
    c.set_entity("ops", "warehouse-lodz", {"text": "warehouse location in lodz and its capacity"})
    c.set_entity("ops", "annual-stocktake", {"text": "annual stocktake happens in every warehouse"})


def test_n4_function_word_does_not_anchor_the_ranking(tmp_path):
    c = MemoryClient.local(tmp_path / "n4.db", tenant_id="t1")
    _seed_warehouse_corpus(c)

    # pre-N4 behaviour: 'our' (df=1, rarer than 'warehouses' df=3) anchored the
    # ranking and courier-pickups' high coverage-share crowded the genuine
    # warehouse rows below COVERAGE_THRESHOLD.
    res = multi_record_search(c, "where are our warehouses", limit=10)
    keys = {h.get("key") for h in res}
    assert keys == {"warehouse-staff", "warehouse-lodz", "annual-stocktake"}
    assert "courier-pickups" not in keys


def test_n4_all_function_query_is_untouched(tmp_path):
    """Guard: dropping is conditioned on a content token surviving. An
    all-function query behaves exactly as it did pre-N4 (nothing to anchor
    scoring on if every token were dropped)."""
    c = MemoryClient.local(tmp_path / "n4b.db", tenant_id="t1")
    _seed_warehouse_corpus(c)
    res = multi_record_search(c, "where are our", limit=10)
    keys = [h.get("key") for h in res]
    assert keys == ["courier-pickups"]


def test_n4_content_word_still_scores_normally(tmp_path):
    """A content word that happens to share a df with a dropped function word
    is unaffected — only lexicon-classified function words are dropped."""
    c = MemoryClient.local(tmp_path / "n4c.db", tenant_id="t1")
    _seed_warehouse_corpus(c)
    res = multi_record_search(c, "warehouses", limit=10)
    keys = {h.get("key") for h in res}
    assert keys == {"warehouse-staff", "warehouse-lodz", "annual-stocktake"}


# --------------------------------------------------------------------------
# N5 — a dropped negation word must not answer with the affirmative record
# --------------------------------------------------------------------------

def _seed_negation_corpus(c):
    c.set_entity("legal", "kontrakt-a", {"text": "the vendor contract was approved by finance"})


def test_n5_default_policy_abstains_on_dropped_negation(tmp_path):
    c = MemoryClient.local(tmp_path / "n5.db", tenant_id="t1")
    _seed_negation_corpus(c)
    # 'not' is function-shaped (in _DF0_FUNCTION) and would otherwise be
    # dropped, leaving 'contract approved' to match the affirmative record.
    # NEGATION_POLICY="abstain" (default) must return [] instead.
    assert multi_record_search(c, "contract not approved", limit=10) == []
    # the un-negated form still matches normally
    res = multi_record_search(c, "contract approved", limit=10)
    assert {h.get("key") for h in res} == {"kontrakt-a"}


def test_n5_polish_negation_word_also_abstains(tmp_path):
    c = MemoryClient.local(tmp_path / "n5b.db", tenant_id="t1")
    c.set_entity("legal", "umowa-b", {"text": "umowa z dostawca zatwierdzona przez finanse"})
    assert multi_record_search(c, "umowa nie zatwierdzona", limit=10) == []
    res = multi_record_search(c, "umowa zatwierdzona", limit=10)
    assert {h.get("key") for h in res} == {"umowa-b"}


def test_n5_ignore_policy_preserves_pre_n5_behaviour(tmp_path, monkeypatch):
    """The escape hatch: NEGATION_POLICY='ignore' reproduces the pre-N5 (buggy)
    behaviour byte for byte, so a caller relying on the old contract can opt
    back in explicitly."""
    import sibyl_memory_client.multi_record as mr
    monkeypatch.setattr(mr, "NEGATION_POLICY", "ignore")
    c = MemoryClient.local(tmp_path / "n5c.db", tenant_id="t1")
    _seed_negation_corpus(c)
    res = multi_record_search(c, "contract not approved", limit=10)
    assert {h.get("key") for h in res} == {"kontrakt-a"}


# --------------------------------------------------------------------------
# N1' — diagnostics channel (the ratio-abstention fix was rejected on evidence)
# --------------------------------------------------------------------------

def test_n1prime_diagnostics_names_the_blocking_token(tmp_path):
    c = MemoryClient.local(tmp_path / "n1p.db", tenant_id="t1")
    # deliberately omit 'wynosi' (the connecting verb) from the stored text —
    # it is genuinely absent from the corpus, which is what makes it a
    # CONTENT-shaped zero-df token (Dylan's actual scenario: a common verb
    # that varies by inflection/context and simply never appears verbatim).
    c.set_entity("ops", "stawka-ryczalt", {"text": "stawka ryczaltu dwadziescia procent"})
    d = {}
    res = multi_record_search(c, "ile procent wynosi stawka ryczaltu", limit=10, diagnostics=d)
    assert res == []  # N1' itself is UNCHANGED — this still abstains (by design)
    assert d["abstained"] is True
    assert d["abstained_on"] == ["wynosi"]
    assert d["coverage"] == 0.0


def test_n1prime_diagnostics_on_success_reports_dropped_function_words(tmp_path):
    c = MemoryClient.local(tmp_path / "n1p2.db", tenant_id="t1")
    _seed_warehouse_corpus(c)
    d = {}
    res = multi_record_search(c, "where are our warehouses", limit=10, diagnostics=d)
    assert res
    assert d["abstained"] is False
    assert "our" in d["dropped_function"]
    assert d["negation_dropped"] == []
    assert 0.0 < d["coverage"] < 1.0  # 2 of 3 significant tokens survived to scoring


def test_diagnostics_is_optional_and_additive(tmp_path):
    """Every existing caller (diagnostics=None, the default) is unaffected."""
    c = MemoryClient.local(tmp_path / "n1p3.db", tenant_id="t1")
    _seed_warehouse_corpus(c)
    res = multi_record_search(c, "where are our warehouses", limit=10)
    assert {h.get("key") for h in res} == {"warehouse-staff", "warehouse-lodz", "annual-stocktake"}


# --------------------------------------------------------------------------
# Precision gate must still hold (the constraint any N-series fix must respect)
# --------------------------------------------------------------------------

def test_precision_gate_intact_after_n_series(tmp_path):
    c = MemoryClient.local(tmp_path / "gate.db", tenant_id="t1")
    for i in range(20):
        c.set_entity("report", f"report-{i}",
                     {"text": f"co{i:04d} quarterly report revenue forecast status update"})
    assert multi_record_search(c, "xyzqwerty", limit=10) == []
    assert multi_record_search(c, "acme corporation", limit=10) == []
    assert multi_record_search(c, "rejected invoice", limit=10) == []
