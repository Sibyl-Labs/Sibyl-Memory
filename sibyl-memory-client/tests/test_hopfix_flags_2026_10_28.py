"""Default-off retrieval flags (0.10.0): SIBYL_MEMORY_HOP_OWNERS and
SIBYL_MEMORY_RANKED_FALLBACK.

Unset, both leave results, verdicts and the verdict envelope exactly as the
merged graph line (S2) produced them. Set to "1" they reproduce the declared
configuration of the agent-bench harness runs.
"""
from __future__ import annotations

import pytest

from sibyl_memory_client import MemoryClient, entity_ref
from sibyl_memory_client.multi_record import multi_record_search
from sibyl_memory_client.testing import NullEntitlementGate
from sibyl_memory_client.verdicts import VerdictCode

OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

# The verdict envelope keys of the merged graph line, in order (no ranked_appended).
S2_KEYS = ["code", "tokens", "gate", "gate_drops", "best_pre_gate_coverage", "tokens_total",
           "tokens_scored", "dropped_function", "candidates", "returned", "tiers_hit",
           "name_matches", "recall_depth", "scope", "links_ms", "decoration_note", "abstained",
           "recovery", "explain"]

FLAGS = ("SIBYL_MEMORY_HOP_OWNERS", "SIBYL_MEMORY_RANKED_FALLBACK",
         "SIBYL_MEMORY_RANKED_MIN_TOKENS", "SIBYL_MEMORY_RANKED_EXCLUDE_CATEGORIES")


@pytest.fixture(autouse=True)
def _flags_unset(monkeypatch):
    for f in FLAGS:
        monkeypatch.delenv(f, raising=False)


def _client(tmp_path, tenant=None) -> MemoryClient:
    kw = {"tenant_id": tenant} if tenant else {}
    return MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=NullEntitlementGate(), **kw)


# ----------------------------------------------------------------------
# HOP_OWNERS
# ----------------------------------------------------------------------

@pytest.fixture
def hubs(tmp_path) -> MemoryClient:
    c = _client(tmp_path)
    c.set_entity("email", "d1", {})
    c.set_entity("email", "d2", {})
    c.set_entity("person", "Ann", {})
    for kind, key, docs in (("customer", "Acme", ["d1", "d2"]), ("ticket", "T-1", ["d1"])):
        c.set_entity(kind, key, {})
        for d in docs:
            c.link(entity_ref(kind, key), entity_ref("email", d), "source_of")
    c.link(entity_ref("email", "d1"), entity_ref("person", "Ann"), "mentions")
    return c


def test_links_of_unchanged_without_hop_owners(hubs) -> None:
    out = hubs.links_of([entity_ref("email", "d1")])[("entity", "email", "d1")]
    assert "owners_total" not in out
    assert all("doc_count" not in e for e in out["sample"])


def test_links_of_hop_owners_reorders_source_of_samples(hubs, monkeypatch) -> None:
    before = hubs.links_of([entity_ref("email", "d1")])[("entity", "email", "d1")]
    monkeypatch.setenv("SIBYL_MEMORY_HOP_OWNERS", "1")
    out = hubs.links_of([entity_ref("email", "d1")])[("entity", "email", "d1")]
    assert out["owners_total"] == 2
    top = [e for e in out["sample"] if e["relation"] == "source_of"]
    assert [(e["node"]["key"], e["doc_count"]) for e in top] == [("T-1", 1), ("Acme", 2)]
    assert out["sample"][:2] == top                                # owners first
    rest = [e for e in out["sample"] if e["relation"] != "source_of"]
    assert rest == [e for e in before["sample"] if e["relation"] != "source_of"]
    assert out["degree"] == before["degree"] and out["by_relation"] == before["by_relation"]
    # a node without source_of in-edges is untouched
    ann = hubs.links_of([entity_ref("person", "Ann")])[("entity", "person", "Ann")]
    assert "owners_total" not in ann


# ----------------------------------------------------------------------
# RANKED_FALLBACK
# ----------------------------------------------------------------------

@pytest.fixture
def notes(tmp_path) -> MemoryClient:
    c = _client(tmp_path)
    c.set_entity("note", "alpha", {"text": "quarterly budget review with finance team"})
    c.set_entity("note", "beta", {"text": "kitchen renovation contractor schedule"})
    c.set_entity("note", "gamma", {"text": "budget spreadsheet"})
    c.set_entity("secret", "delta", {"text": "finance kitchen notes"})
    return c


GATED_Q = "finance kitchen spreadsheet contractor quarterly"   # no row clears the gates
SHORT_Q = "finance kitchen spreadsheet"                         # one gated row, short


def _run(c, q):
    r = multi_record_search(c, q, limit=5)
    return [(h["category"], h["key"], h.get("match")) for h in r], r.verdict.as_dict()


def test_ranked_unset_is_the_s2_baseline(notes) -> None:
    rows, d = _run(notes, GATED_Q)
    assert rows == [] and d["code"] == "gated"
    assert list(d) == S2_KEYS
    rows, d = _run(notes, SHORT_Q)
    assert rows == [("secret", "delta", "text")] and list(d) == S2_KEYS
    assert all(m != "ranked" for _, _, m in rows)


def test_ranked_fills_a_gated_result(notes, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_FALLBACK", "1")
    rows, d = _run(notes, GATED_Q)
    assert rows and all(m == "ranked" for _, _, m in rows)
    assert d["code"] == VerdictCode.RANKED_FALLBACK.value
    assert d["ranked_appended"] == len(rows)
    assert list(d) == S2_KEYS[:16] + ["ranked_appended"] + S2_KEYS[16:]
    assert "ranked candidates" in d["explain"]


def test_ranked_appends_after_gated_rows_without_ranked_code(notes, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_FALLBACK", "1")
    rows, d = _run(notes, SHORT_Q)
    assert rows[0] == ("secret", "delta", "text")
    assert {m for _, _, m in rows[1:]} == {"ranked"}
    assert len({(c, k) for c, k, _ in rows}) == len(rows)       # gated row not repeated
    assert d["code"] == "ok" and d["ranked_appended"] == len(rows) - 1


def test_ranked_min_tokens(notes, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_FALLBACK", "1")
    rows, d = _run(notes, "finance kitchen")                    # 2 tokens < 3
    assert "ranked_appended" not in d and all(m != "ranked" for _, _, m in rows)
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_MIN_TOKENS", "6")
    rows, d = _run(notes, GATED_Q)                              # 5 tokens < 6
    assert rows == [] and d["code"] == "gated" and "ranked_appended" not in d


def test_ranked_excluded_categories(notes, monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_FALLBACK", "1")
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_EXCLUDE_CATEGORIES", "secret, nothing")
    rows, _ = _run(notes, GATED_Q)
    assert rows and "secret" not in {c for c, _, _ in rows}


def test_ranked_tenant_boundary(tmp_path, notes, monkeypatch) -> None:
    other = MemoryClient.local(str(tmp_path / "m.db"), tenant_id=OTHER,
                               entitlement_gate=NullEntitlementGate())
    other.set_entity("note", "intruder", {"text": "finance kitchen spreadsheet contractor"})
    monkeypatch.setenv("SIBYL_MEMORY_RANKED_FALLBACK", "1")
    rows, _ = _run(notes, GATED_Q)
    assert "intruder" not in {k for _, k, _ in rows}
    orows, _ = _run(other, GATED_Q)
    assert {k for _, k, _ in orows} <= {"intruder"}
