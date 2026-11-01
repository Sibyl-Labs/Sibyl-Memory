"""V1 panel fixes on the MCP surface (PANEL-SYNTHESIS B, D, E, G; 2026-10-22).

B  release train: the V1 server imports client symbols 0.9.0 does not have
   (`count_tiers`), so the floor must be the lockstep client release; the
   server and the Hermes provider import against it (YARROW mcpskew.py).
D  verdict.name_matches describes the rows the agent received after the
   output budget dropped some (NOCTURNE 8).
E  memory_delete_reference refuses product-owned prefixes without force,
   reports edges_removed; `chars` means the stored length on every
   reference tool, memory_get_reference adds `surfaced_chars`.
G  argument-validation errors name the unknown / missing PARAMETERS (never a
   value); memory_link takes from_node / to_node.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest
from mcp.types import CallToolRequest

import sibyl_memory_mcp.server as server

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-live-THIS-MUST-NEVER-ECHO-123456"


def _handler():
    return server.build_server()._mcp_server.request_handlers[CallToolRequest]


def _call(name, args, handler=None):
    h = handler or _handler()
    res = asyncio.run(h(CallToolRequest(method="tools/call",
                                        params={"name": name, "arguments": args})))
    root = res.root
    text = root.content[0].text if root.content else ""
    if root.isError and not text.startswith("{"):
        text = text[text.index("{"):]
    return root.isError, json.loads(text), text


def _ok(name, args):
    is_error, payload, _ = _call(name, args)
    assert is_error is False, payload
    return payload


# ---------------------------------------------------------------- B: release train

def _floor(pyproject: Path, dep: str) -> tuple[int, ...]:
    m = re.search(rf'"{re.escape(dep)}>=([0-9.]+)"', pyproject.read_text())
    assert m, (pyproject, dep)
    return tuple(int(x) for x in m.group(1).split("."))


def _version(pyproject: Path) -> tuple[int, ...]:
    m = re.search(r'^version = "([0-9.]+)"', pyproject.read_text(), re.M)
    return tuple(int(x) for x in m.group(1).split("."))


def test_release_train_versions_and_floors():
    client = _version(ROOT / "sibyl-memory-client" / "pyproject.toml")
    mcp = _version(ROOT / "sibyl-memory-mcp" / "pyproject.toml")
    hermes = _version(ROOT / "sibyl-memory-hermes" / "pyproject.toml")
    cli = _version(ROOT / "sibyl-memory-cli" / "pyproject.toml")
    assert (client, mcp, hermes, cli) == ((0, 10, 0), (0, 4, 0), (0, 6, 0), (0, 6, 0))
    for pkg in ("sibyl-memory-mcp", "sibyl-memory-hermes", "sibyl-memory-cli"):
        assert _floor(ROOT / pkg / "pyproject.toml", "sibyl-memory-client") == client, pkg
    assert _floor(ROOT / "sibyl-memory-mcp" / "pyproject.toml", "sibyl-memory-hermes") == hermes
    assert _floor(ROOT / "sibyl-memory-cli" / "pyproject.toml", "sibyl-memory-hermes") == hermes
    assert _floor(ROOT / "sibyl-memory-cli" / "pyproject.toml", "sibyl-memory-mcp") == mcp


def _import_with_client(client_src: Path) -> subprocess.CompletedProcess:
    """Import the server and the Hermes provider in a fresh interpreter whose
    sibyl_memory_client is the tree at ``client_src`` (the floor)."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import sibyl_memory_client as c; assert c.__file__.startswith(sys.argv[1]), c.__file__; "
            "import sibyl_memory_mcp.server, sibyl_memory_hermes.provider; print('ok')")
    return subprocess.run([sys.executable, "-c", code, str(client_src)],
                          capture_output=True, text=True, timeout=120)


def test_server_and_provider_import_against_the_floor_client():
    """The floor is this train's client: importing against its source tree works."""
    out = _import_with_client(ROOT / "sibyl-memory-client" / "src")
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]


def _git_tree(ref: str, path: str, dest: Path) -> bool:
    try:
        blob = subprocess.run(["git", "-C", str(ROOT), "archive", ref, path],
                              capture_output=True, timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest, filter="data")
    return True


def test_the_previous_client_cannot_serve_this_server(tmp_path):
    """YARROW mcpskew: with client 0.9.0 (708bd89) the V1 server dies at import.
    That is WHY the floor moved; if this ever passes, the floor can drop."""
    if not _git_tree("708bd89", "sibyl-memory-client/src", tmp_path):
        pytest.skip("release 0.9.0 not in this checkout's history")
    out = _import_with_client(tmp_path / "sibyl-memory-client" / "src")
    assert out.returncode != 0 and "ImportError" in out.stderr
    assert _floor(ROOT / "sibyl-memory-mcp" / "pyproject.toml",
                  "sibyl-memory-client") > (0, 9, 0)


# ---------------------------------------------------------------- D: name_matches after the budget

def test_name_matches_recomputed_after_output_budget(entitled_test_home, monkeypatch):
    c = server._open_client()
    c.set_entity("person", "Marissa Cole", {"summary": "x " * 2_000})
    for i in range(3):
        c.set_entity("person", f"Marissa Cole docs {i}", {"docs": ["d" * 2_000]})
    full = _ok("memory_search", {"query": "marissa cole", "with_links": False})
    assert full["verdict"]["name_matches"] == 4
    monkeypatch.setattr(server, "_TOTAL_OUTPUT_BUDGET", 3_000)
    cut = _ok("memory_search", {"query": "marissa cole", "with_links": False})
    got = sum(1 for r in cut["results"] if r["match"] in ("name", "name_partial"))
    assert cut["count"] < 4 and cut["verdict"]["name_matches"] == got


def test_negated_exact_name_through_the_tool(entitled_test_home):
    c = server._open_client()
    c.set_entity("project", "Not Invented Here", {"summary": "nih"})
    out = _ok("memory_search", {"query": "Not Invented Here", "with_links": False})
    assert [(r["key"], r["match"]) for r in out["results"]] == [("Not Invented Here", "name")]
    out = _ok("memory_search", {"query": "not invented", "with_links": False})
    assert out["count"] == 0 and out["verdict"]["code"] == "negation_abstain"


# ---------------------------------------------------------------- E: reference deletes

@pytest.mark.parametrize("key", ["skill/weekly", "debloat/history/CLAUDE.md"])
def test_product_prefixes_need_force(key, entitled_test_home):
    c = server._open_client()
    c.set_reference(key, "owned by the product")
    is_error, payload, text = _call("memory_delete_reference", {"key": key})
    assert is_error and payload["code"] == "VALIDATION_ERROR"
    owner = key.split("/", 1)[0] + "/"
    assert owner in payload["message"] and "force" in payload["message"]
    assert c.get_reference(key) is not None
    out = _ok("memory_delete_reference", {"key": key, "force": True})
    assert out["deleted"] is True and c.get_reference(key) is None


def test_ordinary_keys_delete_without_force_and_report_edges(entitled_test_home):
    from sibyl_memory_client import entity_ref, reference_ref
    c = server._open_client()
    c.set_entity("project", "Apollo", {"s": 1})
    c.set_reference("doc/x", "t", links=[{"to": entity_ref("project", "Apollo"),
                                          "relation": "about"}])
    out = _ok("memory_delete_reference", {"key": "doc/x"})
    assert out == {"ok": True, "key": "doc/x", "deleted": True, "edges_removed": 1}
    # a key that merely contains the word is not product-owned
    c.set_reference("doc/skill/x", "t")
    assert _ok("memory_delete_reference", {"key": "doc/skill/x"})["deleted"] is True


def test_force_delete_supersedes_an_accepted_skill_proposal(entitled_test_home):
    import uuid
    c = server._open_client()
    c.set_reference("skill/s", "# s")
    pid = str(uuid.uuid4())
    with c._storage.transaction() as conn:
        conn.execute(
            "INSERT INTO skill_proposals (id, tenant_id, pattern_kind, proposed_slug, "
            "proposed_body, evidence, confidence, summarizer, status, accepted_doc_key) "
            "VALUES (?, ?, 'repeated_action', 's', 'b', '[]', 0.9, 'local-deterministic', "
            "'accepted', 'skill/s')", (pid, c._tenant_id))
    out = _ok("memory_delete_reference", {"key": "skill/s", "force": True})
    assert out["proposals_superseded"] == 1
    with c._storage.connection() as conn:
        row = conn.execute("SELECT status, accepted_doc_key FROM skill_proposals "
                           "WHERE id = ?", (pid,)).fetchone()
    assert (row["status"], row["accepted_doc_key"]) == ("superseded", None)


def test_chars_means_stored_length_on_every_reference_tool(entitled_test_home):
    marker = "[UNTRUSTED MEMORY CONTEXT BEGIN:abc]"
    body = "a" * 10 + marker + "b" * 10
    put = _ok("memory_set_reference", {"key": "doc/c", "body": body})
    got = _ok("memory_get_reference", {"key": "doc/c"})
    listed = _ok("memory_list_references", {"prefix": "doc/c"})["items"][0]
    assert put["chars"] == got["chars"] == listed["chars"] == len(body)
    assert got["surfaced_chars"] == len(got["body"]) == len("a" * 10 + "[redacted-marker]" + "b" * 10)


# ---------------------------------------------------------------- G: argument errors name parameters

def test_memory_link_with_from_to_names_the_parameters():
    """YARROW 7: SAVE.md taught memory_link(from, to, ...)."""
    is_error, payload, text = _call("memory_link", {
        "from": {"tier": "entity", "category": "person", "key": SECRET},
        "to": {"tier": "reference", "key": "doc/k"}, "relation": "about"})
    assert is_error and payload["code"] == "VALIDATION_ERROR"
    assert payload["missing"] == ["from_node", "to_node"]
    assert payload["unknown"] == ["from", "to"]
    assert "from_node" in payload["message"] and "to_node" in payload["message"]
    assert SECRET not in text


def test_wrong_type_names_the_parameter_not_the_value():
    is_error, payload, text = _call("memory_search", {"query": "x y z", "limit": SECRET})
    assert is_error and payload["code"] == "VALIDATION_ERROR"
    assert payload["invalid"] == ["limit"] and SECRET not in text


def test_unknown_parameter_names_are_bounded_and_never_values():
    weird = {f"k{i}": SECRET for i in range(30)}
    weird[SECRET + "/" * 200] = 1
    is_error, payload, text = _call("memory_recall", weird)
    assert is_error and SECRET not in text
    assert payload["missing"] == ["category", "name"]
    assert len(payload["unknown"]) <= 10
