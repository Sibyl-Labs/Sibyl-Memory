"""Tests for async (non-blocking) cap verification on the write path (v0.4.20).

The write path must never make a blocking network call. ``CapGate.check_async``
runs the SAME purely-local fast paths as ``check`` but, at the point where
``check`` would urlopen (``_refresh_and_check``), it schedules an opportunistic
BACKGROUND refresh and returns immediately without raising. Authoritative local
enforcement is left to the in-transaction ``check_total_local`` (no network).

Invariants proven here:
  1. A write at the cap boundary returns immediately and never calls the
     injected ``check_fn`` on the CALLER's thread (off-thread, non-blocking).
  2. A free account over the free cap still raises CapExceededError with NO
     network — via ``check_total_local`` (the authoritative local gate).
  3. Single-flight + rate-limited: a burst of concurrent at-cap ``check_async``
     calls starts at most ONE background refresh; a follow-up within
     ``REFRESH_MIN_INTERVAL_SECONDS`` of the last one finishing starts none.
  4. Upgrade convergence: after a background refresh writes a paid result into
     the cache, the next write is allowed by the same local gate.
  5. Background exceptions (CapExceededError / TierAuthError / anything) are
     swallowed and never surface to the caller; the daemon thread exits cleanly.

Synchronization is deterministic (Events + join-with-timeout), never sleep-and-
hope, so the tests are not flaky.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    TierCache,
    TierCacheEntry,
)
from sibyl_memory_client._capcheck import (
    FREE_TIER_CAP_BYTES,
    HTTP_TIMEOUT_SECONDS,
    REFRESH_MIN_INTERVAL_SECONDS,
    TierAuthError,
    TierVerificationError,
)

CAP = FREE_TIER_CAP_BYTES  # 2 MB
OVER_CAP = CAP + 4096       # comfortably past the free cap
CALLER_RETURN_BUDGET = 1.0  # check_async must return in << HTTP_TIMEOUT_SECONDS
assert CALLER_RETURN_BUDGET < HTTP_TIMEOUT_SECONDS  # sanity: it IS "well under"


class RecordingServer:
    """An injectable ``check_fn`` that records how it was called so a test can
    assert the call happened OFF the caller's thread and (optionally) block or
    raise to control the background refresh deterministically.

    Args:
        tier: tier to report on a successful (non-raising) response.
        block: if True, the call blocks on ``release`` until the test sets it,
            so the background refresh can be caught mid-flight.
        raise_exc: if set, the call raises this exception instead of returning.
    """

    def __init__(self, *, tier: str = "lifetime", block: bool = False,
                 raise_exc: BaseException | None = None) -> None:
        self._tier = tier
        self._block = block
        self._raise_exc = raise_exc
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.threads: list[int] = []
        self.called = threading.Event()   # set the instant the call is entered
        self.release = threading.Event()   # gate the return when block=True

    def __call__(self, url, payload, timeout: float = HTTP_TIMEOUT_SECONDS):
        with self._lock:
            self.calls.append(payload)
            self.threads.append(threading.get_ident())
        self.called.set()
        if self._block:
            # Bounded so a buggy test can't hang the suite forever.
            self.release.wait(timeout=10.0)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._tier in ("sync", "team", "lifetime", "stake", "enterprise"):
            return {"ok": True, "tier": self._tier, "cap_bytes": None}
        new = payload["current_size_bytes"] + payload["proposed_delta_bytes"]
        if new <= CAP:
            return {"ok": True, "tier": "free", "cap_bytes": CAP}
        return {"ok": False, "tier": "free", "cap_bytes": CAP,
                "upgrade_url": "https://docs.sibyllabs.org/memory/tiers"}


def _gate(tmp_path: Path, *, db_size: int, server: RecordingServer,
          tier_hint: str = "free", account_id: str | None = "acc-1",
          cache: TierCache | None = None, cache_name: str = "tc.json") -> CapGate:
    return CapGate(
        account_id=account_id,
        session_token="sess-1" if account_id else None,
        db_size_fn=lambda: db_size,
        local_tier_hint=tier_hint,
        cache=cache if cache is not None else TierCache(tmp_path / cache_name),
        check_fn=server,
    )


# ----------------------------------------------------------------------
# (1) Boundary write returns immediately and never blocks on the caller thread
# ----------------------------------------------------------------------

def test_check_async_at_boundary_returns_without_blocking_on_network(tmp_path: Path) -> None:
    caller = threading.get_ident()
    server = RecordingServer(tier="lifetime", block=True)  # hangs until released
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)

    t0 = time.monotonic()
    gate.check_async(proposed_delta_bytes=500)   # MUST NOT block on the hung server
    elapsed = time.monotonic() - t0

    # Returned near-instantly — well under the 4 s network timeout (and the
    # server is currently blocked for up to 10 s), proving no synchronous call.
    assert elapsed < CALLER_RETURN_BUDGET, elapsed

    # The refresh runs, but on a DIFFERENT (background daemon) thread.
    assert server.called.wait(timeout=5.0)
    assert server.threads, "check_fn was never invoked"
    assert all(t != caller for t in server.threads), "check_fn ran on the caller thread"

    server.release.set()
    assert gate._refresh_thread is not None
    gate._refresh_thread.join(timeout=5.0)
    assert not gate._refresh_thread.is_alive()


# ----------------------------------------------------------------------
# (2) Free over cap still hard-blocks locally, with NO network
# ----------------------------------------------------------------------

def test_free_over_cap_raises_locally_with_no_network(tmp_path: Path) -> None:
    server = RecordingServer(tier="free")
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)

    # The authoritative in-transaction gate is LOCAL-ONLY and blocks the write.
    with pytest.raises(CapExceededError) as exc:
        gate.check_total_local(OVER_CAP)
    assert exc.value.cap == CAP
    # It never phoned home for the authoritative decision.
    assert server.calls == []

    # And the pre-write async check makes no SYNCHRONOUS server call either: it
    # returns immediately (any refresh it schedules is off-thread, best effort).
    t0 = time.monotonic()
    gate.check_async(proposed_delta_bytes=100)
    assert time.monotonic() - t0 < CALLER_RETURN_BUDGET


def test_unactivated_over_cap_still_blocked_locally(tmp_path: Path) -> None:
    """A pre-activation account (account_id=None) over the free cap is blocked
    by the local gate with no network — the async pre-write check cannot even
    schedule a server refresh (no credentials), and check_total_local enforces
    the free cap regardless."""
    server = RecordingServer(tier="free")
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server, account_id=None)

    gate.check_async(proposed_delta_bytes=100)  # never raises, never blocks
    with pytest.raises(CapExceededError) as exc:
        gate.check_total_local(OVER_CAP)
    assert exc.value.cap == CAP
    assert server.calls == []


# ----------------------------------------------------------------------
# (3) Single-flight + rate-limiting
# ----------------------------------------------------------------------

def test_check_async_is_single_flight_then_rate_limited(tmp_path: Path) -> None:
    server = RecordingServer(tier="lifetime", block=True)
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)

    # Fire a burst of concurrent at-cap async checks.
    start = threading.Event()

    def worker() -> None:
        start.wait(5.0)
        gate.check_async(proposed_delta_bytes=500)

    workers = [threading.Thread(target=worker) for _ in range(16)]
    for w in workers:
        w.start()
    start.set()
    for w in workers:
        w.join(timeout=5.0)
        assert not w.is_alive(), "a check_async caller blocked (not single-flight-safe)"

    # Exactly ONE refresh got into flight: the sole background thread has
    # entered the (blocked) server; no other caller spawned a second one.
    assert server.called.wait(timeout=5.0)
    assert len(server.calls) == 1, server.calls

    # Let it finish, then prove the rate limiter blocks an immediate follow-up.
    prev_thread = gate._refresh_thread
    server.release.set()
    assert prev_thread is not None
    prev_thread.join(timeout=5.0)
    assert not prev_thread.is_alive()
    assert len(server.calls) == 1

    # Within REFRESH_MIN_INTERVAL_SECONDS of the last refresh FINISHING: no new
    # refresh is started (same thread object, no new server call).
    assert REFRESH_MIN_INTERVAL_SECONDS >= 1.0
    gate.check_async(proposed_delta_bytes=500)
    assert gate._refresh_thread is prev_thread, "rate limiter did not suppress the refresh"
    assert len(server.calls) == 1


# ----------------------------------------------------------------------
# (4) Upgrade convergence
# ----------------------------------------------------------------------

def test_upgrade_convergence_after_background_refresh(tmp_path: Path) -> None:
    server = RecordingServer(tier="lifetime")  # server reports PAID
    cache = TierCache(tmp_path / "tc.json")
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server, cache=cache)

    # Current write: authoritative LOCAL gate blocks it (no paid cache yet).
    with pytest.raises(CapExceededError):
        gate.check_total_local(OVER_CAP)

    # Pre-write async check schedules a background refresh that learns "paid".
    gate.check_async(proposed_delta_bytes=100)
    assert gate._refresh_thread is not None
    gate._refresh_thread.join(timeout=5.0)
    assert not gate._refresh_thread.is_alive()

    # Cache now reflects the paid grant...
    cached = cache.load()
    assert cached is not None
    assert cached.tier == "lifetime"
    assert cached.cap_bytes is None

    # ...so a SUBSEQUENT write is allowed by the very same local gate (uncapped).
    gate.check_total_local(OVER_CAP)  # must NOT raise
    assert len(server.calls) == 1     # exactly one background verification


def test_revoked_token_convergence_blocks_subsequent_write(tmp_path: Path) -> None:
    """Mirror of upgrade convergence in the other direction: once a background
    refresh observes a 401 (TierAuthError), the account is treated as free and
    the local gate keeps blocking at-cap writes (the cache is not upgraded)."""
    server = RecordingServer(raise_exc=TierAuthError("HTTP 401 refused"))
    cache = TierCache(tmp_path / "tc.json")
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server, cache=cache)

    gate.check_async(proposed_delta_bytes=100)  # schedules a refresh that 401s
    assert gate._refresh_thread is not None
    gate._refresh_thread.join(timeout=5.0)
    assert not gate._refresh_thread.is_alive()

    # No paid grant was cached (auth-denied never upgrades the cache)...
    cached = cache.load()
    assert cached is None or cached.cap_bytes is not None
    # ...so the local gate still enforces the free cap on the next write.
    with pytest.raises(CapExceededError) as exc:
        gate.check_total_local(OVER_CAP)
    assert exc.value.cap == CAP


# ----------------------------------------------------------------------
# (5) Background exceptions are swallowed; the thread exits cleanly
# ----------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    TierAuthError("nope"),                 # authoritative deny → CapExceededError inside refresh
    TierVerificationError("offline"),      # unreachable → CapExceededError inside refresh
    RuntimeError("unexpected transport bug"),
])
def test_background_refresh_swallows_all_exceptions(tmp_path: Path, exc: BaseException) -> None:
    server = RecordingServer(raise_exc=exc)
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)

    # check_async itself never raises, whatever the (eventual) refresh does.
    gate.check_async(proposed_delta_bytes=500)
    assert gate._refresh_thread is not None
    gate._refresh_thread.join(timeout=5.0)
    assert not gate._refresh_thread.is_alive()  # thread exited, did not hang/crash
    assert server.called.is_set()               # it really did run the refresh


# ----------------------------------------------------------------------
# Local fast paths schedule NOTHING (no thread, no call)
# ----------------------------------------------------------------------

def test_under_cap_async_schedules_no_refresh(tmp_path: Path) -> None:
    server = RecordingServer(tier="free")
    gate = _gate(tmp_path, db_size=100_000, server=server)
    gate.check_async(proposed_delta_bytes=1000)
    assert gate._refresh_thread is None
    assert server.calls == []


def test_fresh_paid_cache_async_schedules_no_refresh(tmp_path: Path) -> None:
    server = RecordingServer(tier="free")  # would say NO if ever called
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    gate = _gate(tmp_path, db_size=100 * 1024 * 1024, server=server, cache=cache)
    gate.check_async(proposed_delta_bytes=10_000)
    assert gate._refresh_thread is None
    assert server.calls == []


# ----------------------------------------------------------------------
# No thread leaks; the refresh thread is a daemon
# ----------------------------------------------------------------------

def test_refresh_thread_is_daemon(tmp_path: Path) -> None:
    server = RecordingServer(tier="lifetime", block=True)
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)
    gate.check_async(proposed_delta_bytes=500)
    assert gate._refresh_thread is not None
    assert gate._refresh_thread.daemon is True  # interpreter exit is never blocked
    server.release.set()
    gate._refresh_thread.join(timeout=5.0)
    assert not gate._refresh_thread.is_alive()


# ----------------------------------------------------------------------
# check() (sync, blocking) is preserved — same local decision, still raises
# ----------------------------------------------------------------------

def test_sync_check_still_blocks_and_raises_at_cap(tmp_path: Path) -> None:
    """check() is retained (public API): unlike check_async it DOES consult the
    server synchronously and raises on a free-over-cap result."""
    server = RecordingServer(tier="free")
    gate = _gate(tmp_path, db_size=OVER_CAP, server=server)
    with pytest.raises(CapExceededError):
        gate.check(proposed_delta_bytes=500)
    assert len(server.calls) == 1  # sync check DID phone home (on the caller thread)
