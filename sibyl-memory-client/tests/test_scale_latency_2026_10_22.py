"""2.D latency follow-up (2026-10-22): the fixes must not move a result.

* ``_load_rows`` batch loads go through the primary key, never a tenant index
  (``+tenant_id``): EXPLAIN QUERY PLAN pins it.
* The prep-gate prefilter (``multi_record._PREP_FTS_QUERY`` +
  ``MemoryClient._prep_candidates``) is a SUPERSET of the rows ``_PREP_RE``
  can match: adversarial and random bodies in every tier give the same prep
  verdict with and without the prefilter.
* ``_PREP_RE`` is pinned verbatim: changing it means revisiting the prefilter.
"""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client import client as client_mod
from sibyl_memory_client import multi_record as mr
from sibyl_memory_client.multi_record import _PREP_RE, _pure_prep, multi_record_search

import _scale_corpus as sc


def test_prep_regex_is_pinned_to_the_prefilter():
    # If this fails you changed _PREP_RE: extend _PREP_MARKERS /
    # _PREP_CTRL_TAILS so the FTS prefilter stays a superset, then update here.
    assert _PREP_RE.pattern == (
        r'\b(draft|triage|forecast|planning|proposed|tentative|pending|agenda|'
        r'scheduled|rehearsal|sample|option|wip|follow-?up)\b|work in progress')


def test_load_rows_uses_the_primary_key_not_a_tenant_index(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t")
    c.set_entity("person", "a", {"x": 1})
    con = sqlite3.connect(str(tmp_path / "m.db"))
    for prep_only in (False, True):
        for tier, sql in client_mod._load_sql(prep_only).items():
            plan = " ".join(str(r[3]) for r in con.execute(
                "EXPLAIN QUERY PLAN " + sql.format("?,?,?"), ("t", 1, 2, 3)))
            assert ("PRIMARY KEY" in plan or "sqlite_autoindex" in plan), (tier, plan)
            assert "tenant" not in plan, (tier, plan)
    con.close()


_MARKERS = ["draft", "triage", "forecast", "planning", "proposed", "tentative",
            "pending", "agenda", "scheduled", "rehearsal", "sample", "option",
            "wip", "follow-up", "followup", "work in progress", "Draft", "PENDING",
            "Work In Progress"]
_BEFORE = ["", " ", "\t", "\r", "\f", "\n", "\b", "\\", '"', "(", "_", "x",
           "é", "中", "́", " ", "home", "1"]
_AFTER = ["", " ", ".", "é", "中", "́", "s", "ing", "ed", "_", "-", "!",
          "٣", "\U0001f600", " ", "ive", "e", "\t"]


def _texts(seed: int = 3, n_random: int = 600) -> list[str]:
    out = []
    for m in _MARKERS:
        for b in _BEFORE:
            for a in _AFTER:
                out.append(f"note {b}{m}{a} end")
    # a control character whose JSON escape supplies the marker's first letter:
    # TAB + "riage" serialises to "\\triage", which the regex reads as "triage"
    for ctrl, tail in (("\t", "riage"), ("\t", "entative"), ("\r", "ehearsal"),
                       ("\f", "orecast"), ("\f", "ollow-up"), ("\f", "ollowup"),
                       ("\f", "ollowup\u00e9"), ("\t", "riage中")):
        out.append(f"x {ctrl}{tail} y")
        out.append(f"{ctrl}{tail}")
    rng = random.Random(seed)
    alphabet = (list("abcdefghijklmnopqrstuvwxyz -_.\\\"'\t\r\n\f") +
                ["é", "中", "́", "draft", "riage", "progress", "work in ",
                 "follow", "up", "approved", "final", "not "])
    for _ in range(n_random):
        out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30))))
    return out


@pytest.fixture()
def adversarial_store(tmp_path):
    db = tmp_path / "adv.db"
    c = MemoryClient.local(db, tenant_id="t")
    sc.lift_cap(c)
    texts = _texts()
    for i, t in enumerate(texts):
        c.set_entity("note", f"e{i}", {"text": t})
        c.set_reference(f"doc/{i}", t)
        if i % 3 == 0:
            c.set_state(f"s{i}", {"text": t})
        if i % 5 == 0:
            c.write_event(evaluated=t)
    # rows written by another JSON encoder: ASCII escapes, \\u-escaped letters
    con = sqlite3.connect(str(db))
    for i, t in enumerate(texts[:400]):
        con.execute("INSERT INTO entities (id, tenant_id, category, name, status, body) "
                    "VALUES (?, 't', 'raw', ?, 'active', ?)",
                    (f"r{i}", f"r{i}", json.dumps({"text": t})))
    for i, word in enumerate(["\\u0064raft", "work\\u0020in progress",
                              "\\u0009riage", "pen\\u0064ing"]):
        con.execute("INSERT INTO entities (id, tenant_id, category, name, status, body) "
                    "VALUES (?, 't', 'esc', ?, 'active', ?)",
                    (f"x{i}", f"x{i}", '{"text": "a ' + word + ' b"}'))
    # reference rows whose gate input is metadata.status, not the body
    con.execute("INSERT INTO reference_documents (tenant_id, doc_key, body, metadata) "
                "VALUES ('t', 'meta/1', 'plain words', '{\"status\": \"draft\"}')")
    con.execute("INSERT INTO reference_documents (tenant_id, doc_key, body, metadata) "
                "VALUES ('t', 'meta/2', 'plain words', '{\"st\\u0061tus\": \"pending\"}')")
    con.commit()
    con.close()
    return c, db


def _all_candidates(db):
    con = sqlite3.connect(str(db))
    cands = []
    for tier, table in (("entity", "entities"), ("state", "state_documents"),
                        ("reference", "reference_documents")):
        cands += [{"tier": tier, "_rid": r[0]}
                  for r in con.execute(f"SELECT rowid FROM {table}")]
    cands += [{"tier": "journal", "_rid": r[0]}
              for r in con.execute("SELECT id FROM journal_events")]
    con.close()
    return cands


def test_prep_prefilter_is_a_superset_of_the_regex(adversarial_store, monkeypatch):
    c, db = adversarial_store
    cands = _all_candidates(db)
    fast = c._load_rows(cands, prep_only=True)
    monkeypatch.setattr(MemoryClient, "_prep_candidates",
                        lambda self, conn, tier, ids: ids)
    full = c._load_rows(cands, prep_only=True)
    assert len(full) == len(cands)
    matched = 0
    for k, row in full.items():
        if _PREP_RE.search(row["prep"]):
            matched += 1
            assert k in fast and fast[k] == row, (k, row["prep"][:120])
        assert _pure_prep(fast.get(k, {"prep": ""})["prep"]) == _pure_prep(row["prep"])
    assert matched > 500                        # the corpus really exercises the gate
    assert len(fast) < len(full)                # and the prefilter really prunes


def test_terminal_queries_identical_with_and_without_prefilter(adversarial_store,
                                                               monkeypatch):
    c, _db = adversarial_store
    qs = ["approved note end", "final note", "approved draft", "closed pending end",
          "final work progress"]
    fast = [(list(multi_record_search(c, q, limit=10)),
             multi_record_search(c, q, limit=10).verdict.as_dict()) for q in qs]
    prep_drops = [multi_record_search(c, q, limit=10).verdict.gates.prep_filter
                  for q in qs]
    monkeypatch.setattr(MemoryClient, "_prep_candidates",
                        lambda self, conn, tier, ids: ids)
    full = [(list(multi_record_search(c, q, limit=10)),
             multi_record_search(c, q, limit=10).verdict.as_dict()) for q in qs]
    assert fast == full
    assert any(prep_drops), prep_drops


def test_prefilter_falls_back_to_every_row_on_fts_error(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t")
    c.set_entity("person", "a", {"x": "draft"})

    class Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table")

    assert c._prep_candidates(Boom(), "entity", [1, 2, 3]) == [1, 2, 3]


def test_prefilter_query_is_valid_fts(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="t")
    c.set_entity("person", "a", {"x": "a draft here"})
    c.set_entity("person", "b", {"x": "nothing"})
    con = sqlite3.connect(str(tmp_path / "m.db"))
    rows = con.execute("SELECT rowid FROM entities_fts WHERE entities_fts MATCH ?",
                       (f"body : ({mr._PREP_FTS_QUERY})",)).fetchall()
    con.close()
    assert len(rows) == 1
