"""Reminder engine (spec plugin-save-debloat 4.2). Pure functions over the
``lifecycle.json`` dict: they decide, format one line, and record the throttle
timestamps; the caller persists the state and delivers the line on its
channel (MCP ``notice``, a Claude Code hook's additionalContext, a Hermes log
line or prefetch line, ``sibyl status``).

Hard limits: one line; never a token; never file content (the only file
data is a label and a token count); ``reminders: false`` silences everything;
on Pro the reminders are replaced by one-line reports of what ran (``quiet``
silences those too). The "on Pro" clause appears at most once per 7 days per
tenant.
"""
from __future__ import annotations

from typing import Any

from . import state as _st
from .state import tenant

SAVE_WRITES = 25
SAVE_MIN_WRITES = 5
SAVE_STALE_SECONDS = 6 * 3600
SAVE_REMINDER_INTERVAL = 6 * 3600
DEBLOAT_REMINDER_INTERVAL = 7 * 86400
PRO_LINE_INTERVAL = 7 * 86400
UNSAVED_SESSION_WRITES = 5
UNSAVED_SESSION_AGE = 24 * 3600


def _n(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def enabled(st: dict[str, Any]) -> bool:
    return st.get("reminders") is not False


def reports_enabled(st: dict[str, Any]) -> bool:
    return enabled(st) and st.get("quiet") is not True


def _pro_clause(t: dict[str, Any], now: float, text: str) -> str:
    last = _n(t.get("pro_line_shown_at"))
    if last is None or now - last >= PRO_LINE_INTERVAL or now < last:
        t["pro_line_shown_at"] = now
        return " " + text
    return ""


def save_due(t: dict[str, Any], now: float, s: dict[str, Any] | None = None) -> bool:
    """Free save-due rule. ``s`` is the asking session's record: its own
    compaction counts, another session's never does."""
    w = int(t.get("writes_since_save") or 0)
    if isinstance(s, dict) and s.get("compaction_seen") and w > 0:
        return True
    if w >= SAVE_WRITES:
        return True
    if w >= SAVE_MIN_WRITES:
        since = _n(t.get("since_save_started_at"))
        if since is not None and now - since >= SAVE_STALE_SECONDS:
            return True
    return False


def save_reminder(st: dict[str, Any], tid: str, now: float, *, pro: bool,
                  sk: str | None = None) -> str | None:
    """The free save-due line, or None. Pro gets no reminder (the automation
    acts; a report follows instead). ``sk``: the asking session (its own
    compaction makes a save due)."""
    if not enabled(st) or pro:
        return None
    t = tenant(st, tid)
    s = st.get("sessions", {}).get(sk) if sk else None
    if not save_due(t, now, s if isinstance(s, dict) else None):
        return None
    last = _n(t.get("last_save_reminder_at"))
    if last is not None and 0 <= now - last < SAVE_REMINDER_INTERVAL:
        return None
    t["last_save_reminder_at"] = now
    w = int(t.get("writes_since_save") or 0)
    head = (f"Sibyl Memory: {w} writes since the last save" if w else
            "Sibyl Memory: the conversation was compacted since the last save")
    return (head + ". Run /sibyl-save (or the save prompt) before you stop."
            + _pro_clause(t, now, "Automatic save is on Pro."))


def debloat_due_files(record: dict[str, Any]) -> list[str]:
    heavy = [f["file"] for f in record.get("files", []) if f.get("heavy")]
    if heavy:
        return heavy
    if record.get("weight") in ("watch", "heavy"):
        return [t["file"] for t in record.get("targets", [])] or ["(surface)"]
    return []


def debloat_reminder(st: dict[str, Any], tid: str, now: float, record: dict[str, Any], *,
                     pro: bool) -> str | None:
    """The free debloat line (``context: ~48.3k tok always-load (watch) |
    heavy: AGENTS.md 10223t. Run /sibyl-debloat to trim. Automatic on Pro.``)
    once per 7 days per file, or None."""
    if not enabled(st) or pro:
        return None
    files = debloat_due_files(record)
    if not files:
        return None
    t = tenant(st, tid)
    seen = t.setdefault("last_debloat_reminder_at", {})
    due = [f for f in files if _n(seen.get(f)) is None or not (0 <= now - seen[f] < DEBLOAT_REMINDER_INTERVAL)]
    if not due:
        return None
    for f in files:
        seen[f] = now
    from .measure import fyi
    return (fyi(record, hint="") + ". Run /sibyl-debloat to trim."
            + _pro_clause(t, now, "Automatic on Pro."))


def unsaved_session_line(st: dict[str, Any], tid: str, now: float, *,
                         exclude_session: str | None = None) -> str | None:
    """"your last session (Claude Code, 2 h, 31 writes) was not saved", once."""
    if not enabled(st):
        return None
    t = tenant(st, tid)
    best = None
    for sk, s in st.get("sessions", {}).items():
        if sk == exclude_session or s.get("tenant") != tid or s.get("reported") or s.get("saved"):
            continue
        started = _n(s.get("started_at"))
        ended = _n(s.get("ended_at"))
        if started is None:
            continue
        if ended is None and now - started < UNSAVED_SESSION_AGE:
            continue
        writes = int(s.get("writes") or 0)
        if writes == 0:
            writes = max(0, int(t.get("writes_total") or 0) - int(s.get("writes_at_start") or 0)) \
                if ended is None else 0
        if writes < UNSAVED_SESSION_WRITES:
            continue
        last_save = _n(t.get("last_save_at"))
        if last_save is not None and last_save >= started:
            continue
        if best is None or started > (_n(best[1].get("started_at")) or 0):
            best = (sk, s, writes)
    if best is None:
        return None
    sk, s, writes = best
    s["reported"] = True
    dur = ""
    started, ended = _n(s.get("started_at")), _n(s.get("ended_at"))
    if started is not None and ended is not None and ended >= started:
        hours = (ended - started) / 3600
        dur = f"{hours:.0f} h, " if hours >= 1 else f"{max(1, int((ended - started) / 60))} min, "
    harness = {"claude-code": "Claude Code", "hermes": "Hermes", "codex": "Codex",
               "mcp": "an MCP host"}.get(s.get("harness") or "", s.get("harness") or "an agent")
    return (f"Sibyl Memory: your last session ({harness}, {dur}{writes} writes) was not saved. "
            "Run /sibyl-save now to save what is still worth keeping.")


# ----------------------------------------------------------------------
# Pro automatic save by active time (R1, plan 2.2; operator D6)
# ----------------------------------------------------------------------

FIRST_SAVE_WRITES = 5


def autosave_due(st: dict[str, Any], tid: str, sk: str, now: float, *,
                 extra_writes: int = 0) -> str | None:
    """Why an automatic save is due for session ``sk`` now (``"interval"``,
    ``"first"``, ``"compaction"``), or None. Pure: reads the state, changes
    nothing (the caller records the nudge with ``state.record_nudge``).

    Due when ALL hold: something to save (``writes_since_save >= 1``); the
    session's ACTIVE time since the last save reached the interval, OR the
    first-save rule (5 writes and no save or nudge yet in this session), OR
    THIS session was compacted since the last save (another session's
    compaction does not count; the writes are tenant-wide, plan D6.5); no nudge in the last interval of
    active time; under the per-session and per-day caps; the automatic save
    and the reminders are on. The caller has already checked
    ``automation_allowed`` (verified paid entitlement): nothing here can make
    a free tier automate."""
    if not enabled(st):
        return None
    cfg = _st.autosave_settings(st)
    if not cfg["enabled"]:
        return None
    t = tenant(st, tid)
    s = st.get("sessions", {}).get(sk)
    if not isinstance(s, dict):
        return None
    writes = int(t.get("writes_since_save") or 0) + max(0, int(extra_writes or 0))
    if writes < 1:
        return None
    nudges = s.get("nudges") or []
    if len(nudges) >= cfg["max_per_session"]:
        return None
    if _st.nudges_today(t, now) >= cfg["max_per_day"]:
        return None
    interval = float(cfg["interval_seconds"])
    active = float(s.get("active_seconds") or 0.0)
    last_nudge = _n(s.get("last_nudge_active"))
    if nudges and last_nudge is not None and active - last_nudge < interval:
        return None
    if active - float(s.get("active_at_save") or 0.0) >= interval:
        return "interval"
    if s.get("compaction_seen"):
        return "compaction"
    if (writes >= FIRST_SAVE_WRITES and not nudges and not s.get("seq_floor")
            and not s.get("saved")):
        return "first"
    return None


def _minutes(st: dict[str, Any]) -> int:
    return int(_st.autosave_settings(st)["interval_seconds"] // 60)


def autosave_notice(st: dict[str, Any], reason: str, *, harness: str, writes: int = 0) -> str:
    """The one line an MCP host (Cursor, Codex, any) or Hermes gets when a save
    is due. The Claude Code Stop hook uses its own continuation reason."""
    if reason == "interval":
        why = f"about {_minutes(st)} minutes of work since the last save"
    elif reason == "compaction":
        why = "the conversation was compacted since the last save"
    else:
        why = f"{writes} memory writes since the last save"
    if harness == "hermes":
        how = "Run the save procedure now in headless mode (~/.sibyl-memory/skills/SAVE.md)"
    else:
        how = ("Run the save procedure now in headless mode (the `save` prompt of the "
               "sibyl-memory server with mode headless, or ~/.sibyl-memory/skills/SAVE.md)")
    return f"Sibyl Memory Pro: {why}. {how}, then continue."


def pending_line(count: int) -> str | None:
    if count <= 0:
        return None
    items = "1 item" if count == 1 else f"{count} items"
    return (f"Sibyl Memory: last session left {items} to decide (candidate skills or ambiguous "
            "entities); say /sibyl-save curate to go through them.")


def skills_line(names: list[str], total: int) -> str | None:
    """The Pro skill-proposal line: at most 3 named (plan 3.2)."""
    if total <= 0 or not names:
        return None
    shown = ", ".join(names[:3])
    more = f" and {total - min(3, len(names))} more" if total > min(3, len(names)) else ""
    what = "1 candidate skill" if total == 1 else f"{total} candidate skills"
    return (f"Sibyl Memory: {what} from your recent sessions ({shown}{more}). "
            "Say /sibyl-save curate to review, or run sibyl skills proposals.")


TIER_UP_LINE = ("Sibyl Memory: this computer is now on Pro. Automatic save and debloat are on; "
                "run sibyl status to see them.")
TIER_DOWN_LINE = ("Sibyl Memory: this account is back on Free. Automatic save and debloat are off; "
                  "the 5 MB cap applies to new writes; nothing was deleted.")


def tier_change_line(st: dict[str, Any], tid: str, tier_class: str | None) -> str | None:
    """One line when the verified tier class (``paid`` / ``free``) differs from
    the one this home last saw; records the new class. The first sighting says
    nothing."""
    if tier_class not in ("paid", "free"):
        return None
    t = tenant(st, tid)
    prev = t.get("tier_seen")
    t["tier_seen"] = tier_class
    if prev is None or prev == tier_class or not enabled(st):
        return None
    return TIER_UP_LINE if tier_class == "paid" else TIER_DOWN_LINE


def saved_report(entities: int | None = None, events: int | None = None) -> str:
    if entities is None and events is None:
        return "Sibyl Memory saved this session."
    parts = []
    if entities is not None:
        parts.append(f"{entities} entit{'y' if entities == 1 else 'ies'}")
    if events is not None:
        parts.append(f"{events} journal event{'' if events == 1 else 's'}")
    return f"Sibyl Memory saved this session ({', '.join(parts)})."


__all__ = ["DEBLOAT_REMINDER_INTERVAL", "FIRST_SAVE_WRITES", "PRO_LINE_INTERVAL", "SAVE_MIN_WRITES",
           "SAVE_REMINDER_INTERVAL", "SAVE_STALE_SECONDS", "SAVE_WRITES", "TIER_DOWN_LINE",
           "TIER_UP_LINE", "autosave_due", "autosave_notice", "debloat_due_files",
           "debloat_reminder", "enabled", "pending_line", "reports_enabled", "save_due",
           "save_reminder", "saved_report", "skills_line", "tier_change_line",
           "unsaved_session_line"]
