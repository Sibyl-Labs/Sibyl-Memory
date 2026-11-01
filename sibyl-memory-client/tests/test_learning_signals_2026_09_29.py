"""R2 detectors (spec plugin-paid-tier-r2 1.4, 1.5, 1.6): ``tool_sequence``
and ``correction_loop`` from the signals snapshot, feeding R1's pipeline.

On b591aa1 every test here fails: ``_detect_tool_sequence``,
``_detect_correction_loop``, ``Learner.run(signals=...)``,
``proposals.KINDS`` with the two kinds and the ``tool_sequence`` span waiver
do not exist (ImportError / TypeError), and a tool-sequence row whose evidence
sits inside one session was not relevant.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills as SK
from sibyl_memory_client.learning import (
    REMOVED_ENTITY,
    Learner,
    LocalDeterministicSummarizer,
    _detect_correction_loop,
    _detect_tool_sequence,
)
from sibyl_memory_client.lifecycle import proposals as PR
from sibyl_memory_client.lifecycle import reminders as R
from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.lifecycle import state as ST

DAY = 86400.0
S = SG.arg_shape({"x": 1})
CANARY_ENTITY = "Canary Person 3e5d"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    yield
    with SG._registry_lock:
        for b in SG._BUFFERS.values():
            b.discard()
        SG._BUFFERS.clear()


def _seq(sig, s8, names, t0, step=10.0, shapes=None):
    evs = [(n, (shapes[i] if shapes else S), t0 + i * step) for i, n in enumerate(names)]
    SG.apply_tool_events(sig, s8, evs, t0 + len(names) * step)
    return t0 + len(names) * step


def _slugs(cands):
    return sorted(c.slug for c in cands)


# ----------------------------------------------------------------------
# tool_sequence thresholds
# ----------------------------------------------------------------------

def test_three_times_in_one_session_qualifies_two_does_not():
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    for i in range(2):
        t = _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash", f"Sep{i}"], t)
    assert _detect_tool_sequence(sig, now) == []
    _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash", "Sep9"], t)
    got = _detect_tool_sequence(sig, now)
    assert [c.hints["tool_names"] for c in got] == [["Read", "Edit", "Bash"]]
    c = got[0]
    assert c.kind == "tool_sequence" and c.slug == "seq-read-edit-bash" and c.confidence == 0.60
    assert c.hints["hits"] == 3 and c.hints["sessions"] == 1
    assert [e["snippet"] for e in c.events] == ["Read -> Edit -> Bash"] * 3
    assert all(e["id"] is None for e in c.events)


def test_five_in_seven_days_needs_two_sessions():
    now = time.time()
    k = SG.ngram_key([("Read", S), ("Edit", S), ("Bash", S)])
    base = {"n": 3, "tools": ["Read", "Edit", "Bash"], "shapes": [S, S, S], "count_total": 5,
            "days": {SG._day(now): 5}, "first": now - 3 * DAY, "last": now - 60,
            "ts": [now - 3 * DAY, now - 2 * DAY, now - DAY, now - 600, now - 60]}
    one = dict(base, sessions=[["aaaaaaaa", 2, now - 60]])
    assert _detect_tool_sequence({"ngrams": {k: one}}, now) == []
    two = dict(base, sessions=[["aaaaaaaa", 2, now - 60], ["bbbbbbbb", 2, now - DAY],
                               ["cccccccc", 1, now - 2 * DAY]])
    got = _detect_tool_sequence({"ngrams": {k: two}}, now)
    assert len(got) == 1 and got[0].hints["sessions"] == 3
    assert got[0].confidence == 0.75                    # 0.60 + 0.05 * 2 + 0.05 (sessions)
    four = dict(two, count_total=4, days={SG._day(now): 4})
    assert _detect_tool_sequence({"ngrams": {k: four}}, now) == []
    # counts older than 7 days do not count
    old = dict(two, days={SG._day(now - 9 * DAY): 5})
    assert _detect_tool_sequence({"ngrams": {k: old}}, now) == []


def test_confidence_is_capped_at_085():
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    for i in range(12):
        t = _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash", f"Sep{i}"], t)
    t = _seq(sig, "bbbbbbbb", ["Read", "Edit", "Bash", "SepX"], t)
    c = [c for c in _detect_tool_sequence(sig, now) if c.hints["tool_names"] == ["Read", "Edit", "Bash"]]
    assert c and c[0].confidence == 0.85


def test_identical_names_and_read_only_memory_grams_are_excluded():
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    for i in range(4):
        t = _seq(sig, "aaaaaaaa", ["Read", "Read", "Read", f"Sep{i}"], t)
        t = _seq(sig, "aaaaaaaa", ["memory_search", "memory_recall", "mcp__sibyl-memory__memory_list",
                                   f"Sop{i}"], t)
        t = _seq(sig, "aaaaaaaa", ["sibyl_search", "sibyl_recall", "sibyl_list", f"Sup{i}"], t)
    names = [c.hints["tool_names"] for c in _detect_tool_sequence(sig, now)]
    assert ["Read", "Read", "Read"] not in names
    assert ["memory_search", "memory_recall", "mcp__sibyl-memory__memory_list"] not in names
    assert ["sibyl_search", "sibyl_recall", "sibyl_list"] not in names
    assert names == []


def test_longest_gram_wins():
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    for i in range(3):
        t = _seq(sig, "aaaaaaaa", ["Grep", "Read", "Edit", "Bash", "Write", f"Sep{i}"], t)
    got = _detect_tool_sequence(sig, now)
    assert [c.hints["tool_names"] for c in got] == [["Grep", "Read", "Edit", "Bash", "Write"]]
    assert got[0].slug == "seq-grep-read-edit"


def test_one_loop_is_one_pattern():
    now = time.time()
    sig = SG.empty()
    _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash"] * 5, now - 3600)
    got = _detect_tool_sequence(sig, now)
    assert len(got) == 1 and got[0].hints["tool_names"] == ["Read", "Edit", "Bash"]


def test_shape_distinct_sequences_get_distinct_slugs_and_mcp_names_slug_as_server_tool():
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    s1, s2 = SG.arg_shape({"a": 1}), SG.arg_shape({"b": 1})
    names = ["mcp__github__get_pr", "Edit", "Bash"]
    for i in range(3):
        t = _seq(sig, "aaaaaaaa", names + [f"Sep{i}"], t, shapes=[s1, s1, s1, S])
        t = _seq(sig, "aaaaaaaa", names + [f"Sop{i}"], t, shapes=[s2, s1, s1, S])
    got = [c for c in _detect_tool_sequence(sig, now) if c.hints["tool_names"] == names]
    slugs = _slugs(got)
    assert len(got) == 2 and len(set(slugs)) == 2
    assert all(s.startswith("seq-github-get-pr-edit-bash-") and len(s) == len("seq-github-get-pr-edit-bash-") + 4
               for s in slugs)


# ----------------------------------------------------------------------
# correction_loop
# ----------------------------------------------------------------------

def _writes(sig, h, rows):
    SG.apply_entity_writes(sig, [(h, op, ts, s8) for op, ts, s8 in rows])


def _client(tmp_path, entitle_as=None):
    if entitle_as is not None:
        entitle_as("pro")
    return MemoryClient.local(tmp_path / "m.db")


def test_correction_loop_needs_a_ten_minute_span(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    client.set_entity("projects", "acme", {"x": 1})
    tid = client.get_tenant()
    h = SG.entity_hash(tid, "projects", "acme")
    now = time.time()
    sig = SG.empty()
    _writes(sig, h, [("s", now - 900, "aaaaaaaa"), ("s", now - 700, "aaaaaaaa"), ("s", now - 361, "aaaaaaaa")])
    assert _detect_correction_loop(sig, client.storage, tid, now) == []      # 9 minutes: the save at work
    sig = SG.empty()
    _writes(sig, h, [("s", now - 900, "aaaaaaaa"), ("s", now - 700, "aaaaaaaa"), ("s", now - 300, "aaaaaaaa")])
    got = _detect_correction_loop(sig, client.storage, tid, now)
    assert len(got) == 1
    c = got[0]
    assert c.slug == "fix-projects-acme" and c.confidence == 0.65 and c.hints["entity"] == "projects/acme"
    assert [e["snippet"] for e in c.events] == ["set_entity projects/acme"] * 3
    _writes(sig, h, [("s", now - 60, "bbbbbbbb")])
    assert _detect_correction_loop(sig, client.storage, tid, now)[0].confidence == 0.75


def test_delete_then_set_within_24_hours(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    client.set_entity("people", "bob", {"x": 1})
    tid = client.get_tenant()
    h = SG.entity_hash(tid, "people", "bob")
    now = time.time()
    sig = SG.empty()
    _writes(sig, h, [("a", now - 3 * 3600, "aaaaaaaa"), ("s", now - 3600, "bbbbbbbb")])
    got = _detect_correction_loop(sig, client.storage, tid, now)
    assert len(got) == 1 and got[0].hints["deleted_then_recreated"] is True
    assert got[0].confidence == 0.75
    sig = SG.empty()
    _writes(sig, h, [("d", now - 26 * 3600, "aaaaaaaa"), ("s", now - 3600, "aaaaaaaa")])
    assert _detect_correction_loop(sig, client.storage, tid, now) == []


def test_removed_entity_is_named_by_hash_only(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    tid = client.get_tenant()
    h = SG.entity_hash(tid, "projects", "gone")
    now = time.time()
    sig = SG.empty()
    _writes(sig, h, [("s", now - 1200, "aaaaaaaa"), ("s", now - 800, "aaaaaaaa"), ("s", now - 100, "aaaaaaaa")])
    c = _detect_correction_loop(sig, client.storage, tid, now)[0]
    assert c.slug == f"fix-{h[:8]}" and c.hints["entity"] == REMOVED_ENTITY
    assert c.events[0]["snippet"] == "set_entity (removed entity)"
    body, _ = LocalDeterministicSummarizer().summarize(c.kind, c.events, c.hints)
    assert REMOVED_ENTITY in body and "gone" not in body


# ----------------------------------------------------------------------
# Summarizer branches
# ----------------------------------------------------------------------

def test_summarizer_bodies_for_both_kinds(tmp_path, entitle_as):
    now = time.time()
    sig = SG.empty()
    t = now - 3600
    for i in range(3):
        t = _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash", f"Sep{i}"], t)
    c = _detect_tool_sequence(sig, now)[0]
    body, title = LocalDeterministicSummarizer().summarize(c.kind, c.events, c.hints)
    assert "Recurring tool sequence: `Read -> Edit -> Bash`, seen 3 times in one session since" in body
    assert "The arguments had the same shape each time." in body
    assert "## Suggested skill\n\n1. Read\n2. Edit\n3. Bash" in body
    assert "Fill in what each step checks; the sequence is the skeleton, you know the intent." in body
    assert chr(0x2014) not in body and title == "Recurring sequence: Read, Edit, Bash"

    client = _client(tmp_path, entitle_as)
    client.set_entity("projects", "acme", {"x": 1})
    tid = client.get_tenant()
    h = SG.entity_hash(tid, "projects", "acme")
    sig = SG.empty()
    _writes(sig, h, [("s", now - 5 * 3600, "aaaaaaaa"), ("s", now - 4 * 3600, "aaaaaaaa"),
                     ("s", now - 3 * 3600, "aaaaaaaa"), ("s", now - 2 * 3600, "aaaaaaaa"),
                     ("s", now - 600, "bbbbbbbb")])
    c = _detect_correction_loop(sig, client.storage, tid, now)[0]
    body, _ = LocalDeterministicSummarizer().summarize(c.kind, c.events, c.hints)
    assert "The entity `projects/acme` was rewritten 4 times in one session on" in body
    assert ", and 1 more time since." in body
    assert "A short checklist for how this entity is recorded would save the rewrites." in body
    assert "1. What to check before writing this entity." in body


# ----------------------------------------------------------------------
# The pipeline: Learner.run(signals=), muted kinds, blocked slugs, relevance
# ----------------------------------------------------------------------

def _loop_sig(now):
    sig = SG.empty()
    t = now - 7200
    for i in range(3):
        t = _seq(sig, "aaaaaaaa", ["Read", "Edit", "Bash", f"Sep{i}"], t, step=300.0)
    return sig


def test_learner_run_with_signals_proposes_even_with_an_empty_journal(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    learner = client.learner()
    now = time.time()
    rep = learner.run(signals=_loop_sig(now))
    assert rep.events_scanned == 0 and rep.proposals_made == 1 and len(rep.signal_keys) == 1
    p = learner.get_proposal(rep.proposal_ids[0])
    assert p.pattern_kind == "tool_sequence" and p.summarizer == "local-deterministic"
    assert p.proposed_slug == "seq-read-edit-bash" and len(p.evidence) == 3
    assert all(e["event_id"] is None and e["snippet"] == "Read -> Edit -> Bash" for e in p.evidence)
    # the same run again: pending, not proposed twice
    assert learner.run(signals=_loop_sig(now)).proposals_made == 0
    # no signals, no journal: nothing (R1 behaviour unchanged)
    assert learner.run().proposals_made == 0


def test_both_kinds_respect_muted_kinds_blocked_slugs_and_the_cool_off(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    client.set_entity("projects", "acme", {"x": 1})
    learner = client.learner()
    now = time.time()
    tid = client.get_tenant()
    sig = _loop_sig(now)
    _writes(sig, SG.entity_hash(tid, "projects", "acme"),
            [("s", now - 1800, "aaaaaaaa"), ("s", now - 1200, "aaaaaaaa"), ("s", now - 100, "aaaaaaaa")])
    assert learner.run(signals=sig, exclude_kinds={"tool_sequence", "correction_loop"}).proposals_made == 0
    rep = learner.run(signals=sig)
    kinds = sorted(learner.get_proposal(i).pattern_kind for i in rep.proposal_ids)
    assert kinds == ["correction_loop", "tool_sequence"]
    for pid in rep.proposal_ids:
        learner.reject_proposal(pid)
    assert learner.run(signals=sig).proposals_made == 0           # rejected: 30-day cool-off
    with client.storage.transaction() as con:
        con.execute("UPDATE skill_proposals SET reviewed_at = ?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 31 * DAY)),))
    assert learner.run(signals=sig).proposals_made == 2           # cool-off over (one rejection)
    for p in learner.list_proposals(status="pending"):
        learner.reject_proposal(p.id)
    with client.storage.transaction() as con:
        con.execute("UPDATE skill_proposals SET reviewed_at = ?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 31 * DAY)),))
    assert learner.run(signals=sig).proposals_made == 0           # twice rejected: muted
    assert learner.run(signals=sig, unmuted={"seq-read-edit-bash": now}).proposals_made == 1


def test_is_relevant_waives_the_span_rule_for_tool_sequence_and_correction_loop_only():
    now = time.time()
    ev = [{"ts": now - 600}, {"ts": now - 300}, {"ts": now - 60}]     # ten minutes, one session
    row = {"status": "pending", "confidence": 0.6, "evidence": json.dumps(ev)}
    assert PR.is_relevant(dict(row, pattern_kind="tool_sequence"), now, anchor=now - 3600)
    assert PR.is_relevant(dict(row, pattern_kind="correction_loop"), now, anchor=now - 3600)   # R2F N1
    assert not PR.is_relevant(dict(row, pattern_kind="repeated_action"), now, anchor=now - 3600)
    # every other rule still applies to tool_sequence
    assert not PR.is_relevant(dict(row, pattern_kind="tool_sequence", confidence=0.55), now, anchor=now - 3600)
    assert not PR.is_relevant(dict(row, pattern_kind="tool_sequence"), now, anchor=now)
    assert not PR.is_relevant(dict(row, pattern_kind="tool_sequence",
                                   evidence=json.dumps(ev[:2])), now, anchor=now - 3600)
    assert not PR.is_relevant(dict(row, pattern_kind="tool_sequence"), now,
                              anchor=now - 3600, muted={"tool_sequence"})
    assert {"tool_sequence", "correction_loop"} <= set(PR.KINDS)


def test_pending_line_names_a_tool_sequence_slug(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    now = time.time()
    rep = client.learner().run(signals=_loop_sig(now))
    assert rep.proposals_made == 1
    names, total = PR.read_relevant(client.storage.db_path, client.get_tenant(), now, anchor=now - 3 * 3600)
    assert names == ["seq-read-edit-bash"] and total == 1
    assert "seq-read-edit-bash" in (R.skills_line(names, total) or "")


def test_accepted_tool_sequence_is_a_valid_skill_and_a_reference_copy(tmp_path, entitle_as):
    client = _client(tmp_path, entitle_as)
    rep = client.learner().run(signals=_loop_sig(time.time()))
    p = client.learner().get_proposal(rep.proposal_ids[0])
    text = SK.proposal_skill_text(p)
    name = SK.skill_name(p.proposed_slug)
    assert SK.validate_skill_text(text, name) == []
    client.accept_skill_proposal(p.id)
    doc = client.get_reference(f"skill/{p.proposed_slug}")
    assert "Read -> Edit -> Bash" in json.dumps(doc)


def test_run_learner_uses_the_file_and_marks_what_was_proposed(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = MemoryClient.local(tmp_path / "m.db")
    now = time.time()
    SG.save_unlocked(home, _loop_sig(now), now=now)
    rep = PR.run_learner(client, home=home, now=now)
    assert rep is not None and rep.proposals_made == 1
    k = rep.signal_keys[0]
    assert SG.load_unlocked(home)["ngrams"][k]["proposed_at"] == pytest.approx(now)
    # recording off: the learner reads no signals
    client2 = MemoryClient.local(tmp_path / "m2.db")
    SG.set_enabled(home, False)
    assert PR.run_learner(client2, home=home, now=now).proposals_made == 0


def test_canary_entity_is_named_only_in_memory_db(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    client = MemoryClient.local(tmp_path / "m.db")
    client.enable_lifecycle_counting(home)
    for i in range(3):
        client.set_entity("people", CANARY_ENTITY, {"v": i})
    SG.flush_all()
    ST.flush_all()

    def _spread(sig, life):                     # the writes happened over 20 minutes
        for rows in sig["entity_writes"].values():
            for j, r in enumerate(rows):
                r[1] = time.time() - 1200 + 600 * j
        return None, True
    SG.update(home, _spread)
    rep = PR.run_learner(client, home=home)
    assert rep is not None and rep.proposals_made == 1
    p = client.learner().get_proposal(rep.proposal_ids[0])
    assert p.pattern_kind == "correction_loop" and CANARY_ENTITY in p.proposed_body
    ST.flush_all()
    SG.flush_all()
    for f in (home / ".sibyl-memory").iterdir():
        if f.is_file() and f.suffix != ".db" and "-wal" not in f.name and "-shm" not in f.name:
            assert CANARY_ENTITY.encode() not in f.read_bytes(), f.name


def test_r2f_n1_a_ten_minute_correction_loop_is_relevant_but_keeps_every_other_rule():
    """R2F N1: the detector fires at a 10-minute span; is_relevant no longer
    demands 30 minutes of it. Confidence, evidence count, recency, the anchor
    and muting still apply."""
    now = time.time()
    ev = [{"ts": now - 11 * 60}, {"ts": now - 6 * 60}, {"ts": now - 60}]   # 10 minutes, under 30
    row = {"status": "pending", "confidence": 0.65, "evidence": json.dumps(ev), "pattern_kind": "correction_loop"}
    assert "correction_loop" in PR.SPAN_WAIVED_KINDS
    assert PR.is_relevant(row, now, anchor=now - 3600)
    assert not PR.is_relevant(dict(row, confidence=0.55), now, anchor=now - 3600)
    assert not PR.is_relevant(dict(row, evidence=json.dumps(ev[:2])), now, anchor=now - 3600)
    assert not PR.is_relevant(row, now, anchor=now)
    assert not PR.is_relevant(row, now, anchor=now - 3600, muted={"correction_loop"})
    assert not PR.is_relevant(dict(row, status="rejected"), now, anchor=now - 3600)
