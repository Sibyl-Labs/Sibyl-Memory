"""cli 0.5.0 save/debloat surface (spec plugin-save-debloat, tests 8.4 items
30-45 and the CLI-side adversarial list of 8.5).

Every test here fails on the pre-change tree: the `save`, `debloat`, `skills`,
`reminders` and `hook` commands did not exist (argparse exits 2), the wirers
wrote no shims, hooks or AGENTS.md block, and `sibyl status` had no lifecycle
rows. Test 44 reads the installed Claude Code binary and is skipped without it.
"""
from __future__ import annotations

import builtins
import json
import mmap
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import hooks as H
from sibyl_memory_cli import lifecycle_cli as LC
from sibyl_memory_cli import setup as SU
from sibyl_memory_client import MemoryClient
from sibyl_memory_client import skills as SK
from sibyl_memory_client.lifecycle import apply as A
from sibyl_memory_client.lifecycle import policy as P
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

CURRENT = "# Agent rules\n\nRun the tests first.\nUse the venv.\nKeep answers short.\nNo secrets.\n\n"
HIST = "## History\n" + "".join(f"- an old decision number {i} that is history now\n" for i in range(12))
SENTINEL = "ZEBRA-SENTINEL-7731"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def home():
    return Path.home()


@pytest.fixture
def sm(home):
    """~/.sibyl-memory of the activated fake home (credentials + entitlement)."""
    return home / ".sibyl-memory"


@pytest.fixture(autouse=True)
def _offline_server(monkeypatch):
    """No test reaches an api: /check-write answers "pro, uncapped" for a paid
    entitlement (what the server says after an upgrade), and the CLI's own
    HTTP calls fail as offline."""
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})
    def offline(*a, **k):
        raise cli.HttpError(0, {}, "offline")
    monkeypatch.setattr(cli, "http_request", offline)


@pytest.fixture
def pro(sm, test_signer):
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="pro"))
    return sm


@pytest.fixture
def proj(tmp_path, monkeypatch):
    p = tmp_path / "proj"
    p.mkdir()
    monkeypatch.chdir(p)
    return p


def _age(p: Path, s: float = 3600) -> None:
    t = time.time() - s
    os.utime(p, (t, t))


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _age(p)
    return p


def _acct(sm: Path) -> list[str]:
    return ["--credentials", str(sm / "credentials.json"), "--db", str(sm / "memory.db")]


def _tenant(sm: Path) -> str:
    c = json.loads((sm / "credentials.json").read_text())
    return c.get("tenant_id") or c["account_id"]


def _client(sm: Path) -> MemoryClient:
    return MemoryClient.local(str(sm / "memory.db"), tenant_id=_tenant(sm),
                              credentials_path=sm / "credentials.json")


def _hook(event: str, payload: dict | str, home: Path, now: float | None = None) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return H.run_hook(event, text, home=home, now=now)


def _warm_tier_cache(sm: Path, tier: str = "pro") -> None:
    """What /check-write leaves after a paid write: a fresh, account-matched,
    uncapped tier cache. SD-10: a hook writes memory only when the cap gate
    can decide without the network."""
    from sibyl_memory_client._capcheck import TierCache, TierCacheEntry
    acct = json.loads((sm / "credentials.json").read_text())["account_id"]
    TierCache(sm / "tier_cache.json").store(TierCacheEntry(account_id=acct, tier=tier,
                                                           checked_at=time.time(), cap_bytes=None))


def _bump_writes(home: Path, tenant: str, n: int) -> None:
    def _u(st):
        t = ST.tenant(st, tenant)
        t["writes_total"] = int(t.get("writes_total") or 0) + n
        t["writes_since_save"] = int(t.get("writes_since_save") or 0) + n
        t["since_save_started_at"] = t.get("since_save_started_at") or time.time()
    ST.LifecycleState(home).update(_u)


# ----------------------------------------------------------------------
# 30. sibyl debloat measurement
# ----------------------------------------------------------------------

def test_30_debloat_report_fyi_json_and_exit_codes(proj, home, capsys):
    _write(proj / "AGENTS.md", "a" * 40000)
    _write(home / "CLAUDE.md", "c" * 400)
    assert cli.main(["debloat"]) == 0
    out = capsys.readouterr().out
    assert "CONTEXT WEIGHT" in out and "10000 tok  AGENTS.md  <-- heavy (soft 6000)" in out
    assert "~/CLAUDE.md" in out
    assert cli.main(["debloat", "--fyi"]) == 0
    fyi = capsys.readouterr().out.strip().splitlines()
    assert fyi == ["context: ~10.1k tok always-load (watch) | heavy: AGENTS.md 10000t - /sibyl-debloat to trim"]
    assert cli.main(["debloat", "--json"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert set(rec) >= {"ts", "root", "total_tok", "total_bytes", "ref", "weight", "files", "targets"}
    assert not any(k.startswith("_") for k in rec)
    assert rec["total_tok"] == 10100 and rec["targets"][0]["kind"] == "file_heavy"


def test_30_exit_0_with_no_files_and_with_a_broken_config(proj, capsys):
    assert cli.main(["debloat", "--fyi"]) == 0
    assert capsys.readouterr().out.strip() == "context: no always-load files found"
    (proj / ".debloat.json").write_text("{broken")
    _write(proj / "CLAUDE.md", "x" * 40)
    assert cli.main(["debloat", "--json"]) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["total_tok"] == 10
    assert "could not parse" in cap.err and "defaults used" in cap.err


def test_30_json_carries_the_plan_when_a_policy_exists(proj, home, capsys):
    _write(proj / "AGENTS.md", CURRENT + HIST)
    P.save({"mode": "propose"}, home)
    assert cli.main(["debloat", "--json"]) == 0
    rec = json.loads(capsys.readouterr().out)
    plan = {p["file"]: p for p in rec["plan"]}
    assert plan["AGENTS.md"]["safe"] and plan["AGENTS.md"]["sections"] == ["History"]
    assert "old decision" not in json.dumps(rec)


# ----------------------------------------------------------------------
# 31. sibyl debloat apply
# ----------------------------------------------------------------------

def test_31_apply_free_writes_the_sibling_and_constructs_no_client(proj, sm, capsys, monkeypatch):
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    before = f.stat().st_size

    def no_client(*a, **k):
        raise AssertionError("a memory client was constructed on the free path")
    monkeypatch.setattr(LC, "_client", no_client)
    assert cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    out = capsys.readouterr().out
    sib = proj / "AGENTS-history.md"
    assert sib.exists() and "an old decision number 3" in sib.read_text()
    txt = f.read_text()
    assert "<!-- history moved to AGENTS-history.md on " in txt and f.stat().st_size < before
    assert "old decision" not in txt and "AGENTS.md:" in out
    assert not (sm / "memory.db").exists() or _client(sm).list_entities() == []


def test_31_apply_pro_writes_a_reference_with_a_verified_backup(proj, pro, capsys):
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    original = f.read_bytes()
    assert cli.main(_acct(pro) + ["debloat", "apply", "AGENTS.md", "--yes"]) == 0
    out = capsys.readouterr().out
    key = re.search(r"history in (debloat/AGENTS\.md/\d{4}-\d{2}-\d{2})", out).group(1)
    ref = _client(pro).get_reference(key)
    assert "old decision number 11" in ref["body"]
    assert f"({key}, recall with memory_search)" in f.read_text()
    backups = list((pro / "debloat" / "backups").rglob("AGENTS.md"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not (proj / "AGENTS-history.md").exists()


def test_apply_without_yes_on_a_non_terminal_changes_nothing(proj, sm, capsys, monkeypatch):
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    before = f.read_bytes()
    monkeypatch.setattr(LC.sys.stdin, "isatty", lambda: False, raising=False)
    cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md"])
    assert f.read_bytes() == before and "re-run with --yes" in capsys.readouterr().out


def test_debloat_undo_restores(proj, sm, capsys):
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    before = f.read_bytes()
    cli.main(_acct(sm) + ["debloat", "apply", "AGENTS.md", "--yes"])
    assert f.read_bytes() != before
    assert cli.main(_acct(sm) + ["debloat", "undo", "AGENTS.md", "--yes"]) == 0
    assert f.read_bytes() == before


# ----------------------------------------------------------------------
# 32. auto / manual
# ----------------------------------------------------------------------

def test_32_auto_asks_once_manual_removes_status_shows(proj, pro, home, capsys, monkeypatch):
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    fid = P.auth_id(f, home)
    asked: list[str] = []
    monkeypatch.setattr(LC.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda q="": asked.append(q) or "y")
    assert cli.main(_acct(pro) + ["debloat", "auto", "AGENTS.md"]) == 0
    out = capsys.readouterr().out
    assert len(asked) == 1 and f"Automatic context trim for this file at {fid} means" in out
    assert f"this file at {fid} automatically?" in asked[0]
    pol = P.load(home)
    assert pol["auto_files"] == [fid] and pol["mode"] == "auto"
    LC.status_rows(cli.build_parser().parse_args(_acct(pro) + ["status"]), json.loads(
        (pro / "credentials.json").read_text()))
    rows = capsys.readouterr().out
    assert "policy auto (1 file auto)" in rows
    assert cli.main(_acct(pro) + ["debloat", "manual", "AGENTS.md"]) == 0
    assert P.load(home)["auto_files"] == [] and P.load(home)["mode"] == "propose"
    LC.status_rows(cli.build_parser().parse_args(_acct(pro) + ["status"]), {})
    assert "policy propose" in capsys.readouterr().out


# ----------------------------------------------------------------------
# 33. sibyl save
# ----------------------------------------------------------------------

def test_33_save_fact_bare_and_capsule(proj, sm, capsys):
    assert cli.main(_acct(sm) + ["save", "--fact", "The deploy key lives in the vault"]) == 0
    out = capsys.readouterr().out
    assert "Saved fact/the-deploy-key-lives-in-the-vault." in out
    assert cli.main(_acct(sm) + ["memory", "recall", "fact", "the-deploy-key-lives-in-the-vault"]) == 0
    assert "The deploy key lives in the vault" in capsys.readouterr().out
    n = len(_client(sm).list_entities())
    assert cli.main(_acct(sm) + ["save"]) == 0
    bare = capsys.readouterr().out
    assert "/sibyl-save" in bare and "save` prompt" in bare and "SAVE.md" in bare
    assert cli.main(_acct(sm) + ["save", "--capsule"]) == 0
    assert "Automatic save is on Pro. Nothing was written." in capsys.readouterr().out
    c = _client(sm)
    assert len(c.list_entities()) == n and c.read_events(limit=5) == []


def test_33_save_fact_without_an_account_writes_nothing(proj, tmp_path, capsys):
    cred = tmp_path / "none" / "credentials.json"
    rc = cli.main(["--credentials", str(cred), "--db", str(tmp_path / "none" / "m.db"),
                   "save", "--fact", "x"])
    assert rc == 1 and "Nothing was written" in capsys.readouterr().out


# ----------------------------------------------------------------------
# 34-35. skills and reminders
# ----------------------------------------------------------------------

def test_34_skills_install_status_reset_and_never_overwrite(proj, home, capsys, monkeypatch):
    assert cli.main(["skills", "status"]) == 0
    assert "installed        no" in capsys.readouterr().out
    assert cli.main(["skills", "install"]) == 0
    capsys.readouterr()
    p = home / ".sibyl-memory" / "skills" / "SAVE.md"
    p.write_text("my own save procedure")
    real = SK.shipped_bytes
    monkeypatch.setattr(SK, "shipped_bytes", lambda n: real(n) + b"\nnew\n" if n == "SAVE.md" else real(n))
    monkeypatch.setattr(SK, "version", lambda: "1.2.0")
    assert cli.main(["skills", "install"]) == 0
    out = capsys.readouterr().out
    assert p.read_text() == "my own save procedure"
    assert "SAVE.md was edited by you; the new version is beside it as SAVE.md.upstream-1.2.0" in out
    assert cli.main(["skills", "status"]) == 0
    st = capsys.readouterr().out
    assert "edited by you" in st and "SAVE.md.upstream-1.2.0" in st
    assert cli.main(["skills", "reset", "SAVE.md", "--yes"]) == 0
    assert p.read_bytes().endswith(b"\nnew\n")


def test_35_reminders_off_on_and_status(proj, sm, capsys):
    assert cli.main(["reminders", "off"]) == 0
    assert "reminders        off" in capsys.readouterr().out
    LC.status_rows(cli.build_parser().parse_args(_acct(sm) + ["status"]), {})
    assert "reminders        off" in capsys.readouterr().out
    assert ST.LifecycleState().load()["reminders"] is False
    assert cli.main(["reminders", "on"]) == 0
    LC.status_rows(cli.build_parser().parse_args(_acct(sm) + ["status"]), {})
    assert "reminders        on" in capsys.readouterr().out


def test_status_shows_the_lifecycle_eyebrow(proj, sm, capsys, monkeypatch):
    monkeypatch.setattr(cli, "http_request", lambda *a, **k: (_ for _ in ()).throw(cli.HttpError(0, {}, "x")))
    from sibyl_memory_client import _entitlement as E
    monkeypatch.setattr(E, "http_refresh", lambda a, t: E.RefreshOutcome("transient", code="offline"))
    cli.main(_acct(sm) + ["status"])
    out = capsys.readouterr().out
    assert "L I F E C Y C L E" in out or "lifecycle" in out.lower()
    assert "manual (automatic on Pro)" in out


# ----------------------------------------------------------------------
# 36. init / setup install points
# ----------------------------------------------------------------------

def test_36_init_installs_the_skill_files_after_the_entitlement(tmp_path, home, test_signer, capsys,
                                                                  monkeypatch):
    acct, bearer = "11111111-2222-4333-8444-555555555555", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    class FakeServer:
        def __init__(self):
            self.routes = {}

        def on(self, method, prefix, resp):
            self.routes[(method, prefix)] = resp

        def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
            for (m, prefix), resp in self.routes.items():
                if m == method and path.startswith(prefix):
                    return json.loads(json.dumps(resp))
            raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")

    def bound_check(ent):
        return {"bound": True, "entitlement": ent, "credentials": {
            "account_id": acct, "tenant_id": acct, "tier": "free", "email": "u@example.com",
            "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "bearer_token": bearer}}

    def mint(signer):
        return signer.mint(account_id=acct, session_token=bearer)
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cli, "invalidate_tier_cache", lambda *a, **k: None)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
    s.on("POST", "/api/plugin/session-init", {"pairing_ttl_seconds": 300})
    s.on("GET", "/api/plugin/check", bound_check(mint(test_signer)))
    shutil.rmtree(home / ".sibyl-memory" / "skills", ignore_errors=True)
    cred = tmp_path / "c" / "credentials.json"
    assert cli.main(["--credentials", str(cred), "--db", str(tmp_path / "c" / "m.db"),
                     "init", "--force", "--no-email"]) == 0
    out = capsys.readouterr().out
    assert (cred.parent / "entitlement.json").exists()
    assert (home / ".sibyl-memory" / "skills" / "SAVE.md").read_bytes() == SK.shipped_bytes("SAVE.md")
    assert "Save + debloat" in out and "procedures v1.1.0 ready" in out


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def test_36_setup_dry_run_lists_shims_and_hooks_and_writes_nothing(proj, home, capsys, monkeypatch):
    monkeypatch.setattr(SU.ClaudeCodeWirer, "_mcp_binary_found", lambda self: True)
    (home / ".claude").mkdir()
    rc = cli.main(["setup", "claude-code", "--dry-run", "--yes"])
    out = capsys.readouterr().out
    assert rc in (0, 2)
    assert out.count("would write shim") == 2 and out.count("would add hook") == 5
    assert "skills/sibyl-save/SKILL.md" in out and "skills/sibyl-debloat/SKILL.md" in out
    assert not _settings(home).exists()
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".sibyl-memory" / "skills").exists()


def test_36_setup_twice_is_byte_identical(proj, home, monkeypatch):
    monkeypatch.setattr(SU.ClaudeCodeWirer, "_mcp_binary_found", lambda self: True)
    monkeypatch.setattr(SU.CodexWirer, "_mcp_binary_found", lambda self: True)
    monkeypatch.setattr(SU, "_verify_mcp_starts", lambda b: (True, "ok"))
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").write_text("# my global rules\nbe kind\n")
    cli.main(["setup", "claude-code", "--yes"])
    cli.main(["setup", "codex", "--yes"])
    first = (_settings(home).read_bytes(), (home / ".codex" / "AGENTS.md").read_bytes())
    cli.main(["setup", "claude-code", "--yes"])
    cli.main(["setup", "codex", "--yes"])
    second = (_settings(home).read_bytes(), (home / ".codex" / "AGENTS.md").read_bytes())
    assert first == second
    assert SU.count_our_hooks(json.loads(first[0])) == 5


# ----------------------------------------------------------------------
# 37-38. Wirers
# ----------------------------------------------------------------------

def test_37_hooks_merge_into_existing_settings_and_replace_only_ours(tmp_path, monkeypatch):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    user = {"permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "notify-send done"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}]},
            "env": {"X": "1"}}
    p.write_text(json.dumps(user))
    monkeypatch.setattr(SU, "sibyl_binary", lambda: "/opt/venv with space/bin/sibyl")
    w = SU.ClaudeCodeWirer(settings_path=p)
    w.wire_hooks()
    cfg = json.loads(p.read_text())
    assert cfg["permissions"] == user["permissions"] and cfg["env"] == user["env"]
    assert cfg["hooks"]["PreToolUse"] == user["hooks"]["PreToolUse"]
    assert cfg["hooks"]["Stop"][0] == user["hooks"]["Stop"][0]
    ours = cfg["hooks"]["Stop"][1]["hooks"][0]
    q = "'/opt/venv with space/bin/sibyl'"
    assert ours == {"type": "command", "command": f"[ -x {q} ] && exec {q} hook stop; exit 0", "timeout": 5}
    assert SU.count_our_hooks(cfg) == 5
    for ev, arg in SU.HOOK_EVENTS:
        cmd = cfg["hooks"][ev][-1]["hooks"][0]["command"]
        assert cmd == f"[ -x {q} ] && exec {q} hook {arg}; exit 0"
        assert "$" not in cmd and "`" not in cmd
    # A re-run with a new binary path replaces only our entries.
    monkeypatch.setattr(SU, "sibyl_binary", lambda: "/new/bin/sibyl")
    w.wire_hooks()
    cfg2 = json.loads(p.read_text())
    assert SU.count_our_hooks(cfg2) == 5
    assert cfg2["hooks"]["Stop"][0] == user["hooks"]["Stop"][0]
    assert all("/opt/venv" not in json.dumps(v) for v in cfg2["hooks"].values())
    # Uninstall removes only ours.
    w.unwire_hooks()
    cfg3 = json.loads(p.read_text())
    assert cfg3["hooks"] == user["hooks"] and cfg3["permissions"] == user["permissions"]


def test_37_corrupt_settings_are_refused_as_today(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text("{not json")
    lines = SU.ClaudeCodeWirer(settings_path=p).wire_hooks()
    assert lines[0].startswith("hooks not written:") and p.read_text() == "{not json"
    p.write_text(json.dumps({"hooks": ["weird"]}))
    assert SU.ClaudeCodeWirer(settings_path=p).wire_hooks()[0].startswith("hooks not written:")


def test_37_the_absolute_sibyl_path_is_used():
    b = SU.sibyl_binary()
    assert os.path.isabs(b) and b.endswith("sibyl")


def test_38_codex_block_insert_replace_remove_and_prompts_fallback(tmp_path, home):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    ag = cfg.parent / "AGENTS.md"
    ag.write_text("# mine\nkeep this\n")
    w = SU.CodexWirer(config_path=cfg)
    w.wire_block()
    t1 = ag.read_text()
    assert t1.startswith("# mine\nkeep this\n") and t1.count(SU.CODEX_BLOCK_BEGIN) == 1
    ag.write_text(t1 + "\nmore of mine\n")
    w.wire_block()
    t2 = ag.read_text()
    assert t2.count(SU.CODEX_BLOCK_BEGIN) == 1 and t2.endswith("\nmore of mine\n")
    old = t2.replace("Sibyl Memory is wired", "OLD TEXT")
    ag.write_text(old)
    w.wire_block()
    assert "OLD TEXT" not in ag.read_text() and ag.read_text().count(SU.CODEX_BLOCK_BEGIN) == 1
    w.unwire_block()
    assert ag.read_text() == "# mine\nkeep this\n\nmore of mine\n"
    # Verified on Codex CLI 0.135.0: no custom-prompt loader, so no prompt
    # files; skills live in $CODEX_HOME/skills (SD-9).
    w.wire_lifecycle()
    assert not (cfg.parent / "prompts").exists()
    assert (cfg.parent / "skills" / "sibyl-save" / "SKILL.md").is_file()


def test_hermes_wirer_writes_skill_shims_in_the_documented_dir(tmp_path, home):
    hh = tmp_path / "hermes"
    w = SU.HermesWirer(hermes_home=hh)
    lines = w.wire_lifecycle()
    for shim in ("sibyl-save", "sibyl-debloat"):
        p = hh / "skills" / shim / "SKILL.md"
        fm = SK.frontmatter_fields(p.read_text())
        assert fm["name"] == shim
    assert any("shim" in ln for ln in lines)


# ----------------------------------------------------------------------
# 39-43. Hooks
# ----------------------------------------------------------------------

def test_39_stop_hook_pro_continues_once_and_never_loops(home, pro, tmp_path):
    # R1: the payload carries a transcript and the session has had two prompts
    # (a `claude -p` run is never continued); the first request is "save 1".
    tid = _tenant(pro)
    tr = tmp_path / "t1.jsonl"
    tr.write_text("{}\n")
    stop = {"session_id": "s1", "stop_hook_active": False, "transcript_path": str(tr)}
    _hook("session-start", {"session_id": "s1", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}, home)
    _hook("prompt", {"session_id": "s1"}, home)
    _hook("prompt", {"session_id": "s1"}, home)
    assert _hook("stop", stop, home) == ""   # 0 writes
    _bump_writes(home, tid, 6)
    out = _hook("stop", stop, home)
    assert json.loads(out) == {"decision": "block", "reason": H.STOP_REASON}
    assert "(save 1 of this session)" in H.STOP_REASON
    sess = ST.LifecycleState(home).load()["sessions"][ST.session_key("s1")]
    assert sess["nudged"] is True and len(sess["nudges"]) == 1
    assert _hook("stop", dict(stop, stop_hook_active=True), home) == ""
    assert _hook("stop", stop, home) == ""


def test_39_stop_hook_free_tier_and_malformed_input_print_nothing(home, sm, tmp_path):
    _hook("session-start", {"session_id": "f1", "cwd": str(tmp_path)}, home)
    _bump_writes(home, _tenant(sm), 50)
    assert _hook("stop", {"session_id": "f1", "stop_hook_active": False}, home) == ""
    assert _hook("stop", "{not json", home) == ""
    assert _hook("stop", "", home) == ""
    log = (sm / "lifecycle.log").read_text()
    assert "hook stop: malformed input" in log


def test_adv_stop_nudged_at_most_once_even_across_a_rewritten_lifecycle_json(home, pro, tmp_path):
    tid = _tenant(pro)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"user","message":"hi"}\n')
    _hook("session-start", {"session_id": "s2", "cwd": str(tmp_path)}, home)
    _hook("prompt", {"session_id": "s2"}, home)
    _hook("prompt", {"session_id": "s2"}, home)
    _bump_writes(home, tid, 9)
    out = _hook("stop", {"session_id": "s2", "stop_hook_active": False,
                         "transcript_path": str(transcript)}, home)
    assert out
    # Claude Code records the continuation reason in the transcript.
    transcript.write_text(transcript.read_text() + json.dumps({"type": "user", "content": H.STOP_REASON}) + "\n")
    # Someone rewrites lifecycle.json (the nudged flag is gone).
    (pro / "lifecycle.json").write_text(json.dumps({"tenants": {tid: {"writes_total": 99}}}))
    assert _hook("stop", {"session_id": "s2", "stop_hook_active": False,
                          "transcript_path": str(transcript)}, home) == ""


def test_39_every_hook_returns_within_5_seconds_on_2000_surface_files(home, pro, tmp_path):
    proj = tmp_path / "big"
    rules = proj / ".cursor" / "rules"
    rules.mkdir(parents=True)
    for i in range(2000):
        (rules / f"r{i}.mdc").write_text("x" * 10)
    (proj / ".debloat.json").write_text(json.dumps({"surface": [f".cursor/rules/r{i}.mdc" for i in range(2000)]
                                                   + ["AGENTS.md"]}))
    _write(proj / "AGENTS.md", CURRENT + HIST)
    for ev in ("session-start", "prompt", "pre-compact", "stop", "session-end"):
        t0 = time.monotonic()
        _hook(ev, {"session_id": "perf", "cwd": str(proj), "stop_hook_active": False}, home)
        assert time.monotonic() - t0 < 5, ev


def test_40_shim_files_parse_and_point_at_the_canonical_path(tmp_path, home):
    w = SU.ClaudeCodeWirer(settings_path=tmp_path / ".claude" / "settings.json")
    SU.install_shims("claude-code", w.skill_shim_targets())
    for name, shim in SK.SHIM_NAMES.items():
        t = (tmp_path / ".claude" / "skills" / shim / "SKILL.md").read_text()
        assert t.startswith("---\n")
        fm = SK.frontmatter_fields(t)
        assert fm["name"] == shim and "Triggers" in fm["description"] or "Use when" in fm["description"]
        assert f"Read `~/.sibyl-memory/skills/{name}` and follow it exactly" in t


def test_41_session_start_free_fyi_and_unsaved_lines_throttled(home, sm, tmp_path):
    proj = tmp_path / "p41"
    _write(proj / "AGENTS.md", "a" * 40000)
    tid = _tenant(sm)
    now = time.time()
    # A previous session that wrote 31 times and ended unsaved.
    def _prev(st):
        st["sessions"]["old"] = {"tenant": tid, "harness": "claude-code", "started_at": now - 7200,
                                 "ended_at": now - 3600, "writes": 31, "saved": False}
    ST.LifecycleState(home).update(_prev)
    out = _hook("session-start", {"session_id": "n1", "cwd": str(proj)}, home, now=now)
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    lines = ctx["additionalContext"].splitlines()
    assert lines[0].startswith("Sibyl Memory: your last session (Claude Code, 1 h, 31 writes) was not saved.")
    assert lines[1].startswith("context: ~10.0k tok always-load (watch) | heavy: AGENTS.md 10000t. "
                               "Run /sibyl-debloat to trim.")
    assert _hook("session-start", {"session_id": "n2", "cwd": str(proj)}, home, now=now + 60) == ""


def test_41_session_start_pro_plans_under_propose_and_applies_only_a_named_file(home, pro, tmp_path):
    proj = tmp_path / "p41b"
    f = _write(proj / "AGENTS.md", CURRENT + HIST)
    g = _write(proj / "CLAUDE.md", CURRENT + HIST)
    before = f.read_bytes()
    _warm_tier_cache(pro)
    out = json.loads(_hook("session-start", {"session_id": "p1", "cwd": str(proj)}, home))
    line = out["hookSpecificOutput"]["additionalContext"]
    assert "would move to memory" in line and f.read_bytes() == before
    P.set_auto(P.auth_id(f, home), True, home)
    ST.LifecycleState(home).update(lambda st: st["debloat"].clear())
    out2 = json.loads(_hook("session-start", {"session_id": "p2", "cwd": str(proj)}, home))
    line2 = out2["hookSpecificOutput"]["additionalContext"]
    assert re.search(r"context trim: AGENTS\.md \d+ -> \d+ tok, history stored to memory as debloat/AGENTS\.md/", line2)
    assert f.read_bytes() != before and g.read_text() == CURRENT + HIST


def test_42_prompt_hook_free_save_due_once_per_6h_and_pro_silent(home, sm, test_signer, tmp_path):
    tid = _tenant(sm)
    now = time.time()
    _hook("session-start", {"session_id": "q1", "cwd": str(tmp_path)}, home, now=now)
    _bump_writes(home, tid, 31)
    out = json.loads(_hook("prompt", {"session_id": "q1", "prompt": "hi"}, home, now=now))
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert out["hookSpecificOutput"]["additionalContext"].startswith("Sibyl Memory: 31 writes since the last save.")
    assert _hook("prompt", {"session_id": "q1"}, home, now=now + 3600) == ""
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="pro"))
    assert _hook("prompt", {"session_id": "q1"}, home, now=now + 7 * 3600) == ""


def test_43_session_end_and_pre_compact_capsules_on_pro_only(home, sm, test_signer, tmp_path):
    tid = _tenant(sm)
    _hook("session-start", {"session_id": "e1", "cwd": str(tmp_path)}, home)
    _hook("pre-compact", {"session_id": "e1", "trigger": "auto"}, home)
    _hook("session-end", {"session_id": "e1", "reason": "other"}, home)
    assert _client(sm).read_events(limit=10) == []          # free: no capsule
    assert ST.LifecycleState(home).load()["sessions"][ST.session_key("e1")]["compaction_seen"] is True
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="pro"))
    _warm_tier_cache(sm)
    _hook("session-start", {"session_id": "e2", "cwd": str(tmp_path / "work-dir")}, home)
    _bump_writes(home, tid, 7)
    _hook("pre-compact", {"session_id": "e2", "trigger": "auto"}, home)
    evs = _client(sm).read_events(limit=10)
    assert len(evs) == 1 and evs[0]["acted"]["kind"] == "session_capsule"
    # The save marker from the skill makes the session saved; the end writes a
    # second capsule (the save came after the first one) that says so.
    agent = _client(sm)
    agent.enable_lifecycle_counting(home)      # as the MCP server does (SD-9: opt-in)
    agent.set_state("sibyl_save:last_save", {"ts": "now"})
    ST.flush_all()
    _hook("session-end", {"session_id": "e2", "reason": "other", "cwd": str(tmp_path / "work-dir")}, home)
    evs = _client(sm).read_events(limit=10)
    caps = [e["acted"]["body"] for e in evs if e["acted"]["kind"] == "session_capsule"]
    assert len(caps) == 2
    assert caps[0]["saved_by_skill"] is True and caps[0]["cwd_name"] == "work-dir"
    assert caps[0]["memory_writes"] == 8     # 7 + the save marker write itself
    sess = ST.LifecycleState(home).load()["sessions"][ST.session_key("e2")]
    assert sess["saved"] is True and sess["ended_at"]


# ----------------------------------------------------------------------
# 44. The installed Claude Code's hook contract
# ----------------------------------------------------------------------

def _claude_binary() -> Path | None:
    w = shutil.which("claude")
    if not w:
        return None
    p = Path(os.path.realpath(w))
    return p if p.is_file() and p.stat().st_size > 1_000_000 else None


def test_44_installed_claude_code_accepts_our_hook_output_shapes():
    """Reads the installed binary's hook schemas (no session is started, no
    hook is run): Stop's `decision` enum is approve|block with a `reason`
    string, SessionStart and UserPromptSubmit take hookSpecificOutput.
    additionalContext, the Stop input carries stop_hook_active, and settings
    hook entries are {type: "command", command, timeout}."""
    b = _claude_binary()
    if b is None:
        pytest.skip("claude not on PATH")
    try:
        version = subprocess.run([str(b), "--version"], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
    except Exception:
        version = b.name
    with open(b, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        checks = {
            "decision enum": rb'decision:[A-Za-z_$]{1,4}\(\["approve","block"\]\)',
            "reason string": rb'reason:[A-Za-z_$]{1,4}\(\)\.describe\("Explanation for the decision"\)',
            "SessionStart additionalContext": rb'hookEventName:[A-Za-z_$]{1,4}\("SessionStart"\),additionalContext:',
            "UserPromptSubmit additionalContext": rb'hookEventName:[A-Za-z_$]{1,4}\("UserPromptSubmit"\),additionalContext:',
            "Stop input stop_hook_active": rb'hook_event_name:[A-Za-z_$]{1,4}\("Stop"\),stop_hook_active:',
            "command hook timeout seconds": rb'timeout:[A-Za-z_$]{1,4}\(\)\.positive\(\)\.optional\(\)\.describe\("Timeout in seconds for this specific command"\)',
        }
        missing = [name for name, rx in checks.items() if not re.search(rx, mm)]
    assert not missing, f"Claude Code {version}: hook contract changed, missing {missing}"


# ----------------------------------------------------------------------
# 45. Source guard: no tokens, no file bodies in output or logs
# ----------------------------------------------------------------------

def test_45_no_token_and_no_file_body_in_any_output_or_log(home, pro, tmp_path, capsys):
    proj = tmp_path / "p45"
    body = CURRENT + "## History\n" + "".join(f"- {SENTINEL} secret history line {i}\n" for i in range(12))
    f = _write(proj / "AGENTS.md", body)
    P.set_auto(P.auth_id(f, home), True, home)
    _warm_tier_cache(pro)
    outs = [_hook("session-start", {"session_id": "z", "cwd": str(proj)}, home)]
    _bump_writes(home, _tenant(pro), 8)
    outs.append(_hook("stop", {"session_id": "z", "stop_hook_active": False}, home))
    outs.append(_hook("pre-compact", {"session_id": "z"}, home))
    outs.append(_hook("session-end", {"session_id": "z"}, home))
    os.chdir(proj)
    cli.main(_acct(pro) + ["debloat", "plan"])
    cli.main(_acct(pro) + ["debloat", "--json"])
    cli.main(_acct(pro) + ["status"])
    outs.append(capsys.readouterr().out)
    blob = "\n".join(outs)
    tok = json.loads((pro / "entitlement.json").read_text())["token"]
    creds = json.loads((pro / "credentials.json").read_text())
    assert SENTINEL not in blob and tok not in blob and creds["session_token"] not in blob
    log = (pro / "lifecycle.log").read_text().splitlines()
    assert log and all(len(ln) <= 330 for ln in log)
    assert not any(SENTINEL in ln or tok in ln for ln in log)
    st = (pro / "lifecycle.json").read_text()
    assert SENTINEL not in st and tok not in st
    for plan in (pro / "debloat" / "plans").glob("*.json"):
        assert SENTINEL not in plan.read_text()
