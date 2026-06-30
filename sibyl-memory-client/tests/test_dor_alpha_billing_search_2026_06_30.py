"""Confirmation test for the dor_alpha report (2026-06-30).

Report: store "Alice manages the billing system", then ask the natural-language
question "who manages billing". The zero-hit paraphrase fallback strips the
stopword "who" and recovers the record on shared content tokens. The plain
content query "billing system" must also recover it.

This is expected to PASS on current code — it confirms the existing fallback
already covers the reported case. No search behavior is changed here; if it
fails, that is a finding, not a fix target.
"""
from sibyl_memory_client import MemoryClient


def _client(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="dor")
    c.set_entity("people", "alice", {"note": "Alice manages the billing system"})
    return c


def test_question_who_manages_billing_recovers(tmp_path):
    c = _client(tmp_path)
    hits = c.search("who manages billing", limit=10)
    assert any(h.get("key") == "alice" for h in hits), hits


def test_billing_system_query_recovers(tmp_path):
    c = _client(tmp_path)
    hits = c.search("billing system", limit=10)
    assert any(h.get("key") == "alice" for h in hits), hits
