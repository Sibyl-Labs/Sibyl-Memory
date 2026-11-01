"""Skill signals on Pro (R2, spec plugin-paid-tier-r2 section 1): the
``tool_sequence`` and ``correction_loop`` evidence, recorded locally in
``~/.sibyl-memory/signals.json``.

What is recorded, and nothing else:

* tool events ``(tool_name, arg_shape, ts, session)``: the tool's name as the
  harness reports it (cut to 64 characters, ``[A-Za-z0-9_.:-]`` only), and
  ``arg_shape`` = the first 12 hex of ``sha256(",".join(sorted(top-level key
  names)))``. Never an argument value, never a nested key, never a key name in
  plain text, never a result, a path, a prompt or transcript text;
* entity write events ``(op, entity_hash, ts, session)`` for ``set_entity``,
  ``delete_entity`` and ``archive_entity``: ``entity_hash`` = the first 16 hex
  of ``sha256(tenant + ":" + category + ":" + name)``. The category and name
  are never stored; the learner resolves a hash by hashing the entities that
  exist in ``memory.db`` when it runs.

Who records: only a process whose client holds a VERIFIED paid entitlement
(``entitled.automation_allowed``); on the free tier or a locked device no
buffer is created and no file is written. ``sibyl skills signals off`` stops
recording (``lifecycle.json`` ``skills.signals``); nothing in a file can turn
it on for a free tier. ``on`` and ``clear`` move a floor
(``skills.signals_since``): an event older than the floor is never written, so
a running process cannot bring back what was cleared, and a transcript is
never read back over the time recording was off.

Who reads: the learner run (``proposals.run_learner``) and ``sibyl skills
signals``. Nothing here opens a network connection; no network path imports
this module (a source-guard test enforces it).

The file: 0600, atomic ``os.replace``, symlink-refusing, read bounded at 64 KB
(a damaged or oversize file is EMPTY, never permission), written under the
lifecycle lock. Bounds on every write and on load: at most 200 n-grams
(evicted by lowest count, then oldest), an n-gram seen fewer than 3 times and
not for 14 days is dropped, every n-gram 30 days after it was last seen (and
30 days after it became a proposal), 7 day buckets; at most 200 entity keys of
32 writes each, writes older than 7 days dropped; the whole file at most
64 KB (n-grams evicted until it fits).

This module imports only the standard library and the lifecycle state helpers,
so the Claude Code Stop hook can use it (``record_transcript``).
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir, read_bounded
from . import state as ST

SIGNALS_NAME = "signals.json"
MAX_BYTES = 64 * 1024
MAX_NGRAMS = 200
MAX_ENTITY_KEYS = 200
MAX_WRITES_PER_KEY = 32
MAX_TS_KEPT = 8
MAX_SESSIONS_KEPT = 8
MAX_TAILS = 8
N_MIN, N_MAX = 3, 5
DAY_BUCKETS = 7
LOW_COUNT = 3
LOW_COUNT_DAYS = 14
MAX_AGE_DAYS = 30
WRITE_DAYS = 7
PROPOSED_DROP_DAYS = 30
TAIL_MAX_AGE_DAYS = 2
FLUSH_EVERY_EVENTS = 20
FLUSH_EVERY_SECONDS = 30.0
TOOL_NAME_MAX = 64
# Claude Code transcript slice per Stop hook (spec 1.2): the rest waits for the
# next Stop.
SLICE_MAX_BYTES = 4 * 1024 * 1024

ENTITY_OPS = {"set_entity": "s", "delete_entity": "d", "archive_entity": "a"}
OP_NAMES = {v: k for k, v in ENTITY_OPS.items()}

_HEX = re.compile(r"^[0-9a-f]+$")
_NAME_BAD = re.compile(r"[^A-Za-z0-9_.:\-]")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------
# Fingerprints
# ----------------------------------------------------------------------

def safe_tool_name(name: Any) -> str:
    """The tool name as stored: at most 64 characters of ``[A-Za-z0-9_.:-]``
    (anything else becomes ``_``)."""
    return _NAME_BAD.sub("_", str(name or ""))[:TOOL_NAME_MAX] or "_"


def arg_shape(args: Any) -> str:
    """First 12 hex of sha256 over the sorted TOP-LEVEL key names; values and
    nested keys are never looked at."""
    keys = sorted(str(k) for k in args.keys()) if isinstance(args, dict) else []
    return hashlib.sha256(",".join(keys).encode("utf-8", "replace")).hexdigest()[:12]


def entity_hash(tenant_id: Any, category: Any, name: Any) -> str:
    raw = f"{tenant_id or ''}:{category or ''}:{name or ''}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def ngram_key(tokens: Iterable[tuple[str, str]]) -> str:
    raw = "|".join(f"{n}#{s}" for n, s in tokens)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def session8(session_key: Any) -> str:
    """The 8-character session reference kept in the file (a prefix of the
    lifecycle session key, itself ``sha256(session id)[:16]``)."""
    sk = str(session_key or "")
    if len(sk) >= 8 and _HEX.match(sk[:8]):
        return sk[:8]
    return ST.session_key(sk)[:8]


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _is_hex(v: Any, n: int) -> bool:
    return isinstance(v, str) and len(v) == n and bool(_HEX.match(v))


def _num(v: Any) -> float | None:
    return ST._num(v)


# ----------------------------------------------------------------------
# The file
# ----------------------------------------------------------------------

def path_of(home: Path | str | None = None) -> Path:
    return ST.memory_dir(home) / SIGNALS_NAME


def empty() -> dict[str, Any]:
    # R2F N2: no "entity_proposed" map. It was written by mark_proposed and never
    # read; the learner's own slug rules (pending, cool-off, muted) already stop a
    # re-proposal. A file that still carries the key loads fine (unknown keys are
    # ignored) and the next write drops it.
    return {"v": 1, "ngrams": {}, "entity_writes": {}, "tails": {}}


def _clean_ngram(e: Any) -> dict[str, Any] | None:
    if not isinstance(e, dict):
        return None
    tools, shapes = e.get("tools"), e.get("shapes")
    if not isinstance(tools, list) or not isinstance(shapes, list):
        return None
    n = len(tools)
    if not (N_MIN <= n <= N_MAX) or len(shapes) != n:
        return None
    if any(not isinstance(t, str) or safe_tool_name(t) != t for t in tools):
        return None
    if any(not _is_hex(s, 12) for s in shapes):
        return None
    first, last = _num(e.get("first")), _num(e.get("last"))
    if first is None or last is None:
        return None
    days = {}
    raw_days = e.get("days")
    if isinstance(raw_days, dict):
        for d, c in raw_days.items():
            cn = _num(c)
            if isinstance(d, str) and _DAY.match(d) and cn is not None and cn > 0:
                days[d] = int(cn)
    sessions = []
    raw_s = e.get("sessions")
    if isinstance(raw_s, list):
        for item in raw_s:
            if isinstance(item, list) and len(item) == 3 and _is_hex(item[0], 8) \
                    and _num(item[1]) is not None and _num(item[2]) is not None:
                sessions.append([item[0], max(1, int(item[1])), float(item[2])])
    ts = [float(t) for t in (e.get("ts") if isinstance(e.get("ts"), list) else [])
          if _num(t) is not None]
    count = _num(e.get("count_total"))
    out = {"n": n, "tools": list(tools), "shapes": list(shapes),
           "count_total": max(1, int(count)) if count is not None else max(1, sum(days.values())),
           "days": days, "first": first, "last": last,
           "sessions": sessions[-MAX_SESSIONS_KEPT:], "ts": ts[-MAX_TS_KEPT:]}
    pa = _num(e.get("proposed_at"))
    if pa is not None:
        out["proposed_at"] = pa
    return out


def _clean(raw: Any) -> dict[str, Any]:
    """Validate strictly: anything that is not the documented shape is
    dropped (a hand-planted string never survives a load)."""
    st = empty()
    if not isinstance(raw, dict):
        return st
    ng = raw.get("ngrams")
    if isinstance(ng, dict):
        for k, e in ng.items():
            ce = _clean_ngram(e)
            if _is_hex(k, 16) and ce is not None and ngram_key(zip(ce["tools"], ce["shapes"])) == k:
                st["ngrams"][k] = ce
    ew = raw.get("entity_writes")
    if isinstance(ew, dict):
        for k, rows in ew.items():
            if not _is_hex(k, 16) or not isinstance(rows, list):
                continue
            keep = []
            for r in rows:
                if isinstance(r, list) and len(r) == 3 and r[0] in OP_NAMES \
                        and _num(r[1]) is not None and _is_hex(r[2], 8):
                    keep.append([r[0], float(r[1]), r[2]])
            if keep:
                st["entity_writes"][k] = keep[-MAX_WRITES_PER_KEY:]
    tails = raw.get("tails")
    if isinstance(tails, dict):
        for k, t in tails.items():
            if not _is_hex(k, 8) or not isinstance(t, dict) or _num(t.get("at")) is None:
                continue
            seq = []
            for item in t.get("t") if isinstance(t.get("t"), list) else []:
                if isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) \
                        and safe_tool_name(item[0]) == item[0] and _is_hex(item[1], 12) \
                        and _num(item[2]) is not None:
                    seq.append([item[0], item[1], float(item[2])])
            st["tails"][k] = {"t": seq[-(N_MAX - 1):], "at": float(t["at"])}
    return st


def load_unlocked(home: Path | str | None = None) -> dict[str, Any]:
    raw = read_bounded(path_of(home), MAX_BYTES)
    if raw is None:
        return empty()
    try:
        return _clean(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return empty()


def load(home: Path | str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """The snapshot the learner reads (pruned; never raises)."""
    try:
        return prune(load_unlocked(home), time.time() if now is None else now)
    except Exception:
        return empty()


def prune(st: dict[str, Any], now: float) -> dict[str, Any]:
    day = 86400.0
    ng = st.setdefault("ngrams", {})
    for k in list(ng):
        e = ng[k]
        last = e.get("last") or 0.0
        pa = e.get("proposed_at")
        if now - last > MAX_AGE_DAYS * day \
                or (e.get("count_total", 0) < LOW_COUNT and now - last > LOW_COUNT_DAYS * day) \
                or (isinstance(pa, (int, float)) and now - pa > PROPOSED_DROP_DAYS * day):
            del ng[k]
            continue
        days = e.get("days") or {}
        if len(days) > DAY_BUCKETS:
            e["days"] = {d: days[d] for d in sorted(days)[-DAY_BUCKETS:]}
        e["sessions"] = sorted(e.get("sessions") or [], key=lambda s: s[2])[-MAX_SESSIONS_KEPT:]
        e["ts"] = sorted(e.get("ts") or [])[-MAX_TS_KEPT:]
    if len(ng) > MAX_NGRAMS:
        order = sorted(ng, key=lambda k: (ng[k].get("count_total", 0), ng[k].get("last") or 0.0))
        for k in order[:len(ng) - MAX_NGRAMS]:
            del ng[k]
    ew = st.setdefault("entity_writes", {})
    for k in list(ew):
        rows = [r for r in ew[k] if now - r[1] <= WRITE_DAYS * day][-MAX_WRITES_PER_KEY:]
        if rows:
            ew[k] = rows
        else:
            del ew[k]
    if len(ew) > MAX_ENTITY_KEYS:
        order = sorted(ew, key=lambda k: ew[k][-1][1])
        for k in order[:len(ew) - MAX_ENTITY_KEYS]:
            del ew[k]
    tails = st.setdefault("tails", {})
    for k in list(tails):
        if now - tails[k].get("at", 0.0) > TAIL_MAX_AGE_DAYS * day:
            del tails[k]
    if len(tails) > MAX_TAILS:
        for k in sorted(tails, key=lambda k: tails[k]["at"])[:len(tails) - MAX_TAILS]:
            del tails[k]
    return st


def _serialise(st: dict[str, Any]) -> bytes:
    return json.dumps(st, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fit(st: dict[str, Any]) -> bytes:
    """Serialise within ``MAX_BYTES``: n-grams are evicted (lowest count,
    oldest) until it fits; then tails, then the oldest entity keys."""
    data = _serialise(st)
    if len(data) <= MAX_BYTES:
        return data
    ng = st["ngrams"]
    order = sorted(ng, key=lambda k: (ng[k].get("count_total", 0), ng[k].get("last") or 0.0))
    # Drop about as many entries as the excess holds (by the average entry
    # size), then re-measure.
    while len(data) > MAX_BYTES and order:
        excess = len(data) - MAX_BYTES
        avg = max(1, len(data) // max(1, len(ng)))
        batch = max(1, min(len(order), excess // avg + 1))
        for k in order[:batch]:
            ng.pop(k, None)
        order = order[batch:]
        data = _serialise(st)
    if len(data) > MAX_BYTES:
        st["tails"] = {}
        data = _serialise(st)
    ew = st["entity_writes"]
    eorder = sorted(ew, key=lambda k: ew[k][-1][1])
    while len(data) > MAX_BYTES and eorder:
        ew.pop(eorder.pop(0), None)
        data = _serialise(st)
    return data


def save_unlocked(home: Path | str | None, st: dict[str, Any], *, now: float | None = None) -> bool:
    try:
        d = ST.memory_dir(home)
        if d.is_symlink():
            return False
        ensure_private_dir(d)
        prune(st, time.time() if now is None else now)
        atomic_write_bytes(path_of(home), _fit(st))
        return True
    except (RefusedPath, OSError, TypeError, ValueError):
        return False


def update(home: Path | str | None, fn: Callable[[dict[str, Any], dict[str, Any]], Any],
           *, now: float | None = None) -> Any:
    """Read-modify-write of signals.json under ``lifecycle.lock``. ``fn(sig,
    lifecycle_state)`` returns ``(result, write)``; ``lifecycle.json`` is
    re-read under the same lock so a concurrent ``off``/``clear`` is seen."""
    store = ST.LifecycleState(home)
    with store._locked():
        life = store.load()
        sig = load_unlocked(home)
        res, write = fn(sig, life)
        if write:
            save_unlocked(home, sig, now=now)
        return res


# ----------------------------------------------------------------------
# Settings (lifecycle.json skills.signals / skills.signals_since)
# ----------------------------------------------------------------------

def settings_of(life: dict[str, Any]) -> tuple[bool, float | None]:
    sk = life.get("skills") or {}
    return sk.get("signals") is not False, _num(sk.get("signals_since"))


def enabled(home: Path | str | None = None) -> bool:
    return settings_of(ST.LifecycleState(home).load())[0]


def set_enabled(home: Path | str | None, on: bool, *, now: float | None = None) -> None:
    now = time.time() if now is None else now

    def _u(st: dict[str, Any]) -> None:
        sk = st.setdefault("skills", ST.default_skills())
        sk["signals"] = bool(on)
        if on:
            sk["signals_since"] = now      # never read back over the time it was off
    ST.LifecycleState(home).update(_u)


def clear(home: Path | str | None = None, *, now: float | None = None) -> bool:
    """Delete signals.json and move the floor (a running process cannot write
    back what it buffered before). True when a file was removed."""
    now = time.time() if now is None else now
    store = ST.LifecycleState(home)
    removed = False
    with store._locked():
        st = store.load()
        st.setdefault("skills", ST.default_skills())["signals_since"] = now
        store.save(st)
        p = path_of(home)
        try:
            if p.is_symlink() or p.is_file():
                os.unlink(p)
                removed = True
        except OSError:
            pass
    with _registry_lock:
        buf = _BUFFERS.get(str(path_of(home)))
    if buf is not None:
        buf.discard()
    return removed


def status(home: Path | str | None = None) -> dict[str, Any]:
    """Counts, size and the oldest / newest timestamps; never an entry."""
    p = path_of(home)
    size = None
    try:
        if p.is_file() and not p.is_symlink():
            size = p.stat().st_size
    except OSError:
        size = None
    st = load_unlocked(home)
    stamps: list[float] = []
    for e in st["ngrams"].values():
        stamps += [e["first"], e["last"]]
    for rows in st["entity_writes"].values():
        stamps += [r[1] for r in rows]
    on, since = settings_of(ST.LifecycleState(home).load())
    return {"exists": size is not None, "bytes": size, "ngrams": len(st["ngrams"]),
            "entity_keys": len(st["entity_writes"]),
            "entity_writes": sum(len(r) for r in st["entity_writes"].values()),
            "oldest": min(stamps) if stamps else None, "newest": max(stamps) if stamps else None,
            "on": on, "since": since}


def mark_proposed(home: Path | str | None, ngram_keys: Iterable[str] = (), *,
                  now: float | None = None) -> None:
    """Stamp ``proposed_at`` on proposed tool sequences (prune drops them
    PROPOSED_DROP_DAYS later). Entity keys are not marked (R2F N2)."""
    now = time.time() if now is None else now
    nk = [k for k in ngram_keys if _is_hex(k, 16)]
    if not nk:
        return

    def _m(sig: dict[str, Any], life: dict[str, Any]) -> tuple[None, bool]:
        changed = False
        for k in nk:
            e = sig["ngrams"].get(k)
            if e is not None:
                e["proposed_at"] = now
                changed = True
        return None, changed
    try:
        update(home, _m, now=now)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Applying events
# ----------------------------------------------------------------------

def _bump_ngram(sig: dict[str, Any], window: list[list[Any]], s8: str, ts: float) -> None:
    toks = [(w[0], w[1]) for w in window]
    k = ngram_key(toks)
    e = sig["ngrams"].get(k)
    if e is None:
        e = {"n": len(toks), "tools": [t[0] for t in toks], "shapes": [t[1] for t in toks],
             "count_total": 0, "days": {}, "first": ts, "last": ts, "sessions": [], "ts": []}
        sig["ngrams"][k] = e
    e["count_total"] = int(e.get("count_total") or 0) + 1
    d = _day(ts)
    e["days"][d] = int(e["days"].get(d, 0)) + 1
    e["first"] = min(e["first"], ts)
    e["last"] = max(e["last"], ts)
    for s in e["sessions"]:
        if s[0] == s8:
            s[1] += 1
            s[2] = max(s[2], ts)
            break
    else:
        e["sessions"].append([s8, 1, ts])
    e["sessions"] = sorted(e["sessions"], key=lambda s: s[2])[-MAX_SESSIONS_KEPT:]
    e["ts"] = sorted(e["ts"] + [ts])[-MAX_TS_KEPT:]


def apply_tool_events(sig: dict[str, Any], s8: str, events: Iterable[tuple[str, str, float]],
                      now: float) -> int:
    """Append ``(name, shape, ts)`` events of one session; every n-gram (3..5)
    that ends at a new event is counted. The session's last four events are
    kept (``tails``) so a sequence spanning two flushes or two Stop hooks is
    still seen. Returns how many events were applied."""
    tail = sig["tails"].get(s8, {}).get("t") or []
    seq = [list(x) for x in tail]
    n_applied = 0
    for name, shape, ts in events:
        seq.append([safe_tool_name(name), shape if _is_hex(shape, 12) else arg_shape(None), float(ts)])
        n_applied += 1
        for n in range(N_MIN, N_MAX + 1):
            if len(seq) >= n:
                _bump_ngram(sig, seq[-n:], s8, float(ts))
        seq = seq[-(N_MAX - 1):]
    if n_applied:
        sig["tails"][s8] = {"t": seq, "at": now}
    return n_applied


def apply_entity_writes(sig: dict[str, Any], writes: Iterable[tuple[str, str, float, str]]) -> int:
    n = 0
    for h, op, ts, s8 in writes:
        if not _is_hex(h, 16) or op not in OP_NAMES:
            continue
        rows = sig["entity_writes"].setdefault(h, [])
        rows.append([op, float(ts), s8])
        sig["entity_writes"][h] = rows[-MAX_WRITES_PER_KEY:]
        n += 1
    return n


# ----------------------------------------------------------------------
# The in-process buffer (MCP server, Hermes adapter, the client's writes)
# ----------------------------------------------------------------------

_registry_lock = threading.Lock()
_BUFFERS: dict[str, "SignalBuffer"] = {}
_atexit_registered = False


def _allowed(client: Any) -> bool:
    try:
        from .entitled import automation_allowed
        return bool(automation_allowed(client)[0])
    except Exception:
        return False


class SignalBuffer:
    """One per lifecycle home and process. ``note_*`` are cheap (a list
    append after the entitlement peek); the file is written at most every
    30 s or 20 events, and at exit."""

    def __init__(self, home: Path | str | None = None) -> None:
        self.home = Path(home) if home is not None else Path.home()
        self._lock = threading.Lock()
        self._tools: list[tuple[str, str, str, float]] = []      # (s8, name, shape, ts)
        self._writes: list[tuple[str, str, float, str]] = []     # (hash, op, ts, s8)
        self._last_flush = time.monotonic()
        self._timer: threading.Timer | None = None
        self.session_key = ST.session_key(f"process:{os.getpid()}:{time.time()!r}")
        self._flag: tuple[Any, bool] = (None, True)

    # -- arming ---------------------------------------------------------------
    def set_session(self, session_key: Any) -> None:
        if session_key:
            self.session_key = str(session_key)

    def _on(self) -> bool:
        """``skills.signals`` (re-read when lifecycle.json changes)."""
        p = ST.memory_dir(self.home) / ST.LIFECYCLE_NAME
        try:
            m = p.stat().st_mtime_ns
        except OSError:
            m = None
        if m != self._flag[0]:
            self._flag = (m, enabled(self.home))
        return self._flag[1]

    def armed(self, client: Any) -> bool:
        return _allowed(client) and self._on()

    # -- events ---------------------------------------------------------------
    def note_tool(self, name: Any, args: Any, *, client: Any, now: float | None = None) -> bool:
        if not self.armed(client):
            return False
        ev = (session8(self.session_key), safe_tool_name(name), arg_shape(args),
              float(int(time.time() if now is None else now)))
        with self._lock:
            self._tools.append(ev)
        self._maybe_flush()
        return True

    def note_entity_write(self, kind: str, category: Any, name: Any, tenant_id: Any, *,
                          client: Any, now: float | None = None) -> bool:
        op = ENTITY_OPS.get(kind)
        if op is None or ST.is_suppressed() or not self.armed(client):
            return False
        ev = (entity_hash(tenant_id, category, name), op,
              float(int(time.time() if now is None else now)), session8(self.session_key))
        with self._lock:
            self._writes.append(ev)
        self._maybe_flush()
        return True

    def pending(self) -> int:
        with self._lock:
            return len(self._tools) + len(self._writes)

    def discard(self) -> None:
        with self._lock:
            self._tools.clear()
            self._writes.clear()

    # -- flushing -------------------------------------------------------------
    def _maybe_flush(self) -> None:
        _ensure_atexit()
        with self._lock:
            n = len(self._tools) + len(self._writes)
            due = n >= FLUSH_EVERY_EVENTS or time.monotonic() - self._last_flush >= FLUSH_EVERY_SECONDS
            if not due and self._timer is None:
                t = threading.Timer(FLUSH_EVERY_SECONDS, self.flush)
                t.daemon = True
                self._timer = t
                t.start()
        if due:
            self.flush()

    def flush(self, *, now: float | None = None) -> bool:
        """Merge what is buffered into the file (under the lock). Nothing is
        written when recording is off; events older than the floor are
        dropped. Returns True when the file was written."""
        with self._lock:
            tools, writes = self._tools, self._writes
            self._tools, self._writes = [], []
            self._last_flush = time.monotonic()
            t, self._timer = self._timer, None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        if not tools and not writes:
            return False
        now = time.time() if now is None else now

        def _m(sig: dict[str, Any], life: dict[str, Any]) -> tuple[bool, bool]:
            on, since = settings_of(life)
            if not on:
                return False, False
            floor = since if since is not None else float("-inf")
            by_session: dict[str, list[tuple[str, str, float]]] = {}
            for s8, name, shape, ts in tools:
                if ts >= floor:
                    by_session.setdefault(s8, []).append((name, shape, ts))
            n = 0
            for s8, evs in by_session.items():
                n += apply_tool_events(sig, s8, evs, now)
            n += apply_entity_writes(sig, [w for w in writes if w[2] >= floor])
            return n > 0, n > 0
        try:
            return bool(update(self.home, _m, now=now))
        except Exception:
            return False


def recorder(home: Path | str | None = None) -> SignalBuffer:
    """The process's buffer for ``home`` (created on first use)."""
    key = str(path_of(home))
    with _registry_lock:
        buf = _BUFFERS.get(key)
        if buf is None:
            buf = SignalBuffer(home)
            _BUFFERS[key] = buf
        return buf


def flush_all() -> None:
    with _registry_lock:
        bufs = list(_BUFFERS.values())
    for b in bufs:
        try:
            b.flush()
        except Exception:
            pass


def _ensure_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        _atexit_registered = True
        atexit.register(flush_all)


def note_tool(home: Path | str | None, name: Any, args: Any, *, client: Any) -> bool:
    """One tool call the process handled (never raises)."""
    try:
        return recorder(home).note_tool(name, args, client=client)
    except Exception:
        return False


def note_entity_write(home: Path | str | None, kind: str, category: Any, name: Any, tenant_id: Any,
                      *, client: Any) -> bool:
    try:
        return recorder(home).note_entity_write(kind, category, name, tenant_id, client=client)
    except Exception:
        return False


# ----------------------------------------------------------------------
# Claude Code: the transcript slice (inside the Stop hook, stdlib only)
# ----------------------------------------------------------------------

_TOOL_USE = b'"tool_use"'


def _line_ts(obj: dict[str, Any], default: float) -> float:
    t = ST._iso_to_epoch(obj.get("timestamp"))
    return float(int(t)) if t is not None else float(int(default))


def parse_tool_uses(chunk: bytes, now: float) -> list[tuple[str, str, float]]:
    """Every ``tool_use`` block of the main conversation's assistant entries in
    complete JSONL ``chunk``: (name, shape, ts). Inputs are reduced to their
    sorted top-level key names, hashed; nothing else is kept."""
    out: list[tuple[str, str, float]] = []
    for line in chunk.split(b"\n"):
        if _TOOL_USE not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("isSidechain") is True:
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        ts = None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if ts is None:
                    ts = _line_ts(obj, now)
                out.append((safe_tool_name(block.get("name")), arg_shape(block.get("input")), ts))
    return out


def read_slice(path: Path | str, offset: int, *, mid_line: bool = False,
               max_bytes: int = SLICE_MAX_BYTES, now: float | None = None
               ) -> tuple[list[tuple[str, str, float]], int, bool]:
    """(events, new offset, mid_line) for the bytes after ``offset``, at most
    ``max_bytes``; only complete lines are parsed (a line still being written
    waits). A line longer than ``max_bytes`` is skipped across calls
    (``mid_line``). The caller resets ``offset`` when the file shrank."""
    now = time.time() if now is None else now
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        return [], offset, mid_line
    with open(p, "rb") as fh:
        fh.seek(offset)
        data = fh.read(max_bytes)
    if not data:
        return [], offset, mid_line
    start = 0
    if mid_line:
        nl = data.find(b"\n")
        if nl < 0:
            return [], offset + len(data), True
        start = nl + 1
        mid_line = False
    last = data.rfind(b"\n")
    if last < start:
        if len(data) >= max_bytes and start == 0:
            return [], offset + len(data), True     # one line larger than the window: skip it
        return [], offset + start, False            # an incomplete line: wait for its end
    events = parse_tool_uses(data[start:last + 1], now)
    return events, offset + last + 1, False


def transcript_ref(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16]


def record_transcript(home: Path | str | None, sk: str, transcript_path: Any, *,
                      tenant_id: str, now: float | None = None,
                      max_bytes: int = SLICE_MAX_BYTES) -> dict[str, Any]:
    """The Stop hook's capture (spec 1.2): parse the transcript from the
    session's stored offset (``lifecycle.json`` sessions[..].transcript_offset,
    at most ``max_bytes`` per call), append the tool events to signals.json and
    advance the offset, in one pass under ``lifecycle.lock``. The caller has
    already established Pro. When recording is off the offset only moves to
    the end of the file (nothing is read), so turning it on later never reads
    back over that time. Returns a small report (counts only)."""
    now = time.time() if now is None else now
    rep: dict[str, Any] = {"events": 0, "bytes": 0, "reset": False, "off": False}
    if not isinstance(transcript_path, str) or not transcript_path:
        return rep
    p = Path(transcript_path)
    try:
        if p.is_symlink() or not p.is_file():
            return rep
        size = p.stat().st_size
    except OSError:
        return rep
    ref = transcript_ref(transcript_path)
    store = ST.LifecycleState(home)
    with store._locked():
        life = store.load()
        s = ST.ensure_session(life, sk, tenant_id=tenant_id, harness="claude-code", now=now)
        on, since = settings_of(life)
        offset = int(s.get("transcript_offset") or 0)
        mid = s.get("transcript_mid") is True
        if s.get("transcript_ref") != ref or offset > size:
            offset, mid = 0, False
            rep["reset"] = s.get("transcript_ref") is not None
        if not on:
            s.update(transcript_ref=ref, transcript_offset=size, transcript_mid=False)
            store.save(life)
            rep["off"] = True
            return rep
        events, new_off, mid = read_slice(p, offset, mid_line=mid, max_bytes=max_bytes, now=now)
        rep["bytes"] = new_off - offset
        floor = since if since is not None else float("-inf")
        events = [e for e in events if e[2] >= floor]
        if events:
            sig = load_unlocked(home)
            rep["events"] = apply_tool_events(sig, session8(sk), events, now)
            save_unlocked(home, sig, now=now)
        s.update(transcript_ref=ref, transcript_offset=new_off, transcript_mid=mid)
        store.save(life)
    return rep


__all__ = ["ENTITY_OPS", "MAX_BYTES", "MAX_ENTITY_KEYS", "MAX_NGRAMS", "MAX_WRITES_PER_KEY",
           "SIGNALS_NAME", "SLICE_MAX_BYTES", "SignalBuffer", "apply_entity_writes",
           "apply_tool_events", "arg_shape", "clear", "empty", "enabled", "entity_hash", "flush_all",
           "load", "mark_proposed", "ngram_key", "note_entity_write", "note_tool", "parse_tool_uses",
           "path_of", "prune", "read_slice", "record_transcript", "recorder", "safe_tool_name",
           "session8", "set_enabled", "settings_of", "status", "transcript_ref", "update"]
