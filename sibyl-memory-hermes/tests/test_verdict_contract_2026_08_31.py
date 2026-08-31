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

import ast
import inspect
import json
from pathlib import Path

from sibyl_memory_hermes import SibylMemoryProvider
from sibyl_memory_hermes import provider as provider_mod
from sibyl_memory_hermes._hermes_plugin import adapter as adapter_mod
# THE canonical vocabulary, imported exactly as the provider imports it.
from sibyl_memory_client.verdicts import (
    GateCause, SearchResults, VerdictCode, ZERO_CAUSES)


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


def test_provider_search_leaves_the_empty_store_probe_to_the_surface(tmp_path):
    """`provider.search()` is the RAW primitive, not a user-facing surface, so it
    reports the cheap cause and does not pay the probe. A surface that reports
    the zero calls `refine_zero` once, itself."""
    from sibyl_memory_client.verdicts import refine_zero
    p = _provider(tmp_path)
    miss = p.search("anything")
    assert miss == []
    assert miss.verdict.code is VerdictCode.NO_MATCH
    assert refine_zero(p._client, miss).verdict.code is VerdictCode.EMPTY_STORE


def test_prefetch_does_not_probe_the_store_once_per_token(tmp_path):
    """REGRESSION (adversarial review, 2026-08-31). `provider.search()` briefly
    called `refine_zero`, which LOOKS like a surface-level probe and is not: the
    adapter's `prefetch()` calls that method once for the whole query and then
    once per significant token, so the probe became a per-token probe by another
    name — 24 COUNT(*) on one turn against an entities-empty store, measured.

    Pinned by counting the real thing rather than by reading the code, because
    the cost is invisible at every level above the storage layer."""
    from sibyl_memory_client.storage import Storage
    a = _adapter(tmp_path)
    # A journal-only store: `entities` is empty, so `store_is_empty` would walk
    # all four tables on every probe. This is the worst case, and the one the
    # review measured at 24.
    a._sibyl.client.write_event(acted=["a journal row so the store is not empty"])

    calls = {"n": 0}
    real = Storage.count_rows

    def counting(self, table, tenant_id):
        calls["n"] += 1
        return real(self, table, tenant_id)

    Storage.count_rows = counting
    try:
        a.prefetch("where are the courier warehouse invoice shelving parcels rates")
    finally:
        Storage.count_rows = real
    # prefetch issues 1 full-query search + up to 5 per-token searches. The raw
    # primitive must not probe on any of them.
    assert calls["n"] == 0, f"{calls['n']} COUNT(*) from a prefetch that should issue none"


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

# --------------------------------------------------------------------------
# one vocabulary
# --------------------------------------------------------------------------
def _code_only(text: str, extra_drop: frozenset[int] | set[int] = frozenset()) -> str:
    """Strip docstrings and comments, leaving code.

    Parsed, not split on `\"\"\"`. The first version of this helper did
    `text.replace('\"\"\"', chr(0)).split(chr(0))[0::2]`, which silently INVERTS
    which half it scans when the marker count is odd — the test would then check
    the prose and skip the code, and still pass.

    `extra_drop` excises additional 1-based line numbers. It exists for the
    adapter, whose two agent-facing teaching strings must name the causes; the
    spans are located structurally by `_teaching_line_numbers`, never guessed.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:                                # pragma: no cover
        return text
    spans = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):                 # ast.IfExp.body is an expr
            continue
        for child in body:
            if (isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)):
                spans.append((child.lineno, child.end_lineno))
    drop = {i for a, b in spans for i in range(a, b + 1)} | set(extra_drop)
    return "\n".join(ln.split("#", 1)[0]
                     for i, ln in enumerate(text.splitlines(), 1) if i not in drop)


#: The only two places in `adapter.py` allowed to spell a cause name: the tool
#: schema description and the system-prompt block. Both are TEACHING surfaces —
#: what a real Hermes agent reads — and are already bound to the enum by
#: `test_the_agent_facing_teaching_names_the_causes_the_engine_emits`.
TEACHING_SURFACES = ("SEARCH_SCHEMA['description']", "system_prompt_block")


def _teaching_line_numbers(source: str) -> set[int]:
    """1-based line numbers of the two teaching strings, located by AST.

    Located structurally rather than by line range or by a marker comment, so a
    refactor cannot silently widen the exemption. If either surface is renamed
    away this RAISES instead of quietly exempting nothing (or everything).
    """
    tree = ast.parse(source)
    spans, found = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SEARCH_SCHEMA"
                for t in node.targets):
            assert isinstance(node.value, ast.Dict), "SEARCH_SCHEMA is not a dict literal"
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value == "description":
                    spans.append((v.lineno, v.end_lineno))
                    found.add("SEARCH_SCHEMA['description']")
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "system_prompt_block"):
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    spans.append((child.value.lineno, child.value.end_lineno))
                    found.add("system_prompt_block")
    missing = set(TEACHING_SURFACES) - found
    assert not missing, (
        f"teaching surface(s) {sorted(missing)} not found in adapter.py; the "
        "vocabulary exemption is stale and must be re-derived, not widened")
    return {i for a, b in spans for i in range(a, b + 1)}


#: Derived from the enums, so renaming a cause updates the guard automatically.
CAUSE_LITERALS = tuple([c.value for c in VerdictCode if c is not VerdictCode.OK]
                       + [g.value for g in GateCause])


def _assert_declares_no_vocabulary(*modules, exempt_lines: set[int] = frozenset()):
    """BOTH quote styles. These tests originally checked only double quotes, and
    an adversarial review slipped `_LOCAL_CAUSE_COPY = 'abstained_on'` into the
    MCP server past a green suite — exactly the drift the guard exists to stop."""
    offenders = []
    for mod in modules:
        code = _code_only(inspect.getsource(mod), exempt_lines)
        for lit in CAUSE_LITERALS:
            if f'"{lit}"' in code or f"'{lit}'" in code:
                offenders.append((mod.__name__, lit))
    assert offenders == [], f"cause strings re-declared outside verdicts.py: {offenders}"


def test_this_package_imports_the_vocabulary_and_declares_none():
    assert "from sibyl_memory_client.verdicts import" in inspect.getsource(provider_mod)
    _assert_declares_no_vocabulary(provider_mod)


def test_the_adapter_declares_no_vocabulary_outside_its_teaching_strings():
    """S3-A. The guard read `provider.py` only, so `adapter.py` — the module that
    builds what a real Hermes agent receives, and therefore the one where drift
    matters most — was unscanned. A ratification pass planted
    `_LOCAL_CAUSE_COPY = 'abstained_on'` above `class SibylAdapter` and kept the
    suite green. Scanned now, with only the two teaching strings excised."""
    src = inspect.getsource(adapter_mod)
    _assert_declares_no_vocabulary(
        adapter_mod, exempt_lines=_teaching_line_numbers(src))


def test_the_adapter_scan_actually_reads_the_module():
    """The exemption must not have swallowed the surface it exempts. Excising the
    teaching lines is only safe if the REST of adapter.py is still scanned, so
    prove the scan sees code from outside those two spans."""
    src = inspect.getsource(adapter_mod)
    exempt = _teaching_line_numbers(src)
    assert exempt, "no teaching lines located"
    scanned = _code_only(src, exempt)
    assert "class SibylAdapter" in scanned
    assert "def prefetch" in scanned
    # and the teaching strings themselves are genuinely gone from the scan
    assert "verdict.tokens[0]" not in scanned
    assert len(scanned.splitlines()) < len(src.splitlines())


def test_the_agent_facing_teaching_names_the_causes_the_engine_emits():
    """The tool schema and system prompt TEACH the cause names, so they are
    exempt from the no-literals scan. Nothing bound them to the enum until now,
    so a rename would ship a stale instruction with a green suite."""
    a = adapter_mod.SibylAdapter()
    desc = {s["name"]: s for s in a.get_tool_schemas()}["sibyl_search"]["description"]
    prompt = a.system_prompt_block()
    for code in (VerdictCode.ABSTAINED_ON, VerdictCode.GATED,
                 VerdictCode.EMPTY_STORE, VerdictCode.NO_MATCH):
        assert code.value in desc, ("schema", code)
        assert code.value in prompt, ("prompt", code)
