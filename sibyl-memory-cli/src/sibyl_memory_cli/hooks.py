"""`sibyl hook <event>`: the Claude Code hooks (spec plugin-save-debloat 5.2).

Installed by `sibyl setup` into ~/.claude/settings.json; not for manual use.
Each call reads the hook JSON from stdin (bounded), always exits 0, prints at
most one JSON object on stdout, and logs one short line (event and outcome,
never content) to ~/.sibyl-memory/lifecycle.log. A failure never blocks a
session: it degrades to printing nothing.

Output contract, verified against Claude Code 2.1.283's hook schemas (the
binary's zod definitions; see the release notes):

* SessionStart / UserPromptSubmit add context with
  ``{"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "<text>"}}``;
* Stop continues the session with ``{"decision": "block", "reason": "<text>"}``
  (``decision`` is ``"approve" | "block"``); the re-entry carries
  ``stop_hook_active: true`` and gets nothing.

Free tier: reminders only (the debloat FYI and the unsaved-last-session line
at start, the save-due line on a prompt). Pro (a verified fresh/stale
entitlement whose tier is paid, never a file hint): the start runs the debloat
plan under the person's policy and reports one line, and names up to three
relevant skill proposals (read-only, at most once a day); Stop asks the model
to run the save procedure headless by ACTIVE time (R1, plan 2.2): once early
(the first turn end after 5 memory writes, or after a compaction of THIS
session; the write count is the account's, plan D6.5), then after
every interval of active work (default 60 minutes; `sibyl autosave`), never
more than 8 times a session or 12 a day, and only when there is something to
save (Stop fires at every turn end, not only at the end of a session). Active
time: every SessionStart, UserPromptSubmit, Stop and PreCompact adds the time
since the previous event, capped at 5 minutes, except the model's own turn
(from a prompt to its Stop), capped at 30 minutes. A scripted `claude -p` run is
never continued (no transcript, or fewer than two prompts in the session).
Each continuation carries its sequence number ("save 2 of this session"), so
the transcript guard applies per request; a request is recorded (per-session
and per-day caps, the interval) only when it is actually emitted. PreCompact
and SessionEnd write the session capsule.

Time bound (Claude Code kills a hook at its timeout, 5 s): nothing in a hook
touches the network. The Pro client's entitlement gate never refreshes, its
usage heartbeat is off (the exit flush would wait on the network), and a
memory write (a trim's history, a capsule) happens only when the cap gate can
decide it locally (a fresh tier cache) and within ``WRITE_BUDGET_SECONDS`` of
the hook's start; otherwise the start only plans and the write waits for a
later start or the MCP server. Measured: see the release notes.

Fast path: this module imports only the standard library. A hook first reads
the payload, the credentials and the UNVERIFIED tier claim in
entitlement.json; when that claim is not a paid tier the device cannot be Pro
(the verified tier is that same claim), so Stop returns at once and
PreCompact / SessionEnd record their counters without verifying the token or
opening the memory database. A claim that says paid is always verified.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MAX_STDIN_BYTES = 1024 * 1024
MAX_TRANSCRIPT_SCAN = 64 * 1024 * 1024
STOP_REASON_BASE = ("Sibyl Memory Pro auto-save: read ~/.sibyl-memory/skills/SAVE.md and run it in "
                    "headless mode now (ask nothing, skip skill extraction, queue decisions)")
# A scripted run (`claude -p`) has one prompt: never continue a session with
# fewer prompts than this.
MIN_PROMPTS_FOR_STOP = 2


def stop_reason(seq: int) -> str:
    """The Stop continuation for the ``seq``-th automatic save of a session."""
    return f"{STOP_REASON_BASE} (save {int(seq)} of this session). Then stop."


STOP_REASON = stop_reason(1)
EVENTS = {"session-start": "SessionStart", "prompt": "UserPromptSubmit", "stop": "Stop",
          "pre-compact": "PreCompact", "session-end": "SessionEnd"}
# A memory write starts only this long after the hook started (the harness
# kills the hook at 5 s; the write itself is local and takes milliseconds).
WRITE_BUDGET_SECONDS = 2.5
# Mirrors sibyl_memory_client.tiers.PAID_TIERS (kept literal so the fast path
# imports nothing); test_f11_paid_tier_literal_matches_the_client asserts
# equality; edit both. Canonical paid tiers pro + elite, then the legacy aliases.
PAID_TIERS = frozenset({"pro", "elite", "sync", "team", "lifetime", "stake", "enterprise"})
ENTITLEMENT_FILENAME = "entitlement.json"


def _paths(home: Path | None) -> tuple[Path, Path, Path]:
    home = home or Path.home()
    cred = Path(os.environ.get("SIBYL_CREDENTIALS") or (home / ".sibyl-memory" / "credentials.json"))
    db = Path(os.environ.get("SIBYL_MEMORY_DB") or (home / ".sibyl-memory" / "memory.db"))
    return home, cred.expanduser(), db.expanduser()


def _read_creds(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _transcript_has(path: Any, needle: str) -> bool:
    """True when the session transcript already carries ``needle`` (our
    continuation reason with its sequence number): a second guard beside
    lifecycle.json, so each numbered request is made at most once even if that
    file was rewritten."""
    if not isinstance(path, str) or not path:
        return False
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return False
        b = needle.encode("utf-8")
        seen = 0
        tail = b""
        with open(p, "rb") as fh:
            while seen < MAX_TRANSCRIPT_SCAN:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    return False
                seen += len(chunk)
                if b in tail + chunk:
                    return True
                tail = chunk[-len(b):]
    except OSError:
        return False
    return False


def _context(event_name: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name,
                                              "additionalContext": "\n".join(lines)}})


def claim_label(cred: Path) -> str | None:
    """The UNVERIFIED tier label of the entitlement next to ``cred``, stripped
    and lowercased; None when there is no readable SE1 token or no string
    label. Standard library only (no signature check, no client import)."""
    try:
        p = Path(cred).parent / ENTITLEMENT_FILENAME
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 64 * 1024:
            return None
        token = json.loads(p.read_text(encoding="utf-8")).get("token")
        if not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "SE1":
            return None
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        claim = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
        if not isinstance(claim, dict) or not isinstance(claim.get("tier"), str):
            return None
        return claim["tier"].strip().lower()
    except (OSError, ValueError, UnicodeError, AttributeError):
        return None


def claims_paid(cred: Path) -> bool:
    """The UNVERIFIED tier claim of the entitlement next to ``cred`` is a paid
    tier. Standard library only (no signature check, no client import). False
    means the device cannot be Pro: the verified tier is this same claim. True
    means nothing until the gate has verified the token."""
    return claim_label(cred) in PAID_TIERS


def _pro_client(db: Path, cred: Path, creds: dict[str, Any], tenant: str):
    """The gated client a Pro hook writes through, bounded for a hook: its
    entitlement gate never refreshes (no network, no thread) and its usage
    heartbeat is off (its exit flush would wait on the network)."""
    from sibyl_memory_client import MemoryClient
    from sibyl_memory_client._entitlement import EntitlementGate
    from sibyl_memory_client._heartbeat import _NullHeartbeat
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = MemoryClient.local(str(db), tenant_id=tenant, account_id=creds.get("account_id"),
                                session_token=creds.get("session_token"),
                                tier=creds.get("tier", "free"), credentials_path=cred,
                                entitlement_gate=EntitlementGate(cred, allow_refresh=False))
    client._heartbeat = _NullHeartbeat()
    return client


def _local_write_ok(client: Any, delta: int = 0) -> bool:
    gate = getattr(client, "_cap_gate", None)
    try:
        return bool(gate is not None and gate.decides_locally(delta))
    except Exception:
        return False


def _payload(stdin_text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else None
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def run_hook(event: str, stdin_text: str, *, home: Path | None = None, now: float | None = None,
             started: float | None = None) -> str:
    """The hook's stdout ('' for nothing). Never raises. ``started`` is the
    ``time.monotonic()`` of the process start (the write budget counts from
    it; default: now)."""
    t0 = time.monotonic() if started is None else started
    home, cred, db = _paths(home)
    paid_claim = True
    try:
        paid_claim = claims_paid(cred)
        if event == "stop":
            payload = _payload(stdin_text)
            if payload is not None:
                if payload.get("stop_hook_active") is True:
                    return ""        # the re-entry after our own continuation: never loop
                if not paid_claim:
                    return ""        # free: Stop does nothing (no import, no verify, no DB)
    except Exception:
        paid_claim = True            # unsure: take the verified path
    try:
        return _run(event, stdin_text, home, cred, db, time.time() if now is None else now,
                    t0, paid_claim)
    except Exception as e:
        try:
            from sibyl_memory_client.lifecycle import state as ST
            ST.log(f"hook {event}: error {type(e).__name__}", home)
        except Exception:
            pass
        return ""


def main_hook(event: str) -> int:
    """`sibyl hook <event>` as a process: bounded stdin, one write to stdout,
    exit 0 always."""
    t0 = time.monotonic()
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1) if not sys.stdin.isatty() else b""
        text = raw[:MAX_STDIN_BYTES].decode("utf-8", "replace")
    except Exception:
        text = ""
    out = run_hook(event, text, started=t0)
    if out:
        try:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _run(event: str, stdin_text: str, home: Path, cred: Path, db: Path, now: float,
         t0: float, paid_claim: bool) -> str:
    from sibyl_memory_client.lifecycle import capsule as C
    from sibyl_memory_client.lifecycle import reminders as R
    from sibyl_memory_client.lifecycle import state as ST

    if event not in EVENTS:
        ST.log(f"hook {event}: unknown event", home)
        return ""
    payload = _payload(stdin_text)
    if payload is None:
        ST.log(f"hook {event}: malformed input", home)
        return ""
    creds = _read_creds(cred)
    tenant = (creds.get("tenant_id") or creds.get("account_id") or "")
    if not isinstance(tenant, str) or not tenant:
        ST.log(f"hook {event}: no account", home)
        return ""
    if event == "stop" and payload.get("stop_hook_active") is True:
        return ""            # the re-entry after our own continuation: never loop
    if paid_claim:
        from sibyl_memory_client.lifecycle.entitled import automation_allowed_for_credentials
        pro, why = automation_allowed_for_credentials(cred)
        if why == "tier_unknown":
            # The signed label is not one this version knows: treated as Free.
            # Logged so a support ticket can be diagnosed without the token.
            ST.log(f"hook {event}: tier_unknown (access label not recognised, treated as free)", home)
    else:
        pro, why = False, "free"
        label = claim_label(cred)
        if label and label != "free":
            # Not free and not a paid label this version knows (claims_paid
            # said no): treated as Free; the reason goes to lifecycle.log.
            why = "tier_unknown"
            ST.log(f"hook {event}: tier_unknown (access label not recognised, treated as free)", home)
    sk = ST.session_key(payload.get("session_id"))
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    store = ST.LifecycleState(home)
    # The save marker lives in memory.db. A free PreCompact / SessionEnd does
    # not need it (the next start or prompt folds it in), so it opens nothing.
    need_marker = pro or event in ("session-start", "prompt")
    marker = ST.read_save_marker(db, tenant) if need_marker else None

    def _budget_left() -> bool:
        return time.monotonic() - t0 < WRITE_BUDGET_SECONDS

    def _session(st: dict[str, Any]) -> dict[str, Any]:
        s = ST.ensure_session(st, sk, tenant_id=tenant, harness="claude-code", now=now)
        if marker is not None and s.get("started_at") is not None and marker >= s["started_at"]:
            s["saved"] = True
        return s

    if event == "session-start":
        tier_class = "paid" if pro else ("free" if why == "free" else None)

        def _start(st: dict[str, Any]) -> list[str]:
            ST.observe_marker(st, tenant, marker)
            s = _session(st)
            ST.add_active(s, now, kind="start")
            out = []
            line = R.tier_change_line(st, tenant, tier_class)
            if line:
                out.append(line)
            line = R.unsaved_session_line(st, tenant, now, exclude_session=sk)
            if line:
                out.append(line)
            return out
        lines = store.update(_start)
        if pro:
            try:
                from sibyl_memory_client.lifecycle.runner import run_automatic
                client = _pro_client(db, cred, creds, tenant)
                local = _local_write_ok(client, 64 * 1024)
                rep = run_automatic(client, home=home, cwd=Path(cwd), harness="claude-code", now=now,
                                    allow_apply=local, deadline=t0 + WRITE_BUDGET_SECONDS)
                if rep.line and R.reports_enabled(store.load()):
                    lines.append(rep.line)
                ST.log(f"hook session-start: pro run {rep.reason}"
                       f"{'' if local else ' (plan only: no fresh tier cache)'}", home)
            except Exception as e:
                ST.log(f"hook session-start: pro run failed {type(e).__name__}", home)
            pending = ST.read_pending_count(db, tenant)
            pl = R.pending_line(pending)
            if pl and R.enabled(store.load()):
                lines.append(pl)
            # R1 (plan 3.2): relevant skill proposals, named, read-only.
            from sibyl_memory_client.lifecycle import proposals as PR
            st0 = store.load()
            s0 = st0.get("sessions", {}).get(sk) or {}
            anchor = PR.last_session_start(st0, tenant, exclude=sk, before=s0.get("started_at") or now)
            names, total = PR.read_relevant(db, tenant, now, anchor=anchor,
                                            muted=PR.muted_kinds(st0, now))
            if total:
                sl = store.update(lambda st: PR.skill_prompt(st, tenant, sk, now, names, total))
                if sl:
                    lines.append(sl)
        else:
            from sibyl_memory_client.lifecycle.runner import measure_for_reminder
            rec = measure_for_reminder(home, Path(cwd))
            if rec is not None:
                line = store.update(lambda st: R.debloat_reminder(st, tenant, now, rec, pro=False))
                if line:
                    lines.append(line)
        ST.log(f"hook session-start: {len(lines)} line(s), {'pro' if pro else why}", home)
        return _context("SessionStart", lines)

    if event == "prompt":
        def _prompt(st: dict[str, Any]) -> str | None:
            ST.observe_marker(st, tenant, marker)
            s = _session(st)
            ST.add_active(s, now, kind="prompt")
            s["prompts"] = int(s.get("prompts") or 0) + 1
            if pro:
                return None       # Pro: the prompt only counts active time
            return R.save_reminder(st, tenant, now, pro=False, sk=sk)
        line = store.update(_prompt)
        return _context("UserPromptSubmit", [line] if line else [])

    if event == "pre-compact":
        def _pre(st: dict[str, Any]) -> dict[str, Any]:
            ST.observe_marker(st, tenant, marker)
            s = _session(st)
            in_turn = s.get("last_event_kind") in ("prompt", "compact")
            ST.add_active(s, now, kind="compact",
                          cap=ST.TURN_CAP_SECONDS if in_turn else ST.IDLE_CAP_SECONDS)
            s["compactions"] = int(s.get("compactions") or 0) + 1
            # This session's compaction (another session's never makes this
            # one due); cleared by the next save.
            s["compaction_seen"] = True
            t = ST.tenant(st, tenant)
            return dict(s, writes=max(0, int(t.get("writes_total") or 0) - int(s.get("writes_at_start") or 0)))
        s = store.update(_pre)
        if pro:
            cap = C.build_capsule(harness="claude-code", session_id=payload.get("session_id"),
                                  started_at=s.get("started_at"), ended_at=None, turns=None,
                                  memory_writes=s.get("writes", 0), saved_by_skill=bool(s.get("saved")),
                                  cwd=cwd)
            ev, why_not = _write_capsule(C, db, cred, creds, tenant, cap, _budget_left)
            if ev:
                store.update(lambda st: _session(st).update(capsule_written=True, capsule_at=now))
            ST.log(f"hook pre-compact: capsule {'written' if ev else 'not written' + why_not}", home)
        return ""

    if event == "stop":
        if not pro:
            return ""
        scripted = not (isinstance(payload.get("transcript_path"), str) and payload["transcript_path"])

        def _stop(st: dict[str, Any]) -> tuple[int, str] | None:
            # Decides only; the request is recorded (caps, interval) once it is
            # actually emitted, after the transcript guard below.
            ST.observe_marker(st, tenant, marker)
            s = _session(st)
            # The model worked from the prompt to this turn end: that gap is
            # active (bounded); a gap after a turn end is the person's pause.
            in_turn = s.get("last_event_kind") in ("prompt", "compact")
            ST.add_active(s, now, kind="stop",
                          cap=ST.TURN_CAP_SECONDS if in_turn else ST.IDLE_CAP_SECONDS)
            if scripted or int(s.get("prompts") or 0) < MIN_PROMPTS_FOR_STOP:
                return None       # a `claude -p` run: never continued
            why_due = R.autosave_due(st, tenant, sk, now)
            if why_due is None:
                return None
            return ST.next_nudge_seq(s), why_due
        got = store.update(_stop)
        if not got:
            ST.log("hook stop: pass", home)
            return ""
        seq, why_due = got
        reason = stop_reason(seq)
        if _transcript_has(payload.get("transcript_path"), reason):
            # This numbered request is already in the transcript (the state
            # lost it): not asked again and not counted; the next one is
            # numbered past it.
            store.update(lambda st: ST.note_seq_seen(st, tenant, sk, seq, now=now,
                                                     harness="claude-code"))
            ST.log(f"hook stop: pass (request {seq} already in the transcript)", home)
            return ""

        def _record(st: dict[str, Any]) -> int | None:
            s = _session(st)
            if ST.next_nudge_seq(s) != seq or R.autosave_due(st, tenant, sk, now) is None:
                return None       # another hook recorded a request, or a save landed, meanwhile
            return ST.record_nudge(st, tenant, sk, now, harness="claude-code")
        if store.update(_record) != seq:
            ST.log("hook stop: pass (recorded elsewhere)", home)
            return ""
        ST.log(f"hook stop: continue {seq} ({why_due})", home)
        return json.dumps({"decision": "block", "reason": reason})

    # session-end
    def _end(st: dict[str, Any]) -> dict[str, Any]:
        ST.observe_marker(st, tenant, marker)
        s = _session(st)
        t = ST.tenant(st, tenant)
        s["ended_at"] = now
        s["writes"] = max(0, int(t.get("writes_total") or 0) - int(s.get("writes_at_start") or 0))
        last_save = t.get("last_save_at")
        need = (not s.get("capsule_at")) or (last_save is not None and s["capsule_at"] <= last_save)
        return dict(s, need_capsule=need)
    s = store.update(_end)
    if pro and s.get("need_capsule"):
        cap = C.build_capsule(harness="claude-code", session_id=payload.get("session_id"),
                              started_at=s.get("started_at"), ended_at=now, turns=None,
                              memory_writes=s.get("writes", 0), saved_by_skill=bool(s.get("saved")),
                              cwd=cwd)
        ev, why_not = _write_capsule(C, db, cred, creds, tenant, cap, _budget_left)
        if ev:
            store.update(lambda st: _session(st).update(capsule_written=True, capsule_at=now))
        ST.log(f"hook session-end: capsule {'written' if ev else 'not written' + why_not}", home)
    else:
        ST.log("hook session-end: recorded", home)
    return ""


def _write_capsule(C: Any, db: Path, cred: Path, creds: dict[str, Any], tenant: str,
                   cap: dict[str, Any], budget_left: Any) -> tuple[Any, str]:
    """(event id or None, why not). Writes only when the cap gate decides
    locally and the hook is inside its write budget."""
    if not budget_left():
        return None, " (out of time)"
    client = _pro_client(db, cred, creds, tenant)
    if not _local_write_ok(client, 4096):
        return None, " (no fresh tier cache; a later hook or the MCP server writes it)"
    return C.write_capsule(client, cap), ""


__all__ = ["EVENTS", "MIN_PROMPTS_FOR_STOP", "PAID_TIERS", "STOP_REASON", "STOP_REASON_BASE",
           "WRITE_BUDGET_SECONDS", "claim_label", "claims_paid", "main_hook", "run_hook", "stop_reason"]
