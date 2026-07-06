"""Unit C4 super-patch regressions (2026-07-05) for learning.py.

Covers the five findings routed to build unit C4:

  H#1  — the Sibyl-routed (VeniceX402) redaction must NOT relay literal dict KEY
         NAMES (content can hide in keys); keys become a count + per-key lengths.
  H#11 — hosted-path hint redaction is now an ALLOWLIST, so an unknown / future
         content-derived hint field is stubbed by default instead of leaking.
  H#8  — accept_proposal does an in-transaction CAP-2 recheck and guards the
         state transition with ``status = 'pending'`` (rowcount == 0 → raise), so
         an over-cap accept is rejected in-txn and a concurrent double-accept
         cannot both commit.
  H#15 — the learner watermark cursors on the monotonic journal ``rowid`` instead
         of ``max(ts)`` with strict ``ts >``, so concurrent / same-timestamp /
         backdated events are never skipped.
  R13  — co-occurrence token extraction is capped, so a pathological event with
         ~100k unique tokens completes fast and bounded instead of O(tokens²).

Hermetic: reuses tests/conftest.py (src/ on path + HOME isolation). The BYOK path
is intentionally NOT exercised here — only the Sibyl-routed redact=True path is
minimized; BYOK full fidelity is asserted by test_learning_redaction_2026_06_30.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient, VeniceX402Summarizer
from sibyl_memory_client.exceptions import ValidationError
from sibyl_memory_client.learning import (
    Learner,
    _Candidate,
    _extract_tokens,
    _redact_hints_for_prompt,
    _MAX_TOKENS_PER_EVENT,
)
from sibyl_memory_client.storage import Storage, dumps, new_id


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _storage(tmp_path: Path) -> Storage:
    return Storage(str(tmp_path / "m.db"))


def _insert_events(
    storage: Storage,
    tenant: str,
    ts: str,
    acted_payloads: list,
    evaluated: dict | None = None,
) -> None:
    """Insert journal_events rows directly so the test controls ts + rowid."""
    with storage.transaction() as conn:
        for acted in acted_payloads:
            conn.execute(
                "INSERT INTO journal_events "
                "(id, tenant_id, ts, evaluated, acted, forward, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    tenant,
                    ts,
                    dumps(evaluated) if evaluated is not None else None,
                    dumps(acted),
                    None,
                    None,
                ),
            )


def _seed_pending_proposal(learner: Learner, *, slug: str, body: str) -> str:
    return learner._insert_proposal(
        _Candidate(kind="repeated_action", slug=slug, confidence=0.9, events=[], hints={}),
        body=body,
        title="Seed",
    )


# ======================================================================
# H#1 — dict KEY NAMES must not reach the Sibyl-routed prompt
# ======================================================================

_SECRET_KEY = "exfiltrate_the_quarterly_secret_9f3a"


def test_h1_dict_key_names_never_reach_sibyl_routed_prompt(tmp_path: Path) -> None:
    prompts: list[str] = []

    def capture(prompt: str) -> str:
        prompts.append(prompt)
        return "# Skill\n\nDo the thing."

    summ = VeniceX402Summarizer(capture, account_id="acc-stub")
    client = MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")
    # The secret lives in the dict KEYS, not the values.
    for _ in range(4):
        client.write_event(
            evaluated={_SECRET_KEY: 1, "sibling_" + _SECRET_KEY: 2},
            acted=[{"kind": "noop"}],
        )

    report = client.learner(summarizer=summ).run()
    assert report.proposals_made >= 1
    assert prompts, "summarizer was never invoked"

    for p in prompts:
        assert _SECRET_KEY not in p, "a raw dict KEY name leaked to the Sibyl-routed prompt"
    # Non-vacuous: the dict shape is still relayed, just as counts / lengths.
    assert any("key_count" in p for p in prompts)


# ======================================================================
# H#11 — hint redaction is an ALLOWLIST (unknown field stubbed by default)
# ======================================================================

def test_h11_hint_redaction_is_allowlist() -> None:
    secret = "TOP_SECRET_leak_me_please_x99"
    hints = {
        "hits": 5,                                  # allowlisted numeric → kept
        "cadence_minutes": 12.5,                    # allowlisted → kept
        "cov": 0.12,                                # allowlisted → kept
        "future_content_field": secret,             # unknown → stubbed by default
        "shared_keys": ["alpha_key", "beta_key"],   # key NAMES → shaped, not raw
    }
    out = _redact_hints_for_prompt(hints)

    assert out["hits"] == 5
    assert out["cadence_minutes"] == 12.5
    assert out["cov"] == 0.12

    # The unknown / future field is stubbed — no raw value survives.
    assert out["future_content_field"] != secret
    blob = json.dumps(out)
    assert secret not in blob
    # shared_keys reduced to a count + per-key lengths, never the literal names.
    assert "alpha_key" not in blob
    assert out["shared_keys"]["key_count"] == 2

    # The caller's original hints dict is left untouched.
    assert hints["future_content_field"] == secret


# ======================================================================
# H#8 — in-transaction CAP recheck + status='pending' guard
# ======================================================================

class _OverCap(Exception):
    pass


class _InTxnRaisingGate:
    """Pre-write estimate passes; the in-transaction absolute recheck raises."""

    def check(self, proposed_delta_bytes: int = 0) -> None:
        return

    def check_total_local(self, total_size_bytes: int) -> None:
        raise _OverCap("footprint over cap")


def test_h8_accept_over_cap_rejected_in_txn(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    learner = Learner(storage, tenant_id="qa", cap_gate=_InTxnRaisingGate())
    pid = _seed_pending_proposal(learner, slug="cap-demo", body="x" * 200)

    with pytest.raises(_OverCap):
        learner.accept_proposal(pid)

    # Rolled back: proposal still pending, no reference doc written.
    assert learner.get_proposal(pid).status == "pending"
    with storage.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM reference_documents "
            "WHERE tenant_id = ? AND doc_key = ?",
            ("qa", "skill/cap-demo"),
        ).fetchone()
    assert row["c"] == 0


def test_h8_double_accept_status_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _storage(tmp_path)
    learner = Learner(storage, tenant_id="qa")  # no cap gate
    pid = _seed_pending_proposal(learner, slug="dup-demo", body="hello world")

    # First accept commits.
    learner.accept_proposal(pid)
    assert learner.get_proposal(pid).status == "accepted"

    # Simulate a racing second caller that read status='pending' before the first
    # accept committed: force the top-level guard to see 'pending' so the request
    # reaches the in-transaction status guard, which must reject it (rowcount 0).
    accepted = learner.get_proposal(pid)
    pending_view = dataclasses.replace(accepted, status="pending")
    monkeypatch.setattr(learner, "get_proposal", lambda *_a, **_k: pending_view)

    with pytest.raises(ValidationError):
        learner.accept_proposal(pid)


# ======================================================================
# H#15 — rowid watermark never skips same-ts / backdated events
# ======================================================================

def test_h15_watermark_does_not_skip_same_ts_events(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    tenant = "qa"
    learner = Learner(storage, tenant_id=tenant, min_pattern_hits=2)
    ts = "2026-07-05T12:00:00.000Z"

    # First batch: two events at ts T (rowids 1, 2).
    _insert_events(storage, tenant, ts, [["a"], ["b"]])
    r1 = learner.run()
    assert r1.events_scanned == 2

    # Second batch: two MORE events at the SAME ts T (concurrent / backdated).
    # A strict `ts > watermark` cursor would skip these; the rowid cursor must not.
    _insert_events(storage, tenant, ts, [["c"], ["d"]])
    r2 = learner.run()
    assert r2.events_scanned == 2, "same-ts events after the watermark were skipped"

    # Third run with nothing new is empty (watermark still advances monotonically).
    r3 = learner.run()
    assert r3.events_scanned == 0


# ======================================================================
# R13 — co-occurrence token cap bounds a pathological event
# ======================================================================

def test_r13_cooccurrence_token_cap_bounds_pathological_event(tmp_path: Path) -> None:
    # A single event carrying 100k unique tokens must NOT trigger O(tokens²).
    big = [f"tok{i}" for i in range(100_000)]
    toks = _extract_tokens({"evaluated": None, "acted": big})
    # Deterministic discriminator (fails fast on unpatched code, no hang).
    assert len(toks) <= _MAX_TOKENS_PER_EVENT

    storage = _storage(tmp_path)
    _insert_events(storage, "qa", "2026-07-05T00:00:00.000Z", [big])
    learner = Learner(storage, tenant_id="qa", min_pattern_hits=2)

    start = time.perf_counter()
    report = learner.run()
    elapsed = time.perf_counter() - start

    assert report.events_scanned == 1
    assert elapsed < 5.0, f"co-occurrence blew up on a pathological event ({elapsed:.1f}s)"


def test_r13_realistic_events_unchanged(tmp_path: Path) -> None:
    # Shallow, realistic events keep their full token set and still co-occur.
    ev = {"evaluated": {"module": "auth", "owner": "jane"}, "acted": ["deploy staging"]}
    assert set(_extract_tokens(ev)) == {"module", "owner", "deploy-staging"}

    storage = _storage(tmp_path)
    for _ in range(3):
        _insert_events(
            storage,
            "qa",
            "2026-07-05T00:00:00.000Z",
            [["deploy staging"]],
            evaluated={"module": "auth", "owner": "jane"},
        )
    learner = Learner(storage, tenant_id="qa", min_pattern_hits=2)
    learner.run()

    kinds = {p.pattern_kind for p in learner.list_proposals(status="pending")}
    assert "co_occurrence" in kinds or "structural_similarity" in kinds
