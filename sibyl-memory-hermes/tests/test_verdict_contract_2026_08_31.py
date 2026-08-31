"""The verdict contract on the Hermes provider + adapter (stage 3, 2026-08-31).

The provider is what every non-MCP embedding calls, and the adapter's
`sibyl_search` is what a Hermes agent actually receives. Both used to hand back
a bare empty list / `{"results": []}` with no way to tell an empty store from a
blocked query. The verdict now rides on the RETURN of `search_multi_record`, so
the provider gets it without asking and the adapter forwards it.

The provider's own docstring already DOCUMENTED this failure mode ("abstains ...
the moment one significant query token is content-shaped and has zero corpus
support anywhere", citing the Kravento PL eval) while still offering only an
optional `diagnostics=` dict to see it. Documented-but-unreachable is what these
tests close.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes import provider as provider_mod
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_mod
# THE canonical vocabulary, imported exactly as the provider imports it.
from sibyl_memory_client.verdicts import SearchResults, VerdictCode, ZERO_CAUSES


def _provider(tmp_path: Path) -> SibylMemoryProvider:
    return SibylMemoryProvider(db_path=str(tmp_path / "verdict.db"),
                               autoload_credentials=False)


def _seed(p: SibylMemoryProvider) -> None:
    p.remember("ops", "warehouse-lodz", {"text": "warehouse lodz shelving"})
    p.remember("ops", "courier-rates", {"text": "courier rates table for parcels"})
    p.remember("ops", "invoice-2024", {"text": "invoice numbering scheme for accounting"})
    p.remember("ops", "kontrakt-a", {"text": "contract approved by legal, final decision"})


# --------------------------------------------------------------------------
# provider
# --------------------------------------------------------------------------
def test_search_multi_record_carries_a_verdict_with_no_kwarg(tmp_path):
    """By DEFAULT. The defect was that the only channel was opt-in."""
    p = _provider(tmp_path)
    _seed(p)
    hits = p.search_multi_record("quantum flux capacitor alignment")
    assert isinstance(hits, SearchResults)
    assert hits == []
    assert hits.verdict.code is VerdictCode.ABSTAINED_ON
    assert hits.verdict.tokens


def test_no_empty_provider_result_lacks_a_cause(tmp_path):
    p = _provider(tmp_path)
    _seed(p)
    for q in ["quantum flux capacitor alignment",
              "contract not approved",
              "warehouse courier accounting parcels shelving",
              "aa"]:
        hits = p.search_multi_record(q)
        assert hits == [], q
        assert hits.verdict.code in ZERO_CAUSES, (q, hits.verdict.code)


def test_provider_search_primitive_also_carries_one(tmp_path):
    p = _provider(tmp_path)
    _seed(p)
    hit = p.search("warehouse")
    assert hit and hit.verdict.code is VerdictCode.OK
    miss = p.search("qwzjvxzzyplm")
    assert miss == [] and miss.verdict.code is VerdictCode.NO_MATCH


def test_provider_search_reports_an_empty_store_as_empty(tmp_path):
    p = _provider(tmp_path)
    miss = p.search("anything")
    assert miss == []
    assert miss.verdict.code is VerdictCode.EMPTY_STORE


def test_provider_results_stay_list_compatible(tmp_path):
    p = _provider(tmp_path)
    _seed(p)
    hits = p.search_multi_record("warehouse lodz shelving")
    assert isinstance(hits, list)
    assert hits == list(hits)
    json.dumps(hits, default=str)


def test_legacy_diagnostics_passthrough_still_works(tmp_path):
    """Deprecated, not removed. An existing caller passing the dict is unbroken."""
    p = _provider(tmp_path)
    p.remember("order", "co0001-order", {"text": "co0001 order shipped confirmation"})
    d: dict = {}
    hits = p.search_multi_record("co0001 nonexistenttokenzzzq report", diagnostics=d)
    assert hits == []
    assert d["abstained"] is True
    assert d["abstained_on"] == ["nonexistenttokenzzzq"]
    assert d["verdict"]["code"] == VerdictCode.ABSTAINED_ON.value


# --------------------------------------------------------------------------
# adapter (what a Hermes agent actually receives)
# --------------------------------------------------------------------------
def _tool(adapter, name, args):
    return json.loads(adapter.handle_tool_call(name, args))


def _adapter(tmp_path):
    a = adapter_mod.SibylAdapter()
    a._sibyl = SibylMemoryProvider(db_path=str(tmp_path / "adapter.db"),
                                   autoload_credentials=False)
    a._session_id = "verdict-test"
    a._hermes_home = tmp_path
    return a


def test_adapter_sibyl_search_forwards_the_verdict(tmp_path):
    a = _adapter(tmp_path)
    a._sibyl.remember("ops", "warehouse-lodz", {"text": "warehouse lodz shelving"})
    out = _tool(a, "sibyl_search", {"query": "quantum flux capacitor alignment"})
    assert out["results"] == []
    assert out["verdict"]["code"] == VerdictCode.ABSTAINED_ON.value
    assert out["verdict"]["tokens"]
    hit = _tool(a, "sibyl_search", {"query": "warehouse lodz shelving"})
    assert hit["results"]
    assert hit["verdict"]["code"] == VerdictCode.OK.value


def test_adapter_verdict_carries_no_stored_record_text(tmp_path):
    a = _adapter(tmp_path)
    marker = "CANARYSTRINGZZZ"
    a._sibyl.remember("ops", f"row-{marker}",
                      {"text": f"body with {marker}, draft, work in progress"})
    for q in ["quantum flux capacitor", "contract not approved", "warehouse"]:
        out = _tool(a, "sibyl_search", {"query": q})
        assert marker not in json.dumps(out.get("verdict", {}), ensure_ascii=False), q


def test_adapter_tool_schema_teaches_the_cause_scoped_retry(tmp_path):
    a = _adapter(tmp_path)
    schemas = {s["name"]: s for s in a.get_tool_schemas()}
    desc = schemas["sibyl_search"]["description"]
    assert "verdict" in desc
    assert "abstained_on" in desc
    assert "drop" in desc.lower()
    # the system-prompt block the agent reads at every turn
    assert "verdict" in a.system_prompt_block()


# --------------------------------------------------------------------------
# one vocabulary
# --------------------------------------------------------------------------
def test_this_package_imports_the_vocabulary_and_declares_none():
    assert "from sibyl_memory_client.verdicts import" in inspect.getsource(provider_mod)
    for mod in (provider_mod, adapter_mod):
        src = inspect.getsource(mod)
        code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        parts = code.replace('"""', "\x00").split("\x00")
        code_only = "".join(parts[0::2])
        for lit in ("negation_abstain", "coverage_floor", "anchor_gate",
                    "prep_filter", "empty_store"):
            assert f'"{lit}"' not in code_only, (mod.__name__, lit)
