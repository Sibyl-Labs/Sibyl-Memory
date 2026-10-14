"""Save/debloat lifecycle in the client (spec plugin-save-debloat, tests 8.1
items 7-17 and the client-side adversarial list of 8.5).

Every test here fails on the pre-change tree: ``sibyl_memory_client.lifecycle``
did not exist (ImportError at collection), and the write counter in
``_track_op`` did not exist."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.exceptions import CapExceededError
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import capsule as C
from sibyl_memory_client.lifecycle import measure as M
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import reminders as R
from sibyl_memory_client.lifecycle import runner as RUN
from sibyl_memory_client.lifecycle import splitter as S
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client import tiers
from sibyl_memory_client.lifecycle.entitled import PAID_TIERS, automation_allowed
from sibyl_memory_client.testing import NullEntitlementGate, write_entitlement

NOW = datetime(2026, 9, 27, 12, 0, 0)
OLD = "2026-01-05"
RECENT = "2026-09-20"


@pytest.fixture(autouse=True)
def _no_heartbeat(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")


def _age(p: Path, seconds: float = 3600) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def _client(tmp_path: Path, name: str = "m.db") -> MemoryClient:
    return MemoryClient.local(tmp_path / name)


# ----------------------------------------------------------------------
# 7. Measurement conformance with context-hygiene.mjs
# ----------------------------------------------------------------------

# Records produced by `node context-hygiene.mjs --json` (upstream f0bf26d, Node
# 22.23.2) on the two fixture trees built below; ts and root removed.
EXPECTED_A = {"files": [{"file": "CLAUDE.md", "heavy": True, "soft": 6000, "tok": 6003},
                        {"file": "AGENTS.md", "heavy": False, "soft": 50000, "tok": 42245},
                        {"file": ".claude/rules.md", "heavy": False, "soft": None, "tok": 3},
                        {"file": "tiny.md", "heavy": False, "soft": None, "tok": 1}],
              "ref": {"heavy": 60000, "watch": 45000},
              "targets": [{"file": "CLAUDE.md", "kind": "file_heavy", "soft": 6000,
                           "suggestion": "CLAUDE.md is 6003 tok (soft 6000). Move accreted history "
                                         "to a *-history file; keep current state + a pointer.",
                           "tok": 6003}],
              "total_bytes": 193002, "total_tok": 48251, "weight": "watch"}
FYI_A = "context: ~48.3k tok always-load (watch) | heavy: CLAUDE.md 6003t - /debloat to trim"
EXPECTED_B = {"files": [{"file": "CLAUDE.md", "heavy": False, "soft": None, "tok": 48250}],
              "ref": {"heavy": 55000, "watch": 45000},
              "targets": [{"file": "CLAUDE.md", "kind": "file_largest", "soft": None,
                           "suggestion": "CLAUDE.md is the heaviest at 48250 tok. If it carries "
                                         "accreted history, extract it to a *-history file.",
                           "tok": 48250}],
              "total_bytes": 193000, "total_tok": 48250, "weight": "watch"}
FYI_B = "context: ~48.3k tok always-load (watch) - /debloat to trim"


def _tree_a(root: Path) -> None:
    (root / ".claude").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("x" * 24010)
    (root / "AGENTS.md").write_text("y" * 168980)
    (root / ".claude" / "rules.md").write_text("z" * 10)      # 2.5 -> 3 (half up)
    (root / "tiny.md").write_text("q" * 2)                    # 0.5 -> 1 (half up)
    (root / ".debloat.json").write_text(json.dumps({
        "surface": ["CLAUDE.md", "AGENTS.md", ".claude/rules.md", "tiny.md", "missing.md"],
        "thresholds": {"watch": 45000, "heavy": 60000},
        "perFileSoft": {"CLAUDE.md": 6000, "AGENTS.md": 50000}}))


def _tree_b(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "CLAUDE.md").write_text("x" * 193000)             # 48.25k -> "48.3" (toFixed)


def _strip(rec: dict) -> dict:
    out = M.public_record(rec)
    out.pop("ts")
    out.pop("root")
    return out


def _node() -> str | None:
    for cand in (shutil.which("node"),
                 *sorted(Path.home().glob(".nvm/versions/node/*/bin/node"))):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def test_7_measure_reproduces_the_stored_node_records(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    _tree_a(a)
    _tree_b(b)
    ra = M.measure(a, public_defaults=True)
    rb = M.measure(b, public_defaults=True)
    assert _strip(ra) == EXPECTED_A
    assert _strip(rb) == EXPECTED_B
    assert M.fyi(ra, hint=M.PUBLIC_HINT) == FYI_A
    assert M.fyi(rb, hint=M.PUBLIC_HINT) == FYI_B


CONTEXT_HYGIENE = (Path(__file__).parent / "fixtures" / "context-hygiene.mjs")


def test_7_conformance_against_node_when_available(tmp_path):
    """Runs the vendored upstream script (tests/fixtures/context-hygiene.mjs,
    MIT, byte-identical to github.com/Sibyl-Labs/debloat f0bf26d) and the
    Python port on the same trees; equal total_tok, weight, files, targets,
    and the same --fyi line and default report body."""
    node = _node()
    if node is None:
        pytest.skip("node not on PATH")
    for build in (_tree_a, _tree_b):
        root = tmp_path / build.__name__
        if build is _tree_a:
            root.mkdir()
        build(root)
        out = subprocess.run([node, str(CONTEXT_HYGIENE), "--json"], cwd=root, capture_output=True,
                             text=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        js = json.loads(out.stdout)
        py = M.measure(root, public_defaults=True)
        for k in ("total_tok", "total_bytes", "weight", "files", "targets", "ref"):
            assert py[k] == js[k], k
        fyi = subprocess.run([node, str(CONTEXT_HYGIENE), "--fyi"], cwd=root, capture_output=True,
                             text=True, timeout=30).stdout.strip()
        assert M.fyi(py, hint=M.PUBLIC_HINT) == fyi
        rep = subprocess.run([node, str(CONTEXT_HYGIENE)], cwd=root, capture_output=True,
                             text=True, timeout=30).stdout.rstrip("\n").splitlines()
        mine = M.report(py).splitlines()
        assert mine[3:] == rep[3:]      # line 2 carries the timestamp


def test_vendored_fixture_is_the_upstream_script():
    txt = CONTEXT_HYGIENE.read_text(encoding="utf-8")
    assert "const tok = (bytes) => Math.round(bytes / 4);" in txt
    assert "context-hygiene.mjs - deterministic measurement" in txt


def test_measure_plugin_default_surface_home_and_project(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (proj / ".cursor" / "rules").mkdir(parents=True)
    (home / "CLAUDE.md").write_text("h" * 400)
    (home / ".claude" / "CLAUDE.md").write_text("c" * 40)
    (proj / "AGENTS.md").write_text("a" * 800)
    (proj / ".cursorrules").write_text("r" * 80)
    (proj / ".cursor" / "rules" / "one.mdc").write_text("m" * 8)
    rec = M.measure(proj, home=home)
    files = {f["file"]: f["tok"] for f in rec["files"]}
    assert files == {"AGENTS.md": 200, ".cursorrules": 20, ".cursor/rules/one.mdc": 2,
                     "~/CLAUDE.md": 100, "~/.claude/CLAUDE.md": 10}
    assert rec["total_tok"] == 332


def test_measure_ignores_entries_outside_home_and_project(tmp_path):
    home, proj, other = tmp_path / "home", tmp_path / "proj", tmp_path / "other"
    for d in (home, proj, other):
        d.mkdir()
    (other / "file.md").write_text("o" * 400)
    (proj / "AGENTS.md").write_text("a" * 40)
    (proj / ".debloat.json").write_text(json.dumps({"surface": ["AGENTS.md", "../other/file.md",
                                                                 str(other / "file.md")]}))
    rec = M.measure(proj, home=home)
    assert [f["file"] for f in rec["files"]] == ["AGENTS.md"]
    assert set(rec["ignored"]) == {"../other/file.md", str(other / "file.md")}


def test_measure_broken_config_uses_defaults(tmp_path):
    (tmp_path / ".debloat.json").write_text("{not json")
    (tmp_path / "CLAUDE.md").write_text("x" * 40)
    rec = M.measure(tmp_path, public_defaults=True)
    assert rec["total_tok"] == 10 and "could not parse" in rec["config_error"]


# ----------------------------------------------------------------------
# 8. Splitter
# ----------------------------------------------------------------------

CUR = "# Project rules\n\nAlways run the tests.\nUse the venv.\nKeep answers short.\nNo secrets in logs.\n\n"


def test_8_history_by_marker_region():
    text = CUR + "<!-- sibyl:history -->\n" + "old note\n" * 20 + "<!-- /sibyl:history -->\n"
    pl = S.split(text, now=NOW)
    assert pl.safe and pl.history.startswith("<!-- sibyl:history -->") and "old note" not in pl.current


def test_8_history_by_heading_regex_including_subsections():
    text = CUR + "## Changelog\n" + "- did a thing\n" * 20 + "### sub\nmore\n## Current\nstay here\n"
    pl = S.split(text, now=NOW)
    assert pl.safe
    assert "did a thing" in pl.history and "### sub" in pl.history
    assert "stay here" in pl.current and "## Current" in pl.current
    assert pl.sections[0] == "Changelog"


def test_8_history_by_old_date_without_open_markers():
    text = CUR + f"## {OLD} session\n" + "we decided X\n" * 20 + f"## {RECENT} session\nfresh\n"
    pl = S.split(text, now=NOW)
    assert pl.safe and "we decided X" in pl.history and "fresh" in pl.current


def test_8_old_dated_block_with_todo_stays():
    text = CUR + f"## {OLD} session\n" + "we decided X\n" * 20 + "TODO: follow up\n"
    pl = S.split(text, now=NOW)
    assert "we decided X" in pl.current and not pl.history


def test_8_trim_below_marker():
    text = CUR + "<!-- sibyl:trim-below -->\n" + "archived line\n" * 20
    pl = S.split(text, now=NOW)
    assert pl.safe and pl.current.endswith("<!-- sibyl:trim-below -->\n")
    assert "archived line" in pl.history and "archived line" not in pl.current


def test_8_keep_and_immutable_win():
    text = (CUR + "## History\n" + "<!-- sibyl:keep -->\nkept history\n<!-- /sibyl:keep -->\n"
            + "movable\n" * 20)
    pl = S.split(text, now=NOW)
    assert "kept history" in pl.current and "movable" in pl.history


def test_adv_immutable_around_a_dated_block_stays():
    text = (CUR + "<!-- sibyl:immutable -->\n" + f"## {OLD} ledger\n" + "entry\n" * 30
            + "<!-- /sibyl:immutable -->\n")
    pl = S.split(text, now=NOW)
    assert not pl.history and "entry" in pl.current


def test_8_cursorrules_paragraph_mode():
    text = ("Use tabs.\nPrefer small diffs.\nWrite tests.\nNo secrets.\nBe brief.\n\n"
            f"{OLD} old decision\n" + "because of Y\n" * 20 + "\nHistory\nold stuff\n")
    pl = S.split(text, kind="paragraph", now=NOW)
    assert pl.safe and "because of Y" in pl.history and "Use tabs." in pl.current
    assert S.kind_for(".cursorrules") == "paragraph" and S.kind_for("CLAUDE.md") == "markdown"
    assert S.kind_for("settings.json") is None


def test_8_under_10_and_over_90_percent_are_no_safe_trim():
    small = CUR * 20 + "## History\nx\n"
    assert S.split(small, now=NOW).reason.startswith("no safe trim: would move under 10")
    whole = f"## {OLD} a\n" + "a\n" * 50 + f"## {OLD} b\n" + "b\n" * 50
    pl = S.split(whole, now=NOW)
    assert not pl.safe and "over 90" in pl.reason


def test_adv_claude_md_entirely_dated_history_is_no_safe_trim(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    f = home / "CLAUDE.md"
    f.write_text("".join(f"## {OLD} s{i}\n" + "old\n" * 10 for i in range(10)))
    _age(f)
    c, _ = A.find_candidate("~/CLAUDE.md", home, tmp_path / "proj_missing")
    res = A.apply_one(c, home=home, cwd=tmp_path, destination="sibling")
    assert not res.applied and "over 90" in res.reason
    assert not A.sibling_path(f).exists()


def test_adv_clock_set_back_a_year_is_more_conservative():
    text = CUR + f"## {OLD} session\n" + "decided\n" * 20
    assert S.split(text, now=NOW).safe
    back = S.split(text, now=NOW - timedelta(days=365))
    assert not back.history      # the date is now in the future: nothing is older than the clock


def test_splitter_ignores_headings_inside_code_fences():
    text = CUR + "```\n## History\ncode\n```\n" + "tail\n" * 5
    assert not S.split(text, now=NOW).history


# ----------------------------------------------------------------------
# 9. Loss-free round trip (20 fixtures, CRLF, BOM)
# ----------------------------------------------------------------------

def _fixtures() -> list[str]:
    base = [
        CUR + "## Changelog\n" + "- item\n" * 30,
        CUR + f"## {OLD} notes\n" + "n\n" * 30 + "## Rules\nr\n",
        CUR + "<!-- sibyl:history -->\n" + "h\n" * 30 + "<!-- /sibyl:history -->\ntail\n",
        CUR + "<!-- sibyl:trim-below -->\n" + "t\n" * 30,
        CUR + "## Archive\n### a\n" + "x\n" * 15 + "### b\n" + "y\n" * 15,
        CUR + "## Log\n" + "l\n" * 30 + "no trailing newline",
        CUR + "## Done\n" + "- [x] thing\n" * 30,
        CUR + "## Session notes\n" + "s\n" * 30 + "## Keep\n<!-- sibyl:keep -->\nk\n<!-- /sibyl:keep -->\n",
        "# T\n" + "été règles ✓ line\n" * 10 + "## History\n" + "über 中文\n" * 10,
        CUR + f"# {OLD} top\n" + "z\n" * 30 + "# Now\nnow\n",
    ]
    out = list(base)
    out += [t.replace("\n", "\r\n") for t in base[:5]]
    out += ["﻿" + t for t in base[5:10]]
    assert len(out) == 20
    return out


@pytest.mark.parametrize("idx", range(20))
def test_9_loss_free_round_trip(idx):
    text = _fixtures()[idx]
    pl = S.split(text, now=NOW)
    assert pl.safe, pl.reason
    assert S.reassemble(pl.current, pl.history, pl.layout) == text
    eol = "\r\n" if "\r\n" in text else "\n"
    new = A.with_pointer(pl.current, A.pointer_line(key="debloat/X/2026-09-27", sibling=None,
                                                    date="2026-09-27", backup_display="~/b"), eol)
    assert new.count("<!-- history moved") == 1
    back = A.strip_pointer(new)
    # Byte-exact modulo the single pointer line and one trailing newline.
    assert back in (pl.current, pl.current + eol)
    rebuilt = S.reassemble(pl.current, pl.history, pl.layout)
    assert rebuilt == text


def test_9_round_trip_through_a_real_pro_apply(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    proj = tmp_path / "proj"
    proj.mkdir()
    for i, text in enumerate(_fixtures()):
        name = "AGENTS.md"
        f = proj / name
        f.write_bytes(text.encode("utf-8"))
        _age(f)
        client = _client(tmp_path, f"rt{i}.db")
        c, _ = A.find_candidate(name, home, proj)
        res = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
        assert res.applied, (i, res.reason)
        ref = client.get_reference(res.key)
        current = A.strip_pointer(f.read_bytes().decode("utf-8"))
        rebuilt = S.reassemble(current, ref["body"], ref["metadata"]["layout"])
        assert rebuilt == text, i
        backup = Path(res.backup)
        assert backup.read_bytes() == text.encode("utf-8")


# ----------------------------------------------------------------------
# 10-12. Apply refusals, ordering, undo
# ----------------------------------------------------------------------

def _proj(tmp_path: Path, text: str = None, name: str = "AGENTS.md") -> tuple[Path, Path, Path]:
    home = Path.home()
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    f = proj / name
    f.write_text(text if text is not None else CUR + "## History\n" + "old\n" * 30)
    _age(f)
    return home, proj, f


def test_10_refusals_write_nothing(tmp_path, monkeypatch):
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    # symlink
    link = proj / "CLAUDE.md"
    link.symlink_to(f)
    cl, _ = A.find_candidate("CLAUDE.md", home, proj)
    assert A.apply_one(cl, home=home, cwd=proj, destination="sibling").reason == \
        "refused: the file is a symlink"
    link.unlink()
    # directory
    d = A.Candidate("AGENTS2.md", proj, "AGENTS.md")
    assert "not a regular file" in A.apply_one(d, home=home, cwd=proj, destination="sibling").reason
    # outside home and cwd
    outside = Path("/tmp") / f"sd-outside-{os.getpid()}.md"
    try:
        outside.write_text(CUR + "## History\n" + "x\n" * 30)
        _age(outside)
        o = A.Candidate("x.md", outside, "AGENTS.md")
        assert "outside your home" in A.apply_one(o, home=home, cwd=proj, destination="sibling").reason
    finally:
        outside.unlink()
    # not in the surface
    assert "not in the measured surface" in A.apply_one(
        c, home=home, cwd=proj, destination="sibling", surface_labels={"CLAUDE.md"}).reason
    # never glob / config file
    j = proj / "notes.json"
    j.write_text("{}")
    cj = A.Candidate("notes.json", j, "notes.json",
                     trimmable=False, why_not=A.trimmable_reason("notes.json", j, P.load(), set()))
    assert "never" in A.apply_one(cj, home=home, cwd=proj, destination="sibling").reason
    # > 2 MB
    big = proj / "MEMORY.md"
    big.write_text("## History\n" + "b" * (2 * 1024 * 1024 + 10))
    _age(big)
    cb, _ = A.find_candidate("MEMORY.md", home, proj)
    assert A.apply_one(cb, home=home, cwd=proj, destination="sibling").reason == "refused: over 2 MB"
    # modified < 10 min ago
    os.utime(f, None)
    assert "last 10 minutes" in A.apply_one(c, home=home, cwd=proj, destination="sibling").reason
    _age(f)
    # missing backup -> backup failure (symlinked backups dir)
    bdir = A.debloat_dir(home)
    bdir.mkdir(parents=True, exist_ok=True)
    trap = tmp_path / "trap"
    trap.mkdir()
    (bdir / "backups").symlink_to(trap)
    res = A.apply_one(c, home=home, cwd=proj, destination="sibling")
    assert res.reason.startswith("refused: backup failed") and not res.applied
    (bdir / "backups").unlink()
    assert f.read_bytes() == before
    assert not A.sibling_path(f).exists()
    assert list(trap.iterdir()) == []


def test_10_size_mismatched_backup_is_refused(tmp_path, monkeypatch):
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    real = A.atomic_write_bytes

    def short_backup(path, data, **kw):
        if "backups" in str(path):
            return real(path, data[:-1], **kw)
        return real(path, data, **kw)
    monkeypatch.setattr(A, "atomic_write_bytes", short_backup)
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    res = A.apply_one(c, home=home, cwd=proj, destination="sibling")
    assert res.reason.startswith("refused: backup failed") and f.read_bytes() == before


def test_10_free_locked_off_and_not_named_never_apply(tmp_path, entitle_as, monkeypatch):
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    calls = []
    spy = lambda *a, **k: calls.append(1) or A.ApplyResult("x", False, "spy")  # noqa: E731
    client = _client(tmp_path)
    # free entitlement + mode auto + named file
    P.save({"mode": "auto", "auto_files": ["AGENTS.md"]}, home)
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="test", apply_fn=spy)
    assert not r.ran and "not entitled (free)" == r.reason and not calls
    # locked
    entitle_as("pro")
    (home / ".sibyl-memory" / "credentials.json").unlink()
    r = RUN.run_automatic(_client(tmp_path, "b.db"), home=home, cwd=proj, harness="test", apply_fn=spy)
    assert not r.ran and "no_account" in r.reason and not calls
    assert f.read_bytes() == before


def test_10_pro_mode_off_and_unnamed_file_never_apply(tmp_path, entitle_as):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    calls = []
    spy = lambda *a, **k: calls.append(1) or A.ApplyResult("x", False, "spy")  # noqa: E731
    client = _client(tmp_path)
    P.save({"mode": "off", "auto_files": ["AGENTS.md"]}, home)
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="t", apply_fn=spy)
    assert r.ran and not calls and r.line is None
    ST.LifecycleState(home).update(lambda st: st["debloat"].clear())
    P.save({"mode": "auto", "auto_files": ["CLAUDE.md"]}, home)
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="t", apply_fn=spy)
    assert r.ran and not calls and "would move to memory" in r.line
    assert f.read_bytes() == before


def test_adv_auto_with_empty_auto_files_behaves_as_propose(tmp_path, entitle_as):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    P.save({"mode": "auto", "auto_files": []}, home)
    r = RUN.run_automatic(_client(tmp_path), home=home, cwd=proj, harness="t")
    assert r.ran and r.applied == [] and "would move to memory" in r.line
    assert "`sibyl debloat auto AGENTS.md`" in r.line
    assert f.read_bytes() == before
    plans = list((A.debloat_dir(home) / "plans").glob("*.json"))
    assert len(plans) == 1
    body = plans[0].read_text()
    assert "old\nold" not in body and '"file": "AGENTS.md"' in body


def test_pro_auto_named_file_applies_and_reports(tmp_path, entitle_as):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    P.set_auto(P.auth_id(f, home), True, home)
    client = _client(tmp_path)
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="t")
    assert r.applied and r.applied[0]["applied"], r.applied
    key = r.applied[0]["key"]
    assert key.startswith("debloat/AGENTS.md/")
    assert re.match(r"context trim: AGENTS\.md \d+ -> \d+ tok, history stored to memory as debloat/AGENTS\.md/",
                    r.line)
    txt = f.read_text()
    assert "old\nold" not in txt and f"<!-- history moved to Sibyl Memory ({key}, recall with memory_search)" in txt
    assert client.get_reference(key)["body"].count("old\n") == 30
    assert json.loads((A.debloat_dir(home) / "history.jsonl").read_text().splitlines()[-1])["applied"] == 1


def test_adv_symlink_to_etc_hosts_is_refused(tmp_path, entitle_as):
    entitle_as("pro")
    home = Path.home()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").symlink_to("/etc/hosts")
    hosts = Path("/etc/hosts").read_bytes()
    P.set_auto(P.auth_id(proj / "CLAUDE.md", home), True, home)
    c, _ = A.find_candidate("CLAUDE.md", home, proj)
    res = A.apply_one(c, home=home, cwd=proj, destination="memory", client=_client(tmp_path))
    assert res.reason == "refused: the file is a symlink"
    r = RUN.run_automatic(_client(tmp_path, "z.db"), home=home, cwd=proj, harness="t")
    assert all(not a["applied"] for a in r.applied)
    assert Path("/etc/hosts").read_bytes() == hosts


def test_adv_debloat_json_naming_a_path_outside_is_ignored(tmp_path):
    home, proj, f = _proj(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.md").write_text(CUR + "## History\n" + "x\n" * 30)
    (proj / ".debloat.json").write_text(json.dumps({"surface": ["AGENTS.md", "../other/file.md"]}))
    cands, rec = A.candidates(home, proj)
    assert [c.label for c in cands] == ["AGENTS.md"] and rec["ignored"] == ["../other/file.md"]


def test_11_memory_write_failure_leaves_source_identical(tmp_path, entitle_as, monkeypatch):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    client = _client(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(client, "set_reference", boom)
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    res = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    assert not res.applied and "memory write failed" in res.reason
    assert f.read_bytes() == before
    # SD-7 (F3): the source was never rewritten, so the apply's backup is gone.
    assert res.backup is None and A.backups_of(A.backup_rel(f, home), home) == []


def test_adv_cap_exceeded_leaves_source_untouched_and_says_so(tmp_path, entitle_as, monkeypatch):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    client = _client(tmp_path)

    def cap(*a, **k):
        raise CapExceededError("cap", current_size=1, cap=1)
    monkeypatch.setattr(client, "set_reference", cap)
    P.set_auto(P.auth_id(f, home), True, home)
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="t")
    assert "store is at the cap" in r.applied[0]["reason"]
    assert f.read_bytes() == before


def test_11_rewrite_failure_keeps_memory_copy_and_reapply_reuses_it(tmp_path, entitle_as, monkeypatch):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    client = _client(tmp_path)
    real = A.atomic_write_bytes

    def fail_source(path, data, **kw):
        if Path(path) == f:
            raise OSError("injected replace failure")
        return real(path, data, **kw)
    monkeypatch.setattr(A, "atomic_write_bytes", fail_source)
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    res = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    assert not res.applied and "rewrite failed" in res.reason and f.read_bytes() == before
    key = f"debloat/AGENTS.md/{time.strftime('%Y-%m-%d')}"
    assert client.get_reference(key)["body"].count("old\n") == 30
    monkeypatch.setattr(A, "atomic_write_bytes", real)
    res2 = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    assert res2.applied and res2.reused_memory_copy and res2.key == key
    assert client.get_reference(key + "-2") is None


def test_second_trim_same_day_gets_a_new_key_never_overwrites(tmp_path, entitle_as):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    client = _client(tmp_path)
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    r1 = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    f.write_text(f.read_text() + "## History\n" + "second batch\n" * 40)
    _age(f)
    r2 = A.apply_one(c, home=home, cwd=proj, destination="memory", client=client)
    assert r1.applied and r2.applied and r2.key == r1.key + "-2"
    assert "second batch" in client.get_reference(r2.key)["body"]
    assert "old" in client.get_reference(r1.key)["body"]


def test_12_undo_restores_the_backup_and_remeasures(tmp_path):
    home, proj, f = _proj(tmp_path, CUR + "## History\n" + "an old line of accreted history\n" * 15)
    before = f.read_bytes()
    c, _ = A.find_candidate("AGENTS.md", home, proj)
    res = A.apply_one(c, home=home, cwd=proj, destination="sibling")
    assert res.applied and f.read_bytes() != before
    assert A.sibling_path(f).name == "AGENTS-history.md"
    trimmed_tok = M.measure(proj, home=home)["total_tok"]
    ok, msg, src = A.undo(f, home=home, cwd=proj)
    assert ok and f.read_bytes() == before
    assert M.measure(proj, home=home)["total_tok"] > trimmed_tok
    undo_dirs = [d for d in (A.debloat_dir(home) / "backups").iterdir() if d.name.endswith("-undo")]
    assert len(undo_dirs) == 1


# ----------------------------------------------------------------------
# 13. Gate
# ----------------------------------------------------------------------

def test_13_automation_allowed_matrix(tmp_path, entitle_as, test_signer, monkeypatch):
    home = Path.home()
    d = home / ".sibyl-memory"
    from sibyl_memory_client._entitlement import EntitlementGate

    def _offline(name):
        # No refresh at all: a band-test client must not spawn a background
        # refresh that (via the signer stub) re-mints a fresh token mid-test.
        return MemoryClient.local(tmp_path / name, entitlement_gate=EntitlementGate(
            d / "credentials.json", allow_refresh=False))
    client = _client(tmp_path)
    assert automation_allowed(client) == (False, "free")
    # Every paid label (canonical pro/elite and the legacy aliases) automates
    # and answers with its canonical tier (tier convention 2026-10-05).
    assert "elite" in PAID_TIERS
    for tier in sorted(PAID_TIERS):
        entitle_as(tier)
        assert automation_allowed(_client(tmp_path, f"{tier}.db")) == (True, tiers.normalize(tier))
    # stale band, paid
    write_entitlement(d, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 8 * 86400, tier="pro", cap=None, src="pro")))
    stale = _offline("stale.db")
    assert stale.entitlement_gate.peek().state == "stale"
    assert automation_allowed(stale) == (True, "pro")
    # locked (past exp + og)
    write_entitlement(d, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 40 * 86400, tier="pro", cap=None, src="pro")))
    ok, why = automation_allowed(_offline("locked.db"))
    assert not ok and why != "pro"
    # setup_incomplete: credentials, no entitlement, refresh offline
    from sibyl_memory_client import _entitlement as E
    monkeypatch.setattr(E, "http_refresh", lambda a, t: E.RefreshOutcome("transient", code="offline"))
    (d / "entitlement.json").unlink()
    ok, why = automation_allowed(_client(tmp_path, "setup.db"))
    assert (ok, why) == (False, "setup_incomplete")
    # no account
    (d / "credentials.json").unlink()
    assert automation_allowed(_client(tmp_path, "none.db")) == (False, "no_account")
    # the escape-hatch gate never automates, whatever the hint says
    null = MemoryClient.local(tmp_path / "n.db", tier="lifetime", entitlement_gate=NullEntitlementGate())
    assert automation_allowed(null) == (False, "no_entitlement")


def test_13_edited_files_unlock_nothing(tmp_path):
    home, proj, f = _proj(tmp_path)
    before = f.read_bytes()
    d = home / ".sibyl-memory"
    creds = json.loads((d / "credentials.json").read_text())
    creds["tier"] = "lifetime"
    (d / "credentials.json").write_text(json.dumps(creds))
    (d / "tier_cache.json").write_text(json.dumps({"tier": "lifetime", "checked_at": time.time()}))
    (d / "lifecycle.json").write_text(json.dumps({"tier": "pro", "automation": True, "pro": True,
                                                  "tenants": {}}))
    P.save({"mode": "auto", "auto_files": ["AGENTS.md"]}, home)
    client = _client(tmp_path)
    assert automation_allowed(client) == (False, "free")
    r = RUN.run_automatic(client, home=home, cwd=proj, harness="t")
    assert not r.ran and f.read_bytes() == before


def test_paid_tier_sets_agree():
    assert PAID_TIERS == MemoryClient._PAID_ONLY_TIERS == tiers.PAID_TIERS
    from sibyl_memory_client._entitlement import WRITE_OPS
    assert ST.WRITE_OPS == WRITE_OPS
    from sibyl_memory_client.client import _LIFECYCLE_WRITE_KINDS
    assert _LIFECYCLE_WRITE_KINDS == WRITE_OPS


# ----------------------------------------------------------------------
# 14. Reminder engine + the write counter
# ----------------------------------------------------------------------

def _st(**t):
    st = ST.default_state()
    tt = ST.tenant(st, "ten")
    tt.update(t)
    return st


def test_14_save_thresholds_and_throttle():
    now = 1_000_000.0
    st = _st(writes_since_save=24, since_save_started_at=now - 60)
    assert R.save_reminder(st, "ten", now, pro=False) is None
    st = _st(writes_since_save=25, since_save_started_at=now - 60)
    line = R.save_reminder(st, "ten", now, pro=False)
    assert line == ("Sibyl Memory: 25 writes since the last save. Run /sibyl-save (or the save "
                    "prompt) before you stop. Automatic save is on Pro.")
    st["tenants"]["ten"]["writes_since_save"] = 31
    assert R.save_reminder(st, "ten", now + 3600, pro=False) is None          # 6 h throttle
    again = R.save_reminder(st, "ten", now + 6 * 3600 + 1, pro=False)
    assert again.startswith("Sibyl Memory: 31 writes") and not again.endswith("on Pro.")
    st = _st(writes_since_save=5, since_save_started_at=now - 6 * 3600 - 1)
    assert R.save_reminder(st, "ten", now, pro=False)
    st = _st(writes_since_save=5, since_save_started_at=now - 3600)
    assert R.save_reminder(st, "ten", now, pro=False) is None
    # R1B-4: a compaction is the session's own (the asking session's key).
    st = _st(writes_since_save=1)
    ST.ensure_session(st, "sk", tenant_id="ten", harness="x", now=now)["compaction_seen"] = True
    assert R.save_reminder(st, "ten", now, pro=False, sk="sk")
    st = _st(writes_since_save=1)
    ST.ensure_session(st, "sk", tenant_id="ten", harness="x", now=now)["compaction_seen"] = True
    assert R.save_reminder(st, "ten", now, pro=False, sk="other") is None


def test_14_off_switch_pro_replacement_and_pro_clause_once_per_week():
    now = 2_000_000.0
    st = _st(writes_since_save=40, since_save_started_at=now - 60)
    st["reminders"] = False
    assert R.save_reminder(st, "ten", now, pro=False) is None
    st["reminders"] = True
    assert R.save_reminder(st, "ten", now, pro=True) is None       # Pro: the automation acts
    rec = {"files": [{"file": "AGENTS.md", "tok": 10223, "soft": 6000, "heavy": True}],
           "targets": [], "weight": "watch", "total_tok": 48300}
    line = R.debloat_reminder(st, "ten", now, rec, pro=False)
    assert line == ("context: ~48.3k tok always-load (watch) | heavy: AGENTS.md 10223t. Run "
                    "/sibyl-debloat to trim. Automatic on Pro.")
    assert R.debloat_reminder(st, "ten", now + 86400, rec, pro=False) is None     # 7 days per file
    line2 = R.debloat_reminder(st, "ten", now + 7 * 86400 + 1, rec, pro=False)
    assert line2 and line2.endswith("Automatic on Pro.")      # a week later the clause may return
    st2 = _st(writes_since_save=30, since_save_started_at=now)
    R.save_reminder(st2, "ten", now, pro=False)
    line3 = R.debloat_reminder(st2, "ten", now, rec, pro=False)
    assert line3.endswith("Run /sibyl-debloat to trim.")      # clause already shown this week


def test_14_unsaved_last_session_and_pending_lines():
    now = 3_000_000.0
    st = _st(writes_total=40)
    st["sessions"]["s1"] = {"tenant": "ten", "harness": "claude-code", "started_at": now - 7200,
                            "ended_at": now - 100, "writes": 31, "saved": False}
    line = R.unsaved_session_line(st, "ten", now)
    assert line == ("Sibyl Memory: your last session (Claude Code, 2 h, 31 writes) was not saved. "
                    "Run /sibyl-save now to save what is still worth keeping.")
    assert R.unsaved_session_line(st, "ten", now) is None     # once
    assert R.pending_line(2).startswith("Sibyl Memory: last session left 2 items to decide")
    assert R.pending_line(0) is None


def test_14_counter_counts_writes_and_the_save_marker_resets(tmp_path):
    client = _client(tmp_path)
    client.enable_lifecycle_counting()        # SD-9: opt-in (the MCP server / hermes / CLI do this)
    tid = client.get_tenant()
    for i in range(12):
        client.set_entity("fact", f"f{i}", {"v": i})
    client.get_entity("fact", "f1")            # a read never counts
    ST.flush_all()
    t = ST.LifecycleState().load()["tenants"][tid]
    assert t["writes_total"] == 12 and t["writes_since_save"] == 12
    client.set_state("sibyl_save:last_save", {"ts": "2026-09-27T12:00:00Z"})
    t = ST.LifecycleState().load()["tenants"][tid]
    assert t["writes_since_save"] == 0 and t["last_save_at"]
    with ST.suppressed():
        client.write_event(acted={"kind": "x"})
    ST.flush_all()
    assert ST.LifecycleState().load()["tenants"][tid]["writes_since_save"] == 0
    raw = (Path.home() / ".sibyl-memory" / "lifecycle.json").read_text()
    assert "f1" not in raw and "fact" not in raw


def test_adv_editing_lifecycle_json_changes_timing_only(tmp_path):
    home, proj, f = _proj(tmp_path)
    client = _client(tmp_path)
    tid = client.get_tenant()
    p = home / ".sibyl-memory" / "lifecycle.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({"tenants": {tid: {"writes_since_save": 999, "pro_line_shown_at": 0}},
                             "tier": "lifetime", "entitled": True}))
    st = ST.LifecycleState().load()
    assert R.save_reminder(st, tid, time.time(), pro=False)          # timing changed
    assert automation_allowed(client) == (False, "free")             # entitlement did not
    p.write_text("{garbage")
    assert ST.LifecycleState().load() == ST.default_state()          # damaged = empty


# ----------------------------------------------------------------------
# 15. Capsule
# ----------------------------------------------------------------------

def test_15_capsule_shape_and_pro_only(tmp_path, entitle_as):
    cap = C.build_capsule(harness="claude-code", session_id="abc-secret-session",
                          started_at=1_700_000_000, ended_at=1_700_003_600, turns=12,
                          memory_writes=31, saved_by_skill=False,
                          cwd="/home/someone/projects/" + "x" * 100)
    assert set(cap) == {"harness", "session_id", "started_at", "ended_at", "turns", "memory_writes",
                        "saved_by_skill", "cwd_name"}
    assert "abc-secret-session" not in json.dumps(cap) and "/home/someone" not in json.dumps(cap)
    assert all(len(v) <= 64 for v in cap.values() if isinstance(v, str))
    free = _client(tmp_path)
    assert C.write_capsule(free, cap) is None
    assert free.read_events(limit=5) == []
    entitle_as("pro")
    pro = _client(tmp_path, "p.db")
    ev = C.write_capsule(pro, cap)
    assert ev
    got = pro.read_events(limit=5)
    assert got[0]["acted"]["kind"] == "session_capsule" and got[0]["extra"] == {"category": "session"}


# ----------------------------------------------------------------------
# 16-17. No network, no environment switch
# ----------------------------------------------------------------------

def _lifecycle_modules() -> list[Path]:
    import sibyl_memory_client
    root = Path(sibyl_memory_client.__file__).parent
    return sorted((root / "lifecycle").glob("*.py")) + [root / "skills.py", root / "_fsio.py"]


def test_16_lifecycle_imports_no_network_modules():
    banned = {"urllib", "socket", "http", "ssl", "requests", "httpx"}
    for p in _lifecycle_modules():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not (set(names) & banned), (p.name, names)


def test_17_no_environment_switch():
    for p in _lifecycle_modules():
        src = p.read_text(encoding="utf-8")
        assert "os.environ" not in src and "getenv" not in src, p.name
    import sibyl_memory_client
    root = Path(sibyl_memory_client.__file__).parent
    for p in root.rglob("*.py"):
        for m in re.findall(r"SIBYL_[A-Z0-9_]+", p.read_text(encoding="utf-8")):
            assert not re.search(r"AUTO|PRO|SKIP", m), (p.name, m)


# ----------------------------------------------------------------------
# Adversarial: two processes start together
# ----------------------------------------------------------------------

def test_adv_two_processes_one_plan_one_apply(tmp_path, entitle_as):
    entitle_as("pro")
    home, proj, f = _proj(tmp_path)
    P.set_auto(P.auth_id(f, home), True, home)
    script = f"""
import sys, json, time
sys.path.insert(0, {str(Path(A.__file__).parents[2])!r})
from sibyl_memory_client import MemoryClient, _entitlement as E
from sibyl_memory_client.lifecycle import runner
E.TRUSTED_KEYS = {{k: bytes.fromhex(v) for k, v in json.loads(sys.argv[1]).items()}}
E.http_refresh = lambda a, t: E.RefreshOutcome("transient", code="offline")
time.sleep(max(0.0, float(sys.argv[2]) - time.time()))
c = MemoryClient.local({str(tmp_path)!r} + "/" + sys.argv[3])
r = runner.run_automatic(c, home={str(home)!r}, cwd={str(proj)!r}, harness=sys.argv[3])
print(json.dumps({{"ran": r.ran, "reason": r.reason, "applied": [a["applied"] for a in r.applied]}}))
"""
    from sibyl_memory_client import _entitlement as E
    keys = json.dumps({k: v.hex() for k, v in E.TRUSTED_KEYS.items()})
    start = time.time() + 1.5
    env = dict(os.environ, SIBYL_MEMORY_TELEMETRY="0")
    import sys
    procs = [subprocess.Popen([sys.executable, "-c", script, keys, str(start), name],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for name in ("mcp.db", "hermes.db")]
    outs = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    ran = [o for o in outs if o["ran"]]
    assert len(ran) == 1, outs
    assert ran[0]["applied"] == [True]
    other = [o for o in outs if not o["ran"]][0]
    assert other["reason"] in ("another process holds the debloat lock", "ran moments ago")
    assert f.read_text().count("<!-- history moved") == 1


def test_run_lock_stale_after_60_seconds(tmp_path):
    home = Path.home()
    d = home / ".sibyl-memory"
    d.mkdir(exist_ok=True)
    lp = d / "debloat.lock"
    lp.write_text("123")
    with A.run_lock(home) as got:
        assert not got
    t = time.time() - 120
    os.utime(lp, (t, t))
    with A.run_lock(home) as got:
        assert got
    assert not lp.exists()
