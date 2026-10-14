"""Agent handoff block (spec agent-handoff-messages 2026-10-03, section 1).

Every stop of a ``sibyl`` command ends with this block, printed to stdout in
both interactive and non-interactive runs, after the status line:

    later: <command>            zero or more; optional follow-ups
    ask your human: <sentence>  zero or one; only when a person must act
    next: <command>             exactly one; always the last line

A wait (the process is still running and watching) prints, once, when the
watching starts:

    ask your human: <sentence>
    waiting: <what is watched> (up to <m:ss>)

Plain text at column 0: lowercase key, one colon, one space, no glyph, no
colour, no indentation, even on a TTY. ``next`` is a shell command, ``done``,
``retry`` or ``retry in <N> minutes``. Placeholders (``<code>``,
``<address>``, ``<index>``, ``<cs_code>``, ``<file>``) appear only when the
``ask your human:`` line names them.

With ``--json`` the block is never printed; ``json_handoff`` adds the same two
values to the document as ``next`` and ``human_action``.
"""
from __future__ import annotations

from typing import Any, Iterable

DONE = "done"
RETRY = "retry"


def retry_in(minutes: Any) -> str:
    return f"retry in {minutes} minutes"


def block(next: str, *, human: str | None = None, later: Iterable[str] = ()) -> str:
    lines = [f"later: {c}" for c in later if c]
    if human:
        lines.append(f"ask your human: {human}")
    lines.append(f"next: {next}")
    return "\n".join(lines)


def handoff(next: str, *, human: str | None = None, later: Iterable[str] = ()) -> None:
    """Print the handoff block (the last thing a stopping command prints)."""
    print(block(next, human=human, later=later), flush=True)


def waiting(what: str, seconds: Any, *, human: str | None = None) -> None:
    """Print the wait lines once, when the watching starts."""
    try:
        secs = max(0, int(seconds))
    except (TypeError, ValueError):
        secs = 0
    if human:
        print(f"ask your human: {human}", flush=True)
    print(f"waiting: {what} (up to {secs // 60}:{secs % 60:02d})", flush=True)


def json_handoff(payload: dict, next: str, human: str | None = None) -> dict:
    """Add the two handoff keys to a ``--json`` document."""
    payload["next"] = next
    payload["human_action"] = human
    return payload


def from_exception(e: BaseException, *, default: str = "sibyl status") -> tuple[str, str | None]:
    """(next, human) carried by a client exception (rows C1 to C17), or the
    default when the exception predates the convention."""
    nxt = getattr(e, "next", None)
    if not isinstance(nxt, str) or not nxt:
        return default, None
    human = getattr(e, "human_action", None)
    return nxt, human if isinstance(human, str) and human else None
