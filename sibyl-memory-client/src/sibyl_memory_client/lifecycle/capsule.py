"""The session capsule (Pro, mechanical, tiny; spec 5.5).

One COLD journal event per session end or compaction::

    write_event(acted={"kind": "session_capsule", "body": {"harness", "session_id" (hashed),
                "started_at", "ended_at", "turns", "memory_writes", "saved_by_skill",
                "cwd_name"}}, extra={"category": "session"})

No conversation text, no file names, no path beyond the cwd basename, no text
field longer than 64 characters. Written only when ``automation_allowed``;
the write itself goes through the gated client (the gate applies twice) and
is not counted as a user write.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .entitled import automation_allowed
from .state import suppressed

MAX_TEXT = 64


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _short(v: Any) -> str:
    return str(v or "")[:MAX_TEXT]


def build_capsule(*, harness: str, session_id: Any, started_at: float | None,
                  ended_at: float | None, turns: int | None, memory_writes: int,
                  saved_by_skill: bool, cwd: str | Path | None) -> dict[str, Any]:
    sid = hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:16]
    name = Path(str(cwd)).name if cwd else ""
    return {
        "harness": _short(harness),
        "session_id": sid,
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "turns": int(turns) if isinstance(turns, int) and turns >= 0 else None,
        "memory_writes": max(0, int(memory_writes or 0)),
        "saved_by_skill": bool(saved_by_skill),
        "cwd_name": _short(name),
    }


def write_capsule(client: Any, capsule: dict[str, Any]) -> str | None:
    """Write the capsule when this device may automate; returns the event id
    or None (free tier, locked, or a refused write)."""
    ok, _why = automation_allowed(client)
    if not ok:
        return None
    try:
        with suppressed():
            return client.write_event(acted={"kind": "session_capsule", "body": capsule},
                                      extra={"category": "session"})
    except Exception:
        return None


__all__ = ["build_capsule", "write_capsule"]
