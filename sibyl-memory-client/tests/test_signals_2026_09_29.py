"""R2 skill signals, the recorder and its file (spec plugin-paid-tier-r2 1.1,
1.3, 1.6): what is captured and what never is, Pro only, off / on / clear, the
bounds and the file discipline, the source guard.

On b591aa1 every test here fails: ``sibyl_memory_client.lifecycle.signals``
does not exist (ImportError at collection), the client's write path records no
entity-write signal, and ``lifecycle.json`` keeps no ``skills.signals``,
``signals_since`` or session ``transcript_offset``.
"""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sibyl_memory_client import MemoryClient
from sibyl_memory_client.lifecycle import signals as SG
from sibyl_memory_client.lifecycle import state as ST
from sibyl_memory_client.testing import write_entitlement

CANARY_VALUE = "CANARY-VALUE-7a1f"
CANARY_KEY = "canary_key_9b2c"
CANARY_ENTITY = "Canary Person 3e5d"
SRC = Path(__file__).resolve().parent.parent / "src" / "sibyl_memory_client"


@pytest.fixture(autouse=True)
def _fresh_buffers(monkeypatch):
    monkeypatch.setenv("SIBYL_MEMORY_TELEMETRY", "0")
    yield
    with SG._registry_lock:
        for b in SG._BUFFERS.values():
            b.discard()
        SG._BUFFERS.clear()


def _pro_client(tmp_path: Path, entitle_as, name: str = "m.db") -> MemoryClient:
    entitle_as("pro")
    c = MemoryClient.local(tmp_path / name)
    c.enable_lifecycle_counting(Path.home())
    return c


def _sig_bytes(home: Path | None = None) -> bytes:
    p = SG.path_of(home or Path.home())
    return p.read_bytes() if p.exists() else b""


def _all_home_bytes() -> bytes:
    out = b""
    for p in (Path.home() / ".sibyl-memory").rglob("*"):
        if p.is_file() and p.suffix != ".db" and "-wal" not in p.name and "-shm" not in p.name:
            out += p.read_bytes()
    return out


# ----------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------

def test_shape_is_twelve_hex_of_sorted_top_level_key_names_only():
    a = SG.arg_shape({"b": 1, "a": {"nested": CANARY_VALUE}})
    b = SG.arg_shape({"a": "other value", "b": [1, 2, 3]})
    assert a == b and len(a) == 12 and all(c in "0123456789abcdef" for c in a)
    assert SG.arg_shape({"a": 1}) != a
    assert SG.arg_shape(None) == SG.arg_shape({}) == SG.arg_shape("a string")


def test_tool_names_are_cut_and_restricted():
    assert SG.safe_tool_name("mcp__sibyl-memory__memory_search") == "mcp__sibyl-memory__memory_search"
    assert SG.safe_tool_name("Read file/../etc") == "Read_file_.._etc"
    assert len(SG.safe_tool_name("x" * 500)) == 64
    assert SG.safe_tool_name("") == "_"


# ----------------------------------------------------------------------
# Privacy: canaries (spec 1.6)
# ----------------------------------------------------------------------

def test_tool_argument_values_and_key_names_never_reach_disk(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    home = Path.home()
    buf = SG.recorder(home)
    for _ in range(4):
        for tool in ("memory_remember", "memory_recall", "memory_record_event"):
            assert buf.note_tool(tool, {CANARY_KEY: CANARY_VALUE, "category": CANARY_VALUE},
                                 client=client)
    assert buf.flush()
    blob = _all_home_bytes()
    assert CANARY_VALUE.encode() not in blob and CANARY_KEY.encode() not in blob
    assert b'"category"' not in _sig_bytes()
    sig = json.loads(_sig_bytes())
    shapes = {s for e in sig["ngrams"].values() for s in e["shapes"]}
    assert shapes and all(len(s) == 12 for s in shapes)


def test_entity_names_are_hashed_never_stored(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    for i in range(3):
        client.set_entity("people", CANARY_ENTITY, {"note": f"{CANARY_VALUE} {i}"})
    client.archive_entity("people", CANARY_ENTITY)
    SG.flush_all()
    raw = _sig_bytes()
    assert raw
    assert CANARY_ENTITY.encode() not in _all_home_bytes() and b"people" not in raw
    assert CANARY_VALUE.encode() not in _all_home_bytes()
    sig = json.loads(raw)
    h = SG.entity_hash(client.get_tenant(), "people", CANARY_ENTITY)
    assert [r[0] for r in sig["entity_writes"][h]] == ["s", "s", "s", "a"]


def test_search_query_leaves_no_signal_but_the_tool_name(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    client.search(f"where is {CANARY_VALUE}")
    SG.recorder(Path.home()).note_tool("memory_search", {"query": CANARY_VALUE, "limit": 5}, client=client)
    SG.flush_all()
    assert CANARY_VALUE.encode() not in _all_home_bytes()
    assert b"where is" not in _all_home_bytes()


# ----------------------------------------------------------------------
# Pro only; off, on, clear
# ----------------------------------------------------------------------

def test_free_tier_records_nothing_and_writes_no_file(tmp_path):
    client = MemoryClient.local(tmp_path / "m.db")          # the default home is free
    client.enable_lifecycle_counting(Path.home())
    for i in range(5):
        client.set_entity("projects", f"p{i}", {"x": i})
    assert SG.recorder(Path.home()).note_tool("memory_remember", {"a": 1}, client=client) is False
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()
    assert SG.recorder(Path.home()).pending() == 0


def test_locked_device_records_nothing(tmp_path, test_signer, entitled_test_home):
    write_entitlement(entitled_test_home, test_signer.sign_payload(test_signer.payload(
        now=time.time() - 40 * 86400, tier="pro", cap=None, src="pro")))
    client = MemoryClient.local(tmp_path / "m.db")
    assert SG.recorder(Path.home()).note_tool("memory_list", {}, client=client) is False
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()


def test_sdk_clients_without_lifecycle_counting_record_nothing(tmp_path, entitle_as):
    entitle_as("pro")
    client = MemoryClient.local(tmp_path / "m.db")           # a library user: not opted in
    client.set_entity("projects", "acme", {"x": 1})
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()


def test_suppressed_writes_are_not_recorded(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    with ST.suppressed():
        client.set_entity("sessions", "capsule", {"x": 1})
        client.delete_entity("sessions", "capsule")
    SG.flush_all()
    assert not SG.path_of(Path.home()).exists()


def test_off_stops_capture_and_drops_what_was_buffered(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    home = Path.home()
    buf = SG.recorder(home)
    assert buf.note_tool("Read", {"a": 1}, client=client)
    SG.set_enabled(home, False)
    assert buf.note_tool("Edit", {"a": 1}, client=client) is False
    assert buf.flush() is False                    # the buffered event is not written while off
    assert not SG.path_of(home).exists()
    client.set_entity("projects", "acme", {"x": 1})
    SG.flush_all()
    assert not SG.path_of(home).exists()


def test_on_moves_the_floor_so_nothing_older_is_written(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    home = Path.home()
    buf = SG.recorder(home)
    now = time.time()
    buf.note_tool("Read", {}, client=client, now=now - 100)
    SG.set_enabled(home, False)
    SG.set_enabled(home, True, now=now)
    for t in ("Read", "Edit", "Bash"):
        buf.note_tool(t, {}, client=client, now=now + 1)
    buf.flush()
    sig = SG.load(home)
    assert [e["tools"] for e in sig["ngrams"].values()] == [["Read", "Edit", "Bash"]]


def test_clear_deletes_the_file_and_a_running_buffer_cannot_bring_it_back(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    home = Path.home()
    buf = SG.recorder(home)
    for t in ("Read", "Edit", "Bash", "Read", "Edit", "Bash"):
        buf.note_tool(t, {"p": 1}, client=client)
    buf.flush()
    assert SG.path_of(home).exists()
    buf.note_tool("Grep", {}, client=client, now=time.time() - 5)   # buffered before the clear
    assert SG.clear(home) is True
    assert not SG.path_of(home).exists()
    # another process's buffer (not the registry's) flushing an old event
    other = SG.SignalBuffer(home)
    other._tools.append(("abcdef01", "Read", SG.arg_shape({}), time.time() - 10))
    assert other.flush() is False
    assert not SG.path_of(home).exists()


# ----------------------------------------------------------------------
# The file: bounds and discipline
# ----------------------------------------------------------------------

def _ng(tools, shapes=None, *, count=1, last=None, first=None, sessions=None, proposed_at=None):
    shapes = shapes or [SG.arg_shape({})] * len(tools)
    last = time.time() if last is None else last
    e = {"n": len(tools), "tools": tools, "shapes": shapes, "count_total": count,
         "days": {SG._day(last): count}, "first": first or last, "last": last,
         "sessions": sessions or [["abcdef01", count, last]], "ts": [last]}
    if proposed_at is not None:
        e["proposed_at"] = proposed_at
    return SG.ngram_key(zip(tools, shapes)), e


def test_ngram_cap_evicts_lowest_count_then_oldest():
    now = time.time()
    sig = SG.empty()
    for i in range(210):
        k, e = _ng([f"T{i}", "Edit", "Bash"], count=1 + (i % 3), last=now - i)
        sig["ngrams"][k] = e
    SG.prune(sig, now)
    assert len(sig["ngrams"]) == SG.MAX_NGRAMS
    kept = {e["tools"][0] for e in sig["ngrams"].values()}
    # the ten evicted are count-1 entries, oldest first (i = 207, 204, ... 180)
    evicted = {f"T{i}" for i in range(210)} - kept
    assert evicted == {f"T{i}" for i in range(180, 210, 3)}


def test_pruning_14_and_30_days_and_seven_day_buckets():
    now = time.time()
    day = 86400
    sig = SG.empty()
    k1, e1 = _ng(["A", "B", "C"], count=2, last=now - 15 * day)       # low count, 15 days: dropped
    k2, e2 = _ng(["A", "B", "D"], count=5, last=now - 15 * day)       # frequent, 15 days: kept
    k3, e3 = _ng(["A", "B", "E"], count=50, last=now - 31 * day)      # anything past 30 days: dropped
    k4, e4 = _ng(["A", "B", "F"], count=9, last=now, proposed_at=now - 31 * day)
    e2["days"] = {SG._day(now - i * day): 1 for i in range(10)}
    for k, e in ((k1, e1), (k2, e2), (k3, e3), (k4, e4)):
        sig["ngrams"][k] = e
    SG.prune(sig, now)
    assert set(sig["ngrams"]) == {k2}
    assert len(sig["ngrams"][k2]["days"]) == SG.DAY_BUCKETS
    assert min(sig["ngrams"][k2]["days"]) == SG._day(now - 6 * day)


def test_entity_bounds_200_keys_32_writes_7_days():
    now = time.time()
    sig = SG.empty()
    writes = []
    for i in range(220):
        h = SG.entity_hash("t", "c", f"n{i}")
        writes.append((h, "s", now - 220 + i, "abcdef01"))
    h0 = SG.entity_hash("t", "c", "busy")
    writes += [(h0, "s", now - i, "abcdef01") for i in range(40)]
    old = SG.entity_hash("t", "c", "old")
    writes.append((old, "s", now - 8 * 86400, "abcdef01"))
    SG.apply_entity_writes(sig, writes)
    SG.prune(sig, now)
    assert len(sig["entity_writes"]) == SG.MAX_ENTITY_KEYS
    assert len(sig["entity_writes"][h0]) == SG.MAX_WRITES_PER_KEY
    assert old not in sig["entity_writes"]


def test_file_is_capped_at_64_kb_by_evicting_ngrams(tmp_path):
    home = tmp_path
    now = time.time()
    sig = SG.empty()
    for i in range(SG.MAX_NGRAMS):
        tools = [f"mcp__server{i:03d}__tool_{'x' * 30}", f"Edit{i}", f"Bash{i}", "Read", "Grep"]
        k, e = _ng(tools, count=3 + i % 5, last=now - i)
        e["ts"] = [now - j for j in range(SG.MAX_TS_KEPT)]
        e["sessions"] = [[f"{j:08x}", 1, now - j] for j in range(8)]
        sig["ngrams"][k] = e
    assert len(SG._serialise(sig)) > SG.MAX_BYTES
    assert SG.save_unlocked(home, sig, now=now)
    data = SG.path_of(home).read_bytes()
    assert len(data) <= SG.MAX_BYTES
    back = SG.load(home, now=now)
    assert 0 < len(back["ngrams"]) < SG.MAX_NGRAMS
    # the lowest counts went first
    assert min(e["count_total"] for e in back["ngrams"].values()) >= 3


def test_damaged_oversize_and_foreign_content_load_as_empty(tmp_path):
    home = tmp_path
    p = SG.path_of(home)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert SG.load(home) == SG.empty()
    p.write_bytes(b"{" + b" " * (SG.MAX_BYTES + 10) + b"}")
    assert SG.load(home) == SG.empty()
    # a planted entry that is not the documented shape does not survive
    k, e = _ng(["Read", "Edit", "Bash"])
    bad = dict(e, tools=["Read", "Edit", CANARY_VALUE + " spaces"])
    p.write_text(json.dumps({"ngrams": {k: bad, "zz": e},
                             "entity_writes": {"notahash": [["s", 1.0, "abcdef01"]],
                                               "0" * 16: [["s", 1.0, CANARY_VALUE]]},
                             "tails": {"abcdef01": {"t": [[CANARY_VALUE, "x", 1]], "at": 1.0}}}))
    got = SG.load_unlocked(home)
    assert got["ngrams"] == {} and got["entity_writes"] == {}
    assert CANARY_VALUE not in json.dumps(got)


def test_symlinked_file_is_refused_and_mode_is_0600(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    home = Path.home()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    d = ST.memory_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    SG.path_of(home).symlink_to(target)
    assert SG.load(home) == SG.empty()
    buf = SG.recorder(home)
    for t in ("Read", "Edit", "Bash"):
        buf.note_tool(t, {}, client=client)
    buf.flush()
    assert target.read_text() == "{}"               # never written through the link
    SG.path_of(home).unlink()
    for t in ("Read", "Edit", "Bash"):
        buf.note_tool(t, {}, client=client)
    assert buf.flush()
    mode = stat.S_IMODE(os.stat(SG.path_of(home)).st_mode)
    assert mode == 0o600


def _hammer(home: str, tag: str, n: int) -> None:
    for i in range(n):
        def _m(sig, life):
            SG.apply_tool_events(sig, tag, [("Read", SG.arg_shape({}), time.time()),
                                            (f"Tool{tag}", SG.arg_shape({"i": 1}), time.time()),
                                            ("Bash", SG.arg_shape({}), time.time())], time.time())
            return None, True
        SG.update(home, _m)


def test_concurrent_flush_from_two_processes_leaves_a_parseable_file(tmp_path):
    home = str(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    ps = [ctx.Process(target=_hammer, args=(home, tag, 40)) for tag in ("aaaaaaaa", "bbbbbbbb")]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
        assert p.exitcode == 0
    data = json.loads(SG.path_of(home).read_bytes())
    tails = data["tails"]
    assert set(tails) == {"aaaaaaaa", "bbbbbbbb"}
    total = sum(e["count_total"] for e in data["ngrams"].values() if e["n"] == 3
                and e["tools"][1].startswith("Tool") and e["tools"][0] == "Read")
    assert total == 80                              # no lost update under the lock


def test_mark_proposed_sets_proposed_at(tmp_path):
    home = tmp_path
    k, e = _ng(["Read", "Edit", "Bash"], count=4)
    h = SG.entity_hash("t", "c", "n")
    sig = SG.empty()
    sig["ngrams"][k] = e
    SG.apply_entity_writes(sig, [(h, "s", time.time(), "abcdef01")])
    SG.save_unlocked(home, sig)
    SG.mark_proposed(home, [k], now=1234.0 + time.time())
    back = SG.load_unlocked(home)
    assert back["ngrams"][k]["proposed_at"] > 0
    # R2F N2: no dead entity_proposed map; the entity's writes are untouched
    assert "entity_proposed" not in back
    assert h in back["entity_writes"]


def test_r2f_n2_an_old_file_with_entity_proposed_loads_and_the_key_is_dropped(tmp_path):
    home = tmp_path
    h = SG.entity_hash("t", "c", "n")
    sig = SG.empty()
    SG.apply_entity_writes(sig, [(h, "s", time.time(), "abcdef01")])
    SG.save_unlocked(home, sig)
    p = SG.path_of(home)
    raw = json.loads(p.read_bytes())
    raw["entity_proposed"] = {h: time.time()}          # written by a 0.10.0 pre-release build
    p.write_text(json.dumps(raw))
    back = SG.load_unlocked(home)
    assert "entity_proposed" not in back and h in back["entity_writes"]


def test_status_reports_counts_never_entries(tmp_path, entitle_as):
    client = _pro_client(tmp_path, entitle_as)
    buf = SG.recorder(Path.home())
    for t in ("Read", "Edit", "Bash", "Grep"):
        buf.note_tool(t, {CANARY_KEY: 1}, client=client)
    buf.flush()
    st = SG.status(Path.home())
    assert st["exists"] and st["ngrams"] == 3 and st["on"] is True
    assert set(st) == {"exists", "bytes", "ngrams", "entity_keys", "entity_writes", "oldest", "newest",
                       "on", "since"}


def test_lifecycle_state_keeps_the_new_keys_bounded():
    raw = {"skills": {"signals": "no", "signals_since": "x"},
           "sessions": {"s": {"transcript_offset": 42, "transcript_ref": "../../etc/passwd",
                              "transcript_mid": 1}}}
    st = ST._clean(raw)
    assert st["skills"]["signals"] is True and st["skills"]["signals_since"] is None
    s = st["sessions"]["s"]
    assert s["transcript_offset"] == 42 and s["transcript_ref"] is None and s["transcript_mid"] is False
    st = ST._clean({"skills": {"signals": False, "signals_since": 5.0},
                    "sessions": {"s": {"transcript_ref": "0123456789abcdef"}}})
    assert st["skills"]["signals"] is False and st["skills"]["signals_since"] == 5.0
    assert st["sessions"]["s"]["transcript_ref"] == "0123456789abcdef"


# ----------------------------------------------------------------------
# Source guard: nothing on a network path imports the signals module, and
# the module itself opens no connection (spec 1.3, 1.6)
# ----------------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            out.add(mod)
            out.update(f"{mod}.{a.name}" for a in node.names)
    return out


@pytest.mark.parametrize("module", ["_entitlement.py", "_capcheck.py", "_heartbeat.py",
                                    "_entitlement_keys.py", "lifecycle/entitled.py"])
def test_network_modules_never_import_signals(module):
    assert not any("signals" in m for m in _imports(SRC / module)), module


def test_signals_module_imports_no_network_library():
    imps = _imports(SRC / "lifecycle" / "signals.py")
    banned = ("socket", "http", "urllib", "requests", "ssl", "_heartbeat", "_capcheck", "_entitlement")
    assert not [m for m in imps if any(m == b or m.startswith(b + ".") or m.endswith("." + b)
                                       or f".{b}." in m for b in banned)], imps


def test_importing_the_client_and_its_network_paths_does_not_load_signals():
    code = ("import sys, sibyl_memory_client, sibyl_memory_client._heartbeat, "
            "sibyl_memory_client._capcheck, sibyl_memory_client._entitlement; "
            "from sibyl_memory_client import MemoryClient; "
            "print('sibyl_memory_client.lifecycle.signals' in sys.modules)")
    env = dict(os.environ, PYTHONPATH=str(SRC.parent))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == "False", out.stderr
