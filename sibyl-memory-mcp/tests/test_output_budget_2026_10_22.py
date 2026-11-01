"""MCP output budget (2026-10-22 output-size regression).

V1 search hits (1,500-char body prefix + 600-char snippet + title + links)
put memory_search at ~69,000 chars at limit=30. Claude Code persists an MCP
result over 50,000 chars (or MAX_MCP_OUTPUT_TOKENS, default 25,000 tokens) to
a file instead of inlining it (https://code.claude.com/docs/en/mcp), so agents
without a file-read tool saw nothing. Every read result is now held to
an ESTIMATED-token budget (SIBYL_MEMORY_MCP_OUTPUT_TOKENS, default 22,000:
ceil(ascii/4) + one per non-ASCII char) and a hard char cap
(SIBYL_MEMORY_MCP_OUTPUT_BUDGET; 190,000 for Claude Code, which honours the
tools' _meta["anthropic/maxResultSizeChars"] = 200,000, else 45,000) on its
WIRE text, degrading body prefix -> snippet -> link samples -> trailing hits.
In-process call_tool has no client, so it exercises the 45,000 fallback; the
Claude Code path runs over a real in-memory MCP session.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile

import pytest

import sibyl_memory_mcp.server as server
from sibyl_memory_client import MemoryClient, entity_ref, reference_ref
from sibyl_memory_client.output_budget import estimate_tokens

BUDGET_ENV = "SIBYL_MEMORY_MCP_OUTPUT_BUDGET"
TOKENS_ENV = "SIBYL_MEMORY_MCP_OUTPUT_TOKENS"
CJK_WORDS = "路线图 预算 管道 客户 发布 季度 迁移 供应商 合同 事故 评审 招聘 入职 定价 续约 安全".split()
HUB = entity_ref("project", "Hub")
WORDS = ("roadmap budget pipeline customer launch quarterly migration vendor contract "
         "incident review hiring onboarding pricing renewal security audit latency").split()


def _text(rng: random.Random, n: int, words=WORDS) -> str:
    out: list[str] = []
    size = 0
    while size < n:
        w = rng.choice(words)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)


@pytest.fixture(scope="module")
def store_path():
    """40 entities + 40 reference docs, bodies 2-20 KB, every node linked to a
    hub under three relations (so hits carry link samples)."""
    rng = random.Random(7)
    path = os.path.join(tempfile.mkdtemp(), "m.db")
    c = MemoryClient.local(path, tenant_id="qa")
    c.set_entity("project", "Hub", {"summary": "the hub"})
    for i in range(40):
        c.set_entity("project", f"Proj {i}",
                     {"summary": "roadmap pricing \"quoted\"\n" + _text(rng, rng.randint(2_000, 20_000))})
        c.set_reference(f"doc/meet/{i:02d}", "roadmap pricing " + _text(rng, rng.randint(2_000, 20_000)),
                        metadata={"title": f"Meeting {i} roadmap"})
        node = entity_ref("project", f"Proj {i}")
        for rel in ("member_of", "mentions", "related_to"):
            c.link(node, HUB, rel)
        c.link(reference_ref(f"doc/meet/{i:02d}"), HUB, "about")
    c.set_reference("doc/escaped", ("line \"q\" \\ back\n" * 4_000),
                    metadata={"title": "escape heavy"})
    return path


@pytest.fixture
def mcp(store_path, monkeypatch):
    client = MemoryClient.local(store_path, tenant_id="qa")
    monkeypatch.setattr(server, "_open_client", lambda: client)
    monkeypatch.delenv(BUDGET_ENV, raising=False)
    monkeypatch.delenv(TOKENS_ENV, raising=False)
    return server.build_server()


@pytest.fixture(scope="module")
def cjk_path():
    """40 CJK entities (700-7,000 chars: about a token per char)."""
    rng = random.Random(11)
    path = os.path.join(tempfile.mkdtemp(), "cjk.db")
    c = MemoryClient.local(path, tenant_id="qa")
    for i in range(40):
        c.set_entity("project", f"项目 {i}",
                     {"summary": "路线图 定价 " + _text(rng, rng.randint(700, 7_000), CJK_WORDS)})
    return path


def _wire(mcp, tool: str, args: dict) -> tuple[str, dict]:
    res = asyncio.run(mcp.call_tool(tool, args))
    content, structured = res if isinstance(res, tuple) else (res, None)
    text = "".join(getattr(x, "text", "") for x in content)
    return text, (structured if structured is not None else json.loads(text))


def _within(text: str, tokens: int = 22_000, chars: int = 45_000) -> bool:
    return len(text) <= chars and estimate_tokens(text) <= tokens


def _keys(res: dict) -> list[tuple]:
    return [(h.get("tier"), h.get("category"), h.get("key")) for h in res["results"]]


def test_limit_30_returns_every_hit_inside_the_budget(mcp):
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert _within(text)
    v = res["verdict"]
    assert res["count"] == v["returned"] == 30 and "truncated" not in v
    assert v["degraded"][0] == "body_500"
    for h in res["results"]:
        assert h["snippet"] and h["chars"] > 0 and h["match"] and h["tier"]
        assert h["links"]["by_relation"]


def test_limit_50_fits_as_many_as_the_budget_allows(mcp):
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 50})
    assert _within(text)
    v = res["verdict"]
    assert res["count"] == len(res["results"]) == v["returned"] >= 30
    assert sum(v["tiers_hit"].values()) == res["count"]
    if v.get("truncated"):
        assert v["truncated"] == 50 - res["count"]
        assert v["degraded"][-1] == "links_counts_only"


def test_degrade_order_and_truncation_verdict(mcp, monkeypatch):
    monkeypatch.setenv(TOKENS_ENV, "3000")
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert _within(text, tokens=3_000)
    v = res["verdict"]
    assert v["degraded"] == ["body_500", "body_150", "body_omitted", "snippet_250",
                             "links_counts_only"]
    assert v["truncated"] == 30 - res["count"] > 0
    assert "smaller limit" in v["hint"] and "memory_get_reference" in v["hint"]
    for h in res["results"]:
        assert "body" not in h and h["body_omitted"] is True
        assert len(h["snippet"]) <= 252 and "[" in h["snippet"]      # still match-centred
        assert "sample" not in h["links"] and h["links"]["sample_omitted"] is True


def test_hit_order_and_set_unchanged_until_last_resort(mcp, monkeypatch):
    monkeypatch.setenv(BUDGET_ENV, "500000")
    monkeypatch.setenv(TOKENS_ENV, "125000")
    _, full30 = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert "degraded" not in full30["verdict"] and "truncated" not in full30["verdict"]
    monkeypatch.delenv(BUDGET_ENV)
    monkeypatch.delenv(TOKENS_ENV)
    _, shaped30 = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert shaped30["verdict"]["degraded"] and _keys(shaped30) == _keys(full30)
    monkeypatch.setenv(TOKENS_ENV, "3000")
    _, cut30 = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert _keys(cut30) == _keys(full30)[:cut30["count"]] and cut30["count"] < 30


def test_small_result_is_unchanged(mcp):
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 2})
    assert _within(text)
    assert "degraded" not in res["verdict"] and "truncated" not in res["verdict"]
    for h in res["results"]:
        assert len(h["body"]) == server._SEARCH_HIT_BODY_MAX + 1   # stage-0 cap only
        assert "sample" in h["links"] and "body_omitted" not in h


def test_cjk_store_is_held_by_the_token_estimate(cjk_path, monkeypatch):
    client = MemoryClient.local(cjk_path, tenant_id="qa")
    monkeypatch.setattr(server, "_open_client", lambda: client)
    monkeypatch.delenv(BUDGET_ENV, raising=False)
    monkeypatch.delenv(TOKENS_ENV, raising=False)
    mcp = server.build_server()
    for limit in (10, 30):
        text, res = _wire(mcp, "memory_search", {"query": "路线图 定价", "limit": limit})
        assert res["count"] >= 10 and _within(text)
    assert estimate_tokens("路线图") == 3 and estimate_tokens("abcde") == 2


def test_env_overrides(mcp, monkeypatch):
    monkeypatch.setenv(BUDGET_ENV, "8000")
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert len(text) <= 8_000 and res["verdict"]["truncated"] > 0
    monkeypatch.setenv(BUDGET_ENV, "500000")
    monkeypatch.setenv(TOKENS_ENV, "125000")
    text, res = _wire(mcp, "memory_search", {"query": "roadmap pricing", "limit": 30})
    assert len(text) > 50_000 and res["count"] == 30   # the old shape, by request
    monkeypatch.setenv(BUDGET_ENV, "12")      # clamped up to the floors
    monkeypatch.setenv(TOKENS_ENV, "12")
    assert server._output_budget_chars() == 4_000 and server._output_budget_tokens() == 1_000
    monkeypatch.setenv(BUDGET_ENV, "junk")    # junk -> default
    monkeypatch.setenv(TOKENS_ENV, "junk")
    assert server._output_budget_chars() == server._TOTAL_OUTPUT_BUDGET == 45_000
    assert server._output_budget_tokens() == server._OUTPUT_TOKEN_BUDGET == 22_000


def test_memory_list_default_page_fits_and_keeps_bodies(mcp, monkeypatch):
    text, res = _wire(mcp, "memory_list", {})
    assert _within(text)
    assert all(r.get("body") and "body_omitted" not in r for r in res["results"])
    monkeypatch.setenv(TOKENS_ENV, "3000")
    text, res = _wire(mcp, "memory_list", {})
    assert _within(text, tokens=3_000)
    assert res["truncated"] == 41 - res["count"] > 0 and "smaller limit" in res["hint"]
    assert res["degraded"] == ["body_500", "body_150"]
    assert all(r.get("body") and "body_omitted" not in r for r in res["results"])


def test_neighbors_page_fits_and_resumes_without_skipping(mcp, monkeypatch):
    monkeypatch.setenv(TOKENS_ENV, "3000")
    seen: list[int] = []
    cursor = None
    calls = 0
    while True:
        args = {"node": HUB, "limit": 200, "preview": True}
        if cursor:
            args["cursor"] = cursor
        text, res = _wire(mcp, "memory_neighbors", args)
        calls += 1
        assert _within(text, tokens=3_000)
        seen += [e["id"] for e in res["edges"]]
        cursor = res["next_cursor"]
        if not cursor:
            break
    assert calls > 1 and len(seen) == len(set(seen)) == 160


def test_get_reference_page_is_cut_to_budget_and_pages_cleanly(mcp, store_path):
    full = MemoryClient.local(store_path, tenant_id="qa").get_reference("doc/escaped")["body"]
    parts, off = [], 0
    while off is not None:
        text, page = _wire(mcp, "memory_get_reference",
                           {"key": "doc/escaped", "offset": off, "max_chars": 50_000})
        assert _within(text)
        assert page.get("page_capped") is True or page["next_offset"] is None
        parts.append(page["body"])
        off = page["next_offset"]
    assert "".join(parts) == full


def test_list_references_fits(mcp):
    text, res = _wire(mcp, "memory_list_references", {"limit": 200})
    assert _within(text) and res["count"] == len(res["items"])


# ------------------------------------------------- Claude Code path (real session)

def _session_call(mcp, client_name: str, calls: list[tuple[str, dict]]):
    """Run tools/list + tool calls over a real in-memory MCP session whose
    client identifies as ``client_name``. Returns (tools, [text, ...])."""
    from mcp import types as T
    from mcp.shared.memory import create_connected_server_and_client_session

    async def run():
        async with create_connected_server_and_client_session(
                mcp._mcp_server,
                client_info=T.Implementation(name=client_name, version="2.1.0")) as cs:
            tools = (await cs.list_tools()).tools
            texts = []
            for tool, args in calls:
                r = await cs.call_tool(tool, args)
                texts.append("".join(getattr(c, "text", "") for c in r.content))
            return tools, texts
    return asyncio.run(run())


READ_TOOLS = {"memory_recall", "memory_search", "memory_list", "memory_neighbors",
              "memory_get_reference", "memory_list_references"}


def test_tools_list_declares_max_result_size_on_the_wire(mcp):
    tools, _ = _session_call(mcp, "claude-code", [])
    meta = {t.name: (t.meta or {}) for t in tools}
    for name in READ_TOOLS:
        assert meta[name]["anthropic/maxResultSizeChars"] == 200_000
    assert "anthropic/maxResultSizeChars" not in meta["memory_remember"]


def test_claude_code_client_gets_the_token_budget(mcp):
    _, texts = _session_call(mcp, "claude-code", [
        ("memory_search", {"query": "roadmap pricing", "limit": 50}),
        ("memory_list", {"limit": 200})])
    for text in texts:
        assert estimate_tokens(text) <= 22_000 and len(text) <= 190_000
    res = json.loads(texts[0])
    # Past the unannotated 50,000-char line, and every hit returned.
    assert len(texts[0]) > 50_000 and res["count"] == 50 and "truncated" not in res["verdict"]


def test_other_clients_keep_the_char_fallback(mcp):
    _, texts = _session_call(mcp, "codex-mcp-client", [
        ("memory_search", {"query": "roadmap pricing", "limit": 50})])
    assert len(texts[0]) <= 45_000
