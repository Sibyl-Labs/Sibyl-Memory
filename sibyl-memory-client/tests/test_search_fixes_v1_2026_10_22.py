"""V1 panel fixes, search + reference themes (PANEL-SYNTHESIS D, E; 2026-10-22).

D  prep gate: PREP side reads the head (2,000 chars) of a reference document,
   the TERM side reads the WHOLE document (rowid-only FTS pre-screen, then the
   _TERM_RE lookbehind on the full body). 0.9.0 kept "Draft ... approved by the
   board" with the decision late in the text; the 2,000-char head dropped it
   (NOCTURNE exp1 EXP4).
D  stage 0: the exact `lower(name) = ?` probe runs for names under 4 chars
   (IBM, AWS, Bob); the partial phrase keeps the guard. Operator decision 2: an
   exact whole-query name match (match "name" only) survives the negation
   abstain gate; everything else still abstains.
E  delete_reference supersedes an accepted skill proposal pointing at the key,
   in the same transaction, and reports edges removed.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from sibyl_memory_client import MemoryClient, entity_ref, reference_ref
from sibyl_memory_client.multi_record import (
    _TERM_RE, _pure_prep, multi_record_search)
from sibyl_memory_client.testing import NullEntitlementGate
from sibyl_memory_client.verdicts import VerdictCode


def _client(tmp_path, name="m.db", tenant="t") -> MemoryClient:
    return MemoryClient.local(str(tmp_path / name), tenant_id=tenant,
                              entitlement_gate=NullEntitlementGate())


def _keys(res):
    return [h["key"] for h in res]


# ----------------------------------------------------------------------
# D: prep gate, TERM side whole-document
# ----------------------------------------------------------------------
def test_exp4_late_decision_is_kept_on_a_terminal_query(tmp_path):
    """NOCTURNE exp1 EXP4, verbatim shape: 'Draft' in the head, the decision
    after char 2,000. 0.9.0 kept it; the head-only gate dropped it."""
    c = _client(tmp_path)
    filler = " ".join(["lorem ipsum dolor sit amet consectetur"] * 120)
    body = ("Draft budget for the zebrafish facility. " + filler +
            " The zebrafish budget was approved by the board on 2026-03-01.")
    assert body.index("approved") > 2000
    c.set_reference("doc/budget/zebrafish", body, metadata={"title": "Zebrafish budget"})
    c.set_entity("project", "Aquarium", {"summary": "unrelated"})
    res = multi_record_search(c, "zebrafish budget approved", limit=5)
    assert "doc/budget/zebrafish" in _keys(res)
    assert res.verdict.code is VerdictCode.OK


def test_inverse_regression_20kb_draft_at_100_approved_at_10000(tmp_path):
    c = _client(tmp_path)
    words = [f"w{i}" for i in range(4000)]
    text = " ".join(words)
    body = (text[:100] + " draft " + text[100:10_000] +
            " approved by the board " + text[10_000:20_000])
    assert len(body) > 20_000
    assert 90 < body.index(" draft ") < 120 and 9_990 < body.index(" approved ") < 10_020
    c.set_reference("doc/long-decision", "quorvex " + body)
    # controls: same draft head, the late marker negated / absent
    c.set_reference("doc/long-negated", "quorvex " + body.replace(
        " approved by the board ", " not approved by the board "))
    c.set_reference("doc/long-nothing", "quorvex " + body.replace(
        " approved by the board ", " reviewed by the board "))
    # a doc that says approved early and only drafts late stays (as before)
    c.set_reference("doc/early-approved", "quorvex approved " + text)
    res = multi_record_search(c, "approved quorvex", limit=10)
    keys = set(_keys(res))
    assert "doc/long-decision" in keys            # the regression
    assert "doc/long-negated" not in keys         # lookbehind re-checked on the full body
    assert "doc/early-approved" in keys
    assert "doc/long-nothing" not in keys         # no terminal text: the query term is
    # missing, so it never reaches the gate as a match anyway; the prep check
    # below covers the gate directly.


def test_gate_input_for_reference_rows(tmp_path):
    """_load_rows(prep_only) is the gate's input: a prep-marked head whose
    document holds a terminal marker anywhere is NOT purely preparatory; a
    negated or absent late marker leaves it pure prep."""
    c = _client(tmp_path)
    pad = "x " * 3000
    c.set_reference("a", "draft plan " + pad + " final sign-off")
    c.set_reference("b", "draft plan " + pad + " not final yet")
    c.set_reference("c", "draft plan " + pad + " more words")
    c.set_reference("d", "draft plan " + pad + " the deal was\finalized")  # FF + "inalized": "\\finalized"
    c.set_reference("e", "draft plan " + pad + " finalizedé")              # prefix form
    c.set_reference("f", "plain head " + pad + " draft late, final late")  # PREP not in head
    con = sqlite3.connect(str(tmp_path / "m.db"))
    rid = dict(con.execute("SELECT doc_key, rowid FROM reference_documents"))
    con.close()
    rows = c._load_rows([{"tier": "reference", "_rid": rid[k]} for k in "abcdef"],
                        prep_only=True)
    pure = {k: _pure_prep(rows.get(("reference", rid[k]), {"prep": ""})["prep"])
            for k in "abcdef"}
    assert pure == {"a": False, "b": True, "c": True, "d": False, "e": False, "f": False}


def test_term_prescreen_is_a_superset_of_the_full_body_regex(tmp_path, monkeypatch):
    """Every reference body whose full text matches _TERM_RE (as the gate
    serialises it) is in the FTS pre-screen."""
    import random
    c = _client(tmp_path)
    rng = random.Random(7)
    alphabet = (list("abcdefghijklmnopqrstuvwxyz -_.\\\"'\t\r\n\f\b") +
                ["é", "中", "final", "inal", "approved", "resolved", "esolved",
                 "bound", "ound", "sent", "decision", "not ", "signed", "closed"])
    bodies = []
    for i in range(1500):
        bodies.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))))
    for i, b in enumerate(bodies):
        c.set_reference(f"r/{i}", b)
    con = sqlite3.connect(str(tmp_path / "m.db"))
    rows = con.execute("SELECT rowid, body FROM reference_documents").fetchall()
    con.close()
    with c._storage.connection() as conn:
        screened = c._term_candidates(conn, [r[0] for r in rows])
    hits = 0
    for rid, body in rows:
        if _TERM_RE.search(json.dumps(body).lower()):
            hits += 1
            assert rid in screened, body
    assert hits > 200


def test_term_prescreen_falls_back_to_every_row_on_fts_error(tmp_path):
    c = _client(tmp_path)

    class Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table")

    assert c._term_candidates(Boom(), [1, 2, 3]) == {1, 2, 3}


# ----------------------------------------------------------------------
# D: stage 0, short exact names + the negation gate (operator decision 2)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("q", ["IBM", "ibm", "company:IBM", " Ibm "])
def test_three_char_exact_name_is_reachable(tmp_path, q):
    c = _client(tmp_path)
    c.set_entity("company", "IBM", {"kind": "vendor", "summary": "mainframes"})
    c.set_entity("note", "n1", {"text": "ibm mainframe contract"})
    res = multi_record_search(c, q, limit=5)
    assert res[0]["key"] == "IBM" and res[0]["match"] == "name"
    assert res.verdict.name_matches == 1


def test_short_names_bob_and_aws(tmp_path):
    c = _client(tmp_path)
    c.set_entity("person", "Bob", {"role": "cto"})
    c.set_entity("vendor", "AWS", {"kind": "cloud"})
    assert multi_record_search(c, "Bob", limit=3)[0]["match"] == "name"
    assert multi_record_search(c, "aws", limit=3)[0]["key"] == "AWS"


def test_short_query_runs_no_partial_phrase(tmp_path):
    """The <4-char guard stays on name_partial: 'Sam' is never a phrase hit
    for 'Sam Lee' / 'Sam Ortiz'."""
    c = _client(tmp_path)
    c.set_entity("person", "Sam", {"role": "ops"})
    c.set_entity("person", "Sam Lee", {"role": "cfo"})
    c.set_entity("person", "Sam Ortiz", {"role": "eng"})
    got = c._name_matches("sam")
    assert [(h["key"], h["match"]) for h in got] == [("Sam", "name")]
    assert c._name_matches("sa") == []        # no entity named 'sa'


def test_exact_name_survives_negation_abstain(tmp_path):
    c = _client(tmp_path)
    c.set_entity("project", "Not Invented Here", {"summary": "nih audit"})
    c.set_entity("note", "n", {"text": "the invented here audit was not approved"})
    for q in ("Not Invented Here", "not invented here", "project:Not Invented Here"):
        res = multi_record_search(c, q, limit=5)
        assert [(h["key"], h["match"]) for h in res] == [("Not Invented Here", "name")], q
        assert res.verdict.code is VerdictCode.OK
        assert res.verdict.name_matches == 1


def test_negation_still_abstains_without_an_exact_name(tmp_path):
    c = _client(tmp_path)
    c.set_entity("project", "Not Invented Here", {"summary": "nih audit"})
    c.set_entity("deal", "Contract", {"text": "contract approved"})
    # a partial phrase of the name is not an exact match: abstains
    res = multi_record_search(c, "not invented", limit=5)
    assert res == [] and res.verdict.code is VerdictCode.NEGATION_ABSTAIN
    # a different category prefix is not a match: abstains
    # ('person' has no corpus support here, so the df gate answers first)
    res = multi_record_search(c, "person:Not Invented Here", limit=5)
    assert res == [] and res.verdict.code in (VerdictCode.NEGATION_ABSTAIN,
                                              VerdictCode.ABSTAINED_ON)
    c.set_entity("person", "Ann", {"x": 1})
    res = multi_record_search(c, "person:Not Invented Here", limit=5)
    assert res == [] and res.verdict.code is VerdictCode.NEGATION_ABSTAIN
    # an ordinary negated question: abstains
    res = multi_record_search(c, "contract not approved", limit=5)
    assert res == [] and res.verdict.code is VerdictCode.NEGATION_ABSTAIN


def test_negated_name_query_returns_only_the_name_rows(tmp_path):
    """The name rows survive, nothing else does: no name_partial rows and no
    text rows ride along on a negated query."""
    c = _client(tmp_path)
    c.set_entity("project", "Not Invented Here", {"summary": "nih"})
    c.set_entity("project", "Not Invented Here v2", {"summary": "nih again"})
    c.set_entity("note", "n", {"text": "not invented here " * 5})
    res = multi_record_search(c, "not invented here", limit=10)
    assert [(h["key"], h["match"]) for h in res] == [("Not Invented Here", "name")]


def test_abstained_on_still_wins_over_a_name(tmp_path):
    c = _client(tmp_path)
    c.set_entity("person", "Maya Patel", {"role": "lead"})
    res = multi_record_search(c, "maya patel zqxvwyk", limit=5)
    assert res == [] and res.verdict.code is VerdictCode.ABSTAINED_ON


# ----------------------------------------------------------------------
# E: delete_reference vs accepted skill proposals
# ----------------------------------------------------------------------
def _accepted_proposal(c, key: str) -> str:
    pid = str(uuid.uuid4())
    with c._storage.transaction() as conn:
        conn.execute(
            "INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, "
            "proposed_body, evidence, confidence, summarizer, status, accepted_doc_key, "
            "reviewed_at) VALUES (?, ?, 'repeated_action', ?, 'body', '[]', 0.9, "
            "'local-deterministic', 'accepted', ?, '2026-10-01T00:00:00Z')",
            (pid, c._tenant_id, key.split("/", 1)[-1], key))
    return pid


def _proposal(c, pid):
    with c._storage.connection() as conn:
        r = conn.execute("SELECT status, accepted_doc_key, review_note FROM skill_proposals "
                         "WHERE id = ?", (pid,)).fetchone()
    return dict(r)


def test_delete_reference_supersedes_the_accepted_proposal(tmp_path):
    c = _client(tmp_path)
    c.set_reference("skill/weekly-report", "# weekly report skill")
    pid = _accepted_proposal(c, "skill/weekly-report")
    other = _accepted_proposal(c, "skill/other")
    c.set_reference("skill/other", "# other")
    assert c.delete_reference("skill/weekly-report") is True
    p = _proposal(c, pid)
    assert p["status"] == "superseded" and p["accepted_doc_key"] is None
    assert "skill/weekly-report" in (p["review_note"] or "")
    assert _proposal(c, other)["status"] == "accepted"      # untouched


def test_delete_reference_details_reports_edges_and_proposals(tmp_path):
    c = _client(tmp_path)
    c.set_entity("project", "Apollo", {"s": 1})
    c.set_reference("doc/a", "text", links=[{"to": entity_ref("project", "Apollo"),
                                             "relation": "about"}])
    c.link(entity_ref("project", "Apollo"), reference_ref("doc/a"), "source_of")
    pid = _accepted_proposal(c, "doc/a")
    out = c.delete_reference_details("doc/a")
    assert out == {"deleted": True, "edges_removed": 2, "proposals_superseded": 1}
    assert _proposal(c, pid)["status"] == "superseded"
    assert c.delete_reference_details("doc/a") == {
        "deleted": False, "edges_removed": 0, "proposals_superseded": 0}


def test_delete_reference_proposal_update_is_tenant_scoped(tmp_path):
    a = _client(tmp_path, name="x.db", tenant="ta")
    b = _client(tmp_path, name="x.db", tenant="tb")
    a.set_reference("skill/s", "a")
    b.set_reference("skill/s", "b")
    pa = _accepted_proposal(a, "skill/s")
    pb = _accepted_proposal(b, "skill/s")
    a.delete_reference("skill/s")
    assert _proposal(a, pa)["status"] == "superseded"
    assert _proposal(b, pb)["status"] == "accepted"


def test_delete_reference_rolls_back_the_proposal_with_the_delete(tmp_path, monkeypatch):
    c = _client(tmp_path)
    c.set_reference("skill/x", "x")
    pid = _accepted_proposal(c, "skill/x")
    from sibyl_memory_client import _links

    def boom(*a, **k):
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(_links, "cascade_node", boom)
    with pytest.raises(Exception):
        c.delete_reference("skill/x")
    assert _proposal(c, pid)["status"] == "accepted"
    assert c.get_reference("skill/x") is not None


def test_term_anywhere_equals_the_regex():
    import random
    from sibyl_memory_client.multi_record import _term_anywhere
    rng = random.Random(11)
    alpha = (list("abcdefghijklmnopqrstuvwxyz -_.\\\"'\t") + ["é", "中", "final", "sent",
             "not ", "approved", "bound", "finalized", "decision", "no", "t ", "absent"])
    for _ in range(50_000):
        s = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 25)))
        for t in (s, json.dumps(s).lower()):
            assert _term_anywhere(t) == bool(_TERM_RE.search(t)), t
