"""Tier convention (spec tier-convention-spec-2026-10-05, section 5).

The canonical tiers are free, pro and elite. One shared module
(``sibyl_memory_client.tiers``) owns every tier name; legacy labels from older
servers are input-only aliases; an unrecognised label is treated as Free with
the reason code ``tier_unknown``. The source guard at the bottom is the
client's grep gate: no paid-tier literal outside ``tiers.py``.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from sibyl_memory_client import tiers
from sibyl_memory_client._capcheck import CapGate, TierCache
from sibyl_memory_client.client import MemoryClient
from sibyl_memory_client.exceptions import TierGateError, devices_read_only_message
from sibyl_memory_client.lifecycle.entitled import decision_allows
from sibyl_memory_client.testing import NullEntitlementGate, write_entitlement

FIVE_MIB = 5 * 1024 * 1024


# ---- the module -----------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("free", "free"), ("pro", "pro"), ("elite", "elite"),
    ("Pro", "pro"), (" elite ", "elite"), ("STAKE", "pro"), ("FREE", "free"),
    ("sync", "pro"), ("team", "pro"), ("lifetime", "pro"), ("stake", "pro"),
    ("enterprise", "elite"), (" Enterprise\n", "elite"),
    ("", None), (None, None), (3, None), ("gold", None), ("pro plus", None),
    (b"pro", None), (["pro"], None),
])
def test_normalize_table(raw, want):
    assert tiers.normalize(raw) == want
    assert tiers.known(raw) is (want is not None)


def test_is_paid_for_every_canonical_and_legacy_name():
    for name in ("pro", "elite", "sync", "team", "lifetime", "stake", "enterprise",
                 "Pro", " ELITE "):
        assert tiers.is_paid(name) is True, name
    for name in ("free", "Free", "gold", "", None, 0):
        assert tiers.is_paid(name) is False, name


def test_display_words():
    assert [tiers.display(t) for t in ("free", "pro", "elite")] == ["Free", "Pro", "Elite"]
    assert tiers.display("stake") == "Pro"
    assert tiers.display("enterprise") == "Elite"
    assert tiers.display("gold") == "Free"
    assert tiers.display(None) == "Free"


def test_soft_caps():
    assert tiers.soft_cap("gold") == FIVE_MIB
    assert tiers.soft_cap("free") == FIVE_MIB
    assert tiers.soft_cap(None) == FIVE_MIB
    assert tiers.soft_cap("elite") is None
    assert tiers.soft_cap("pro") is None
    assert tiers.soft_cap("lifetime") is None
    assert set(tiers.SOFT_CAPS) == {"free", "pro", "elite"}


def test_the_one_paid_set():
    assert tiers.PAID_TIERS == {"pro", "elite", "sync", "team", "lifetime", "stake", "enterprise"}
    assert "free" not in tiers.PAID_TIERS
    assert tiers.CANONICAL == ("free", "pro", "elite")
    assert tiers.UNKNOWN_REASON == "tier_unknown"


def test_every_public_name_is_the_shared_object():
    from sibyl_memory_client import _capcheck, lint
    from sibyl_memory_client.lifecycle import entitled
    assert _capcheck.PAID_TIERS is tiers.PAID_TIERS
    assert entitled.PAID_TIERS is tiers.PAID_TIERS
    assert MemoryClient._PAID_ONLY_TIERS is tiers.PAID_TIERS
    assert lint.TIER_SOFT_CAPS is tiers.SOFT_CAPS
    assert lint.TIER_SOFT_CAPS["elite"] is None
    # the three five-MiB constants are one
    assert _capcheck.FREE_TIER_CAP_BYTES == lint.DEFAULT_SOFT_CAP_BYTES == tiers.SOFT_CAPS["free"]


# ---- decision_allows ------------------------------------------------------

class _Ent:
    def __init__(self, tier):
        self.tier = tier


class _Dec:
    def __init__(self, tier, state="fresh"):
        self.state = state
        self.code = None
        self.entitlement = _Ent(tier)


def test_decision_allows_by_tier():
    assert decision_allows(_Dec("elite")) == (True, "elite")
    assert decision_allows(_Dec("pro")) == (True, "pro")
    assert decision_allows(_Dec("stake")) == (True, "pro")
    assert decision_allows(_Dec(" Elite ")) == (True, "elite")
    assert decision_allows(_Dec("enterprise", state="stale")) == (True, "elite")
    assert decision_allows(_Dec("gold")) == (False, "tier_unknown")
    assert decision_allows(_Dec("free")) == (False, "free")
    # the existing reasons are unchanged
    assert decision_allows(_Dec("elite", state="locked")) == (False, "locked")
    assert decision_allows(_Dec("")) == (False, "no_entitlement")


# ---- cap gate -------------------------------------------------------------

def _gate(tmp_path, ent_tier, check_fn=None):
    cache = TierCache(tmp_path / "tier_cache.json")
    return CapGate(account_id="acct-1", session_token="sess-1", db_size_fn=lambda: 0,
                   cache=cache, check_fn=check_fn or (lambda *a, **k: {}),
                   entitlement_tier_fn=lambda: ent_tier)


def test_capcheck_verified_elite_trusts_an_uncapped_cache(tmp_path):
    assert _gate(tmp_path, "elite")._cache_uncapped_trusted() is True
    assert _gate(tmp_path, "Elite")._cache_uncapped_trusted() is True
    assert _gate(tmp_path, "gold")._cache_uncapped_trusted() is False
    assert _gate(tmp_path, "free")._cache_uncapped_trusted() is False


def test_capcheck_elite_check_write_answer_sets_server_uncapped(tmp_path):
    gate = _gate(tmp_path, "free",
                 check_fn=lambda *a, **k: {"ok": True, "tier": "elite", "cap_bytes": None})
    gate._refresh_and_check(10)
    assert gate._server_uncapped_until is not None and gate._server_uncapped_until > time.time()
    cached = gate._cache.load()
    assert cached is not None and cached.tier == "elite" and cached.cap_bytes is None


def test_capcheck_unknown_check_write_answer_without_cap_is_capped(tmp_path):
    """A server answer naming a label this version does not know, with no
    cap_bytes field, falls back to the local free cap (fail closed)."""
    gate = _gate(tmp_path, "free", check_fn=lambda *a, **k: {"ok": True, "tier": "gold"})
    gate._refresh_and_check(10)
    assert gate._server_uncapped_until is None
    assert gate._cache.load().cap_bytes == FIVE_MIB


# ---- MemoryClient paid gate ----------------------------------------------

def test_memory_client_elite_passes_the_paid_gate(tmp_path):
    c = MemoryClient.local(tmp_path / "e.db", tier="elite", entitlement_gate=NullEntitlementGate())
    c._require_paid_tier("self-learning")  # no raise
    assert c.free_tier_status()["uncapped"] is True


def test_memory_client_unknown_tier_is_free_and_says_so(tmp_path):
    c = MemoryClient.local(tmp_path / "g.db", tier="gold", entitlement_gate=NullEntitlementGate())
    with pytest.raises(TierGateError) as exc:
        c._require_paid_tier("self-learning")
    msg = str(exc.value)
    assert "'gold'" in msg and "Free" in msg and "tier_unknown" in msg
    assert exc.value.current_tier == "gold"
    st = c.free_tier_status()
    assert st["uncapped"] is False and st["soft_cap_bytes"] == FIVE_MIB


def test_memory_client_free_message_uses_the_display_word(tmp_path):
    c = MemoryClient.local(tmp_path / "f.db", tier="free", entitlement_gate=NullEntitlementGate())
    with pytest.raises(TierGateError) as exc:
        c._require_paid_tier("memory linter")
    assert "Current tier: Free." in str(exc.value)
    assert exc.value.current_tier == "free"


def test_tier_error_copy_names_no_legacy_tier():
    from sibyl_memory_client.exceptions import CapExceededError
    for text in (TierGateError.recovery, CapExceededError.recovery):
        assert not re.search(r"\b(Stake|Sync|Lifetime|Enterprise|Team)\b", text), text
        assert "Pro" in text


# ---- devices read-only copy -----------------------------------------------

def test_devices_read_only_message_elite_vs_unknown():
    acct = {"devices": {"n": 12, "limit": 10, "over": True}}
    elite = devices_read_only_message(acct, "elite")
    assert elite is not None and "get Pro" not in elite
    gold = devices_read_only_message(acct, "gold")
    assert gold is not None and gold.endswith("or get Pro. Nothing was deleted.")


# ---- gate.status() --------------------------------------------------------

def test_gate_status_reports_unknown_tier_and_plan(entitled_test_home, test_signer):
    from sibyl_memory_client._entitlement import EntitlementGate
    acct = {"complete": True, "email_verified": True, "email_required": False,
            "grace_until": None, "enforced": False, "purchase_blocked": False,
            "plan": {"tier": "elite", "src": "comp"}}
    write_entitlement(entitled_test_home, test_signer.mint(tier="gold", cap=None, src="subscription",
                                                           acct=acct))
    gate = EntitlementGate(Path(entitled_test_home) / "credentials.json", background_refresh=False)
    st = gate.status()
    assert st["tier"] == "gold"
    assert st["tier_known"] is False
    assert st["tier_effective"] == "free"
    assert st["plan"] == {"tier": "elite", "src": "comp"}


def test_gate_status_known_tier_without_plan(entitled_test_home, test_signer):
    from sibyl_memory_client._entitlement import EntitlementGate
    write_entitlement(entitled_test_home, test_signer.mint(tier="stake", cap=None, src="staker"))
    st = EntitlementGate(Path(entitled_test_home) / "credentials.json",
                         background_refresh=False).status()
    assert st["tier_known"] is True and st["tier_effective"] == "pro"
    assert "plan" not in st


# ---- source guard (the client's grep gate) --------------------------------

def test_no_other_paid_tier_literal():
    """Every tier name is spelled in tiers.py only. A new paid-tier literal
    anywhere else in the package fails here (spec 5.4, 10.2)."""
    pkg = Path(tiers.__file__).parent
    paid_set = re.compile(r'frozenset\(\{\s*"pro"')
    legacy = re.compile(r'"(sync|team|lifetime|stake|enterprise)"')
    hits = []
    for p in sorted(pkg.rglob("*.py")):
        if p.name == "tiers.py" and p.parent == pkg:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if paid_set.search(line) or legacy.search(line):
                hits.append(f"{p.relative_to(pkg)}:{n}: {line.strip()}")
    assert hits == []
