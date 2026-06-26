"""Tests for the v0.3.0 hard-cap enforcement.

Three concerns covered:
  1. Free-tier writes are blocked once the DB crosses 2 MB
  2. The server check fires at the boundary and updates the local tier cache
  3. The 7-day grace cache works (paid → uncapped writes without phoning home)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    MemoryClient,
    TierCache,
    TierCacheEntry,
    TierVerificationError,
)


# ----------------------------------------------------------------------
# Fake check-write transport: lets us simulate server responses without
# hitting the network
# ----------------------------------------------------------------------

class FakeServer:
    """Mocks the /api/plugin/check-write endpoint."""

    def __init__(self, *, tier: str = "free", offline: bool = False) -> None:
        self.tier = tier
        self.offline = offline
        self.calls: list[dict] = []

    def __call__(self, url, payload, timeout=4.0):
        if self.offline:
            raise TierVerificationError("simulated network down")
        self.calls.append(payload)
        # Paid tier → unconditional ok
        if self.tier in ("sync", "team", "lifetime", "stake", "enterprise"):
            return {"ok": True, "tier": self.tier, "cap_bytes": None}
        # Free tier → check size
        new = payload["current_size_bytes"] + payload["proposed_delta_bytes"]
        cap = 2 * 1024 * 1024
        if new <= cap:
            return {"ok": True, "tier": "free", "cap_bytes": cap,
                    "remaining_bytes": cap - new}
        return {
            "ok": False, "tier": "free", "cap_bytes": cap,
            "upgrade_url": "https://docs.sibyllabs.org/memory/tiers",
        }


# ----------------------------------------------------------------------
# Direct CapGate tests
# ----------------------------------------------------------------------

def test_under_cap_no_server_call(tmp_path: Path) -> None:
    server = FakeServer(tier="free")
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 100_000,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=1000)
    assert len(server.calls) == 0  # didn't phone home


def test_at_cap_server_says_no(tmp_path: Path) -> None:
    server = FakeServer(tier="free")
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 2 * 1024 * 1024 - 100,  # 100 bytes below cap
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    with pytest.raises(CapExceededError) as exc:
        gate.check(proposed_delta_bytes=500)  # would push past cap
    assert exc.value.cap == 2 * 1024 * 1024
    assert "sibyllabs.org" in exc.value.upgrade_url
    assert len(server.calls) == 1  # one boundary check


def test_at_cap_server_upgrades_user(tmp_path: Path) -> None:
    """User claims free in credentials but server says they're now paid
    (upgraded since last activation). The write should be permitted."""
    server = FakeServer(tier="lifetime")
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 2 * 1024 * 1024 + 1000,  # past free cap
        local_tier_hint="free",  # cached credentials say free
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=500)
    # No exception: server told us we're paid
    # Verify cache was updated
    cached = cache.load()
    assert cached is not None
    assert cached.tier == "lifetime"
    assert cached.cap_bytes is None  # paid = no cap


def test_paid_cache_skips_server(tmp_path: Path) -> None:
    """If we have a fresh cache saying we're paid, no server call needed."""
    server = FakeServer(tier="free")  # would say no if called
    cache = TierCache(tmp_path / "tc.json")
    # Pre-populate cache as paid
    cache.store(TierCacheEntry(
        account_id="acc-1",
        tier="lifetime",
        checked_at=time.time(),
        cap_bytes=None,
    ))
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 100 * 1024 * 1024,  # 100 MB: way past free cap
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=10_000)
    assert len(server.calls) == 0  # cache short-circuited


def test_stale_paid_cache_triggers_refresh(tmp_path: Path) -> None:
    """An 8-day-old cache should NOT be honored as fresh."""
    server = FakeServer(tier="lifetime")
    cache = TierCache(tmp_path / "tc.json")
    # Pre-populate cache as paid, but 8 days old
    cache.store(TierCacheEntry(
        account_id="acc-1",
        tier="lifetime",
        checked_at=time.time() - 8 * 24 * 60 * 60,  # 8 days ago
        cap_bytes=None,
    ))
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 5 * 1024 * 1024,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=10_000)
    # Stale cache, so server WAS called
    assert len(server.calls) == 1


def test_offline_at_cap_with_recent_paid_cache(tmp_path: Path) -> None:
    """Honest paid user goes offline. Should still be allowed to write."""
    server = FakeServer(offline=True)
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1",
        tier="lifetime",
        checked_at=time.time(),
        cap_bytes=None,
    ))
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 50 * 1024 * 1024,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    # No exception: cache is fresh and says paid
    gate.check(proposed_delta_bytes=10_000)


def test_offline_at_cap_no_cache_under_free_cap_allows(tmp_path: Path) -> None:
    """CAP-4/CORE-1: a no-cache / never-paid account with unreachable
    verification keeps working as long as it is UNDER the free cap. The outage
    must not block honest free-tier writes that are within the cap."""
    from sibyl_memory_client._capcheck import FREE_TIER_CAP_BYTES
    server = FakeServer(offline=True)
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: FREE_TIER_CAP_BYTES - 50_000,  # comfortably under cap
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=500)  # no exception: under the free cap


def test_offline_no_cache_no_paid_grant_fails_closed_at_free_cap(tmp_path: Path) -> None:
    """CAP-4 + CORE-1 (2026-06-25 pre-launch audit): a no-cache account that
    never had a verified paid grant must FAIL CLOSED at the 2 MB free cap when
    verification is unreachable — NOT fail open to 4x. This is the headline
    revenue fix: blackholing api.sibyllabs.org previously let a free user grow
    to 8 MB write-after-write. The over-cap state is surfaced as a raised
    CapExceededError (not just a logger.warning), and the cap on the error is
    the FREE cap, proving we did not allow the 4x ceiling."""
    from sibyl_memory_client._capcheck import FREE_TIER_CAP_BYTES
    server = FakeServer(offline=True)
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        # Just over the free cap but WELL under the old 4x fail-open ceiling:
        # the old code allowed this; CAP-4 must reject it.
        db_size_fn=lambda: FREE_TIER_CAP_BYTES + 100,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    with pytest.raises(CapExceededError) as exc:
        gate.check(proposed_delta_bytes=500)
    assert exc.value.cap == FREE_TIER_CAP_BYTES  # free cap, not 4x ceiling


def test_offline_no_cache_past_ceiling_blocks(tmp_path: Path) -> None:
    """Fail-open is bounded: past the 4x safety ceiling, an offline no-cache
    write hard-blocks (CapExceededError) so the concession can't be abused by a
    permanently-offline free user."""
    from sibyl_memory_client._capcheck import FAIL_OPEN_CEILING_MULT, FREE_TIER_CAP_BYTES
    ceiling = FREE_TIER_CAP_BYTES * FAIL_OPEN_CEILING_MULT
    server = FakeServer(offline=True)
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: ceiling + 1024,  # already past the fail-open ceiling
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    with pytest.raises(CapExceededError):
        gate.check(proposed_delta_bytes=500)


def test_no_account_id_under_cap_passes(tmp_path: Path) -> None:
    """Pre-activation user under the cap should work."""
    server = FakeServer(tier="free")
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id=None,
        session_token=None,
        db_size_fn=lambda: 1_000_000,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    gate.check(proposed_delta_bytes=1000)
    assert len(server.calls) == 0


def test_no_account_id_at_cap_blocks(tmp_path: Path) -> None:
    """Pre-activation user past the cap → hard block."""
    server = FakeServer(tier="free")
    cache = TierCache(tmp_path / "tc.json")
    gate = CapGate(
        account_id=None,
        session_token=None,
        db_size_fn=lambda: 2 * 1024 * 1024 + 100,
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    with pytest.raises(CapExceededError):
        gate.check(proposed_delta_bytes=500)


# ----------------------------------------------------------------------
# End-to-end test through MemoryClient
# ----------------------------------------------------------------------

def test_e2e_free_tier_blocked_at_cap(tmp_path: Path) -> None:
    """Writing past the 2 MB cap raises CapExceededError when the server
    confirms free tier."""
    server = FakeServer(tier="free")
    cache = TierCache(tmp_path / "tc.json")
    db_path = tmp_path / "memory.db"

    # Build a custom gate using a synthetic large db_size to skip the slow
    # path of actually writing 2 MB of data.
    from sibyl_memory_client._capcheck import CapGate
    fake_size = [100]  # mutable, lets us simulate growth

    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: fake_size[0],
        local_tier_hint="free",
        cache=cache,
        check_fn=server,
    )
    client = MemoryClient(
        storage=__import__("sibyl_memory_client").Storage(str(db_path)),
        tenant_id="alice",
        tier="free",
        account_id="acc-1",
        session_token="sess-1",
        cap_gate=gate,
    )

    # Under the cap: works fine
    client.set_entity("project", "atlas", {"status": "active"})

    # Simulate being near the cap
    fake_size[0] = 2 * 1024 * 1024 - 100

    # Next write would push over → server-checked → blocked
    with pytest.raises(CapExceededError):
        client.set_entity("project", "borealis", {"status": "active", "x": "y" * 500})

    # Server was consulted
    assert len(server.calls) >= 1


def test_e2e_paid_tier_no_cap(tmp_path: Path) -> None:
    """Paid tier bypasses the cap entirely (within grace period)."""
    server = FakeServer(tier="lifetime")
    cache = TierCache(tmp_path / "tc.json")
    db_path = tmp_path / "memory.db"

    fake_size = [50 * 1024 * 1024]  # 50 MB: way past free cap

    from sibyl_memory_client._capcheck import CapGate
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: fake_size[0],
        local_tier_hint="lifetime",
        cache=cache,
        check_fn=server,
    )
    client = MemoryClient(
        storage=__import__("sibyl_memory_client").Storage(str(db_path)),
        tenant_id="alice",
        tier="lifetime",
        account_id="acc-1",
        session_token="sess-1",
        cap_gate=gate,
    )
    # Writes succeed even though we're 50 MB in
    client.set_entity("project", "atlas", {"status": "active"})
    client.set_entity("project", "borealis", {"status": "active"})
    client.set_state("priorities", {"top": ["ship"]})


def test_cache_file_is_0600(tmp_path: Path) -> None:
    """Tier cache must not be world-readable."""
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1",
        tier="free",
        checked_at=time.time(),
        cap_bytes=2 * 1024 * 1024,
    ))
    mode = oct((tmp_path / "tc.json").stat().st_mode)[-3:]
    assert mode == "600"


def test_cap_gate_invalidate_cache(tmp_path: Path) -> None:
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="free", checked_at=time.time(), cap_bytes=2_000_000,
    ))
    assert cache.load() is not None
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 0,
        local_tier_hint="free",
        cache=cache,
        check_fn=FakeServer(),
    )
    gate.invalidate_cache()
    assert cache.load() is None
