"""Skill proposals on Pro (R1, plan section 3 with the four existing detectors;
operator D7).

* ``run_learner``: the local-deterministic learner, run by the Pro process-start
  thread (MCP server, Hermes adapter) and at Hermes shutdown. Only when
  ``automation_allowed`` (verified paid entitlement); inside ``suppressed()``
  (its writes are the plugin's, not the person's); no network (the
  local-deterministic summarizer is the only one this release reaches); never
  inside a Claude Code hook.
* ``read_relevant``: the proposals worth naming, read-only from ``memory.db``
  (``mode=ro``, no gate, no network), so a hook may call it.
* ``skill_prompt``: the throttle (once per session, not within 24 h of the last
  skill prompt) around ``reminders.skills_line``.

Relevant (plan 3.1 / 3.5, deterministic): ``status = pending``, confidence at
least 0.6, at least 3 evidence events, at least 2 of them in the last 7 days,
at least one newer than the start of the person's most recent session (the
pattern recurred in the last session), evidence from at least two sessions
(two events at least 30 minutes apart), kind not muted. Nothing is created
without an explicit accept; the free tier sees none of it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import reminders as R
from . import state as ST
from .entitled import automation_allowed

CONFIDENCE_FLOOR = 0.6
MIN_EVIDENCE = 3
RECENT_DAYS = 7
MIN_RECENT = 2
SESSION_GAP_SECONDS = 30 * 60
NAMED_MAX = 3
PROMPT_THROTTLE_SECONDS = 24 * 3600
COOL_OFF_DAYS = 30
MUTE_DAYS_DEFAULT = 30
KINDS = ("repeated_action", "structural_similarity", "temporal_routine", "co_occurrence")


def muted_kinds(st: dict[str, Any], now: float) -> set[str]:
    m = (st.get("skills") or {}).get("muted_kinds") or {}
    return {k for k, until in m.items() if isinstance(until, (int, float)) and until > now}


def unmuted_slugs(st: dict[str, Any]) -> dict[str, float]:
    return dict((st.get("skills") or {}).get("unmuted") or {})


def last_session_start(st: dict[str, Any], tid: str, *, exclude: str | None = None,
                       before: float | None = None) -> float | None:
    """Start of the tenant's most recent session other than ``exclude`` that
    began before ``before``."""
    best = None
    for sk, s in (st.get("sessions") or {}).items():
        if sk == exclude or s.get("tenant") != tid:
            continue
        started = s.get("started_at")
        if not isinstance(started, (int, float)):
            continue
        if before is not None and started >= before:
            continue
        best = started if best is None or started > best else best
    return best


def is_relevant(row: dict[str, Any], now: float, *, anchor: float | None,
                muted: set[str] = frozenset()) -> bool:
    if row.get("status") != "pending" or row.get("pattern_kind") in muted:
        return False
    try:
        if float(row.get("confidence") or 0.0) < CONFIDENCE_FLOOR:
            return False
    except (TypeError, ValueError):
        return False
    ev = row.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            ev = None
    if not isinstance(ev, list) or len(ev) < MIN_EVIDENCE:
        return False
    ts = sorted(t for t in (ST._iso_to_epoch(e.get("ts")) for e in ev if isinstance(e, dict))
                if t is not None)
    if len(ts) < MIN_EVIDENCE:
        return False
    if sum(1 for t in ts if 0 <= now - t <= RECENT_DAYS * 86400) < MIN_RECENT:
        return False
    since = anchor if anchor is not None else now - RECENT_DAYS * 86400
    if not any(t >= since for t in ts):
        return False
    return ts[-1] - ts[0] >= SESSION_GAP_SECONDS


def read_relevant(db_path: Path | str, tid: str, now: float, *, anchor: float | None,
                  muted: set[str] = frozenset(), limit: int = 200) -> tuple[list[str], int]:
    """(slugs to name, most confident first; how many are relevant). Read-only."""
    p = Path(db_path).expanduser()
    try:
        if not p.is_file() or p.is_symlink():
            return [], 0
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT pattern_kind, proposed_slug, evidence, confidence, status "
                "FROM skill_proposals WHERE tenant_id = ? AND status = 'pending' "
                "ORDER BY confidence DESC, created_at DESC LIMIT ?", (tid, limit)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return [], 0
    names = [r["proposed_slug"] for r in rows
             if is_relevant(dict(r), now, anchor=anchor, muted=muted)]
    return names[:NAMED_MAX], len(names)


def skill_prompt(st: dict[str, Any], tid: str, sk: str | None, now: float,
                 names: list[str], total: int) -> str | None:
    """The skill line, at most once per session and not within 24 h of the
    last one; records the throttle. ``st`` is persisted by the caller."""
    if not R.enabled(st) or total <= 0:
        return None
    sk_state = (st.get("skills") or {})
    last = sk_state.get("last_prompt_at")
    if isinstance(last, (int, float)) and 0 <= now - last < PROMPT_THROTTLE_SECONDS:
        return None
    s = (st.get("sessions") or {}).get(sk) if sk else None
    if isinstance(s, dict) and s.get("skills_prompted"):
        return None
    line = R.skills_line(names, total)
    if not line:
        return None
    st.setdefault("skills", ST.default_skills())["last_prompt_at"] = now
    if isinstance(s, dict):
        s["skills_prompted"] = True
    return line


def db_path_of(client: Any) -> Path | None:
    p = getattr(getattr(client, "storage", None), "db_path", None)
    return Path(p) if p is not None else None


def run_learner(client: Any, *, home: Path | str | None = None, now: float | None = None) -> Any:
    """One local-deterministic learner pass on Pro (never raises; None when not
    entitled or on error). Muted kinds are skipped, rejected slugs cool off
    (the learner's own rule), and ``sibyl skills proposals unmute`` is honoured."""
    ok, _why = automation_allowed(client)
    if not ok:
        return None
    home_p = Path(home) if home is not None else Path.home()
    now = time.time() if now is None else now
    try:
        dec = client.entitlement_gate.peek()
        if getattr(getattr(dec, "entitlement", None), "mem", None) != "full":
            return None      # read-only or paused by policy: the learner writes nothing
        st = ST.LifecycleState(home_p).load()
        with ST.suppressed():
            learner = client.learner()
            return learner.run(exclude_kinds=muted_kinds(st, now), unmuted=unmuted_slugs(st))
    except Exception as e:
        ST.log(f"learner: {type(e).__name__}", home_p)
        return None


__all__ = ["CONFIDENCE_FLOOR", "COOL_OFF_DAYS", "KINDS", "MUTE_DAYS_DEFAULT", "NAMED_MAX",
           "PROMPT_THROTTLE_SECONDS", "db_path_of", "is_relevant", "last_session_start",
           "muted_kinds", "read_relevant", "run_learner", "skill_prompt", "unmuted_slugs"]
