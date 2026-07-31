"""Adversarial hardening tests for the async cap-verification write path (v0.4.20).

RED-TEAM intent: these tests ATTEMPT each documented exploit against
``CapGate.check_async`` / ``_schedule_refresh`` and the write path that now
relies on them. Each test asserts the SAFE outcome, so a GREEN test means the
attack was correctly defended. Three of them are regression guards for real
vulnerabilities found and fixed on this branch (marked REGRESSION):

  * #4  stale / expired PAID cache -> check_total_local was UNBOUNDED (uncapped).
  * #5  archive_entity lost its in-transaction gate -> committed footprint grew
        past the cap.
  * #6  a booby-trapped tier_cache.json (non-numeric cap_bytes / expiry) made
        check_async raise TypeError on the hot write path (contract: never raises).

Synchronization is deterministic (Events + join-with-timeout), never
sleep-and-hope.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    MemoryClient,
    Storage,
    TierCache,
    TierCacheEntry,
)
from sibyl_memory_client._capcheck import (
    FAIL_OPEN_CEILING_MULT,
    FREE_TIER_CAP_BYTES,
    GRACE_PERIOD_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    REFRESH_MIN_INTERVAL_SECONDS,
    TierAuthError,
    TierVerificationError,
)
from sibyl_memory_client.storage import db_size_bytes

CAP = FREE_TIER_CAP_BYTES
CALLER_RETURN_BUDGET = 1.0
assert CALLER_RETURN_BUDGET < HTTP_TIMEOUT_SECONDS


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class Server:
    """Injectable check_fn recording thread + call, optionally blocking/raising."""

    def __init__(self, *, tier="lifetime", block=False, raise_exc=None):
        self._tier = tier
        self._block = block
        self._raise = raise_exc
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.threads: list[int] = []
        self.called = threading.Event()
        self.release = threading.Event()

    def __call__(self, url, payload, timeout=HTTP_TIMEOUT_SECONDS):
        with self._lock:
            self.calls.append(payload)
            self.threads.append(threading.get_ident())
        self.called.set()
        if self._block:
            self.release.wait(timeout=10.0)
        if self._raise is not None:
            raise self._raise
        if self._tier in ("sync", "team", "lifetime", "stake", "enterprise"):
            return {"ok": True, "tier": self._tier, "cap_bytes": None}
        new = payload["current_size_bytes"] + payload["proposed_delta_bytes"]
        if new <= CAP:
            return {"ok": True, "tier": "free", "cap_bytes": CAP}
        return {"ok": False, "tier": "free", "cap_bytes": CAP}


def _offline(url, payload, timeout=HTTP_TIMEOUT_SECONDS):
    raise TierVerificationError("offline")


def _gate(tmp_path, *, db_size, check_fn, tier_hint="free",
          account_id="acc-1", cache=None, cap_bytes=CAP, cache_name="tc.json"):
    return CapGate(
        account_id=account_id,
        session_token="s" if account_id else None,
        db_size_fn=(db_size if callable(db_size) else (lambda: db_size)),
        local_tier_hint=tier_hint,
        cache=cache if cache is not None else TierCache(tmp_path / cache_name),
        check_fn=check_fn,
        cap_bytes=cap_bytes,
    )


def _client_at_cap(tmp_path, *, cap_slack=60 * 1024, check_fn=_offline):
    """Build a real MemoryClient over a real DB with a tiny cap, filled with
    real writes until CapExceededError. Returns (client, db_path, cap)."""
    db_path = tmp_path / "memory.db"
    storage = Storage(str(db_path))
    baseline = db_size_bytes(db_path)
    cap = baseline + cap_slack
    gate = CapGate(
        account_id="acc-1", session_token="s",
        db_size_fn=lambda: db_size_bytes(db_path),
        local_tier_hint="free", cache=TierCache(tmp_path / "tc.json"),
        check_fn=check_fn, cap_bytes=cap,
    )
    client = MemoryClient(storage=storage, tenant_id="alice", tier="free",
                          account_id="acc-1", session_token="s", cap_gate=gate)
    payload = "y" * 3000
    n = 0
    try:
        for i in range(2000):
            client.set_entity("project", f"row-{i}", {"x": payload})
            n += 1
    except CapExceededError:
        pass
    return client, db_path, cap, n


# ======================================================================
# ATTACK 1 — cap bypass via the async window (real growth + concurrency)
# ======================================================================
def test_attack1_serial_writes_never_commit_past_cap(tmp_path: Path) -> None:
    """Drive real DB growth to the boundary; the committed footprint must never
    exceed the cap. The over-cap write is rolled back by check_total_local with
    NO network (offline transport)."""
    client, db_path, cap, n = _client_at_cap(tmp_path)
    assert n >= 1, "no write committed before the cap was hit"
    # The committed footprint stayed within the cap (+ <=2 SQLite pages slack).
    assert db_size_bytes(db_path) <= cap + 8192, db_size_bytes(db_path)


def test_attack1_concurrent_writes_never_commit_past_cap(tmp_path: Path) -> None:
    """Hammer set_entity from many threads at the boundary. SQLite serializes
    writers (BEGIN IMMEDIATE); every commit's check_total_local sees the true
    cumulative footprint, so no interleaving can commit past the cap."""
    db_path = tmp_path / "memory.db"
    storage = Storage(str(db_path))
    baseline = db_size_bytes(db_path)
    cap = baseline + 80 * 1024
    gate = CapGate(
        account_id="acc-1", session_token="s",
        db_size_fn=lambda: db_size_bytes(db_path),
        local_tier_hint="free", cache=TierCache(tmp_path / "tc.json"),
        check_fn=_offline, cap_bytes=cap,
    )
    client = MemoryClient(storage=storage, tenant_id="alice", tier="free",
                          account_id="acc-1", session_token="s", cap_gate=gate)
    payload = "z" * 2500
    start = threading.Event()
    over = []  # any committed sample above cap+slack is a bypass
    lock = threading.Lock()

    def worker(wid: int) -> None:
        start.wait(5.0)
        for i in range(60):
            try:
                client.set_entity("t", f"w{wid}-{i}", {"x": payload})
            except CapExceededError:
                pass
            except Exception:
                # sqlite "database is locked" under contention is fine: it did
                # not commit. Only committed footprint matters.
                pass
            size = db_size_bytes(db_path)
            if size > cap + 8192:
                with lock:
                    over.append(size)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=30.0)
        assert not t.is_alive()
    assert not over, f"committed footprint exceeded the cap: {over[:3]} (cap={cap})"
    assert db_size_bytes(db_path) <= cap + 8192


# ======================================================================
# ATTACK 2 — single-flight / rate-limit / thread-exhaustion
# ======================================================================
def test_attack2_burst_spawns_at_most_one_refresh_thread(tmp_path: Path) -> None:
    """A big concurrent burst of at-cap check_async spawns ONE background
    refresh, not N. Live capgate threads never exceed 1 (no thread-per-call
    exhaustion DoS)."""
    server = Server(tier="lifetime", block=True)
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server)

    def capgate_threads_alive() -> int:
        return sum(1 for t in threading.enumerate()
                   if t.name == "sibyl-capgate-refresh" and t.is_alive())

    start = threading.Event()
    peak = [0]
    peak_lock = threading.Lock()

    def worker() -> None:
        start.wait(5.0)
        for _ in range(20):
            gate.check_async(proposed_delta_bytes=500)
            with peak_lock:
                peak[0] = max(peak[0], capgate_threads_alive())

    workers = [threading.Thread(target=worker) for _ in range(24)]
    for w in workers:
        w.start()
    start.set()
    for w in workers:
        w.join(timeout=10.0)
        assert not w.is_alive(), "a check_async caller blocked (not single-flight-safe)"

    assert server.called.wait(timeout=5.0)
    assert len(server.calls) == 1, server.calls          # single-flight
    assert peak[0] <= 1, f"more than one refresh thread alive at once: {peak[0]}"

    server.release.set()
    prev = gate._refresh_thread
    assert prev is not None
    prev.join(timeout=5.0)
    assert not prev.is_alive()

    # Immediate follow-up within the interval is rate-limited: no new thread/call.
    gate.check_async(proposed_delta_bytes=500)
    assert gate._refresh_thread is prev
    assert len(server.calls) == 1


def test_attack2_no_toctou_on_inflight_flag(tmp_path: Path) -> None:
    """Concurrent schedulers cannot both observe in_flight=False and both start
    a refresh: the flag is set under the lock before the thread starts."""
    server = Server(tier="lifetime", block=True)
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server)
    ready = threading.Barrier(16)

    def racer() -> None:
        ready.wait(timeout=5.0)
        gate.check_async(proposed_delta_bytes=1)

    ts = [threading.Thread(target=racer) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5.0)
    assert server.called.wait(timeout=5.0)
    assert len(server.calls) == 1  # exactly one refresh won the race
    server.release.set()
    if gate._refresh_thread:
        gate._refresh_thread.join(timeout=5.0)


# ======================================================================
# ATTACK 3 — forged / expired token cannot exploit async timing
# ======================================================================
def test_attack3_forged_paid_hint_401_still_blocked_at_cap(tmp_path: Path) -> None:
    """A forged 'paid' credentials hint with a 401 (TierAuthError) transport and
    no valid cache: the local gate still enforces the free cap on an at-cap
    account. Async cannot let a forged token write past the cap."""
    server = Server(raise_exc=TierAuthError("HTTP 401"))
    cache = TierCache(tmp_path / "tc.json")
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server,
                 tier_hint="lifetime", cache=cache)  # forged paid hint

    # Async pre-check schedules a refresh that 401s; it must NOT cache a grant.
    gate.check_async(proposed_delta_bytes=100)
    if gate._refresh_thread:
        gate._refresh_thread.join(timeout=5.0)
    cached = cache.load()
    assert cached is None or cached.cap_bytes is not None, "401 must never cache a paid grant"

    # Authoritative local gate blocks at the FREE cap regardless of the hint.
    with pytest.raises(CapExceededError) as exc:
        gate.check_total_local(CAP + 4096)
    assert exc.value.cap == CAP


def test_attack3_real_client_forged_hint_blocked(tmp_path: Path) -> None:
    """End-to-end: a real client with a forged paid tier hint and a 401 server
    cannot write past the cap."""
    db_path = tmp_path / "memory.db"
    storage = Storage(str(db_path))
    baseline = db_size_bytes(db_path)
    cap = baseline + 40 * 1024
    server = Server(raise_exc=TierAuthError("HTTP 401"))
    gate = CapGate(
        account_id="acc-1", session_token="s",
        db_size_fn=lambda: db_size_bytes(db_path),
        local_tier_hint="lifetime",  # forged
        cache=TierCache(tmp_path / "tc.json"), check_fn=server, cap_bytes=cap,
    )
    client = MemoryClient(storage=storage, tenant_id="a", tier="lifetime",
                          account_id="acc-1", session_token="s", cap_gate=gate)
    payload = "y" * 3000
    with pytest.raises(CapExceededError):
        for i in range(500):
            client.set_entity("p", f"r{i}", {"x": payload})
    assert db_size_bytes(db_path) <= cap + 8192


# ======================================================================
# ATTACK 4 — previously-paid fail-open stays BOUNDED  (REGRESSION)
# ======================================================================
def test_attack4_stale_paid_cache_bounded_by_ceiling(tmp_path: Path) -> None:
    """REGRESSION: a STALE paid cache (grace elapsed) must NOT be treated as
    uncapped by the local in-transaction gate. Before the fix, check_total_local
    returned None (uncapped) for any account-matched paid cache, so a
    previously-paid account could write UNBOUNDED offline. It is now bounded to
    the 4x fail-open ceiling and hard-blocks past it."""
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="lifetime",
        checked_at=time.time() - 30 * GRACE_PERIOD_SECONDS,  # very stale
        cap_bytes=None,
    ))
    gate = _gate(tmp_path, db_size=0, check_fn=_offline, cache=cache)
    ceiling = CAP * FAIL_OPEN_CEILING_MULT
    # Under the ceiling: allowed (durability concession for a once-paid account).
    gate.check_total_local(ceiling - 1)
    # Past the ceiling: HARD BLOCK (no more unbounded).
    with pytest.raises(CapExceededError):
        gate.check_total_local(ceiling + 1)
    with pytest.raises(CapExceededError):
        gate.check_total_local(CAP * 1000)


def test_attack4_expired_subscription_enforces_free_cap(tmp_path: Path) -> None:
    """REGRESSION: a paid cache whose server-supplied subscription expiry has
    passed enforces the FREE cap locally (no fail-open concession for a
    genuinely ended subscription)."""
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="lifetime",
        checked_at=time.time() - 30 * GRACE_PERIOD_SECONDS,
        cap_bytes=None,
        server_expires_at=time.time() - GRACE_PERIOD_SECONDS,  # subscription ended
    ))
    gate = _gate(tmp_path, db_size=0, check_fn=_offline, cache=cache)
    gate.check_total_local(CAP - 1)  # under free cap: fine
    with pytest.raises(CapExceededError) as exc:
        gate.check_total_local(CAP * FAIL_OPEN_CEILING_MULT)  # would pass 4x, but expired
    assert exc.value.cap == CAP


def test_attack4_fresh_paid_cache_still_uncapped(tmp_path: Path) -> None:
    """Guard against over-correction: a FRESH account-matched paid grant is
    still uncapped locally (honest paid users are not broken)."""
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    gate = _gate(tmp_path, db_size=0, check_fn=_offline, cache=cache)
    gate.check_total_local(CAP * 1000)  # must NOT raise


# ======================================================================
# ATTACK 5 — archive_entity cannot grow committed footprint past cap  (REGRESSION)
# ======================================================================
def test_attack5_archive_cannot_grow_footprint_past_cap(tmp_path: Path) -> None:
    """REGRESSION: archiving is NOT footprint-neutral (SQLite doesn't reclaim
    freed pages on DELETE). Before the fix archive_entity had no in-transaction
    gate and grew the committed footprint past the cap. Now every archive re-runs
    check_total_local; the committed footprint never exceeds the cap."""
    client, db_path, cap, n = _client_at_cap(tmp_path)
    assert n >= 1
    peak = db_size_bytes(db_path)
    for i in range(n):
        try:
            client.archive_entity("project", f"row-{i}")
        except CapExceededError:
            pass  # archive that would tip over the cap is correctly rejected
        except Exception:
            pass
        peak = max(peak, db_size_bytes(db_path))
    assert peak <= cap + 8192, f"archive grew committed footprint to {peak} (cap={cap})"


def test_attack5_archive_under_cap_still_works(tmp_path: Path) -> None:
    """Guard: archiving well under the cap still succeeds (fix is not a blanket
    block)."""
    client = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    client.set_entity("notes", "bye", {"text": "small"})
    res = client.archive_entity("notes", "bye", reason="cleanup")
    assert res["archived_id"]


# ======================================================================
# ATTACK 6 — check_async must NEVER raise  (REGRESSION + fuzz)
# ======================================================================
_BOOBY_TRAPPED_CACHES = [
    {"account_id": "acc-1", "tier": "free", "checked_at": time.time(), "cap_bytes": "not-a-number"},
    {"account_id": "acc-1", "tier": "free", "checked_at": time.time(), "cap_bytes": [1, 2, 3]},
    {"account_id": "acc-1", "tier": "free", "checked_at": time.time(), "cap_bytes": {"x": 1}},
    {"account_id": "acc-1", "tier": "lifetime", "checked_at": time.time(), "cap_bytes": None,
     "server_expires_at": [9, 9]},
    {"account_id": "acc-1", "tier": "free", "checked_at": "garbage", "cap_bytes": 100},
    {"account_id": "acc-1", "tier": "free", "checked_at": time.time(), "cap_bytes": True},
    {"account_id": "acc-1", "tier": "free", "checked_at": time.time(), "cap_bytes": float("nan")},
]


@pytest.mark.parametrize("blob", _BOOBY_TRAPPED_CACHES)
def test_attack6_check_async_never_raises_on_corrupt_cache(tmp_path: Path, blob: dict) -> None:
    """REGRESSION: a booby-trapped tier_cache.json must not make check_async
    raise. Before the fix a non-numeric cap_bytes surfaced as a TypeError from
    the '<=' comparison, crashing every write on the hot path."""
    p = tmp_path / "tc.json"
    p.write_text(json.dumps(blob), encoding="utf-8")
    gate = _gate(tmp_path, db_size=1000, check_fn=_offline, cache=TierCache(p))
    gate.check_async(proposed_delta_bytes=100)  # must not raise
    # check() and check_total_local must also tolerate it (never TypeError). A
    # domain-level CapExceededError is a legitimate decision (e.g. a value that
    # coerces to a tiny cap); only a TypeError / crash would be the bug.
    try:
        gate.check(proposed_delta_bytes=100)
    except (CapExceededError, TierAuthError, TierVerificationError):
        pass
    try:
        gate.check_total_local(1000)
    except CapExceededError:
        pass
    if gate._refresh_thread:
        gate._refresh_thread.join(timeout=5.0)


def test_attack6_check_async_never_raises_on_non_json_cache(tmp_path: Path) -> None:
    p = tmp_path / "tc.json"
    p.write_bytes(b"\x00\x01 not json at all \xff")
    gate = _gate(tmp_path, db_size=1000, check_fn=_offline, cache=TierCache(p))
    gate.check_async(proposed_delta_bytes=100)


@pytest.mark.parametrize("delta", [-10**9, -1, 0, 1, 10**12, 2**63])
def test_attack6_check_async_never_raises_on_odd_deltas(tmp_path: Path, delta: int) -> None:
    server = Server(tier="lifetime")
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server)
    gate.check_async(proposed_delta_bytes=delta)  # must not raise
    if gate._refresh_thread:
        gate._refresh_thread.join(timeout=5.0)


def test_attack6_check_async_never_raises_without_account(tmp_path: Path) -> None:
    """Missing account/session (pre-activation) over cap: check_async returns and
    never raises. It may schedule a background refresh, but that refresh makes NO
    network call (no credentials to verify) and swallows its own block."""
    server = Server(tier="free")
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server, account_id=None)
    gate.check_async(proposed_delta_bytes=100)  # must not raise
    if gate._refresh_thread:
        gate._refresh_thread.join(timeout=5.0)
    assert server.calls == []  # never phoned home without credentials


def test_attack6_background_thread_never_leaves_unhandled_exception(tmp_path: Path) -> None:
    """Any exception raised inside the background refresh (auth/verify/unexpected)
    is swallowed; the daemon thread exits cleanly and threading.excepthook never
    fires an unhandled-thread-exception."""
    captured: list = []
    orig_hook = threading.excepthook
    threading.excepthook = lambda args: captured.append(args)
    try:
        for exc in (TierAuthError("nope"), TierVerificationError("down"),
                    RuntimeError("boom"), KeyError("weird"), ValueError("odd")):
            server = Server(raise_exc=exc)
            gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server,
                         cache_name=f"tc-{type(exc).__name__}-{id(exc)}.json")
            gate.check_async(proposed_delta_bytes=500)  # never raises on caller thread
            assert gate._refresh_thread is not None
            gate._refresh_thread.join(timeout=5.0)
            assert not gate._refresh_thread.is_alive()
    finally:
        threading.excepthook = orig_hook
    assert captured == [], f"background thread leaked an unhandled exception: {captured}"


def test_attack6_thread_start_failure_clears_inflight(tmp_path: Path, monkeypatch) -> None:
    """If the daemon thread cannot start (resource limits), check_async still
    does not raise and the in-flight flag is cleared so a later write retries."""
    server = Server(tier="lifetime")
    gate = _gate(tmp_path, db_size=CAP + 4096, check_fn=server)

    def boom_start(self):  # simulate RuntimeError: can't start new thread
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom_start)
    gate.check_async(proposed_delta_bytes=100)  # must not raise
    assert gate._refresh_in_flight is False  # cleared for a later retry
