"""Shown-tier fix (2026-10-08): provider.health()["tier"] (what `sibyl health` prints)
is the tier of the current verified pass; the saved credentials tier only
without a valid pass. Never two tiers between `sibyl health` and `sibyl status`."""
from __future__ import annotations

from pathlib import Path

from sibyl_memory_client.testing import write_credentials, write_entitlement

from sibyl_memory_hermes import SibylMemoryProvider


def test_health_tier_is_the_pass_tier_not_the_saved_one(tmp_path: Path, test_signer) -> None:
    d = tmp_path / "sm"
    cred = write_credentials(d, tier="free")                  # signed in on Free
    write_entitlement(d, test_signer.mint(tier="pro", cap=None, src="subscription"))   # bought Pro
    p = SibylMemoryProvider(db_path=str(tmp_path / "m.db"), credentials_path=cred)
    assert p.health()["tier"] == "pro"


def test_health_tier_free_after_pro_ended(tmp_path: Path, test_signer) -> None:
    d = tmp_path / "sm"
    cred = write_credentials(d, tier="pro")
    write_entitlement(d, test_signer.mint(tier="free"))
    p = SibylMemoryProvider(db_path=str(tmp_path / "m.db"), credentials_path=cred)
    assert p.health()["tier"] == "free"


def test_health_tier_falls_back_to_saved_without_a_pass(tmp_path: Path, monkeypatch) -> None:
    from sibyl_memory_client import _entitlement as E
    monkeypatch.setattr(E, "http_refresh", lambda a, s: E.RefreshOutcome("transient", code="offline"))
    d = tmp_path / "sm"
    cred = write_credentials(d, tier="pro")                   # no entitlement.json, offline
    p = SibylMemoryProvider(db_path=str(tmp_path / "m.db"), credentials_path=cred)
    assert p.health()["tier"] == "pro"
