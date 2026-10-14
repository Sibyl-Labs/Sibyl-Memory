"""``~/.sibyl-memory/lifecycle.json``: the save and debloat bookkeeping.

Counters and timestamps only: never a token, never file content, never a
conversation. 0600, atomic, bounded read (64 KB), symlink-refusing; a damaged
file is treated as EMPTY, never as permission. Nothing in this file can turn
automation on: the tier always comes from the verified entitlement
(``entitled.automation_allowed``); editing counters here changes reminder
timing only.

Shape::

    {"v": 1, "reminders": true, "quiet": false,
     "tenants": {"<tenant>": {"writes_total", "writes_since_save", "since_save_started_at",
                              "last_write_at", "last_save_at", "last_save_reminder_at",
                              "last_debloat_reminder_at": {"<file>": ts}, "pro_line_shown_at",
                              "pending_curation"}},
     "sessions": {"<sha256(session id)[:16]>": {"tenant", "harness", "started_at",
                  "writes_at_start", "nudged", "saved", "ended_at", "capsule_written",
                  "reported", "active_seconds", "last_event_at", "active_at_save",
                  "nudges": [ts], "last_nudge_active", "prompts", "skills_prompted",
                  "compaction_seen", "seq_floor"}},
     "autosave": {"enabled", "interval_seconds", "max_per_day", "max_per_session"},
     "skills": {"last_prompt_at", "muted_kinds": {"<kind>": until}, "unmuted": {"<slug>": ts}},
     "debloat": {"last_measure_at", "last_plan": {...}, "last_apply": {...}}}

Active time (R1, plan 2.1): every harness event adds ``min(now - last_event_at,
IDLE_CAP_SECONDS)`` to the session's ``active_seconds`` (``note_activity``), so a
gap longer than five minutes counts as five; a save (the DB marker, whoever
wrote it) moves every open session's ``active_at_save`` to its current active
time. The automatic save's interval is counted in ACTIVE time since the last
save, never wall clock. Settings are bounded on read (``autosave_settings``):
nothing in the file can make the automation run more often than the bounds.

Scope: a compaction is the SESSION's (``sessions[..].compaction_seen``,
cleared by a save): one session's compaction never makes another session due.
The write counters (``writes_since_save``) stay tenant-wide by design (plan
D6.5): memory is shared, so a write in one session is unsaved work for every
session on the account until any of them saves.

The write counter (``note_write``) is fed by ``client._track_op`` for the
kinds in ``WRITE_OPS``, only for a client a plugin surface enabled
(``MemoryClient.enable_lifecycle_counting``): an in-memory count per lifecycle
file, flushed with one ``os.replace`` at most every 30 s or every 10 writes
(and at exit; the exit hook is registered on the first counted write). The
automation's own writes (capsules, trims, the Hermes per-turn journal) run
inside ``suppressed()`` and are not counted.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir, read_bounded

LIFECYCLE_NAME = "lifecycle.json"
LOCK_NAME = "lifecycle.lock"
LOG_NAME = "lifecycle.log"
MAX_STATE_BYTES = 64 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_SESSIONS = 50
FLUSH_EVERY_WRITES = 10
FLUSH_EVERY_SECONDS = 30.0
SAVE_MARKER_KEY = "sibyl_save:last_save"
PENDING_KEY = "sibyl_save:pending"

# Active-time automatic save (R1, plan 2.1-2.4; operator D6).
IDLE_CAP_SECONDS = 300
# Claude Code only sees a prompt and the turn's end: the time between them is
# the model working (active), bounded so a turn left waiting on a permission
# question overnight counts as half an hour, not a night.
TURN_CAP_SECONDS = 1800
AUTOSAVE_INTERVAL_DEFAULT = 3600
AUTOSAVE_INTERVAL_MIN = 1800
AUTOSAVE_INTERVAL_MAX = 14400
AUTOSAVE_MAX_PER_DAY = 12
AUTOSAVE_MAX_PER_SESSION = 8
MAX_NUDGES_KEPT = 16
MAX_MUTES = 50

# Mirrors _entitlement.WRITE_OPS (kept literal so this module imports nothing
# from the gate; a test asserts the two sets are equal).
WRITE_OPS = frozenset({
    "set_entity", "set_state", "write_event", "set_reference", "delete_entity",
    "archive_entity", "learn", "accept_skill_proposal", "reject_skill_proposal",
})


def memory_dir(home: Path | str | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else Path.home()) / ".sibyl-memory"


def session_key(session_id: Any) -> str:
    return hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:16]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _int(v: Any) -> int:
    n = _num(v)
    return max(0, int(n)) if n is not None else 0


def default_autosave() -> dict[str, Any]:
    return {"enabled": True, "interval_seconds": AUTOSAVE_INTERVAL_DEFAULT,
            "max_per_day": AUTOSAVE_MAX_PER_DAY, "max_per_session": AUTOSAVE_MAX_PER_SESSION}


def default_skills() -> dict[str, Any]:
    return {"last_prompt_at": None, "muted_kinds": {}, "unmuted": {}}


def default_state() -> dict[str, Any]:
    return {"v": 1, "reminders": True, "quiet": False, "tenants": {}, "sessions": {},
            "debloat": {}, "autosave": default_autosave(), "skills": default_skills()}


def default_tenant() -> dict[str, Any]:
    return {"writes_total": 0, "writes_since_save": 0, "since_save_started_at": None,
            "last_write_at": None, "last_save_at": None, "last_save_reminder_at": None,
            "last_debloat_reminder_at": {}, "pro_line_shown_at": None,
            "pending_curation": 0, "nudges_day": None, "nudges_today": 0,
            "last_nudge_at": None, "tier_seen": None}


def _clean_autosave(raw: Any) -> dict[str, Any]:
    """Bounded settings: an interval outside 30..240 minutes, or a cap above the
    defaults, is pulled back inside (a hand-edited file can make the automatic
    save rarer or turn it off, never more frequent than the bounds)."""
    out = default_autosave()
    if not isinstance(raw, dict):
        return out
    out["enabled"] = raw.get("enabled") is not False
    iv = _num(raw.get("interval_seconds"))
    if iv is not None:
        out["interval_seconds"] = int(min(AUTOSAVE_INTERVAL_MAX, max(AUTOSAVE_INTERVAL_MIN, iv)))
    for k, hi in (("max_per_day", AUTOSAVE_MAX_PER_DAY), ("max_per_session", AUTOSAVE_MAX_PER_SESSION)):
        v = _num(raw.get(k))
        if v is not None:
            out[k] = int(min(hi, max(0, v)))
    return out


def _clean_skills(raw: Any) -> dict[str, Any]:
    out = default_skills()
    if not isinstance(raw, dict):
        return out
    out["last_prompt_at"] = _num(raw.get("last_prompt_at"))
    for k in ("muted_kinds", "unmuted"):
        v = raw.get(k)
        if isinstance(v, dict):
            out[k] = {str(a)[:80]: _num(b) for a, b in list(v.items())[:MAX_MUTES] if _num(b) is not None}
    return out


def _clean(raw: Any) -> dict[str, Any]:
    st = default_state()
    if not isinstance(raw, dict):
        return st
    st["reminders"] = raw.get("reminders") is not False
    st["quiet"] = raw.get("quiet") is True
    tenants = raw.get("tenants")
    if isinstance(tenants, dict):
        for tid, t in list(tenants.items())[:20]:
            if not isinstance(tid, str) or not isinstance(t, dict):
                continue
            ct = default_tenant()
            for k in ("writes_total", "writes_since_save", "pending_curation"):
                ct[k] = _int(t.get(k))
            for k in ("since_save_started_at", "last_write_at", "last_save_at",
                      "last_save_reminder_at", "pro_line_shown_at"):
                ct[k] = _num(t.get(k))
            ct["nudges_today"] = _int(t.get("nudges_today"))
            ct["nudges_day"] = str(t["nudges_day"])[:10] if isinstance(t.get("nudges_day"), str) else None
            ct["last_nudge_at"] = _num(t.get("last_nudge_at"))
            ct["tier_seen"] = t.get("tier_seen") if t.get("tier_seen") in ("paid", "free") else None
            ldr = t.get("last_debloat_reminder_at")
            if isinstance(ldr, dict):
                ct["last_debloat_reminder_at"] = {str(k)[:200]: _num(v) for k, v in list(ldr.items())[:50]
                                                  if _num(v) is not None}
            st["tenants"][tid[:200]] = ct
    sessions = raw.get("sessions")
    if isinstance(sessions, dict):
        for sk, s in list(sessions.items())[-MAX_SESSIONS:]:
            if not isinstance(sk, str) or not isinstance(s, dict):
                continue
            st["sessions"][sk[:32]] = {
                "tenant": str(s.get("tenant") or "")[:200],
                "harness": str(s.get("harness") or "")[:32],
                "started_at": _num(s.get("started_at")),
                "writes_at_start": _int(s.get("writes_at_start")),
                "writes": _int(s.get("writes")),
                "nudged": s.get("nudged") is True,
                "saved": s.get("saved") is True,
                "ended_at": _num(s.get("ended_at")),
                "capsule_written": s.get("capsule_written") is True,
                "capsule_at": _num(s.get("capsule_at")),
                "reported": s.get("reported") is True,
                "compactions": _int(s.get("compactions")),
                "active_seconds": max(0.0, _num(s.get("active_seconds")) or 0.0),
                "last_event_at": _num(s.get("last_event_at")),
                "active_at_save": max(0.0, _num(s.get("active_at_save")) or 0.0),
                "nudges": [x for x in (_num(v) for v in (s.get("nudges") if isinstance(
                    s.get("nudges"), list) else [])[-MAX_NUDGES_KEPT:]) if x is not None],
                "last_nudge_active": _num(s.get("last_nudge_active")),
                "prompts": _int(s.get("prompts")),
                "skills_prompted": s.get("skills_prompted") is True,
                "compaction_seen": s.get("compaction_seen") is True,
                "seq_floor": _int(s.get("seq_floor")),
                "last_event_kind": str(s.get("last_event_kind") or "")[:16] or None,
            }
    st["autosave"] = _clean_autosave(raw.get("autosave"))
    st["skills"] = _clean_skills(raw.get("skills"))
    deb = raw.get("debloat")
    if isinstance(deb, dict):
        st["debloat"] = {k: deb[k] for k in ("last_measure_at", "last_measure_root", "last_plan",
                                             "last_apply", "last_report")
                         if k in deb}
    return st


class LifecycleState:
    """Load / update ``lifecycle.json`` under one home."""

    def __init__(self, home: Path | str | None = None) -> None:
        self.dir = memory_dir(home)
        self.path = self.dir / LIFECYCLE_NAME
        self.lock_path = self.dir / LOCK_NAME

    def load(self) -> dict[str, Any]:
        raw = read_bounded(self.path, MAX_STATE_BYTES)
        if raw is None:
            return default_state()
        try:
            return _clean(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return default_state()

    def save(self, st: dict[str, Any]) -> bool:
        try:
            if self.dir.is_symlink():
                return False
            ensure_private_dir(self.dir)
            sessions = st.get("sessions") or {}
            if len(sessions) > MAX_SESSIONS:
                keep = sorted(sessions.items(), key=lambda kv: kv[1].get("started_at") or 0)[-MAX_SESSIONS:]
                st["sessions"] = dict(keep)
            data = json.dumps(st, sort_keys=True).encode("utf-8")
            if len(data) > MAX_STATE_BYTES:
                st["sessions"] = dict(list(st.get("sessions", {}).items())[-10:])
                data = json.dumps(st, sort_keys=True).encode("utf-8")
            atomic_write_bytes(self.path, data)
            return True
        except (RefusedPath, OSError, TypeError, ValueError):
            return False

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        fd = None
        try:
            if not self.dir.is_symlink():
                ensure_private_dir(self.dir)
                if not self.lock_path.is_symlink():
                    fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    except (ImportError, OSError):
                        pass
        except (RefusedPath, OSError):
            fd = None
        try:
            yield
        finally:
            if fd is not None:
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                os.close(fd)

    def update(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Read-modify-write under an advisory lock. Returns ``fn``'s result."""
        with self._locked():
            st = self.load()
            out = fn(st)
            self.save(st)
            return out


def tenant(st: dict[str, Any], tid: str) -> dict[str, Any]:
    t = st.setdefault("tenants", {}).get(tid)
    if not isinstance(t, dict):
        t = default_tenant()
        st["tenants"][tid] = t
    for k, v in default_tenant().items():
        t.setdefault(k, v)
    return t


# ----------------------------------------------------------------------
# Log (never content: event names, counts, reasons)
# ----------------------------------------------------------------------

def log(line: str, home: Path | str | None = None) -> None:
    """Append one short line to ``~/.sibyl-memory/lifecycle.log`` (bounded:
    rotated to ``.1`` past 256 KB). Lines are cut at 300 characters."""
    try:
        d = memory_dir(home)
        if d.is_symlink():
            return
        ensure_private_dir(d)
        p = d / LOG_NAME
        if p.is_symlink():
            return
        try:
            if p.stat().st_size > MAX_LOG_BYTES:
                os.replace(str(p), str(d / (LOG_NAME + ".1")))
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        text = " ".join(str(line).split())[:300]
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{stamp} {text}\n".encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


# ----------------------------------------------------------------------
# Write counter (fed by client._track_op)
# ----------------------------------------------------------------------

_counter_lock = threading.Lock()
_pending: dict[str, dict[str, int]] = {}      # lifecycle.json path -> tenant -> writes
_pending_first: dict[str, float] = {}          # path -> time of the oldest unflushed write
_last_flush: dict[str, float] = {}
_timers: dict[str, threading.Timer] = {}
_local = threading.local()


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Writes made inside this block are not counted (the automation's own
    writes and the Hermes per-turn journal)."""
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1


def is_suppressed() -> bool:
    return getattr(_local, "depth", 0) > 0


def _flush_path(path: str) -> None:
    with _counter_lock:
        counts = _pending.pop(path, None)
        first = _pending_first.pop(path, None)
        t = _timers.pop(path, None)
        _last_flush[path] = time.monotonic()
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
    if not counts:
        return
    home = Path(path).parent.parent
    now = time.time()

    def _apply(st: dict[str, Any]) -> None:
        for tid, n in counts.items():
            t = tenant(st, tid)
            t["writes_total"] = _int(t.get("writes_total")) + n
            if _int(t.get("writes_since_save")) == 0:
                t["since_save_started_at"] = first or now
            t["writes_since_save"] = _int(t.get("writes_since_save")) + n
            t["last_write_at"] = now
    try:
        LifecycleState(home).update(_apply)
    except Exception:
        pass


def pending_writes(tenant_id: str, *, home: Path | str | None = None) -> int:
    """Writes counted in this process and not yet flushed to the file."""
    path = str(memory_dir(home) / LIFECYCLE_NAME)
    with _counter_lock:
        return int(_pending.get(path, {}).get(tenant_id, 0))


def flush_all() -> None:
    with _counter_lock:
        paths = list(_pending)
    for p in paths:
        _flush_path(p)


_atexit_registered = False


def _ensure_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        _atexit_registered = True
        atexit.register(flush_all)


def note_write(kind: str, tenant_id: str, *, home: Path | str | None = None) -> None:
    """Count one successful memory write. Cheap: a dict increment; at most one
    file replace every 30 s or every 10 writes (a daemon timer makes sure a
    short burst is flushed within 30 s)."""
    if kind not in WRITE_OPS or is_suppressed():
        return
    _ensure_atexit()
    path = str(memory_dir(home) / LIFECYCLE_NAME)
    now = time.monotonic()
    flush = False
    with _counter_lock:
        per = _pending.setdefault(path, {})
        per[tenant_id] = per.get(tenant_id, 0) + 1
        _pending_first.setdefault(path, time.time())
        total = sum(per.values())
        last = _last_flush.get(path)
        if total >= FLUSH_EVERY_WRITES or (last is not None and now - last >= FLUSH_EVERY_SECONDS):
            flush = True
        elif path not in _timers:
            t = threading.Timer(FLUSH_EVERY_SECONDS, _flush_path, args=(path,))
            t.daemon = True
            _timers[path] = t
            t.start()
        if last is None:
            _last_flush[path] = now
    if flush:
        _flush_path(path)


def note_save(tenant_id: str, *, home: Path | str | None = None, now: float | None = None) -> None:
    """The save procedure wrote its delta marker (``sibyl_save:last_save``)."""
    path = str(memory_dir(home) / LIFECYCLE_NAME)
    _flush_path(path)
    now = time.time() if now is None else now

    def _apply(st: dict[str, Any]) -> None:
        t = tenant(st, tenant_id)
        t["last_save_at"] = now
        t["writes_since_save"] = 0
        t["since_save_started_at"] = None
        _reset_active_clocks(st, tenant_id, now)
    try:
        LifecycleState(home).update(_apply)
    except Exception:
        pass


def _reset_active_clocks(st: dict[str, Any], tenant_id: str, when: float) -> None:
    """A save (whoever made it: this agent, another agent on the same home, a
    person in a terminal) restarts the active-time clock of every open session
    of the tenant that started before it."""
    for s in st.get("sessions", {}).values():
        if s.get("tenant") != tenant_id or s.get("ended_at"):
            continue
        started = _num(s.get("started_at"))
        if started is not None and started > when:
            continue
        s["active_at_save"] = max(0.0, _num(s.get("active_seconds")) or 0.0)
        s["saved"] = True
        s["compaction_seen"] = False


def observe_marker(st: dict[str, Any], tenant_id: str, marker_ts: float | None) -> bool:
    """Fold a save marker read from the DB into the state (the DB marker is
    authoritative). Returns True when it was newer than what the state knew."""
    if marker_ts is None:
        return False
    t = tenant(st, tenant_id)
    last = _num(t.get("last_save_at"))
    if last is not None and marker_ts <= last:
        return False
    t["last_save_at"] = marker_ts
    t["writes_since_save"] = 0
    t["since_save_started_at"] = None
    _reset_active_clocks(st, tenant_id, marker_ts)
    return True


# ----------------------------------------------------------------------
# Sessions and active time (R1, plan 2.1)
# ----------------------------------------------------------------------

def ensure_session(st: dict[str, Any], sk: str, *, tenant_id: str, harness: str,
                   now: float) -> dict[str, Any]:
    """The session record for ``sk`` (created on first sight)."""
    t = tenant(st, tenant_id)
    s = st.setdefault("sessions", {}).get(sk)
    if not isinstance(s, dict):
        s = {"tenant": tenant_id, "harness": harness, "started_at": now,
             "writes_at_start": int(t.get("writes_total") or 0), "writes": 0, "nudged": False,
             "saved": False, "ended_at": None, "capsule_written": False, "capsule_at": None,
             "reported": False, "compactions": 0, "active_seconds": 0.0, "last_event_at": None,
             "active_at_save": 0.0, "nudges": [], "last_nudge_active": None, "prompts": 0,
             "skills_prompted": False, "last_event_kind": None, "compaction_seen": False,
             "seq_floor": 0}
        st["sessions"][sk] = s
    for k, v in (("active_seconds", 0.0), ("last_event_at", None), ("active_at_save", 0.0),
                 ("nudges", []), ("last_nudge_active", None), ("prompts", 0),
                 ("skills_prompted", False), ("compaction_seen", False), ("seq_floor", 0)):
        s.setdefault(k, v)
    return s


def add_active(s: dict[str, Any], now: float, *, kind: str | None = None,
               cap: float = IDLE_CAP_SECONDS) -> float:
    """Add ``min(now - last_event_at, cap)`` to the session's active time and
    stamp ``now`` (and the event ``kind``). A clock that went backwards adds
    nothing. Returns the new active total."""
    last = _num(s.get("last_event_at"))
    active = max(0.0, _num(s.get("active_seconds")) or 0.0)
    if last is not None and now > last:
        active += min(now - last, float(cap))
    s["active_seconds"] = active
    if last is None or now > last:
        s["last_event_at"] = now
    if kind is not None:
        s["last_event_kind"] = kind
    return active


def note_activity(st: dict[str, Any], sk: str, now: float, *, tenant_id: str,
                  harness: str) -> dict[str, Any]:
    """One harness event (a prompt, a turn end, a tool call, a Hermes turn)."""
    s = ensure_session(st, sk, tenant_id=tenant_id, harness=harness, now=now)
    add_active(s, now)
    return s


def autosave_settings(st: dict[str, Any]) -> dict[str, Any]:
    return _clean_autosave(st.get("autosave"))


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def nudges_today(t: dict[str, Any], now: float) -> int:
    return _int(t.get("nudges_today")) if t.get("nudges_day") == _day(now) else 0


def next_nudge_seq(s: dict[str, Any]) -> int:
    """The sequence number the session's next automatic-save request carries:
    one past the requests recorded here, or past a numbered request the
    Claude Code transcript already holds (``seq_floor``: the last number
    used, or the one the Stop hook's transcript guard found when this state
    had lost it)."""
    n = len([x for x in (s.get("nudges") or []) if _num(x) is not None])
    return max(n, _int(s.get("seq_floor"))) + 1


def note_seq_seen(st: dict[str, Any], tenant_id: str, sk: str, seq: int, *, now: float,
                  harness: str = "") -> None:
    """The transcript already carries request ``seq`` (this state lost it):
    the next request is numbered past it. Nothing is counted: no per-session
    or per-day cap is consumed and the active-time interval is not restarted
    (only an EMITTED request does that, ``record_nudge``)."""
    s = ensure_session(st, sk, tenant_id=tenant_id, harness=harness, now=now)
    s["seq_floor"] = max(_int(s.get("seq_floor")), int(seq))


def record_nudge(st: dict[str, Any], tenant_id: str, sk: str, now: float, *,
                 harness: str = "") -> int:
    """Count one automatic-save request that was actually made (a Stop
    continuation printed, an MCP notice or a Hermes prefetch line returned).
    Returns its sequence number in the session (1, 2, ...)."""
    t = tenant(st, tenant_id)
    s = ensure_session(st, sk, tenant_id=tenant_id, harness=harness, now=now)
    seq = next_nudge_seq(s)
    nudges = [x for x in (s.get("nudges") or []) if _num(x) is not None]
    nudges.append(now)
    s["nudges"] = nudges[-MAX_NUDGES_KEPT:]
    s["seq_floor"] = seq          # the last number used (the list is trimmed; numbers never repeat)
    s["nudged"] = True
    s["last_nudge_active"] = max(0.0, _num(s.get("active_seconds")) or 0.0)
    day = _day(now)
    if t.get("nudges_day") != day:
        t["nudges_day"], t["nudges_today"] = day, 0
    t["nudges_today"] = _int(t.get("nudges_today")) + 1
    t["last_nudge_at"] = now
    return seq


# ----------------------------------------------------------------------
# Read-only DB peeks for processes that must not open a gated client (hooks)
# ----------------------------------------------------------------------

def _iso_to_epoch(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    from datetime import datetime, timezone
    s = v.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_db_state(db_path: Path | str, tenant_id: str, key: str) -> tuple[Any, float | None] | None:
    """(body, updated_at epoch) of one state document, read-only (``mode=ro``),
    no gate, no network; None when absent or unreadable."""
    import sqlite3
    p = Path(db_path).expanduser()
    try:
        if not p.is_file() or p.is_symlink():
            return None
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT body, updated_at FROM state_documents "
                              "WHERE tenant_id = ? AND document_key = ?", (tenant_id, key)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        body = json.loads(row[0])
    except (ValueError, TypeError):
        body = None
    return body, _iso_to_epoch(row[1])


def read_save_marker(db_path: Path | str, tenant_id: str) -> float | None:
    got = read_db_state(db_path, tenant_id, SAVE_MARKER_KEY)
    if got is None:
        return None
    body, updated = got
    ts = _iso_to_epoch(body.get("ts")) if isinstance(body, dict) else None
    # The row's own updated_at is written by the DB: prefer it (a body ts can
    # be anything the model typed); fall back to the body ts.
    return updated if updated is not None else ts


def read_pending_count(db_path: Path | str, tenant_id: str) -> int:
    got = read_db_state(db_path, tenant_id, PENDING_KEY)
    if got is None:
        return 0
    body, _ = got
    items = body.get("items") if isinstance(body, dict) else None
    return len(items) if isinstance(items, list) else 0


__all__ = ["AUTOSAVE_INTERVAL_DEFAULT", "AUTOSAVE_INTERVAL_MAX", "AUTOSAVE_INTERVAL_MIN",
           "AUTOSAVE_MAX_PER_DAY", "AUTOSAVE_MAX_PER_SESSION", "IDLE_CAP_SECONDS", "LifecycleState",
           "TURN_CAP_SECONDS",
           "WRITE_OPS", "add_active", "autosave_settings", "default_state", "ensure_session", "flush_all",
           "is_suppressed", "log", "memory_dir", "next_nudge_seq", "note_activity", "note_save",
           "note_seq_seen", "note_write", "nudges_today", "observe_marker", "pending_writes",
           "read_db_state", "read_pending_count",
           "read_save_marker", "record_nudge", "session_key", "suppressed", "tenant"]
