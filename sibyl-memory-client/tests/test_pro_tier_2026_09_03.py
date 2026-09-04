"""Tier consolidation (2026-09-03): 'pro' is THE single upgraded tier.

Card (Stripe), USDC (x402), and staker qualification all resolve to tier
'pro' server-side from this date. These tests pin the client-side contract:
'pro' must be recognized as paid everywhere a tier name is consulted, and
the legacy names must keep working for historical subscription rows.

Regression class this guards: a server tier name the client does not know
falls out of PAID_TIERS, which silently demotes a paying user to free-tier
behavior (soft-cap warnings, paid-only feature gates raising TierGateError).
"""

from sibyl_memory_client._capcheck import PAID_TIERS
from sibyl_memory_client.client import MemoryClient
from sibyl_memory_client.lint import TIER_SOFT_CAPS


ALL_PAID = ("pro", "sync", "team", "lifetime", "stake", "enterprise")


def test_pro_is_a_paid_tier_for_cap_checks():
    assert "pro" in PAID_TIERS


def test_legacy_paid_tiers_still_recognized():
    for tier in ALL_PAID:
        assert tier in PAID_TIERS, f"{tier} fell out of PAID_TIERS"


def test_pro_unlocks_paid_only_features():
    assert "pro" in MemoryClient._PAID_ONLY_TIERS
    for tier in ALL_PAID:
        assert tier in MemoryClient._PAID_ONLY_TIERS


def test_pro_soft_cap_is_uncapped():
    assert "pro" in TIER_SOFT_CAPS
    assert TIER_SOFT_CAPS["pro"] is None


def test_free_tier_still_capped():
    # The consolidation must not loosen the free tier.
    assert "free" not in PAID_TIERS
    assert "free" not in MemoryClient._PAID_ONLY_TIERS
    assert TIER_SOFT_CAPS["free"] == 5 * 1024 * 1024


def test_paid_sets_agree_everywhere():
    # The three tier surfaces must never drift from each other again.
    assert PAID_TIERS == MemoryClient._PAID_ONLY_TIERS
    lint_paid = {t for t, cap in TIER_SOFT_CAPS.items() if cap is None}
    assert lint_paid == set(PAID_TIERS)
