"""`sibyl save`, `sibyl debloat`, `sibyl skills`, `sibyl reminders`, `sibyl hook`
and the `lifecycle` rows of `sibyl status` (spec plugin-save-debloat 4.3, 6.3,
7.1). The procedures themselves live in sibyl-memory-client (``skills``,
``lifecycle``); this module is the terminal surface.

Naming (spec 0.1): the plugin's step is "the debloat step" / "context trim";
"automatic debloat on Pro". Nothing here prints a token or file content (a
plan shows section headings and token counts only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from . import _aesthetic as a
from . import _handoff as _h


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _home() -> Path:
    return Path.home()


def _cred(args: argparse.Namespace) -> Path:
    from .cli import DEFAULT_CRED_PATH
    return Path(getattr(args, "credentials", None) or DEFAULT_CRED_PATH).expanduser()


def _db(args: argparse.Namespace) -> Path:
    from .cli import DEFAULT_DB_PATH
    return Path(getattr(args, "db", None) or DEFAULT_DB_PATH).expanduser()


def _creds(args: argparse.Namespace) -> dict[str, Any]:
    from .cli import read_credentials
    return read_credentials(_cred(args)) or {}


def _tenant(creds: dict[str, Any]) -> str | None:
    return (creds.get("tenant_id") or creds.get("account_id") or None)


def _client(args: argparse.Namespace):
    """The gated client an agent would use (the account gate applies)."""
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    creds = _creds(args)
    db = _db(args)
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = MemoryClient.local(str(db), tenant_id=_tenant(creds) or DEFAULT_TENANT,
                                account_id=creds.get("account_id"),
                                session_token=creds.get("session_token"),
                                tier=creds.get("tier", "free"), credentials_path=_cred(args))
    client.enable_lifecycle_counting(_home())   # `sibyl save --fact` counts like an agent write
    return client


def _pro(args: argparse.Namespace) -> tuple[bool, str]:
    from sibyl_memory_client.lifecycle.entitled import automation_allowed_for_credentials
    return automation_allowed_for_credentials(_cred(args))


# The question `_ask` could not put because there is no terminal (row G1):
# the caller's handoff relays it and names the same command with --yes.
_NI_QUESTION: str | None = None


def _ask(question: str, *, yes: bool) -> bool:
    global _NI_QUESTION
    _NI_QUESTION = None
    if yes:
        return True
    if not sys.stdin.isatty():
        print(a.dim("  Not a terminal: re-run with --yes to confirm."))
        _NI_QUESTION = question
        return False
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        _NI_QUESTION = question
        return False


def _declined(command: str) -> None:
    """Handoff after `_ask` said no: the same command with --yes and the
    question relayed when there was no terminal (G1), else done (G2)."""
    if _NI_QUESTION:
        _h.handoff(f"{command} --yes", human=_NI_QUESTION)
    else:
        _h.handoff("done")


def _entitlement_handoff(e: BaseException, *, later: tuple = ()) -> None:
    nxt, human = _h.from_exception(e, default="sibyl init")
    _h.handoff(nxt, human=human, later=later)


def _fmt_ts(ts: Any) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return "never"
    from ._timefmt import utc_minute
    return utc_minute(ts) or "never"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return s or ("fact-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8])


# ----------------------------------------------------------------------
# sibyl save
# ----------------------------------------------------------------------

THREE_WAYS = (
    "Saving a session is a distillation, so your agent does it with the save procedure:",
    "  Claude Code     /sibyl-save            (installed by sibyl setup)",
    "  any MCP host    the `save` prompt on the sibyl-memory server (/mcp__sibyl-memory__save)",
    "  Codex, Hermes   ask the agent to follow ~/.sibyl-memory/skills/SAVE.md",
    "From a terminal: sibyl save --fact \"<one fact>\" writes one fact; sibyl save --status shows the last save.",
    "On Pro, your agent is asked to save by itself: once early (after 5 memory writes or a compaction), "
    "then about every hour of active work (sibyl autosave sets the interval or turns it off).",
)


def cmd_save(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import state as ST
    print()
    if getattr(args, "status", False):
        creds = _creds(args)
        tid = _tenant(creds)
        st = ST.LifecycleState(_home()).load()
        t = st.get("tenants", {}).get(tid or "", {}) if tid else {}
        marker = ST.read_save_marker(_db(args), tid) if tid else None
        last = max([x for x in (marker, t.get("last_save_at")) if isinstance(x, (int, float))], default=None)
        print(a.eyebrow("save"))
        print(a.kv("last save", _fmt_ts(last)))
        print(a.kv("writes since", str(int(t.get("writes_since_save") or 0))))
        pend = ST.read_pending_count(_db(args), tid) if tid else 0
        print(a.kv("to decide", f"{pend} item(s) queued by a headless save" if pend else "nothing queued"))
        _h.handoff("done")
        return 0
    fact = getattr(args, "fact", None)
    if fact is not None:
        text = fact.strip()
        if not text:
            print(a.warn_line("Nothing to save: the fact is empty."))
            _h.handoff('sibyl save --fact "<fact>"', human="the fact to save")
            return 1
        slug = _slug(text)
        if getattr(args, "preview", False):
            print(a.dim(f"would write fact/{slug}  (nothing written)"))
            _h.handoff("done")
            return 0
        from sibyl_memory_client import EntitlementError
        from sibyl_memory_client.exceptions import CapExceededError
        try:
            c = _client(args)
            c.set_entity("fact", slug, {"value": text,
                                        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        except CapExceededError as e:
            # Finding 5.3.2 (handoff spec 2026-10-03): the free cap used to
            # escape as a traceback. Its first line, then the handoff.
            print(a.warn_line(str(e).splitlines()[0] if str(e) else "The free 5 MB memory cap is full."))
            print(a.dim("  Nothing was written."))
            _h.handoff("done", later=("sibyl upgrade",))
            return 1
        except EntitlementError as e:
            print(a.warn_line(str(e).splitlines()[0]))
            print(a.dim("  Sign in first: sibyl init. Nothing was written."))
            _entitlement_handoff(e, later=(f"sibyl save --fact {shlex.quote(text)}",))
            return 1
        print(a.success_line(f"Saved fact/{slug}."))
        print(a.dim(f"  recall it with: sibyl memory recall fact {slug}"))
        _h.handoff("done")
        return 0
    if getattr(args, "capsule", False):
        ok, _why = _pro(args)
        if not ok:
            print(a.dim("Automatic save is on Pro. Nothing was written."))
            print(a.dim("  On the free plan run the save procedure yourself (sibyl save shows how)."))
            _h.handoff("sibyl save", later=("sibyl upgrade",))
            return 0
        if getattr(args, "preview", False):
            print(a.dim("would write one session capsule (counts only)  (nothing written)"))
            _h.handoff("done")
            return 0
        from sibyl_memory_client.lifecycle import capsule as C
        cap = C.build_capsule(harness="cli", session_id=f"cli-{time.time()}", started_at=None,
                              ended_at=time.time(), turns=None, memory_writes=0,
                              saved_by_skill=False, cwd=Path.cwd())
        ev = C.write_capsule(_client(args), cap)
        print(a.success_line("Session capsule written.") if ev else a.warn_line("Capsule not written."))
        _h.handoff("done" if ev else "sibyl status")
        return 0 if ev else 1
    for ln in THREE_WAYS:
        print(ln if not ln.startswith("  ") else a.dim(ln))
    if getattr(args, "preview", False):
        print(a.dim("(preview: nothing was written)"))
    _h.handoff("done")
    return 0


# ----------------------------------------------------------------------
# sibyl debloat
# ----------------------------------------------------------------------

def _root(args: argparse.Namespace) -> Path:
    """The project root, as its real path (``--root`` given through a symlink
    names the same files, the same authorisations and the same backups as
    the real folder)."""
    return Path(os.path.realpath(str(Path(getattr(args, "root", None) or Path.cwd()).expanduser())))


def _measure(args: argparse.Namespace) -> dict[str, Any]:
    from sibyl_memory_client.lifecycle import measure as M
    from sibyl_memory_client.lifecycle import policy as P
    pol = P.load(_home())
    root = _root(args)
    return M.measure(root, home=_home(), config_path=getattr(args, "config", None),
                     includes=getattr(args, "include", None) or [], surface=pol.get("surface"),
                     thresholds=pol.get("thresholds"), per_file_soft=pol.get("perFileSoft"),
                     write_journal=True)


def _candidates(args: argparse.Namespace):
    from sibyl_memory_client.lifecycle import apply as A
    from sibyl_memory_client.lifecycle import policy as P
    pol = P.load(_home())
    root = _root(args)
    cands, rec = A.candidates(_home(), root, pol)
    return cands, rec, pol, root


def _plans(args: argparse.Namespace) -> list[dict[str, Any]]:
    from sibyl_memory_client.lifecycle import apply as A
    cands, _rec, pol, _root = _candidates(args)
    out = []
    for c in cands:
        if not c.trimmable:
            out.append({"file": c.label, "trimmable": False, "reason": c.why_not})
            continue
        pl, reason = A.plan(c, pol)
        d = {"file": c.label, "trimmable": True, "reason": reason}
        if pl is not None:
            d.update({"safe": pl.safe, "sections": pl.sections, "tok_before": pl.tok_before,
                      "tok_history": pl.tok_history, "tok_after": pl.tok_after})
        out.append(d)
    return out


def _legacy_notice() -> None:
    """Once: an earlier build stored automatic trims by file NAME; those
    entries are dropped (never guessed at) and the person is told."""
    try:
        from sibyl_memory_client.lifecycle import policy as P
        line = P.take_legacy_notice(_home())
    except Exception:
        line = None
    if line:
        print(a.warn_line(line))


def cmd_debloat(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    if action is not None or not (getattr(args, "json", False) or getattr(args, "fyi", False)):
        _legacy_notice()
    try:
        if action is None:
            return _debloat_measure(args)
        if action == "plan":
            return _debloat_plan(args)
        if action == "apply":
            return _debloat_apply(args)
        if action in ("auto", "manual"):
            return _debloat_auto(args, action == "auto")
        if action == "undo":
            return _debloat_undo(args)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # the measurement always exits 0; actions report
        print(a.warn_line(f"debloat: {type(e).__name__}: {e}"))
        if not (action is None and getattr(args, "fyi", False)) and not getattr(args, "json", False):
            _h.handoff("done" if action is None else "sibyl debloat plan")
        return 0 if action is None else 1
    return 0


def _debloat_measure(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import measure as M
    from sibyl_memory_client.lifecycle import policy as P
    rec = _measure(args)
    if rec.get("config_error"):
        print(rec["config_error"] + " (defaults used)", file=sys.stderr)
    heavy_next = "sibyl debloat plan" if any(f.get("heavy") for f in rec.get("files") or []) else "done"
    if getattr(args, "json", False):
        out = M.public_record(rec)
        if P.policy_path(_home()).exists():
            out["plan"] = _plans(args)
        print(json.dumps(_h.json_handoff(out, heavy_next, None), indent=2))
        return 0
    if getattr(args, "fyi", False):
        # The one-line FYI is a startup-line format (like --json, one shape):
        # no handoff block.
        if not rec["files"]:
            print("context: no always-load files found")
        else:
            print(M.fyi(rec))
        return 0
    print(M.report(rec))
    _h.handoff(heavy_next)
    return 0


def _debloat_plan(args: argparse.Namespace) -> int:
    plans = _plans(args)
    print()
    if not plans:
        print(a.dim("No instruction files found for this project or your home."))
        _h.handoff("done")
        return 0
    print(a.eyebrow("context trim plan (nothing is changed)"))
    for p in plans:
        if not p.get("trimmable"):
            print(a.kv(p["file"], f"not trimmed: {p['reason']}"))
        elif p.get("safe"):
            secs = ", ".join(p["sections"][:5]) + (" ..." if len(p["sections"]) > 5 else "")
            print(a.kv(p["file"], f"{p['tok_before']} -> {p['tok_after']} tok; would move "
                                  f"{len(p['sections'])} section(s): {secs}"))
        else:
            print(a.kv(p["file"], p.get("reason") or "nothing to move"))
    print()
    print(a.dim("Apply one with: sibyl debloat apply <file>   (asks first; a verified backup is made)"))
    if any(p.get("trimmable") and p.get("safe") for p in plans):
        _h.handoff("sibyl debloat apply <file>",
                   human="which file to trim (it asks before writing; a verified backup is made)")
    else:
        _h.handoff("done")
    return 0


def _debloat_apply(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import apply as A
    from sibyl_memory_client.lifecycle import policy as P
    from sibyl_memory_client.lifecycle import state as ST
    cands, _rec, pol, root = _candidates(args)
    want = getattr(args, "file", None)
    surface = {c.label for c in cands}
    chosen = [c for c in cands if c.trimmable and (want is None or want in (c.label, c.key))]
    print()
    if want is not None and not chosen:
        print(a.warn_line(f"{want} is not a trimmable instruction file here."))
        for c in cands:
            if want in (c.label, c.key):
                print(a.dim(f"  {c.why_not}"))
        _h.handoff("sibyl debloat plan")
        return 1
    pro, _tier = _pro(args)
    destination = pol["destination"] if pro else "sibling"
    client = _client(args) if pro else None     # free: no client is constructed for the write
    results = []
    ids: dict[str, str] = {}
    ni_question = None
    declined = False
    for c in chosen:
        pl, reason = A.plan(c, pol)
        if pl is None or not pl.safe:
            print(a.kv(c.label, reason or "nothing to move"))
            continue
        where = ("Sibyl Memory (a reference document) plus a sibling file" if destination == "memory+sibling"
                 else "Sibyl Memory (a reference document)" if destination == "memory"
                 else f"{A.sibling_path(c.path).name} beside it")
        q = (f"Move {len(pl.sections)} history section(s) of {c.label} ({pl.tok_history} tok) to "
             f"{where}? A verified backup is made first")
        if not _ask(q, yes=bool(getattr(args, "yes", False))):
            print(a.dim(f"  {c.label}: left as is."))
            ni_question = ni_question or _NI_QUESTION
            declined = True
            continue
        res = A.apply_one(c, home=_home(), cwd=root, destination=destination, client=client,
                          policy=pol, surface_labels=surface)
        results.append(res)
        ids[c.label] = P.auth_id(c.path, _home())
        if res.applied:
            dest = res.key or res.sibling
            print(a.success_line(f"{c.label}: {res.tok_before} -> {res.tok_after} tok; history in {dest}."))
            print(a.dim(f"  backup: {res.backup}   undo: sibyl debloat undo {shlex.quote(c.label)}"))
        else:
            print(a.warn_line(f"{c.label}: {res.reason}"))
    if results:
        def _upd(st: dict[str, Any]) -> None:
            st.setdefault("debloat", {})["last_apply"] = {
                "ts": time.time(), "results": [{"file": r.file, "id": ids.get(r.file),
                                                "applied": r.applied,
                                                "reason": r.reason, "key": r.key,
                                                "backup": r.backup if r.applied else None}
                                               for r in results]}
        ST.LifecycleState(_home()).update(_upd)
        if pol.get("journal"):
            rec = _measure(args)
            A.journal(_home(), {"ts": time.time(), "total_tok": rec["total_tok"], "weight": rec["weight"],
                                "targets": len(rec["targets"]),
                                "applied": len([r for r in results if r.applied])})
    elif not chosen:
        print(a.dim("Nothing to trim."))
    applied = [r.file for r in results if r.applied]
    if results and not all(r.applied for r in results):
        _h.handoff("sibyl debloat plan", later=[f"sibyl debloat undo {shlex.quote(str(f))}" for f in applied])
    elif ni_question and not applied:
        _h.handoff("sibyl debloat apply" + (f" {shlex.quote(str(want))}" if want else "") + " --yes",
                   human=ni_question)
    else:
        _h.handoff("done", later=[f"sibyl debloat undo {shlex.quote(str(f))}" for f in applied])
    return 0 if all(r.applied for r in results) else (1 if results else 0)


def _debloat_auto(args: argparse.Namespace, on: bool) -> int:
    from sibyl_memory_client.lifecycle import policy as P
    want = getattr(args, "file", None)
    print()
    if not want:
        print(a.warn_line(f"Usage: sibyl debloat {'auto' if on else 'manual'} <file>"))
        _h.handoff("sibyl debloat plan")
        return 1
    cands, _rec, pol, root = _candidates(args)
    match = [c for c in cands if want in (c.label, c.key)]
    if on:
        if not match or not match[0].trimmable:
            print(a.warn_line(f"{want} is not a trimmable instruction file here."))
            _h.handoff("sibyl debloat plan")
            return 1
        c = match[0]
        fid = P.auth_id(c.path, _home())
        print(f"Automatic context trim for this file at {fid} means:")
        print(a.dim("  at every agent start, Sibyl measures it and, when the deterministic splitter finds"))
        print(a.dim("  accreted history (a sibyl:history region, a History/Changelog heading, dated sections"))
        print(a.dim(f"  older than {pol['history_after_days']} days with no open item), it moves that history"))
        print(a.dim("  into Sibyl Memory (searchable), keeps a byte-verified backup, rewrites the file last"))
        print(a.dim("  with a one-line pointer, and tells you in one line. Nothing is ever deleted."))
        print(a.dim("  Only this file at this path: a file with the same name in another project, or in"))
        print(a.dim("  your home, is not affected."))
        print(a.dim(f"  Take it back any time: sibyl debloat manual {shlex.quote(c.label)}. Undo a trim: sibyl debloat undo {shlex.quote(c.label)}."))
        pro, _t = _pro(args)
        if not pro:
            print(a.dim("  Automatic trims run on Pro; this choice is kept for when they do."))
        if not _ask(f"Let Sibyl trim this file at {fid} automatically?",
                    yes=bool(getattr(args, "yes", False))):
            print(a.dim("Nothing changed."))
            _declined(f"sibyl debloat auto {shlex.quote(str(want))}")
            return 1
        pol = P.set_auto(fid, True, _home())
        print(a.success_line(f"{fid} is on automatic (mode {pol['mode']})."))
        _h.handoff("done")
        return 0
    if match:
        fid = P.auth_id(match[0].path, _home())
    elif P.is_path_id(want):
        fid = P.auth_id(P.id_path(want, _home()), _home())
    else:
        fid = P.auth_id(Path(root) / want, _home())
    if fid not in P.load(_home())["auto_files"]:
        print(a.dim(f"{fid} was not on automatic; nothing changed."))
        _h.handoff("done")
        return 0
    pol = P.set_auto(fid, False, _home())
    print(a.success_line(f"{fid} is back to manual (mode {pol['mode']})."))
    _h.handoff("done")
    return 0


def _debloat_undo(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import apply as A
    from sibyl_memory_client.lifecycle import policy as P
    from sibyl_memory_client.lifecycle import state as ST
    want = getattr(args, "file", None)
    cands, _rec, _pol, root = _candidates(args)
    last = ST.LifecycleState(_home()).load().get("debloat", {}).get("last_apply") or {}
    done = [r for r in last.get("results", []) if isinstance(r, dict) and r.get("applied")]
    # The last apply is matched on the FILE it trimmed (its auth_id), never on
    # the bare label: `AGENTS.md` trimmed in one project is not this project's.
    if want is None:
        if not done:
            print(a.dim("No trim to undo."))
            _h.handoff("done")
            return 0
        rid = done[0].get("id")
        if not isinstance(rid, str) or not P.is_path_id(rid):
            print(a.dim(f"The last trim ({done[0].get('file')}) was recorded without its path; "
                        "name the file: sibyl debloat undo <file>"))
            _h.handoff("sibyl debloat undo <file>", human="which file to restore (the last trim "
                                                         "was recorded without its path)")
            return 1
        match = [c for c in cands if P.auth_id(c.path, _home()) == rid]
        if not match:
            print(a.warn_line(f"The last trim was {rid}, which is not a file of this project or your "
                              "home here; run `sibyl debloat undo <file>` where it lives."))
            _h.handoff("done", human=f"the last trim was {rid}; run sibyl debloat undo in that "
                                     "project's folder")
            return 1
        want = match[0].label
    else:
        match = [c for c in cands if want in (c.label, c.key)]
    path = match[0].path if match else ((_home() / want[2:]) if str(want).startswith("~/") else root / want)
    fid = P.auth_id(path, _home())
    # The backup the last apply of THIS file recorded (not merely the newest
    # one, which an interrupted later attempt could have made identical to
    # the file as it is now).
    prefer = next((r.get("backup") for r in done if r.get("id") == fid and r.get("backup")), None)
    print()
    which = "the backup its last trim made" if prefer else "its most recent backup"
    if not _ask(f"Restore {want} from {which}? (the current file is backed up first)",
                yes=bool(getattr(args, "yes", False))):
        print(a.dim("Nothing changed."))
        _declined(f"sibyl debloat undo {shlex.quote(str(want))}")
        return 1
    ok, msg, _src = A.undo(path, home=_home(), cwd=root, prefer=prefer)
    print(a.success_line(f"{want}: {msg}") if ok else a.warn_line(f"{want}: {msg}"))
    if ok:
        rec = _measure(args)
        print(a.dim(f"  context now ~{rec['total_tok']} tok ({rec['weight']})"))
    _h.handoff("done" if ok else "sibyl debloat plan")
    return 0 if ok else 1


# ----------------------------------------------------------------------
# sibyl skills
# ----------------------------------------------------------------------

def _present_wirers(args: argparse.Namespace) -> dict[str, Any]:
    from .setup import ClaudeCodeWirer, CodexWirer, HermesWirer
    out: dict[str, Any] = {}
    cc = ClaudeCodeWirer()
    if cc.settings_path.parent.exists():
        out["claude-code"] = cc
    cx = CodexWirer()
    if cx.config_path.parent.exists():
        out["codex"] = cx
    hw = HermesWirer()
    if hw.hermes_home.exists():
        out["hermes"] = hw
    return out


def cmd_skills(args: argparse.Namespace) -> int:
    from sibyl_memory_client import skills as SK
    from .setup import install_shims
    op = getattr(args, "skills_cmd", None) or "status"
    home = _home()
    print()
    if op == "proposals":
        return _proposals(args)
    if op == "install":
        rep = SK.install(home, force=bool(getattr(args, "force", False)))
        print(a.eyebrow(f"skills v{rep.version}"))
        for n, s in rep.results.items():
            print(a.kv(n, s))
        for m in rep.messages:
            print(a.dim("  " + m))
        for name, w in _present_wirers(args).items():
            lines = []
            if name == "codex":
                lines = w.wire_block() + install_shims(name, w.skill_shim_targets())
            else:
                lines = install_shims(name, w.skill_shim_targets())
            for ln in lines:
                print(a.dim(f"  {name}: {ln}"))
        _h.handoff("done" if rep.ok else "sibyl skills status")
        return 0 if rep.ok else 1
    if op == "reset":
        name = getattr(args, "name", None)
        if name not in SK.SKILL_NAMES:
            print(a.warn_line(f"Usage: sibyl skills reset {{{'|'.join(SK.SKILL_NAMES)}}}"))
            _h.handoff("sibyl skills status")
            return 1
        if not _ask(f"Replace {name} with the shipped version {SK.version()}? Your edits in it are lost "
                    "(copy them first if you want them)", yes=bool(getattr(args, "yes", False))):
            print(a.dim("Nothing changed."))
            _declined(f"sibyl skills reset {name}")
            return 1
        rep = SK.reset(home, name)
        print(a.kv(name, rep.results.get(name, "?")))
        _h.handoff("done" if rep.ok else "sibyl skills status")
        return 0 if rep.ok else 1
    if op == "uninstall":
        if not _ask("Remove the save/debloat files, shims, hooks and the Codex block this plugin "
                    "installed? (edited files are kept)", yes=bool(getattr(args, "yes", False))):
            print(a.dim("Nothing changed."))
            _declined("sibyl skills uninstall")
            return 1
        for name, w in _present_wirers(args).items():
            if name == "claude-code":
                for ln in w.unwire_hooks():
                    print(a.dim(f"  {name}: {ln}"))
            if name == "codex":
                for ln in w.unwire_block():
                    print(a.dim(f"  {name}: {ln}"))
        for k, v in SK.uninstall(home).items():
            print(a.kv(Path(k).name if "/" in k else k, v))
        _h.handoff("done")
        return 0
    # status
    st = SK.status(home)
    print(a.eyebrow("skills"))
    if not st["exists"]:
        print(a.kv("installed", "no · sibyl skills install (or sibyl setup)"))
        print(a.kv("package", f"v{st['package_version']}"))
        _h.handoff("sibyl skills install")
        return 0
    print(a.kv("version", f"v{st['installed_version'] or '?'} (package v{st['package_version']})"))
    for n, info in st["files"].items():
        extra = ""
        if info.get("upstream_waiting"):
            extra = f" · new version waiting: {', '.join(info['upstream_waiting'])} (sibyl skills reset {n})"
        label = {"ok": "ok", "edited": "edited by you", "outdated": "outdated (sibyl skills install)",
                 "missing": "missing", "symlink": "a symlink (refused)", "oversize": "too large (refused)"}
        print(a.kv(n, label.get(info["state"], info["state"]) + extra))
    for p, info in st["shims"].items():
        print(a.kv(info.get("harness") or "shim", f"{p} · {info['state']}"))
    for name, w in _present_wirers(args).items():
        if name == "claude-code":
            hs = w.hooks_state()
            print(a.kv("claude hooks", f"{hs['present']} of 5 wired" + (f" · settings.json {hs['error']}"
                                                                           if hs["error"] else "")))
        if name == "codex":
            print(a.kv("codex block", "present" if w.block_state() else "absent"))
    # Rows T2: an outdated file is reinstalled; a new version waiting behind
    # a person's edit is their call.
    files = st["files"]
    waiting = [n for n, info in files.items() if info.get("upstream_waiting")]
    if any(info["state"] in ("outdated", "missing") for info in files.values()):
        _h.handoff("sibyl skills install")
    elif waiting:
        _h.handoff("done", later=[f"sibyl skills reset {n}" for n in waiting],
                   human=f"{', '.join(waiting)} {'was' if len(waiting) == 1 else 'were'} edited by you; "
                         "resetting replaces your edits")
    else:
        _h.handoff("done")
    return 0


# ----------------------------------------------------------------------
# sibyl skills proposals (R1, plan 3.3): Pro, local, explicit accept only
# ----------------------------------------------------------------------

def _short_id(pid: str) -> str:
    return str(pid or "")[:8]


def _skill_targets(args: argparse.Namespace, name: str) -> dict[str, Path]:
    """Where an accepted skill goes: ``--to <dir>`` only, else every harness
    folder present on this computer (Claude Code ~/.claude/skills, Codex
    ~/.codex/skills, Hermes $HERMES_HOME/skills)."""
    to = getattr(args, "to", None)
    if to:
        return {"dir": Path(to).expanduser() / name / "SKILL.md"}
    out: dict[str, Path] = {}
    for harness, w in _present_wirers(args).items():
        if harness == "claude-code":
            out[harness] = w.settings_path.parent / "skills" / name / "SKILL.md"
        elif harness == "codex":
            out[harness] = w.config_path.parent / "skills" / name / "SKILL.md"
        elif harness == "hermes":
            out[harness] = w.hermes_home / "skills" / name / "SKILL.md"
    return out


def _find_proposal(learner: Any, ref: str | None) -> Any:
    """A proposal by full id or a unique prefix of at least 6 characters."""
    from sibyl_memory_client.exceptions import NotFoundError
    ref = (ref or "").strip()
    if not ref:
        return None
    try:
        return learner.get_proposal(ref)
    except NotFoundError:
        pass
    if len(ref) < 6:
        return None
    hits = []
    for status in ("pending", "accepted", "rejected"):
        hits += [p for p in learner.list_proposals(status=status, limit=500) if p.id.startswith(ref)]
    return hits[0] if len(hits) == 1 else None


def _proposals(args: argparse.Namespace) -> int:
    from sibyl_memory_client.exceptions import EntitlementError, TierGateError, ValidationError
    from sibyl_memory_client.lifecycle import proposals as PR
    from sibyl_memory_client.lifecycle import state as ST
    action = getattr(args, "proposal_action", None) or "list"
    target = getattr(args, "target", None)
    now = time.time()
    store = ST.LifecycleState(_home())
    print(a.eyebrow("skill proposals"))
    if action in ("mute", "unmute"):
        # Local settings only; they apply on Pro, where proposals exist.
        if not target:
            print(a.warn_line(f"Usage: sibyl skills proposals {action} <kind>"
                              + (" | <slug>" if action == "unmute" else "")))
            print(a.dim("  kinds: " + ", ".join(PR.KINDS)))
            _h.handoff("sibyl skills proposals")
            return 1
        if action == "mute":
            if target not in PR.KINDS:
                print(a.warn_line(f"Unknown kind {target!r}. Kinds: {', '.join(PR.KINDS)}"))
                _h.handoff("sibyl skills proposals")
                return 1
            days = getattr(args, "days", None) or PR.MUTE_DAYS_DEFAULT
            if not (1 <= int(days) <= 365):
                print(a.warn_line("--days must be between 1 and 365."))
                _h.handoff("sibyl skills proposals")
                return 1

            def _mute(st: dict[str, Any]) -> None:
                st.setdefault("skills", ST.default_skills()).setdefault("muted_kinds", {})[target] = \
                    now + int(days) * 86400
            store.update(_mute)
            print(a.kv("muted", f"{target} for {int(days)} days (nothing of this kind is proposed)"))
            _h.handoff("done")
            return 0

        def _unmute(st: dict[str, Any]) -> str:
            sk = st.setdefault("skills", ST.default_skills())
            if target in PR.KINDS:
                sk.setdefault("muted_kinds", {}).pop(target, None)
                return f"{target}: proposed again"
            sk.setdefault("unmuted", {})[target[:80]] = now
            return f"{target[:80]}: may be proposed again (earlier rejections no longer count)"
        print(a.kv("unmuted", store.update(_unmute)))
        _h.handoff("done")
        return 0
    if not _creds(args):
        print(a.warn_line("Not activated. Run `sibyl init`."))
        print(a.dim("  Skill proposals are part of Pro; nothing is proposed without an account."))
        _h.handoff("sibyl init")
        return 0
    client = _client(args)
    try:
        learner = client.learner()
    except TierGateError:
        print(a.kv("proposals", "on Pro · Sibyl proposes skills from your sessions; you decide"))
        print(a.dim("  On the free plan, /sibyl-save still offers skills when you run it yourself."))
        _h.handoff("done", later=("sibyl upgrade",))
        return 0
    except EntitlementError as e:
        print(a.warn_line(str(e).splitlines()[0]))
        _entitlement_handoff(e)
        return 1
    if action == "list":
        st = store.load()
        muted = PR.muted_kinds(st, now)
        rows = learner.list_proposals(status="pending", limit=50)
        if not rows:
            print(a.kv("pending", "none"))
            print(a.dim("  Sibyl proposes a skill when the same work recurs across your sessions."))
            _h.handoff("done")
            return 0
        print(a.kv("pending", f"{len(rows)} · accept or reject: sibyl skills proposals accept|reject <id>"))
        for p in rows:
            ts = sorted(t for t in (ST._iso_to_epoch(e.get("ts")) for e in p.evidence
                                    if isinstance(e, dict)) if t is not None)
            seen = (f"{_fmt_ts(ts[0])} .. {_fmt_ts(ts[-1])}" if ts else "?")
            flag = " · kind muted" if p.pattern_kind in muted else ""
            print(a.kv(_short_id(p.id), f"{p.proposed_title or p.proposed_slug} · {p.pattern_kind} · "
                                        f"confidence {p.confidence:.2f} · {len(p.evidence)} events · "
                                        f"{seen}{flag}"))
        _h.handoff("sibyl skills proposals show <id>",
                   human="which proposals to accept or reject (ids above; a skill file is written only "
                         "on accept)")
        return 0
    p = _find_proposal(learner, target)
    if p is None:
        print(a.warn_line(f"No proposal {target!r}. Run `sibyl skills proposals` for the ids."))
        _h.handoff("sibyl skills proposals")
        return 1
    if action == "show":
        print(a.kv("id", p.id))
        print(a.kv("title", p.proposed_title or p.proposed_slug))
        print(a.kv("kind", f"{p.pattern_kind} · confidence {p.confidence:.2f} · {len(p.evidence)} events"))
        print(a.kv("status", p.status))
        print()
        print(p.proposed_body)
        if p.status == "pending":
            _h.handoff(f"sibyl skills proposals accept {_short_id(p.id)}",
                       later=(f"sibyl skills proposals reject {_short_id(p.id)}",),
                       human="accept this proposal (writes the skill file) or reject it; your call")
        else:
            _h.handoff("done")
        return 0
    if action == "reject":
        if p.status != "pending":
            print(a.warn_line(f"This proposal is {p.status}; only a pending one can be rejected."))
            _h.handoff("sibyl skills proposals")
            return 1
        try:
            client.reject_skill_proposal(p.id, note=getattr(args, "note", None))
        except (ValidationError, EntitlementError) as e:
            print(a.warn_line(str(e).splitlines()[0]))
            if isinstance(e, EntitlementError):
                _entitlement_handoff(e)
            else:
                _h.handoff("sibyl skills proposals")
            return 1
        print(a.kv("rejected", f"{p.proposed_slug} · not proposed again for {PR.COOL_OFF_DAYS} days; "
                               f"a second rejection mutes it (sibyl skills proposals unmute {p.proposed_slug})"))
        _h.handoff("done")
        return 0
    # accept
    if p.status == "rejected":
        print(a.warn_line("This proposal was rejected. It is not proposed again for a while; "
                          f"unmute with sibyl skills proposals unmute {p.proposed_slug}."))
        _h.handoff("done", later=(f"sibyl skills proposals unmute {p.proposed_slug}",))
        return 1
    if p.status == "pending":
        try:
            client.accept_skill_proposal(p.id, note=getattr(args, "note", None))
        except (ValidationError, EntitlementError) as e:
            print(a.warn_line(str(e).splitlines()[0]))
            if isinstance(e, EntitlementError):
                _entitlement_handoff(e)
            else:
                _h.handoff("sibyl skills proposals")
            return 1
        except Exception as e:  # the cap gate (a free cap cannot apply on Pro, but never a traceback)
            print(a.warn_line(f"Not accepted: {type(e).__name__}. Nothing was written."))
            nxt, human = _h.from_exception(e, default="sibyl status")
            _h.handoff(nxt, human=human)
            return 1
        print(a.kv("accepted", f"{p.proposed_slug} · reference copy in memory as skill/{p.proposed_slug}"))
    else:
        print(a.kv("accepted", f"{p.proposed_slug} (earlier) · writing its skill files again"))
    from sibyl_memory_client import skills as SK
    name = SK.skill_name(p.proposed_slug)
    text = SK.proposal_skill_text(p)
    problems = SK.validate_skill_text(text, name)
    if problems:
        print(a.warn_line("The skill file did not pass validation: " + "; ".join(problems)))
        _h.handoff("done", human="this proposal's skill file did not pass validation (above); "
                                 "reject it or write it by hand")
        return 1
    targets = _skill_targets(args, name)
    rc = 0
    for harness, path in targets.items():
        st_ = SK.write_skill(_home(), path, text, harness=harness, slug=p.proposed_slug)
        label = {"installed": "written", "unchanged": "already there", "updated": "updated",
                 "exists": "a different file is already there (kept; use --to <dir>)"}.get(st_, st_)
        print(a.kv(harness, f"{path} · {label}"))
        if st_.startswith("refused") or st_ == "exists":
            rc = 1
    if not targets:
        print(a.dim("  No Claude Code, Codex or Hermes folder on this computer: the skill is in memory as "
                    f"skill/{p.proposed_slug}. Write the file with --to <dir>."))
    if (_home() / ".cursor").exists():
        print(a.dim(f"  Cursor: no skill folder is written; the reference copy is skill/{p.proposed_slug}."))
    if rc:
        _h.handoff(f"sibyl skills proposals accept {_short_id(p.id)} --to <dir>",
                   human="a folder to write the skill file to (a different file is already in the "
                         "usual place)")
    else:
        _h.handoff("done")
    return rc


# ----------------------------------------------------------------------
# sibyl autosave (R1, plan 2.4)
# ----------------------------------------------------------------------

def _autosave_summary(st: dict[str, Any], tid: str | None, now: float) -> str:
    from sibyl_memory_client.lifecycle import state as ST
    cfg = ST.autosave_settings(st)
    if not cfg["enabled"]:
        return "off · sibyl autosave on"
    parts = [f"every {cfg['interval_seconds'] // 60} min of active work"]
    t = st.get("tenants", {}).get(tid or "", {}) if tid else {}
    n = ST.nudges_today(t, now) if t else 0
    parts.append(f"{n} save{'s' if n != 1 else ''} requested today")
    last = t.get("last_nudge_at") if t else None
    if isinstance(last, (int, float)):
        from ._timefmt import utc_hm
        parts.append("last " + (utc_hm(last) or "?"))
    return " · ".join(parts)


def cmd_autosave(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import state as ST
    op = getattr(args, "action", None) or "status"
    store = ST.LifecycleState(_home())
    print()
    if op == "every":
        minutes = getattr(args, "minutes", None)
        lo, hi = ST.AUTOSAVE_INTERVAL_MIN // 60, ST.AUTOSAVE_INTERVAL_MAX // 60
        if minutes is None or not (lo <= int(minutes) <= hi):
            print(a.warn_line(f"Usage: sibyl autosave every <minutes> ({lo} to {hi})."))
            _h.handoff("sibyl autosave")
            return 1

        def _every(st: dict[str, Any]) -> None:
            cfg = ST.autosave_settings(st)
            cfg["interval_seconds"] = int(minutes) * 60
            cfg["enabled"] = True
            st["autosave"] = cfg
        store.update(_every)
    elif op in ("on", "off"):
        def _onoff(st: dict[str, Any]) -> None:
            cfg = ST.autosave_settings(st)
            cfg["enabled"] = op == "on"
            st["autosave"] = cfg
        store.update(_onoff)
    st = store.load()
    tid = _tenant(_creds(args))
    pro, _tier = _pro(args)
    print(a.eyebrow("automatic save"))
    print(a.kv("autosave", _autosave_summary(st, tid, time.time())))
    cfg = ST.autosave_settings(st)
    print(a.kv("rule", f"once early (after 5 memory writes or a compaction), then every "
                       f"{cfg['interval_seconds'] // 60} min of active work; at most "
                       f"{cfg['max_per_session']} a session and {cfg['max_per_day']} a day"))
    if not pro:
        print(a.kv("plan", "automatic save is on Pro · this setting applies the day you upgrade"))
    if not st.get("reminders", True):
        print(a.dim("  reminders are off (sibyl reminders on): nothing is requested meanwhile."))
    print(a.dim("  Each automatic save is one extra model turn that reads and writes memory."))
    _h.handoff("done", later=() if pro else ("sibyl upgrade",))
    return 0


# ----------------------------------------------------------------------
# sibyl refresh (R1, plan 5.2): no new endpoint
# ----------------------------------------------------------------------

def _cap_label(cap: Any) -> str:
    if cap is None:
        return "none"
    if isinstance(cap, int) and not isinstance(cap, bool):
        return f"{cap / (1024 * 1024):.0f} MB"
    return "?"


def _devices_label(acct: Any) -> str | None:
    from sibyl_memory_client.exceptions import device_limit_info
    info = device_limit_info(acct)
    if info is None:
        return None
    lim = info["limit"]
    return f"{info['n']} of {lim}" if lim is not None else f"{info['n']} (no limit)"


def _refresh_diff(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    bt, at = before.get("tier"), after.get("tier")
    src = after.get("src")
    if bt != at:
        rows.append(("tier", f"{bt or 'none'} -> {at or 'none'}" + (f" ({src})" if isinstance(src, str) and src
                                                                     and src != at else "")))
    if "cap" in before or "cap" in after:
        bc, ac = before.get("cap", "?"), after.get("cap", "?")
        if bc != ac:
            rows.append(("cap", f"{_cap_label(bc)} -> {_cap_label(ac)}"))
    bd, ad = _devices_label(before.get("acct")), _devices_label(after.get("acct"))
    if ad is not None and bd != ad:
        rows.append(("computers", f"{bd} -> {ad}" if bd else ad))
    be = (before.get("acct") or {}).get("email_verified")
    ae = (after.get("acct") or {}).get("email_verified")
    if ae is True and be is not True:
        rows.append(("email", "verified"))
    if before.get("state") != after.get("state"):
        rows.append(("access", f"{before.get('state') or 'none'} -> {after.get('state') or 'none'}"))
    return rows


def cmd_refresh(args: argparse.Namespace) -> int:
    """Check the account now (one /entitlement call), refresh the procedures,
    shims and hooks for this computer, and say what changed. Exits 0 with no
    account (it says what to do), 2 when the server could not be reached."""
    from . import _account_gate as G
    from sibyl_memory_client import skills as SK
    print()
    cred = _cred(args)
    creds = _creds(args)
    if not creds:
        print(a.warn_line("Not activated. Run `sibyl init`."))
        print(a.dim("  sibyl refresh picks up a plan change on a signed-in computer; nothing to refresh yet."))
        from . import cli as _cli
        _h.handoff(_cli._init_next())
        return 0
    print(a.eyebrow("account"))
    before = G.status_info(cred, refresh=False) or {}
    after = G.status_info(cred, refresh=True)
    rc = 0
    if after is None:
        print(a.kv("account", "sibyl-memory-client is not installed", value_color="warn"))
        _h.handoff("pip install -U sibyl-memory-client")
        return 2
    outcome = after.get("refreshed")
    nxt, human = G.access_handoff(after)
    if outcome in ("ok", "shared"):
        diff = _refresh_diff(before, after)
        for k, v in diff:
            print(a.kv(k, v, value_color="ok"))
        if not diff:
            print(a.kv("checked", f"no change · {after.get('tier') or 'no plan'} · "
                                  + G.access_row(after)))
        else:
            print(a.kv("access", G.access_row(after)))
    elif outcome == "denied":
        print(a.kv("access", G.access_row(after), value_color="err"))
        rc = 1
    else:
        print(a.kv("account", "could not reach the server; this is the last check-in", value_color="warn"))
        print(a.kv("access", G.access_row(after)))
        rc = 2
        nxt, human = _h.RETRY, "connect this computer to the internet once"
    dev = _devices_label(after.get("acct"))
    if dev:
        print(a.kv("computers", dev))
    print()
    print(a.eyebrow("this computer"))
    rep = SK.install(_home())
    print(a.kv("procedures", f"v{rep.version} " + ", ".join(f"{n} {st_}" for n, st_ in rep.results.items())))
    for m in rep.messages:
        print(a.dim("  " + m))
    from .setup import install_shims
    for name, w in _present_wirers(args).items():
        lines = install_shims(name, w.skill_shim_targets())
        if name == "codex":
            lines = w.wire_block() + lines
        if name == "claude-code" and w.hooks_state().get("present"):
            lines += w.wire_hooks()          # refresh only hooks already wired (sibyl setup adds them)
        for ln in lines:
            print(a.dim(f"  {name}: {ln}"))
    print(a.dim("  Hooks and shims are the same on every plan: Pro behaviour switches on because this "
                "computer's entitlement says so."))
    print(a.dim("  Running agents pick this up on their next call."))
    status_rows(args, creds)
    print()
    _h.handoff(nxt, human=human)
    return rc


# ----------------------------------------------------------------------
# sibyl reminders
# ----------------------------------------------------------------------

def cmd_reminders(args: argparse.Namespace) -> int:
    from sibyl_memory_client.lifecycle import state as ST
    op = getattr(args, "state", None) or "status"
    store = ST.LifecycleState(_home())
    print()
    if op in ("on", "off", "quiet"):
        def _set(st: dict[str, Any]) -> None:
            if op == "on":
                st["reminders"], st["quiet"] = True, False
            elif op == "off":
                st["reminders"] = False
            else:
                st["reminders"], st["quiet"] = True, True
        store.update(_set)
    st = store.load()
    val = "off" if not st.get("reminders", True) else ("quiet (Pro reports silenced)" if st.get("quiet") else "on")
    print(a.kv("reminders", val))
    _h.handoff("done")
    return 0


# ----------------------------------------------------------------------
# sibyl hook
# ----------------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    # The installed `sibyl` command reaches hooks.main_hook directly (see
    # _entry.py) without importing this module; this is the same path.
    from .hooks import main_hook
    return main_hook(getattr(args, "event", ""))


# ----------------------------------------------------------------------
# sibyl status rows
# ----------------------------------------------------------------------

def status_rows(args: argparse.Namespace, creds: dict[str, Any] | None) -> None:
    """The `lifecycle` eyebrow of `sibyl status` (spec 4.3). Never raises."""
    try:
        _status_rows(args, creds or {})
    except Exception:
        pass


def _status_rows(args: argparse.Namespace, creds: dict[str, Any]) -> None:
    from sibyl_memory_client import skills as SK
    from sibyl_memory_client.lifecycle import policy as P
    from sibyl_memory_client.lifecycle import state as ST
    from sibyl_memory_client.lifecycle.runner import measure_for_reminder
    tid = _tenant(creds)
    pro, _tier = _pro(args)
    _legacy_notice()
    st = ST.LifecycleState(_home()).load()
    t = st.get("tenants", {}).get(tid or "", {}) if tid else {}
    marker = ST.read_save_marker(_db(args), tid) if tid else None
    last = max([x for x in (marker, t.get("last_save_at")) if isinstance(x, (int, float))], default=None)
    print()
    print(a.eyebrow("lifecycle"))
    save = f"last {_fmt_ts(last)} · {int(t.get('writes_since_save') or 0)} writes since · "
    save += ("automatic · " + _autosave_summary(st, tid, time.time()) if pro
             else "manual (automatic on Pro)")
    print(a.kv("save", save))
    rec = measure_for_reminder(_home(), Path.cwd())
    if rec is not None and rec["files"]:
        heavy = [f["file"] for f in rec["files"] if f["heavy"]]
        ctx = f"~{rec['total_tok'] / 1000:.1f}k tok always-load ({rec['weight']})"
        if heavy:
            ctx += f" · heavy: {', '.join(heavy[:3])}"
    else:
        ctx = "no always-load files found here"
    pol = P.load(_home())
    if pro:
        ctx += f" · policy {pol['mode']}"
        if pol["auto_files"]:
            ctx += f" ({len(pol['auto_files'])} file{'s' if len(pol['auto_files']) != 1 else ''} auto)"
    elif pol["auto_files"]:
        ctx += f" · {len(pol['auto_files'])} file(s) set to auto (runs on Pro)"
    print(a.kv("context", ctx))
    sst = SK.status(_home())
    if sst["exists"]:
        parts = [f"v{sst['installed_version'] or '?'}"]
        for n, info in sst["files"].items():
            parts.append(f"{n} {'edited by you' if info['state'] == 'edited' else info['state']}")
        harnesses = sorted({i.get('harness') for i in sst['shims'].values() if i.get('state') == 'ok'})
        if harnesses:
            parts.append(", ".join(f"{h} wired" for h in harnesses))
        print(a.kv("skills", " · ".join(parts)))
    else:
        print(a.kv("skills", "not installed · sibyl skills install"))
    if pro and tid:
        n = _pending_proposals(_db(args), tid)
        print(a.kv("proposals", f"{n} skill proposal{'s' if n != 1 else ''} pending"
                   + (" · sibyl skills proposals" if n else "")))
    rem = "off" if not st.get("reminders", True) else ("quiet" if st.get("quiet") else "on")
    print(a.kv("reminders", rem))


def _pending_proposals(db: Path, tid: str) -> int:
    """How many proposals are pending, read-only (no gate, no network)."""
    import sqlite3
    try:
        if not db.is_file() or db.is_symlink():
            return 0
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT count(*) FROM skill_proposals WHERE tenant_id = ? AND "
                              "status = 'pending'", (tid,)).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


__all__ = ["cmd_autosave", "cmd_debloat", "cmd_hook", "cmd_refresh", "cmd_reminders", "cmd_save",
           "cmd_skills", "status_rows"]
