"""Regression: prefetch() output must be fenced as untrusted data.

prefetch() returns stored memory bodies, which can contain prompt-injection
payloads. The block is wrapped in an explicit untrusted-context fence so the host
agent treats it as reference data, never as instructions. The closing fence must
survive even when content is large. Source: beta security report (dor_alpha, 2026-06-01).
"""
from sibyl_memory_client import MemoryClient
from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter


def _adapter_with_data(tmp_path):
    c = MemoryClient.local(tmp_path / "m.db", tenant_id="qa")
    for i in range(4):
        c.set_entity("notes", f"n{i}", {"text": "alpha beta gamma token context payload"})
    a = SibylAdapter()
    a._sibyl = c
    return a


def test_prefetch_output_is_fenced_as_untrusted(tmp_path):
    out = _adapter_with_data(tmp_path).prefetch("alpha beta gamma token context payload")
    assert out, "prefetch returned empty"
    assert "[UNTRUSTED MEMORY CONTEXT BEGIN]" in out
    assert out.rstrip().endswith("[UNTRUSTED MEMORY CONTEXT END]")
