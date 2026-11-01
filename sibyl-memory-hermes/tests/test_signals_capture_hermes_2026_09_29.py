"""R2 skill signals in the Hermes adapter (spec plugin-paid-tier-r2 1.2, 1.6):
``handle_tool_call`` records the tool name and a 12-hex argument-name
fingerprint before dispatch; entity writes are recorded through the client as
hashes; the per-turn journal and the shutdown capsule are not recorded;
shutdown flushes the buffer before the learner pass; the free tier records
nothing; no argument value or query reaches signals.json.

On b591aa1 these fail: the adapter had no ``_note_signal`` / ``_flush_signals``
and ``sibyl_memory_client.lifecycle.signals`` did not exist (ImportError).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.testing import write_entitlement
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter

CANARY_VALUE = "CANARY-VALUE-7a1f"
CANARY_ENTITY = "Canary Person 3e5d"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    yield
    with SG._registry_lock:
        for b in SG._BUFFERS.values():
            b.discard()
        SG._BUFFERS.clear()


def _adapter(tmp_path, sid="r2-sess") -> SibylAdapter:
    a = SibylAdapter()
    a.initialize(sid, hermes_home=str(tmp_path / "hh"))
    return a


def _round(a: SibylAdapter, i: int) -> None:
    a.handle_tool_call("sibyl_remember", {"category": "people", "name": CANARY_ENTITY,
                                          "body": {"note": f"{CANARY_VALUE} {i}"}})
    a.handle_tool_call("sibyl_list", {"category": "people"})
    a.handle_tool_call("sibyl_search", {"query": f"about {CANARY_VALUE}", "limit": 3})
    a.handle_tool_call(f"sibyl_unknown_{i}", {})         # a separator (an unknown tool errors, still recorded)


def _home_text() -> bytes:
    out = b""
    for p in (Path.home() / ".sibyl-memory").rglob("*"):
        if p.is_file() and p.suffix != ".db" and "-wal" not in p.name and "-shm" not in p.name:
            out += p.read_bytes()
    return out


def test_tool_calls_record_names_and_shapes_on_pro(entitled_test_home, test_signer, tmp_path):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    a = _adapter(tmp_path)
    _round(a, 0)
    buf = SG.recorder(Path.home())
    assert [t[1] for t in buf._tools] == ["sibyl_remember", "sibyl_list", "sibyl_search", "sibyl_unknown_0"]
    assert all(len(t[2]) == 12 for t in buf._tools)
    assert {t[0] for t in buf._tools} == {a._notices.session_key[:8]}
    assert [w[1] for w in buf._writes] == ["s"]                    # the remember, as a hash
    a.shutdown()


def test_shutdown_flushes_then_the_learner_proposes_the_sequence(entitled_test_home, test_signer, tmp_path):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    a = _adapter(tmp_path)
    a.sync_turn("hello", "hi")                          # the per-turn journal: not an entity write
    for i in range(3):
        _round(a, i)
    db = a._lifecycle_client().storage.db_path
    a.shutdown()
    sig = json.loads(SG.path_of(Path.home()).read_bytes())
    seqs = [e["tools"] for e in sig["ngrams"].values()]
    assert ["sibyl_remember", "sibyl_list", "sibyl_search"] in seqs
    assert len(sig["entity_writes"]) == 1                # the canary entity only (capsule, journal: not)
    blob = _home_text()
    for canary in (CANARY_VALUE, CANARY_ENTITY, "about ", '"people"'):
        assert canary.encode() not in blob, canary
    con = sqlite3.connect(str(db))
    try:
        kinds = [r[0] for r in con.execute("SELECT pattern_kind FROM skill_proposals")]
    finally:
        con.close()
    assert "tool_sequence" in kinds


def test_free_tier_records_nothing(entitled_test_home, tmp_path):
    a = _adapter(tmp_path)
    _round(a, 0)
    assert SG.recorder(Path.home()).pending() == 0
    a.shutdown()
    assert not SG.path_of(Path.home()).exists()


def test_recording_off_records_nothing(entitled_test_home, test_signer, tmp_path):
    write_entitlement(entitled_test_home, test_signer.mint(tier="pro", cap=None, src="pro"))
    SG.set_enabled(Path.home(), False)
    a = _adapter(tmp_path)
    _round(a, 0)
    a.shutdown()
    assert not SG.path_of(Path.home()).exists()
