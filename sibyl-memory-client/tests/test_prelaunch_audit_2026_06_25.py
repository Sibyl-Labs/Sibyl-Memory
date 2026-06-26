"""Regression tests for the 2026-06-25 pre-launch security/quality fix pass.

Each test is tagged with the finding ID from
memory/research/plugin-security-audit-2026-06-25.md and PROVES the new behavior.
The cap/tier tests are the priority: they encode the revenue-critical fixes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sibyl_memory_client import (
    CapExceededError,
    CapGate,
    MemoryClient,
    Storage,
    StorageError,
    TenantError,
    TierCache,
    TierCacheEntry,
    TierGateError,
    TierVerificationError,
)
from sibyl_memory_client._capcheck import (
    AUTH_DENY_HTTP_CODES,
    FREE_TIER_CAP_BYTES,
    RETRYABLE_HTTP_CODES,
)
from sibyl_memory_client.exceptions import TierAuthError
from sibyl_memory_client.storage import db_size_bytes


# ======================================================================
# CAP-1 — WAL-inclusive sizing: data that lands in the WAL is counted
# ======================================================================

def test_cap1_wal_resident_writes_are_counted(tmp_path: Path) -> None:
    """A committed write that still lives in memory.db-wal (no checkpoint forced)
    must be reflected in db_size_bytes. Sizing memory.db alone would under-report
    and let a free user write past the cap during a burst."""
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa")
    # First write so the DB + WAL exist.
    c.set_entity("notes", "seed", {"text": "x"})
    baseline = db_size_bytes(db)

    # Write a sizeable payload. Do NOT checkpoint. The bytes land in the WAL.
    big = "y" * 200_000
    for i in range(5):
        c.set_entity("notes", f"big-{i}", {"text": big})

    # The WAL file actually holds bytes (proves we're testing the WAL path).
    wal = db.with_name(db.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0

    after = db_size_bytes(db)
    # WAL-inclusive sizing must see the growth even though no checkpoint ran.
    assert after > baseline + 500_000, (after, baseline)


def test_cap1_size_helper_counts_wal_over_main_only(tmp_path: Path) -> None:
    """db_size_bytes (logical/page-count based) must exceed the bare memory.db
    file size when committed data is still in the WAL."""
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa")
    big = "z" * 100_000
    for i in range(6):
        c.set_entity("notes", f"e-{i}", {"text": big})
    main_only = db.stat().st_size
    inclusive = db_size_bytes(db)
    # Logical size accounts for WAL-resident pages the main file hasn't absorbed.
    assert inclusive >= main_only


# ======================================================================
# CAP-2 — gate on the absolute resulting footprint, re-read inside the txn
# ======================================================================

def test_cap2_single_near_cap_write_that_would_exceed_is_rejected(tmp_path: Path) -> None:
    """A single write that would push the ABSOLUTE footprint over the cap is
    rejected by the in-transaction recheck (CAP-2), even when the pre-write
    delta estimate alone looked acceptable. Uses a tiny synthetic cap so the
    test stays fast and deterministic."""
    db = tmp_path / "memory.db"
    storage = Storage(str(db))

    # A fresh schema DB already occupies a baseline (FTS5 tables etc). Set the
    # cap a fixed margin ABOVE that baseline so a handful of writes commit and a
    # later write is the one that tips the absolute footprint over.
    baseline = db_size_bytes(db)
    cap = baseline + 60 * 1024

    # A free gate whose db_size_fn under-reports (returns 0) so the PRE-write
    # check always passes; the CAP-2 in-transaction check (which reads the true
    # logical size) is the only thing that can catch the overage.
    def offline_fn(url, payload, timeout=4.0):
        raise TierVerificationError("blackholed")

    gate = CapGate(
        account_id=None,           # no account: free, fails closed at cap
        session_token=None,
        db_size_fn=lambda: 0,      # pre-write estimate always says "plenty of room"
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),
        check_fn=offline_fn,
        cap_bytes=cap,
    )
    client = MemoryClient(
        storage=storage, tenant_id="qa", tier="free", cap_gate=gate,
    )

    # Grow the DB toward the tiny cap. The single write that would tip the
    # ABSOLUTE footprint over the cap must be rejected (CAP-2), so the COMMITTED
    # footprint never exceeds the cap — that's the property we prove.
    payload = "p" * 4000
    committed_sizes: list[int] = []
    with pytest.raises(CapExceededError):
        for i in range(500):
            client.set_entity("bulk", f"row-{i}", {"text": payload})
            committed_sizes.append(db_size_bytes(db))

    assert committed_sizes, "no write committed before rejection"
    # The pre-write delta estimate said 'plenty of room' (db_size_fn=0), so the
    # ONLY thing that can have rejected the write is the in-transaction CAP-2
    # absolute-footprint recheck. The COMMITTED footprint never exceeds the cap
    # (the over-cap write rolled back) — bounded by cap + ~one page of slack.
    assert max(committed_sizes) <= cap + 8192, max(committed_sizes)


def test_cap2_in_txn_recheck_makes_no_network_call(tmp_path: Path) -> None:
    """BLOCKER fix (2026-06-25 review): the CAP-2 in-transaction recheck runs
    under the BEGIN IMMEDIATE write lock, so it must enforce the cap LOCALLY
    with NO network call (a urlopen under the lock would starve concurrent
    writers past the busy-timeout). Prove it for an ACCOUNT user at the cap
    boundary: the over-cap write is rejected while check_fn is never invoked."""
    import time
    db = tmp_path / "memory.db"
    storage = Storage(str(db))
    baseline = db_size_bytes(db)
    cap = baseline + 60 * 1024

    calls = {"n": 0}

    def counting_fn(url, payload, timeout=4.0):
        calls["n"] += 1
        raise AssertionError("network call made while holding the write lock")

    # Fresh account-matched FREE cache so the local recheck has a cap to enforce
    # and never needs to refresh from the server.
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acct", tier="free", checked_at=time.time(), cap_bytes=cap,
    ))
    gate = CapGate(
        account_id="acct",
        session_token="tok",
        db_size_fn=lambda: 0,      # pre-write estimate always says "room left"
        local_tier_hint="free",
        cache=cache,
        check_fn=counting_fn,
        cap_bytes=cap,
    )
    client = MemoryClient(storage=storage, tenant_id="qa", tier="free", cap_gate=gate)

    payload = "p" * 4000
    committed: list[int] = []
    with pytest.raises(CapExceededError):
        for i in range(500):
            client.set_entity("bulk", f"row-{i}", {"text": payload})
            committed.append(db_size_bytes(db))

    assert committed, "no write committed before rejection"
    assert max(committed) <= cap + 8192, max(committed)
    # The fix: the in-transaction recheck never touched the network.
    assert calls["n"] == 0, f"in-txn recheck made {calls['n']} network call(s)"


# ======================================================================
# CAP-4 + CORE-1 — fail-open is paid-grant-only; free/no-cache fails CLOSED
# ======================================================================

def test_cap4_blackholed_verify_no_cache_cannot_exceed_free_cap(tmp_path: Path) -> None:
    """A no-cache account whose verify endpoint is blackholed must NOT be able to
    grow past the free cap (the old code allowed up to 4x). The over-cap state is
    raised, not merely logged, and the error reports the FREE cap."""
    server_calls: list = []

    def blackholed(url, payload, timeout=4.0):
        server_calls.append(payload)
        raise TierVerificationError("blackholed api.sibyllabs.org")

    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: FREE_TIER_CAP_BYTES + 1024,  # already over free cap
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),  # empty: no prior paid grant
        check_fn=blackholed,
    )
    with pytest.raises(CapExceededError) as exc:
        gate.check(proposed_delta_bytes=500)
    assert exc.value.cap == FREE_TIER_CAP_BYTES  # FREE cap, not 4x ceiling


def test_cap4_no_account_blackholed_fails_closed_at_free_cap(tmp_path: Path) -> None:
    """A no-account (never activated) user past the free cap, with verification
    unreachable, hard-blocks at the free cap."""
    gate = CapGate(
        account_id=None,
        session_token=None,
        db_size_fn=lambda: FREE_TIER_CAP_BYTES + 5000,
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),
        check_fn=lambda *a, **k: (_ for _ in ()).throw(TierVerificationError("x")),
    )
    with pytest.raises(CapExceededError) as exc:
        gate.check(proposed_delta_bytes=100)
    assert exc.value.cap == FREE_TIER_CAP_BYTES


# ======================================================================
# CAP-5 / CORE-2 — 401/403 are authoritative deny, never fail-open
# ======================================================================

def test_cap5_401_403_not_in_retryable_codes() -> None:
    """401/403 must NOT be retryable (they are authoritative, not transient)."""
    assert 401 not in RETRYABLE_HTTP_CODES
    assert 403 not in RETRYABLE_HTTP_CODES
    assert 401 in AUTH_DENY_HTTP_CODES
    assert 403 in AUTH_DENY_HTTP_CODES
    # 429 stays retryable: genuine rate limiting.
    assert 429 in RETRYABLE_HTTP_CODES


def test_cap5_401_from_verify_hard_denies_over_cap_write(tmp_path: Path) -> None:
    """A 401 (TierAuthError) from verify on an over-cap write hard-denies at the
    free cap and NEVER falls through to fail-open. A forged/expired token reaches
    the server check (no fresh paid cache to short-circuit), the server refuses
    with 401, and the gate enforces the free cap instead of failing open to 4x."""
    auth_calls: list = []

    def auth_denied(url, payload, timeout=4.0):
        auth_calls.append(payload)
        raise TierAuthError("HTTP 401 refused")

    gate = CapGate(
        account_id="acc-1",
        session_token="forged-or-expired-token",
        db_size_fn=lambda: FREE_TIER_CAP_BYTES + 10_000,  # over free cap
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),  # no cache: reaches the server
        check_fn=auth_denied,
    )
    with pytest.raises(CapExceededError) as exc:
        gate.check(proposed_delta_bytes=500)
    assert exc.value.cap == FREE_TIER_CAP_BYTES  # free cap, NOT the 4x ceiling
    assert auth_calls, "the server check must have been consulted (then refused)"


def test_cap5_auth_error_never_fails_open_even_under_ceiling(tmp_path: Path) -> None:
    """Belt-and-suspenders: a 401 over-cap write must hard-deny even when the
    footprint is well under the old 4x fail-open ceiling (the ceiling path must
    be unreachable for an auth refusal)."""
    def auth_denied(url, payload, timeout=4.0):
        raise TierAuthError("HTTP 403 refused")

    gate = CapGate(
        account_id="acc-1",
        session_token="forged",
        # Over free cap but FAR under 4x ceiling: old fail-open would allow it.
        db_size_fn=lambda: FREE_TIER_CAP_BYTES + 100,
        local_tier_hint="free",
        cache=TierCache(tmp_path / "tc.json"),
        check_fn=auth_denied,
    )
    with pytest.raises(CapExceededError):
        gate.check(proposed_delta_bytes=10)


# ======================================================================
# CAP-6 — current_cap() account-match guard
# ======================================================================

def test_cap6_current_cap_ignores_mismatched_account_cache(tmp_path: Path) -> None:
    """A cache entry belonging to a DIFFERENT account (or a forged null-account
    uncapped entry) must not be read as this account's cap."""
    import time
    cache = TierCache(tmp_path / "tc.json")
    # Forged uncapped entry for a different / null account.
    cache.store(TierCacheEntry(
        account_id=None, tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    gate = CapGate(
        account_id="acc-1",  # our real account
        session_token="sess-1",
        db_size_fn=lambda: 0,
        local_tier_hint="free",
        cache=cache,
        check_fn=lambda *a, **k: {"ok": True, "tier": "free", "cap_bytes": FREE_TIER_CAP_BYTES},
    )
    # The mismatched cache must be ignored → effective cap is the free cap.
    assert gate.current_cap() == FREE_TIER_CAP_BYTES


def test_cap6_current_cap_rejects_null_account_forged_uncapped(tmp_path: Path) -> None:
    """SEC-13 gap closed (2026-06-25 review): a free/unactivated user
    (account_id=None) must NOT have a forged null-account uncapped cache
    (account_id=None, cap_bytes=None) honored by current_cap() — None==None
    would otherwise report 'uncapped' in status for a free user."""
    import time
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id=None, tier="lifetime", checked_at=time.time(), cap_bytes=None,
    ))
    gate = CapGate(
        account_id=None,           # free / unactivated user
        session_token=None,
        db_size_fn=lambda: 0,
        local_tier_hint="free",
        cache=cache,
        check_fn=lambda *a, **k: {"ok": True, "tier": "free", "cap_bytes": FREE_TIER_CAP_BYTES},
    )
    # Forged null-account uncapped cache must be distrusted → free cap, not None.
    assert gate.current_cap() == FREE_TIER_CAP_BYTES


# ======================================================================
# CAP-7 — accept_proposal size estimate includes metadata + FTS overhead
# ======================================================================

def test_cap7_accept_proposal_estimate_includes_metadata_and_fts(tmp_path: Path) -> None:
    """The accept_proposal cap estimate must be at least body + metadata + FTS
    overhead, not just body + 250. We prove it by capturing the delta the gate
    receives and asserting it exceeds the naive (body + 250) figure."""
    from sibyl_memory_client.learning import Learner, SkillProposal

    storage = Storage(str(tmp_path / "memory.db"))

    captured: list[int] = []

    class SpyGate:
        def check(self, proposed_delta_bytes: int = 0) -> None:
            captured.append(proposed_delta_bytes)

    learner = Learner(storage, tenant_id="qa", cap_gate=SpyGate())

    body = "B" * 3000
    # Insert a pending proposal row directly so accept_proposal has something.
    pid = learner._insert_proposal(
        __import__("sibyl_memory_client.learning", fromlist=["_Candidate"])._Candidate(
            kind="repeated_action", slug="demo-skill", confidence=0.9, events=[], hints={},
        ),
        body=body, title="Demo Skill",
    )
    learner.accept_proposal(pid)

    assert captured, "cap gate was never consulted"
    naive = len(body) + len("skill/demo-skill") + 250
    # New estimate adds metadata JSON + ~1x body FTS overhead, so it must be
    # materially larger than the old naive estimate.
    assert captured[0] > naive + len(body) - 1, (captured[0], naive)


# ======================================================================
# CORE-5 — clamp limits (negative / huge must not broaden)
# ======================================================================

def _seed_events(tmp_path, n=6):
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    for i in range(n):
        c.write_event(acted=[f"did thing {i}"])
    return c


def test_core5_read_events_negative_limit_not_unbounded(tmp_path: Path) -> None:
    """read_events(limit=-1) must NOT return an unbounded result (SQLite LIMIT
    -1 = unbounded). It clamps to 0 rows."""
    c = _seed_events(tmp_path, n=6)
    assert c.read_events(limit=-1) == []
    # Sanity: a positive limit still returns rows.
    assert len(c.read_events(limit=3)) == 3


def test_core5_list_entities_negative_limit_not_unbounded(tmp_path: Path) -> None:
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    for i in range(5):
        c.set_entity("notes", f"n-{i}", {"v": i})
    assert c.list_entities(limit=-1) == []


# ======================================================================
# CORE-7 — malformed stored JSON raises typed StorageError, not a raw crash
# ======================================================================

def test_core7_corrupted_entity_row_raises_storage_error(tmp_path: Path) -> None:
    """Hand-corrupt an entity body to invalid JSON, then prove get_entity raises
    a typed StorageError instead of a raw json.JSONDecodeError escaping the API."""
    db = tmp_path / "memory.db"
    c = MemoryClient.local(db, tenant_id="qa")
    c.set_entity("notes", "victim", {"text": "fine"})

    # Corrupt the stored body directly to invalid JSON. The schema has a
    # json_valid(body) CHECK, so bypass it with ignore_check_constraints — this
    # simulates the real-world corruption vector (partial write / disk fault /
    # manual edit) that the CHECK cannot retroactively prevent.
    raw = sqlite3.connect(str(db))
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute(
        "UPDATE entities SET body = ? WHERE tenant_id = ? AND category = ? AND name = ?",
        ("{not valid json", "qa", "notes", "victim"),
    )
    raw.commit()
    raw.close()

    fresh = MemoryClient.local(db, tenant_id="qa")
    # Must be a typed StorageError, NOT a raw json.JSONDecodeError.
    with pytest.raises(StorageError):
        fresh.get_entity("notes", "victim")


# ======================================================================
# CORE-8 — set_tenant / __init__ validate tenant_id
# ======================================================================

def test_core8_set_tenant_rejects_control_char(tmp_path: Path) -> None:
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    with pytest.raises(TenantError):
        c.set_tenant("bad\x00tenant")


def test_core8_set_tenant_rejects_empty(tmp_path: Path) -> None:
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    with pytest.raises(TenantError):
        c.set_tenant("")


def test_core8_init_rejects_control_char_tenant(tmp_path: Path) -> None:
    with pytest.raises(TenantError):
        MemoryClient.local(tmp_path / "memory.db", tenant_id="bad\ttenant")


# ======================================================================
# CORE-9 — archive_entity sizes inside the same transaction (smoke: still works)
# ======================================================================

def test_core9_archive_entity_still_works_under_cap(tmp_path: Path) -> None:
    """archive_entity now reads/sizes/checks/writes in one transaction. Verify
    the happy path still archives correctly (the TOCTOU close is structural)."""
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    c.set_entity("notes", "to-archive", {"text": "bye"})
    res = c.archive_entity("notes", "to-archive", reason="cleanup")
    assert res["archived_id"]
    from sibyl_memory_client import NotFoundError
    with pytest.raises(NotFoundError):
        c.get_entity("notes", "to-archive")


# ======================================================================
# CORE-11 — short digit-bearing identifiers recover in the relax fallback
# ======================================================================

def test_core11_search_recovers_short_identifier(tmp_path: Path) -> None:
    """A query mixing a stopword-heavy phrase with a short digit-bearing token
    (q3) must still recover the row via the relaxed fallback. Previously the
    len>=3 floor dropped q3 from the last-resort recall."""
    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    c.set_entity("reports", "q3-roadmap", {"text": "q3 planning roadmap notes"})
    # Multi-word query where the strict AND of every token misses, but the rare
    # short identifier q3 should recover it through the relax path.
    res = c.search("what about the q3 nonexistentzzz", limit=5)
    keys = {h.get("key") for h in res}
    assert "q3-roadmap" in keys


# ======================================================================
# CORE-13 — close() reaps connections opened by other threads
# ======================================================================

def test_core13_close_reaps_cross_thread_connections(tmp_path: Path) -> None:
    """A connection opened on a worker thread must be closed by close()."""
    import threading

    storage = Storage(str(tmp_path / "memory.db"))
    opened: list = []

    def worker():
        with storage.connection() as conn:
            conn.execute("SELECT 1")
            opened.append(conn)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert len(opened) == 1
    storage.close()
    # The worker's connection is closed: operating on it now raises.
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ======================================================================
# CORE-14 — a write that errors rolls back without masking the original error
# ======================================================================

def test_core14_transaction_rollback_preserves_original_error(tmp_path: Path) -> None:
    """An exception raised inside a transaction must propagate (not be masked by
    a rollback failure), and the DB must be rolled back."""
    storage = Storage(str(tmp_path / "memory.db"))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with storage.transaction() as conn:
            conn.execute(
                "INSERT INTO entities (id, tenant_id, category, name, body) "
                "VALUES ('x','qa','c','n','{}')"
            )
            raise Boom("caller error")

    # The insert was rolled back.
    with storage.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE tenant_id = 'qa'"
        ).fetchone()["n"]
    assert n == 0


# ======================================================================
# CORE-3 — zero cross-tenant leak (the lock-comment guard's regression test)
# ======================================================================

def test_core3_tenant_isolation(tmp_path: Path) -> None:
    """Two tenants in the SAME DB file must never see each other's rows through
    search / search_entities / read paths. Guards the trailing
    `AND f.tenant_id = ?` post-filter against accidental removal."""
    db = tmp_path / "memory.db"
    a = MemoryClient.local(db, tenant_id="tenant-a")
    b = MemoryClient.local(db, tenant_id="tenant-b")

    a.set_entity("secrets", "alpha", {"text": "tenant a private payload zebra"})
    b.set_entity("secrets", "beta", {"text": "tenant b private payload zebra"})
    a.set_state("akey", {"text": "a-state zebra"})
    b.set_state("bkey", {"text": "b-state zebra"})

    # Shared token "zebra" appears in BOTH tenants' rows.
    a_hits = a.search("zebra", limit=50)
    b_hits = b.search("zebra", limit=50)

    # Tenant A must only ever surface its own keys.
    a_keys = {h.get("key") for h in a_hits}
    b_keys = {h.get("key") for h in b_hits}
    assert "beta" not in a_keys and "bkey" not in a_keys, a_keys
    assert "alpha" not in b_keys and "akey" not in b_keys, b_keys

    # search_entities is also isolated.
    a_ents = {e["name"] for e in a.search_entities("zebra", limit=50)}
    assert "beta" not in a_ents


# ======================================================================
# CORE-6 / MH-3 — multi_record_search uses a cheap COUNT, bounds fan-out
# ======================================================================

def test_core6_multi_record_uses_count_not_full_scan(tmp_path: Path) -> None:
    """multi_record_search must derive corpus_n from a cheap COUNT(*) and NOT
    materialize the whole entity table via list_entities(limit=100000). We assert
    list_entities is never called with the giant limit, and that fan-out is
    bounded for a many-token query."""
    from sibyl_memory_client import multi_record

    c = MemoryClient.local(tmp_path / "memory.db", tenant_id="qa")
    for i in range(8):
        c.set_entity("notes", f"n-{i}", {"text": f"alpha{i} beta gamma project status"})

    list_entities_limits: list[int] = []
    search_calls: list[str] = []
    orig_list = c.list_entities
    orig_search = c.search

    def spy_list(*args, **kwargs):
        list_entities_limits.append(kwargs.get("limit", args[-1] if args else None))
        return orig_list(*args, **kwargs)

    def spy_search(q, *args, **kwargs):
        search_calls.append(q)
        return orig_search(q, *args, **kwargs)

    c.list_entities = spy_list  # type: ignore[assignment]
    c.search = spy_search       # type: ignore[assignment]

    # A query with MANY significant tokens: fan-out must be capped.
    many = " ".join(f"tok{i}word" for i in range(60)) + " alpha0 beta gamma"
    multi_record.multi_record_search(c, many, limit=10)

    # No giant list_entities materialization.
    assert 100000 not in list_entities_limits
    # Fan-out (one search per kept token) is bounded by the cap.
    assert len(search_calls) <= multi_record._MAX_FANOUT_TOKENS


# ======================================================================
# CORE-10 — paid-feature gate uses server-authoritative cache over client hint
# ======================================================================

def test_core10_tampered_tier_blocked_when_cache_says_free(tmp_path: Path) -> None:
    """If the credentials hint claims a paid tier but a FRESH account-matched
    cap-gate cache says free, the paid-feature gate must DENY (server wins)."""
    import time
    cache = TierCache(tmp_path / "tc.json")
    cache.store(TierCacheEntry(
        account_id="acc-1", tier="free", checked_at=time.time(),
        cap_bytes=FREE_TIER_CAP_BYTES,
    ))
    gate = CapGate(
        account_id="acc-1",
        session_token="sess-1",
        db_size_fn=lambda: 0,
        local_tier_hint="lifetime",  # tampered hint
        cache=cache,
        check_fn=lambda *a, **k: {"ok": True, "tier": "free"},
    )
    client = MemoryClient(
        storage=Storage(str(tmp_path / "memory.db")),
        tenant_id="qa",
        tier="lifetime",         # tampered client tier
        account_id="acc-1",
        session_token="sess-1",
        cap_gate=gate,
    )
    with pytest.raises(TierGateError):
        client.lint()  # paid-only feature → denied because server cache says free
