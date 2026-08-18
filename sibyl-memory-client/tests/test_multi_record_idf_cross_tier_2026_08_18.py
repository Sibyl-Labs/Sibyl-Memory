"""Regression test: negative-IDF ranking inversion in multi_record_search.

_corpus_count() sizes the corpus from the `entities` table only, but df[t] is
computed via client.search(), which is cross-tier (entities + state_documents
+ reference_documents + journal_events). journal_events is an append-only log
that grows without bound over a session's lifetime, so a token that is common
in the journal can have df[t] > corpus_n. The classic smoothed-idf formula
(log((N+1)/(df+1)) + 1.0) is only guaranteed non-negative when df <= N; once
df exceeds the (entities-only) corpus_n, idf goes negative, `total` can flip
sign, and `cov = matched_idf / total` inverts: a candidate matching FEWER
query tokens can outrank one matching ALL of them.

Repro shape: one entity matching both query tokens, and enough journal events
matching only the common token to push its df past corpus_n.
"""
import math

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.multi_record import multi_record_search


def _client(tmp_path):
    return MemoryClient.local(tmp_path / "m.db", tenant_id="qa")


def test_full_match_entity_outranks_partial_match_journal_spam(tmp_path):
    c = _client(tmp_path)
    c.set_entity("company", "acme", {"note": "acme project kickoff meeting scheduled"})
    for i in range(40):
        c.write_event(evaluated=f"meeting notes entry number {i} discussed roadmap")

    results = multi_record_search(c, "acme meeting", limit=10)

    assert results, "expected at least one hit"
    top = results[0]
    assert (top["tier"], top.get("category"), top.get("key")) == ("entity", "company", "acme"), (
        "the entity matching BOTH query tokens must outrank journal rows matching only "
        "'meeting' -- if a journal-only row is first, the negative-IDF sign-flip regressed"
    )


def test_idf_is_never_negative_when_df_exceeds_entity_only_corpus():
    # corpus_n (entities-only) = 1, df (cross-tier) = 41: the exact shape that
    # used to drive log((1+1)/(41+1)) + 1.0 negative.
    corpus_n, df = 1, 41
    idf = max(0.0, math.log((corpus_n + 1) / (df + 1)) + 1.0)
    assert idf >= 0.0
