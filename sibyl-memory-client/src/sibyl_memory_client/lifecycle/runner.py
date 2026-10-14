"""The Pro process-start debloat run and the free-tier measurement, shared by
the MCP server, the Hermes adapter and the Claude Code SessionStart hook.

``run_automatic``: only when ``automation_allowed`` (verified fresh/stale
entitlement whose tier is paid). Under ``debloat.lock`` (one planner at a time
across processes; a second process starting within 60 s of a run does
nothing). Measures (stat only), plans every trimmable candidate, writes the
plan summary to ``~/.sibyl-memory/debloat/plans/<ts>.json`` (labels, section
headings and token counts, no bodies; the newest ``MAX_PLANS`` are kept),
and applies ONLY under ``mode: auto`` and ONLY for the files the person named
with ``sibyl debloat auto <file>``: a candidate is matched on its normalised
path (``policy.auth_id``), never on its name, so authorising ``AGENTS.md`` in
one project authorises nothing anywhere else. An ``auto`` policy with an empty
``auto_files`` behaves as ``propose``. Returns one report line (or None).

``allow_apply=False`` (the Claude Code hook when the write could need the
network) and ``deadline`` (a ``time.monotonic()`` value after which no new
apply starts) turn the run into plan-only; the file waits for the next start.

A file set to automatic that a run did not trim (refused, unreadable, not
trimmable) is named in the report line (``on automatic but not trimmed:
<file>: <reason>``), never silently skipped. ``last_apply`` records each
file's ``auth_id`` (``id``) so ``sibyl debloat undo`` without a file name
undoes that exact file, not the same name in another project.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir
from . import apply as A
from . import policy as P
from .entitled import automation_allowed
from .state import LifecycleState, log

DEDUPE_SECONDS = 60
MAX_PLANS = 50


@dataclass
class RunReport:
    ran: bool
    reason: str
    line: str | None = None
    plans: list[dict[str, Any]] = field(default_factory=list)
    applied: list[dict[str, Any]] = field(default_factory=list)
    record: dict[str, Any] | None = None


def _plan_summary(c: A.Candidate, pl: Any, reason: str) -> dict[str, Any]:
    d = {"file": c.label, "trimmable": c.trimmable, "reason": reason}
    if pl is not None:
        d.update({"safe": pl.safe, "sections": pl.sections[:20], "tok_before": pl.tok_before,
                  "tok_history": pl.tok_history, "tok_after": pl.tok_after})
    return d


def _not_trimmed(refused: list[tuple[str, str]]) -> str | None:
    """The part of the report that names a file set to automatic that this
    run did not trim (refused, unreadable, not trimmable), so a refusal is
    never silent."""
    if not refused:
        return None
    f, why = refused[0]
    more = f" (+{len(refused) - 1} more)" if len(refused) > 1 else ""
    return f"on automatic but not trimmed: {f}: {why}{more}"


def _digest(plans: list[dict[str, Any]], applied: list[dict[str, Any]], mode: str,
            refused: list[tuple[str, str]] | None = None) -> str | None:
    base = _digest_base(plans, applied, mode)
    extra = _not_trimmed(refused or [])
    if extra is None:
        return base
    return f"{base}; {extra}" if base else f"context trim: {extra}"


def _digest_base(plans: list[dict[str, Any]], applied: list[dict[str, Any]], mode: str) -> str | None:
    done = [a for a in applied if a.get("applied")]
    if done:
        a = done[0]
        where = f"history stored to memory as {a['key']}" if a.get("key") else \
            f"history moved to {a.get('sibling')}"
        more = f" (+{len(done) - 1} more)" if len(done) > 1 else ""
        return f"context trim: {a['file']} {a['tok_before']} -> {a['tok_after']} tok, {where}.{more}"
    safe = [p for p in plans if p.get("safe")]
    if not safe:
        return None
    p = safe[0]
    n = len(p.get("sections") or [])
    secs = f"{n} history section{'s' if n != 1 else ''}"
    more = f" (+{len(safe) - 1} more file{'s' if len(safe) > 2 else ''})" if len(safe) > 1 else ""
    if mode == "off":
        return None
    return (f"context trim: {p['file']}: {secs}, {p['tok_history']} tok, would move to memory; "
            f"`sibyl debloat apply` or `sibyl debloat auto {p['file']}`{more}")


def _prune_plans(pdir: Path) -> None:
    try:
        plans = sorted((p for p in pdir.glob("*.json") if p.is_file() and not p.is_symlink()),
                       key=lambda p: p.name)
        for old in plans[:-MAX_PLANS]:
            old.unlink()
    except OSError:
        pass


def run_automatic(client: Any, *, home: Path | str, cwd: Path | str, harness: str,
                  now: float | None = None,
                  apply_fn: Callable[..., A.ApplyResult] | None = None,
                  allow_apply: bool = True, deadline: float | None = None) -> RunReport:
    ok, why = automation_allowed(client)
    if not ok:
        return RunReport(False, f"not entitled ({why})")
    home_p, cwd_p = Path(home), Path(cwd)
    now_f = time.time() if now is None else now
    pol = P.load(home_p)
    st_store = LifecycleState(home_p)
    apply_fn = apply_fn or A.apply_one
    with A.run_lock(home_p) as got:
        if not got:
            return RunReport(False, "another process holds the debloat lock")
        legacy = P.take_legacy_notice(home_p)
        if legacy:
            log(f"debloat {harness}: name-only auto entries dropped", home_p)
            pol = P.load(home_p)
        st = st_store.load()
        last = st.get("debloat", {}).get("last_measure_at")
        if (isinstance(last, (int, float)) and 0 <= now_f - last < DEDUPE_SECONDS
                and st.get("debloat", {}).get("last_measure_root") == str(cwd_p)):
            return RunReport(False, "ran moments ago", legacy)
        cands, rec = A.candidates(home_p, cwd_p, pol, now=datetime.fromtimestamp(now_f))
        surface = {c.label for c in cands}
        plans: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        mode = pol["mode"]
        auto_files = set(pol["auto_files"])
        deferred: list[str] = []
        refused: list[tuple[str, str]] = []
        ids: dict[str, str] = {}
        for c in cands:
            on_auto = mode == "auto" and bool(auto_files) and P.auth_id(c.path, home_p) in auto_files
            if on_auto:
                ids[c.label] = P.auth_id(c.path, home_p)
            if not c.trimmable:
                if on_auto:
                    refused.append((c.label, c.why_not))
                continue
            A.sweep_stale_tmp(c.path, now=now_f)
            pl, reason = A.plan(c, pol, now=datetime.fromtimestamp(now_f))
            plans.append(_plan_summary(c, pl, reason))
            if not on_auto:
                continue
            if pl is None:
                refused.append((c.label, reason))
                continue
            if not pl.safe:
                continue
            if not allow_apply or (deadline is not None and time.monotonic() > deadline):
                deferred.append(c.label)
                log(f"debloat {harness}: {c.label} apply deferred to a later start", home_p)
                continue
            res = apply_fn(c, home=home_p, cwd=cwd_p, destination=pol["destination"], client=client,
                           policy=pol, surface_labels=surface, now=now_f)
            applied.append(res.as_dict())
            if not res.applied:
                refused.append((c.label, res.reason))
            log(f"debloat {harness}: {c.label} {res.reason}", home_p)
        stamp = time.strftime("%Y-%m-%dT%H_%M_%S", time.gmtime(now_f))
        try:
            pdir = A.debloat_dir(home_p) / "plans"
            ensure_private_dir(A.debloat_dir(home_p))
            ensure_private_dir(pdir)
            atomic_write_bytes(pdir / f"{stamp}.json", (json.dumps(
                {"ts": now_f, "harness": harness, "mode": mode, "root": str(cwd_p),
                 "total_tok": rec["total_tok"], "weight": rec["weight"], "plans": plans,
                 "applied": applied}, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            _prune_plans(pdir)
        except (OSError, RefusedPath):
            pass
        if pol.get("journal"):
            A.journal(home_p, {"ts": now_f, "total_tok": rec["total_tok"], "weight": rec["weight"],
                               "targets": len(rec["targets"]), "applied": len([a for a in applied
                                                                             if a.get("applied")])})
        line = _digest(plans, applied, mode, refused)
        if deferred and not any(a.get("applied") for a in applied):
            more = f" (+{len(deferred) - 1} more)" if len(deferred) > 1 else ""
            line = (f"context trim: {deferred[0]} is set to automatic; it is trimmed at a later start "
                    f"(this one could not wait for the account check).{more}")
            extra = _not_trimmed(refused)
            if extra:
                line += f"; {extra}"
        if legacy:
            extra = _not_trimmed(refused)
            line = legacy + (f" {extra[0].upper()}{extra[1:]}." if extra else "")

        def _upd(s: dict[str, Any]) -> None:
            d = s.setdefault("debloat", {})
            d["last_measure_at"] = now_f
            d["last_measure_root"] = str(cwd_p)
            d["last_plan"] = {"ts": now_f, "files": [p["file"] for p in plans if p.get("safe")]}
            if applied:
                d["last_apply"] = {"ts": now_f, "results": [
                    {"file": a["file"], "id": ids.get(a["file"]), "applied": a["applied"],
                     "reason": a["reason"], "key": a.get("key"),
                     "backup": a.get("backup") if a.get("applied") else None}
                    for a in applied]}
            d["last_report"] = line
        st_store.update(_upd)
        return RunReport(True, "ok", line, plans, applied, rec)


def measure_for_reminder(home: Path | str, cwd: Path | str) -> dict[str, Any] | None:
    """Stat-only measurement with the person's policy thresholds (free tier)."""
    try:
        pol = P.load(home)
        from .measure import measure
        return measure(Path(cwd), home=Path(home), surface=pol.get("surface"),
                       thresholds=pol.get("thresholds"), per_file_soft=pol.get("perFileSoft"))
    except Exception:
        return None


__all__ = ["MAX_PLANS", "RunReport", "measure_for_reminder", "run_automatic"]
