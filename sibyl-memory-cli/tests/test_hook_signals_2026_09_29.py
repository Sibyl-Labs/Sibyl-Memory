"""R2 skill signals from the Claude Code Stop hook (spec plugin-paid-tier-r2
1.2, 1.6): the incremental transcript slice.

* canaries in tool_use input VALUES and KEY NAMES never reach signals.json,
  lifecycle.json, lifecycle.log or the hook's stdout; shapes are 12 hex;
* incremental: a second Stop reads only the appended bytes; 4 MiB per call on
  a 64 MB transcript; the offset resets on a changed or truncated transcript;
* an unreadable transcript: the hook still exits 0 and prints its existing
  output; the `claude -p` heuristic still skips;
* free tier and a locked device: nothing recorded, no offset written;
  recording off: nothing read, and turning it on never reads the time it was
  off back;
* the slice's wall time is measured (release notes).

On b591aa1 these fail: the Stop hook recorded nothing (no signals.json, no
``transcript_offset``) and ``sibyl_memory_client.lifecycle.signals`` did not
exist (ImportError).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_cli import hooks as H
from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

CANARY_VALUE = "CANARY-VALUE-7a1f"
CANARY_KEY = "canary_key_9b2c"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    monkeypatch.setenv("NO_COLOR", "1")
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro", "cap_bytes": None})

    def offline(*a, **k):
        raise cli.HttpError(0, {}, "offline")
    monkeypatch.setattr(cli, "http_request", offline)


@pytest.fixture
def home():
    return Path.home()


@pytest.fixture
def sm(home):
    return home / ".sibyl-memory"


@pytest.fixture
def pro(sm, test_signer):
    write_entitlement(sm, test_signer.mint(tier="pro", cap=None, src="pro"))
    return sm


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _tool_line(name: str, ts: float, *, sidechain: bool = False) -> str:
    return json.dumps({
        "type": "assistant", "timestamp": _iso(ts), "isSidechain": sidechain, "sessionId": "s",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"I will look at {CANARY_VALUE}"},
            {"type": "tool_use", "id": "toolu_01", "name": name,
             "input": {"file_path": f"/home/x/{CANARY_VALUE}.py", CANARY_KEY: {"deep": CANARY_VALUE}}}]}})


def _result_line(ts: float) -> str:
    return json.dumps({"type": "user", "timestamp": _iso(ts), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": f"contents {CANARY_VALUE}"}]}})


def _prompt_line(ts: float) -> str:
    return json.dumps({"type": "user", "timestamp": _iso(ts),
                       "message": {"role": "user", "content": f"please fix {CANARY_VALUE} \"tool_use\""}})


def _turn(tr: Path, names, t0: float) -> float:
    lines = [_prompt_line(t0)]
    for i, n in enumerate(names):
        lines += [_tool_line(n, t0 + 10 * i + 1), _result_line(t0 + 10 * i + 2)]
    with open(tr, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return t0 + 10 * len(names) + 5


def _stop(home: Path, sid: str, tr: Path | str, now: float) -> str:
    return H.run_hook("stop", json.dumps({"session_id": sid, "stop_hook_active": False,
                                          "transcript_path": str(tr)}), home=home, now=now)


def _start(home: Path, sid: str, cwd: Path, now: float) -> None:
    H.run_hook("session-start", json.dumps({"session_id": sid, "cwd": str(cwd)}), home=home, now=now)
    H.run_hook("prompt", json.dumps({"session_id": sid}), home=home, now=now + 1)


def _session(home: Path, sid: str) -> dict:
    return ST.LifecycleState(home).load()["sessions"].get(ST.session_key(sid)) or {}


def _ngram_count(home: Path, names) -> int:
    sig = SG.load(home)
    return sum(e["count_total"] for e in sig["ngrams"].values() if e["tools"] == list(names))


def _no_canaries(sm: Path, *extra: str) -> None:
    for p in sm.rglob("*"):
        if p.is_file() and p.suffix != ".db" and "-wal" not in p.name and "-shm" not in p.name:
            data = p.read_bytes()
            for c in (CANARY_VALUE, CANARY_KEY, "file_path", "/home/x"):
                assert c.encode() not in data, (p.name, c)
    for text in extra:
        for c in (CANARY_VALUE, CANARY_KEY):
            assert c not in text


# ----------------------------------------------------------------------
# Privacy and the incremental slice
# ----------------------------------------------------------------------

def test_three_stops_record_names_and_shapes_only(home, pro, tmp_path):
    tr = tmp_path / "t.jsonl"
    tr.write_text("")
    t = time.time() - 3600
    _start(home, "c1", tmp_path, t)
    outs = []
    for i in range(3):
        t = _turn(tr, ["Read", "Edit", "Bash"], t + 60)
        outs.append(_stop(home, "c1", tr, t))
    assert _ngram_count(home, ["Read", "Edit", "Bash"]) == 3
    sig = json.loads(SG.path_of(home).read_bytes())
    assert all(len(s) == 12 for e in sig["ngrams"].values() for s in e["shapes"])
    _no_canaries(pro, *outs)
    s = _session(home, "c1")
    assert s["transcript_offset"] == tr.stat().st_size and len(s["transcript_ref"]) == 16
    assert b"t.jsonl" not in (pro / "lifecycle.json").read_bytes()      # never the transcript path


def test_a_second_stop_reads_only_the_appended_bytes(home, pro, tmp_path, monkeypatch):
    tr = tmp_path / "t.jsonl"
    tr.write_text("")
    t = time.time() - 3600
    _start(home, "c2", tmp_path, t)
    t = _turn(tr, ["Grep", "Read", "Edit"], t + 60)
    _stop(home, "c2", tr, t)
    first = _session(home, "c2")["transcript_offset"]
    assert first == tr.stat().st_size
    seen = []
    real = SG.read_slice

    def spy(path, offset, **kw):
        seen.append(offset)
        return real(path, offset, **kw)
    monkeypatch.setattr(SG, "read_slice", spy)
    t = _turn(tr, ["Grep", "Read", "Edit"], t + 60)
    _stop(home, "c2", tr, t)
    assert seen == [first]
    assert _ngram_count(home, ["Grep", "Read", "Edit"]) == 2      # not re-counted
    _stop(home, "c2", tr, t + 5)                                   # nothing new: nothing counted
    assert _ngram_count(home, ["Grep", "Read", "Edit"]) == 2


def test_four_mib_per_stop_on_a_64_mb_transcript_and_the_measured_time(home, pro, tmp_path):
    tr = tmp_path / "big.jsonl"
    t0 = time.time() - 3600
    block = []
    for i in range(0, 60):
        block.append(_tool_line(("Read", "Edit", "Bash")[i % 3], t0 + i))
        block.append(json.dumps({"type": "user", "timestamp": _iso(t0 + i), "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 2000}]}}))
    chunk = ("\n".join(block) + "\n").encode()
    with open(tr, "wb") as fh:
        while fh.tell() < 64 * 1024 * 1024:
            fh.write(chunk)
    size = tr.stat().st_size
    _start(home, "c3", tmp_path, t0)
    t = time.perf_counter()
    _stop(home, "c3", tr, time.time())
    elapsed = time.perf_counter() - t
    off = _session(home, "c3")["transcript_offset"]
    assert 0 < off <= SG.SLICE_MAX_BYTES and off < size
    assert tr.read_bytes()[off - 1:off] == b"\n"                 # a line boundary
    assert elapsed < 2.0, elapsed                                  # measured: see the release notes
    print(f"\nSLICE_TIMING stop with a 4 MiB slice: {elapsed:.3f} s")
    _stop(home, "c3", tr, time.time())
    off2 = _session(home, "c3")["transcript_offset"]
    assert off < off2 <= off + SG.SLICE_MAX_BYTES


def test_offset_resets_on_a_changed_or_truncated_transcript(home, pro, tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text("")
    b.write_text("")
    t = time.time() - 3600
    _start(home, "c4", tmp_path, t)
    t = _turn(a, ["Read", "Edit", "Bash", "Grep"], t + 60)
    _stop(home, "c4", a, t)
    off_a = _session(home, "c4")["transcript_offset"]
    t = _turn(b, ["Write", "Bash", "Read"], t + 60)
    _stop(home, "c4", b, t)                                        # another path: from 0
    s = _session(home, "c4")
    assert s["transcript_offset"] == b.stat().st_size
    assert s["transcript_ref"] == SG.transcript_ref(str(b))
    a.write_text("")                                               # truncated below the offset
    t = _turn(a, ["Glob", "Read", "Edit"], t + 60)
    assert a.stat().st_size < off_a
    _stop(home, "c4", a, t)
    assert _ngram_count(home, ["Glob", "Read", "Edit"]) == 1


def test_an_unreadable_transcript_keeps_the_hook_output(home, pro, tmp_path):
    tid = json.loads((pro / "credentials.json").read_text())
    tid = tid.get("tenant_id") or tid["account_id"]
    tr = tmp_path / "locked.jsonl"
    tr.write_text("{}\n")
    t = time.time() - 4 * 3600
    _start(home, "c5", tmp_path, t)
    H.run_hook("prompt", json.dumps({"session_id": "c5"}), home=home, now=t + 60)

    def _u(st):
        tt = ST.tenant(st, tid)
        tt["writes_total"] = int(tt.get("writes_total") or 0) + 1
        tt["writes_since_save"] = int(tt.get("writes_since_save") or 0) + 1
    ST.LifecycleState(home).update(_u)
    os.chmod(tr, 0)
    try:
        # a turn of the model, then idle turns: over an hour of active time
        now = t + 60
        out = ""
        for i in range(8):
            now += 25 * 60
            H.run_hook("prompt", json.dumps({"session_id": "c5"}), home=home, now=now - 20 * 60)
            out = _stop(home, "c5", tr, now)
            if out:
                break
        assert out and json.loads(out)["decision"] == "block"      # the R1 output is unchanged
        # a directory and a missing file too
        assert _stop(home, "c5", tmp_path, now + 1) == ""
        assert _stop(home, "c5", tmp_path / "missing.jsonl", now + 2) == ""
    finally:
        os.chmod(tr, 0o600)
    assert "Traceback" not in (pro / "lifecycle.log").read_text()


def test_scripted_runs_are_still_never_continued(home, pro, tmp_path):
    tid = json.loads((pro / "credentials.json").read_text())
    tid = tid.get("tenant_id") or tid["account_id"]
    tr = tmp_path / "p.jsonl"
    tr.write_text("")
    t = time.time() - 4 * 3600
    H.run_hook("session-start", json.dumps({"session_id": "p1", "cwd": str(tmp_path)}), home=home, now=t)
    H.run_hook("prompt", json.dumps({"session_id": "p1"}), home=home, now=t + 1)     # one prompt: -p

    def _u(st):
        tt = ST.tenant(st, tid)
        tt["writes_total"] = int(tt.get("writes_total") or 0) + 40
        tt["writes_since_save"] = int(tt.get("writes_since_save") or 0) + 40
    ST.LifecycleState(home).update(_u)
    for i in range(1, 5):
        _turn(tr, ["Read", "Edit", "Bash"], t + i * 1800 - 100)
        assert _stop(home, "p1", tr, t + i * 1800) == ""


# ----------------------------------------------------------------------
# Tier and switch
# ----------------------------------------------------------------------

def test_free_tier_records_nothing_and_writes_no_offset(home, sm, tmp_path):
    tr = tmp_path / "f.jsonl"
    tr.write_text("")
    t = time.time() - 3600
    _start(home, "f1", tmp_path, t)
    for i in range(3):
        t = _turn(tr, ["Read", "Edit", "Bash"], t + 60)
        assert _stop(home, "f1", tr, t) == ""
    assert not SG.path_of(home).exists()
    assert "transcript_offset" not in json.dumps(_session(home, "f1")) or \
        _session(home, "f1").get("transcript_offset") == 0
    assert _session(home, "f1").get("transcript_ref") is None
    assert cli.main(["--credentials", str(sm / "credentials.json"), "skills", "signals", "status"]) == 0


def test_locked_device_records_nothing(home, sm, test_signer, tmp_path):
    write_entitlement(sm, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 40 * 86400, tier="pro", cap=None, src="pro")))
    tr = tmp_path / "l.jsonl"
    tr.write_text("")
    t = time.time() - 3600
    _start(home, "l1", tmp_path, t)
    for i in range(3):
        t = _turn(tr, ["Read", "Edit", "Bash"], t + 60)
        _stop(home, "l1", tr, t)
    assert not SG.path_of(home).exists()
    assert _session(home, "l1").get("transcript_ref") is None


def test_off_reads_nothing_and_on_never_reads_the_off_time_back(home, pro, tmp_path, monkeypatch):
    tr = tmp_path / "o.jsonl"
    tr.write_text("")
    t = time.time() - 3600
    _start(home, "o1", tmp_path, t)
    SG.set_enabled(home, False)
    calls = []
    real = SG.read_slice
    monkeypatch.setattr(SG, "read_slice", lambda *a, **k: calls.append(1) or real(*a, **k))
    for i in range(3):
        t = _turn(tr, ["Read", "Edit", "Bash"], t + 60)
        _stop(home, "o1", tr, t)
    assert calls == [] and not SG.path_of(home).exists()
    SG.set_enabled(home, True)
    _stop(home, "o1", tr, t + 1)
    assert not SG.path_of(home).exists()                   # the off-time turns are not read back
    t = _turn(tr, ["Write", "Bash", "Read"], time.time() + 1)
    _stop(home, "o1", tr, t)
    assert _ngram_count(home, ["Write", "Bash", "Read"]) == 1
    assert _ngram_count(home, ["Read", "Edit", "Bash"]) == 0


def test_sidechain_and_text_mentions_are_not_tool_events(home, pro, tmp_path):
    tr = tmp_path / "s.jsonl"
    t = time.time() - 3600
    lines = [_prompt_line(t)] + [_tool_line(n, t + i, sidechain=True) for i, n in enumerate(["A", "B", "C"])]
    tr.write_text("\n".join(lines) + "\n")
    _start(home, "s1", tmp_path, t)
    _stop(home, "s1", tr, t + 10)
    assert not SG.path_of(home).exists()
    assert _session(home, "s1")["transcript_offset"] == tr.stat().st_size


def test_a_partial_last_line_waits_for_its_end(home, pro, tmp_path):
    tr = tmp_path / "w.jsonl"
    t = time.time() - 3600
    full = "\n".join(_tool_line(n, t + i) for i, n in enumerate(["Read", "Edit", "Bash"])) + "\n"
    tail = _tool_line("Grep", t + 5)
    tr.write_text(full + tail[:40])
    _start(home, "w1", tmp_path, t)
    _stop(home, "w1", tr, t + 10)
    assert _session(home, "w1")["transcript_offset"] == len(full.encode())
    with open(tr, "a") as fh:
        fh.write(tail[40:] + "\n")
    _stop(home, "w1", tr, t + 11)
    assert _ngram_count(home, ["Edit", "Bash", "Grep"]) == 1


def test_the_hook_process_exits_0_on_an_unreadable_transcript(home, pro, tmp_path):
    import subprocess
    import sys
    tr = tmp_path / "gone.jsonl"
    tr.write_text("{}\n")
    os.chmod(tr, 0)
    try:
        payload = json.dumps({"session_id": "x9", "stop_hook_active": False, "transcript_path": str(tr)})
        src = str(Path(H.__file__).resolve().parent.parent)
        env = dict(os.environ, HOME=str(home), PYTHONPATH=src)
        r = subprocess.run([sys.executable, "-c",
                            "import sys; from sibyl_memory_cli.hooks import main_hook; "
                            "sys.exit(main_hook('stop'))"],
                           input=payload.encode(), capture_output=True, env=env, timeout=30)
    finally:
        os.chmod(tr, 0o600)
    assert r.returncode == 0 and r.stderr == b""
