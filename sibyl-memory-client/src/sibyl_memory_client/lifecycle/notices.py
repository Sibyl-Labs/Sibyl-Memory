"""Per-process lifecycle notices for long-running surfaces (the MCP server,
the Hermes adapter). At most ONE line per call; the caller attaches it only to
a SUCCESSFUL result and only when the account gate had nothing to say (account
notices take precedence).

Order after the account notice (R1, plan 3.2): the tier-change line, then on
Pro the automatic-save request (``autosave_due``: every interval of ACTIVE
time, first at 5 writes or a compaction, capped per session and per day), the
process-start trim report, the unsaved-last-session line, then the skill
proposals line; on the free tier the unsaved-last-session line, the save-due
reminder and the debloat reminder.

Active time is counted in this process (``note_activity``: every tool call the
server handles, every Hermes turn; a gap longer than five minutes counts as
five) and written to ``lifecycle.json`` when a nudge is recorded. A save made
anywhere (the DB marker, shared by every agent on this home) restarts the
clock. Nothing new leaves the machine.

On Pro the process start also runs the local-deterministic learner (skill
proposals) after the debloat plan, in the same daemon thread.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import proposals as PR
from . import reminders as R
from . import state as ST
from .entitled import automation_allowed
from .runner import measure_for_reminder, run_automatic
from .state import LifecycleState, log, pending_writes, tenant


# The skill line is looked for at most this often until it is given (the
# learner may still be running when the first results go out).
SKILLS_RECHECK_SECONDS = 120


class ProcessNotices:
    def __init__(self, harness: str, *, session_id: Any = None) -> None:
        self.harness = harness
        self._lock = threading.Lock()
        self._started = False
        self._thread: threading.Thread | None = None
        self._report: str | None = None
        self._report_given = False
        self._unsaved_checked = False
        self._debloat_checked = False
        self._skills_checked = False
        self._tier_checked = False
        self.run_fn: Callable[..., Any] = run_automatic
        self.learn_fn: Callable[..., Any] = PR.run_learner
        # Active-time accounting for this session (one process = one session
        # for MCP hosts; Hermes passes its session id).
        self.started_at = time.time()
        self.session_key = ST.session_key(session_id if session_id is not None else
                                          f"{harness}:{os.getpid()}:{self.started_at!r}")
        self._active = 0.0
        self._last_event: float | None = None
        self._active_at_save = 0.0
        self._seen_save_at: float | None = None
        self._nudges: list[float] = []
        self._last_nudge_active: float | None = None
        self._skills_next_check = 0.0

    # -- process start ------------------------------------------------------
    def start(self, client: Any, *, home: Path | str | None = None, cwd: Path | str | None = None,
              background: bool = True) -> None:
        """Once per process: on Pro, run the debloat plan (and the policy's
        apply) and then the learner in a daemon thread; the one-line report is
        given out later by ``next_notice``. A free or locked device does
        nothing here."""
        with self._lock:
            if self._started:
                return
        ok, _why = automation_allowed(client)
        if not ok:
            return            # a later client (after sign-in or an upgrade) may still start it
        with self._lock:
            if self._started:
                return
            self._started = True
        home_p = Path(home) if home is not None else Path.home()
        cwd_p = Path(cwd) if cwd is not None else Path(os.getcwd())

        def _run() -> None:
            try:
                rep = self.run_fn(client, home=home_p, cwd=cwd_p, harness=self.harness)
                line = getattr(rep, "line", None)
                if line:
                    with self._lock:
                        self._report = line
            except Exception as e:  # never reaches the agent
                log(f"lifecycle start {self.harness}: {type(e).__name__}", home_p)
            try:
                self.learn_fn(client, home=home_p)
            except Exception as e:
                log(f"learner {self.harness}: {type(e).__name__}", home_p)

        if background:
            t = threading.Thread(target=_run, daemon=True, name="sibyl-lifecycle-start")
            self._thread = t
            t.start()
        else:
            _run()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def take_report(self) -> str | None:
        with self._lock:
            if self._report_given or not self._report:
                return None
            self._report_given = True
            return self._report

    # -- active time ------------------------------------------------------------
    def note_activity(self, now: float | None = None) -> None:
        """One event of this session (a tool call, a Hermes turn). Cheap: two
        numbers in memory, no file, no network."""
        now = time.time() if now is None else now
        with self._lock:
            last = self._last_event
            if last is not None and now > last:
                self._active += min(now - last, float(ST.IDLE_CAP_SECONDS))
            if last is None or now > last:
                self._last_event = now

    @property
    def active_seconds(self) -> float:
        return self._active

    @property
    def nudges(self) -> int:
        return len(self._nudges)

    def _session_view(self, st: dict[str, Any], tid: str) -> dict[str, Any]:
        s = ST.ensure_session(st, self.session_key, tenant_id=tid, harness=self.harness,
                              now=self.started_at)
        s.update(active_seconds=self._active, last_event_at=self._last_event,
                 active_at_save=self._active_at_save, nudges=list(self._nudges),
                 last_nudge_active=self._last_nudge_active, nudged=bool(self._nudges))
        if self._seen_save_at is not None and self._seen_save_at >= self.started_at:
            s["saved"] = True
        return s

    def _observe_saves(self, st: dict[str, Any], tid: str, client: Any, home_p: Path) -> None:
        """Fold the DB save marker in (any agent's save restarts this session's
        clock)."""
        marker = None
        db = PR.db_path_of(client)
        if db is not None:
            marker = ST.read_save_marker(db, tid)
        t = tenant(st, tid)
        last = t.get("last_save_at")
        if marker is not None and (last is None or marker > last):
            LifecycleState(home_p).update(lambda s: ST.observe_marker(s, tid, marker))
            ST.observe_marker(st, tid, marker)
            last = marker
        if isinstance(last, (int, float)) and (self._seen_save_at is None or last > self._seen_save_at):
            if self._seen_save_at is not None or last >= self.started_at:
                self._active_at_save = self._active
            self._seen_save_at = last

    # -- per result -----------------------------------------------------------
    def next_notice(self, client: Any, *, home: Path | str | None = None,
                    cwd: Path | str | None = None, now: float | None = None,
                    allow_save: bool = True) -> str | None:
        """One line or None. Never raises. ``allow_save=False`` leaves the
        automatic-save request to another channel (the MCP server inside
        Claude Code, where the Stop hook carries it)."""
        try:
            return self._next(client, home, cwd, now, allow_save)
        except Exception:
            return None

    def _next(self, client: Any, home: Any, cwd: Any, now: float | None, allow_save: bool) -> str | None:
        home_p = Path(home) if home is not None else Path.home()
        cwd_p = Path(cwd) if cwd is not None else Path(os.getcwd())
        now = time.time() if now is None else now
        store = LifecycleState(home_p)
        snapshot = store.load()
        if not R.enabled(snapshot):
            return None
        pro, tier = automation_allowed(client)
        tid = client.get_tenant()
        if not self._tier_checked:
            tier_class = "paid" if pro else ("free" if tier == "free" else None)
            if tier_class is not None:
                self._tier_checked = True
                line = store.update(lambda st: R.tier_change_line(st, tid, tier_class))
                if line:
                    return line
        if pro:
            if allow_save:
                line = self._autosave_line(client, store, snapshot, tid, home_p, now)
                if line:
                    return line
            if R.reports_enabled(snapshot):
                rep = self.take_report()
                if rep:
                    return rep
        # Unsaved last session: first result of the process, both tiers.
        if not self._unsaved_checked:
            self._unsaved_checked = True
            line = store.update(lambda st: R.unsaved_session_line(st, tid, now))
            if line:
                return line
        if pro:
            return self._skills_line(client, store, tid, now)
        extra = pending_writes(tid, home=home_p)

        def _save(st: dict[str, Any]) -> str | None:
            t = tenant(st, tid)
            if extra:
                t["writes_since_save"] = int(t.get("writes_since_save") or 0) + extra
            line = R.save_reminder(st, tid, now, pro=False, sk=self.session_key)
            if extra:
                t["writes_since_save"] = max(0, int(t.get("writes_since_save") or 0) - extra)
            return line

        probe = LifecycleState(home_p).load()
        if _save(probe):
            line = store.update(_save)
            if line:
                return line
        if not self._debloat_checked:
            self._debloat_checked = True
            rec = measure_for_reminder(home_p, cwd_p)
            if rec is not None:
                line = store.update(lambda st: R.debloat_reminder(st, tid, now, rec, pro=False))
                if line:
                    return line
        return None

    def _autosave_line(self, client: Any, store: LifecycleState, snapshot: dict[str, Any], tid: str,
                       home_p: Path, now: float) -> str | None:
        self._observe_saves(snapshot, tid, client, home_p)
        extra = pending_writes(tid, home=home_p)
        self._session_view(snapshot, tid)
        reason = R.autosave_due(snapshot, tid, self.session_key, now, extra_writes=extra)
        if reason is None:
            return None
        out: dict[str, Any] = {}

        def _rec(st: dict[str, Any]) -> None:
            self._session_view(st, tid)
            why = R.autosave_due(st, tid, self.session_key, now, extra_writes=extra)
            if why is None:
                return            # the state moved meanwhile (another process saved)
            ST.record_nudge(st, tid, self.session_key, now, harness=self.harness)
            writes = int(tenant(st, tid).get("writes_since_save") or 0) + extra
            out["line"] = R.autosave_notice(st, why, harness=self.harness, writes=writes)
            out["why"] = why
        store.update(_rec)
        if out.get("line"):
            with self._lock:
                self._nudges.append(now)
                self._last_nudge_active = self._active
            log(f"autosave {self.harness}: requested ({out['why']})", home_p)
        return out.get("line")

    def _skills_line(self, client: Any, store: LifecycleState, tid: str, now: float) -> str | None:
        if self._skills_checked or now < self._skills_next_check:
            return None
        self._skills_next_check = now + SKILLS_RECHECK_SECONDS
        db = PR.db_path_of(client)
        if db is None:
            return None
        st0 = store.load()
        last = (st0.get("skills") or {}).get("last_prompt_at")
        if isinstance(last, (int, float)) and 0 <= now - last < PR.PROMPT_THROTTLE_SECONDS:
            self._skills_checked = True      # prompted in the last 24 h: not this session
            return None
        anchor = PR.last_session_start(st0, tid, exclude=self.session_key, before=self.started_at)
        names, total = PR.read_relevant(db, tid, now, anchor=anchor, muted=PR.muted_kinds(st0, now))
        if total <= 0:
            return None          # looked at again on a later result (the learner may still run)
        self._skills_checked = True

        def _p(st: dict[str, Any]) -> str | None:
            ST.ensure_session(st, self.session_key, tenant_id=tid, harness=self.harness,
                              now=self.started_at)
            return PR.skill_prompt(st, tid, self.session_key, now, names, total)
        return store.update(_p)


__all__ = ["ProcessNotices"]
