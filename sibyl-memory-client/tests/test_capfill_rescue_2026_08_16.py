"""N2 (Kravento PL eval 2026-08-16): relaxed single-token holdback with backfill.

The relaxed single-token last resort (client._relaxed_query_strings step 2) can
fill the cap with rows that all share ONE common token; when it does, both the F2
folded-trigram shadow (client.py :len<cap gate) and the D2L stem rescue are
suppressed exactly when they are needed, and an inflected target that misses the
FTS-rank lottery for a head slot is lost. The fix reserves a small tail slice so
the rescue stages run, then backfills the held rows — so a rescued target arrives
and a no-rescue query is byte-identical to today.
"""
from __future__ import annotations

from sibyl_memory_client import MemoryClient


def _ident_seq(hits):
    return [(h.get("tier"), h.get("category"), h.get("key")) for h in hits]


# A deliberately long target body so 'projekt' ranks it LAST among the junk rows
# (it loses the FTS-rank lottery for the final head slot at limit=20).
_TARGET_BODY = ("reklamacja rozpatrzona i ostatecznie projekt zamkniety wraz z "
                "dokumentacja archiwalna oraz podsumowaniem koncowym zespolu dzialu")


def _corpus(tmp_path, n_junk, name="n2.db"):
    c = MemoryClient.local(tmp_path / name, tenant_id="t1")
    c.set_entity("support", "reklamacja-target", {"text": _TARGET_BODY})
    for i in range(n_junk):
        c.set_entity("proj", f"junk-{i}", {"text": f"projekt numer {i}"})
    return c


# --------------------------------------------------------------------------
# exact threshold: 20 junk -> the target is rescued (pre-patch ABSENT)
# --------------------------------------------------------------------------

def test_capfill_target_rescued_at_20_junk(tmp_path):
    c = _corpus(tmp_path, 20)
    # strict AND misses (no row has both 'reklamacje' and 'projekt'); relaxed
    # single-token 'projekt' fills the cap and the target loses the last slot.
    assert c._search_strict("reklamacje projekt", limit=20) == []
    assert "reklamacja-target" not in [h["key"] for h in c._search_strict("projekt", limit=20)]

    hits = c.search("reklamacje projekt", limit=20)
    keys = [h["key"] for h in hits]
    assert "reklamacja-target" in keys, "N2 rescue failed to surface the target"


def test_capfill_regression_holds_at_19_junk(tmp_path):
    c = _corpus(tmp_path, 19, name="n2b.db")
    hits = c.search("reklamacje projekt", limit=20)
    assert "reklamacja-target" in [h["key"] for h in hits]


# --------------------------------------------------------------------------
# no-rescue variant: junk-only corpus is byte-identical to pre-holdback
# --------------------------------------------------------------------------

def test_no_rescue_backfill_is_byte_identical(tmp_path):
    c = MemoryClient.local(tmp_path / "n2c.db", tenant_id="t1")
    for i in range(20):
        c.set_entity("proj", f"junk-{i}", {"text": f"projekt numer {i}"})
    post = c.search("reklamacje projekt", limit=20)
    # the pre-holdback head is exactly the relaxed single-token 'projekt' result
    pre = c._search_strict("projekt", limit=20)
    assert _ident_seq(post) == _ident_seq(pre), "backfill did not restore the held tail in order"
    assert len(post) == 20  # count == cap


# --------------------------------------------------------------------------
# strict-head invariant: holdback never fires on a non-empty strict head
# --------------------------------------------------------------------------

def test_strict_head_never_held(tmp_path):
    c = MemoryClient.local(tmp_path / "n2d.db", tenant_id="t1")
    # 21 rows all strict-matching BOTH tokens -> strict AND fills the cap.
    for i in range(21):
        c.set_entity("proj", f"row-{i}", {"text": f"projekt raport {i}"})
    strict = c._search_strict("projekt raport", limit=20)
    assert len(strict) == 20, "expected the strict head to fill the cap"
    hits = c.search("projekt raport", limit=20)
    # holdback must not fire: the head is byte-for-byte the strict result
    assert _ident_seq(hits)[:len(strict)] == _ident_seq(strict)
    # no duplicate identity triples anywhere in the output
    seq = _ident_seq(hits)
    assert len(seq) == len(set(seq))
