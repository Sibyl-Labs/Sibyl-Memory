"""Plan and apply a context trim, loss-free (spec plugin-save-debloat 5.4).

Order of an apply, each step only after the previous one succeeded:

1. refuse, before anything is written: a file that is itself a symlink, a
   directory, a file whose REAL location (its folder resolved with
   ``os.path.realpath``, the final name kept) is outside the real home and the
   real project root, a file not in the measured surface, a ``never`` pattern (config files always), a
   file over 2 MB, a file modified in the last 10 minutes, anything that is
   not Markdown or plain text, a ``.cursor/rules`` file (measured, never
   trimmed), a non-UTF-8 file;
2. plan with the safe splitter; "no safe trim" moves nothing;
3. back up the original bytes to ``~/.sibyl-memory/debloat/backups/<ts>/``,
   fsync, and re-verify the copy (byte count and sha256); an apply that ends
   without rewriting the source removes its backup again, and at most
   ``MAX_BACKUPS_PER_FILE`` backups of one file are kept (the newest);
4. store the history: Pro = ``set_reference("debloat/<file>/<date>", ...)``
   through the gated client (then read back and compared); free = the sibling
   ``*-history`` file (no client is constructed); ``memory+sibling`` = both;
5. only then rewrite the source atomically: the CURRENT lines plus one
   pointer line (the file must be unchanged since it was read);
6. re-measure and record.

Every read and write goes to the file's real location (``resolved_target``),
so a symlinked folder above it (Fedora Atomic's ``/home -> var/home``, a
stow-style ``~/.claude -> ~/dotfiles/claude``, a project opened through a
symlink) neither blocks the trim nor is written through: no folder on the
written path is a symlink, and the writer's own parent check holds. The
backup of an apply that ends without rewriting is removed only when the
source is verifiably the file that was read (same size, mtime and inode);
anything else, including an interruption right after the rewrite, keeps it.

A failure at any step leaves the source untouched (the rewrite is last). A
re-apply after a failed rewrite finds the memory copy with the same body and
reuses it. Nothing here sends file content anywhere; the only network is the
gated client's own entitlement refresh and cap check (sizes, not bytes).
"""
from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir, fsync_path, read_bounded
from . import splitter as S
from .measure import CONFIG_FILES, CONTEXT_FILES, measure, tok
from .state import memory_dir, suppressed

MAX_FILE_BYTES = 2 * 1024 * 1024
RECENT_EDIT_SECONDS = 600
LOCK_NAME = "debloat.lock"
LOCK_STALE_SECONDS = 60
MAX_BACKUPS_PER_FILE = 20
MAX_JOURNAL_BYTES = 256 * 1024
STALE_TMP_SECONDS = 60


@dataclass
class Candidate:
    label: str          # "AGENTS.md" (project) or "~/CLAUDE.md" (home)
    path: Path
    key: str            # root-relative name ("AGENTS.md", ".claude/CLAUDE.md")
    tok: int = 0
    trimmable: bool = True
    why_not: str = ""


@dataclass
class ApplyResult:
    file: str
    applied: bool
    reason: str
    key: str | None = None
    backup: str | None = None
    sibling: str | None = None
    tok_before: int = 0
    tok_after: int = 0
    sections: list[str] = field(default_factory=list)
    reused_memory_copy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "applied": self.applied, "reason": self.reason, "key": self.key,
                "backup": self.backup, "sibling": self.sibling, "tok_before": self.tok_before,
                "tok_after": self.tok_after, "sections": list(self.sections),
                "reused_memory_copy": self.reused_memory_copy}


def debloat_dir(home: Path | str | None = None) -> Path:
    return memory_dir(home) / "debloat"


def _lexical_under(p: Path, base: Path) -> bool:
    ap = Path(os.path.normpath(str(p)))
    bp = Path(os.path.normpath(str(base)))
    try:
        return ap == bp or ap.is_relative_to(bp)
    except ValueError:
        return False


def _never(policy: dict[str, Any] | None, key: str, name: str) -> bool:
    pats = list((policy or {}).get("never") or []) + ["*.json", "*.jsonl", "*.toml", "*.yaml",
                                                     "*.yml", "*.env*"]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(key, p) for p in pats)


def trimmable_reason(key: str, path: Path, policy: dict[str, Any] | None,
                     explicit: set[str]) -> str:
    """'' when the file may be trimmed, else the reason."""
    if key in CONFIG_FILES:
        return "configuration file (backed up, never trimmed)"
    if key.startswith(".cursor/rules"):
        return ".cursor/rules is measured, never trimmed"
    if _never(policy, key, path.name):
        return "excluded by the never list"
    if S.kind_for(path.name) is None:
        return "not Markdown or plain text"
    if key not in CONTEXT_FILES and key not in explicit:
        return "not an instruction file the scan knows"
    return ""


def candidates(home: Path | str, cwd: Path | str, policy: dict[str, Any] | None = None,
               *, now: datetime | None = None) -> tuple[list[Candidate], dict[str, Any]]:
    """Every measured file as a Candidate, heaviest first, with trimmability."""
    policy = policy or {}
    home_p, cwd_p = Path(home), Path(cwd)
    rec = measure(cwd_p, home=home_p, surface=policy.get("surface"),
                  thresholds=policy.get("thresholds"), per_file_soft=policy.get("perFileSoft"),
                  now=now)
    explicit: set[str] = set()
    try:
        from .measure import load_config
        cfg, _err, configured = load_config(cwd_p)
        if configured:
            explicit |= set(cfg.get("surface") or [])
    except Exception:
        pass
    if isinstance(policy.get("surface"), list):
        explicit |= set(policy["surface"])
    out: list[Candidate] = []
    paths = rec.get("_paths", {})
    for f in sorted(rec["files"], key=lambda f: -f["tok"]):
        label = f["file"]
        p = Path(paths.get(label, ""))
        key = label[2:] if label.startswith("~/") else label
        why = trimmable_reason(key, p, policy, explicit)
        out.append(Candidate(label, p, key, f["tok"], not why, why))
    return out, rec


def find_candidate(label: str, home: Path | str, cwd: Path | str,
                   policy: dict[str, Any] | None = None) -> tuple[Candidate | None, dict[str, Any]]:
    cands, rec = candidates(home, cwd, policy)
    for c in cands:
        if c.label == label or c.key == label or str(c.path) == str(label):
            return c, rec
    return None, rec


def _read(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    try:
        st = path.lstat()
    except OSError:
        return None, None
    data = read_bounded(path, MAX_FILE_BYTES)
    return data, st


def resolved_target(p: Path | str) -> Path:
    """Where ``p`` really lives: its folder with every symlink resolved
    (``os.path.realpath``), the final component kept as given (a symlinked
    FILE is refused, never followed). The trim reads and writes this path, and
    the automatic-trim authorisation and the backup slot are keyed on it, so a
    file reached through a symlinked folder has one identity."""
    p = Path(p)
    return Path(os.path.realpath(os.path.abspath(str(p.parent)))) / p.name


def real_dir(d: Path | str) -> Path:
    return Path(os.path.realpath(os.path.abspath(str(d))))


def inside_boundary(p: Path | str, home: Path | str, cwd: Path | str) -> bool:
    """``p``'s real location is under the real home or the real project root."""
    t = resolved_target(p)
    return _lexical_under(t, real_dir(home)) or _lexical_under(t, real_dir(cwd))


def refusal(c: Candidate, *, home: Path, cwd: Path, surface_labels: set[str] | None,
            now: float | None = None, check_recent: bool = True) -> str:
    """'' when ``c`` may be trimmed now, else the reason. Decided before
    anything is written."""
    if c.path.is_symlink():
        return "refused: the file is a symlink"
    try:
        p = resolved_target(c.path)
    except (OSError, ValueError):
        return "refused: cannot resolve the file's folder"
    if p.is_symlink():
        return "refused: the file is a symlink"
    if not p.exists():
        return "refused: file not found"
    if not p.is_file():
        return "refused: not a regular file"
    if not inside_boundary(p, home, cwd):
        return "refused: outside your home and this project"
    if surface_labels is not None and c.label not in surface_labels:
        return "refused: not in the measured surface"
    if not c.trimmable:
        return f"refused: {c.why_not}"
    try:
        st = p.stat()
    except OSError:
        return "refused: cannot stat the file"
    if st.st_size > MAX_FILE_BYTES:
        return "refused: over 2 MB"
    now = time.time() if now is None else now
    if check_recent and now - st.st_mtime < RECENT_EDIT_SECONDS:
        return "refused: modified in the last 10 minutes (someone may be editing it)"
    return ""


def plan(c: Candidate, policy: dict[str, Any] | None = None, *, now: datetime | None = None
         ) -> tuple[S.Plan | None, str]:
    """(plan, reason). Reads the file (bounded); never writes."""
    policy = policy or {}
    data, _st = _read(c.path)
    if data is None:
        return None, "unreadable, a symlink, or over 2 MB"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    kind = S.kind_for(c.path.name) or "markdown"
    pl = S.split(text, kind=kind, now=now,
                 history_after_days=policy.get("history_after_days", S.HISTORY_AFTER_DAYS),
                 history_heading_regex=policy.get("history_heading_regex", S.HISTORY_HEADING_REGEX))
    return pl, pl.reason


def backup_rel(p: Path, home: Path, cwd: Path | None = None) -> str:
    """Where a file's backups live under ``backups/<ts>/``: the path relative
    to the REAL home for a file whose real location is under it, else
    ``abs/<its real absolute path>``. Unique per FILE (an earlier
    ``project/<name>`` form made two projects' AGENTS.md share one slot, so
    an undo in one project could pick the other's backup and the per-file cap
    counted them together), and the same whether the file was reached through
    a symlinked folder or not."""
    t = resolved_target(p)
    rh = real_dir(home)
    if _lexical_under(t, rh) and t != rh:
        return t.relative_to(rh).as_posix()
    ap = os.path.normpath(str(t))
    drive, rest = os.path.splitdrive(ap)
    parts = [x for x in Path(rest).parts if x not in ("/", "\\", os.sep)]
    head = [drive.rstrip(":").replace("\\", "_").replace("/", "_")] if drive else []
    return "/".join(["abs"] + head + parts)


_backup_rel = backup_rel


def _stamp(now: float | None = None) -> str:
    now = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H_%M_%S", time.gmtime(now)) + f"_{int((now % 1) * 1e6):06d}"


def _display_home(p: Path, home: Path) -> str:
    for h in (Path(home), real_dir(home)):
        try:
            return "~/" + str(Path(p).relative_to(h))
        except ValueError:
            continue
    return str(p)


def backup(path: Path, data: bytes, *, home: Path, cwd: Path, now: float | None = None,
           suffix: str = "") -> Path:
    """Copy ``data`` (the bytes just read from ``path``) into a fresh backup
    directory and re-verify the copy on disk. Raises on any mismatch."""
    root = debloat_dir(home) / "backups"
    ensure_private_dir(memory_dir(home))
    ensure_private_dir(debloat_dir(home))
    ensure_private_dir(root)
    d = root / (_stamp(now) + suffix)
    d.mkdir(mode=0o700)
    target = d / _backup_rel(path, home, cwd)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_bytes(target, data)
    fsync_path(target)
    fsync_path(d)
    got = read_bounded(target, MAX_FILE_BYTES)
    if got is None or len(got) != len(data) or hashlib.sha256(got).digest() != hashlib.sha256(data).digest():
        raise OSError("backup verification failed")
    return target


def backups_root(home: Path | str) -> Path:
    return debloat_dir(home) / "backups"


def _stamp_dir(bpath: Path, home: Path) -> Path | None:
    """The ``backups/<ts>`` folder a backup file lives in (None when the path
    is not under the backups folder)."""
    root = backups_root(home)
    try:
        rel = Path(os.path.normpath(str(bpath))).relative_to(Path(os.path.normpath(str(root))))
    except ValueError:
        return None
    return root / rel.parts[0] if rel.parts else None


def discard_backup(bpath: Path | str | None, home: Path | str) -> None:
    """Remove the ``backups/<ts>`` folder of an apply that did not rewrite its
    source (the backup is only needed once the file has changed)."""
    if not bpath:
        return
    d = _stamp_dir(Path(bpath), Path(home))
    try:
        if d is not None and d.is_dir() and not d.is_symlink():
            shutil.rmtree(d)
    except OSError:
        pass


def backups_of(rel: str, home: Path | str) -> list[Path]:
    """Every backup copy of the file stored under ``rel`` (``_backup_rel``),
    oldest first (the folder names are UTC timestamps)."""
    root = backups_root(home)
    out: list[Path] = []
    try:
        if not root.is_dir() or root.is_symlink():
            return out
        for d in sorted((x for x in root.iterdir() if x.is_dir() and not x.is_symlink()),
                        key=lambda x: x.name):
            b = d / rel
            if b.is_file() and not b.is_symlink():
                out.append(b)
    except OSError:
        pass
    return out


def prune_backups(rel: str, home: Path | str, keep: int = MAX_BACKUPS_PER_FILE) -> int:
    """Keep the newest ``keep`` backups of one file; returns how many went."""
    old = backups_of(rel, home)[:-keep] if keep > 0 else backups_of(rel, home)
    gone = 0
    for b in old:
        d = _stamp_dir(b, Path(home))
        try:
            b.unlink()
            gone += 1
            parent = b.parent
            while d is not None and parent != d.parent and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                if parent == d:
                    break
                parent = parent.parent
        except OSError:
            continue
    return gone


def sweep_stale_tmp(path: Path, *, now: float | None = None) -> int:
    """Remove ``.<name>.*.sibyl-tmp`` files next to ``path`` older than a
    minute: what a writer killed between mkstemp and ``os.replace`` leaves
    (the Claude Code hook timeout). Never follows a symlink."""
    now = time.time() if now is None else now
    n = 0
    try:
        parent = Path(path).parent
        if parent.is_symlink() or not parent.is_dir():
            return 0
        for t in parent.glob("." + Path(path).name + ".*.sibyl-tmp"):
            try:
                st = t.lstat()
                if t.is_symlink() or not t.is_file() or now - st.st_mtime < STALE_TMP_SECONDS:
                    continue
                t.unlink()
                n += 1
            except OSError:
                continue
    except OSError:
        return n
    return n


def verify_backup(backup_path: Path, data: bytes) -> bool:
    got = read_bounded(Path(backup_path), MAX_FILE_BYTES)
    return got is not None and got == data


def sibling_path(path: Path) -> Path:
    name = path.name
    if name.startswith(".") and name.count(".") == 1:        # .cursorrules
        return path.with_name(name + "-history")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return path.with_name(name + "-history")
    return path.with_name(f"{stem}-history.{ext}")


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def pointer_line(*, key: str | None, sibling: str | None, date: str, backup_display: str) -> str:
    where = []
    if key:
        where.append(f"to Sibyl Memory ({key}, recall with memory_search)")
    if sibling:
        where.append(f"to {sibling}")
    return (f"<!-- history moved {' and '.join(where)} on {date}; "
            f"backup: {backup_display} -->")


def with_pointer(current: str, pointer: str, eol: str) -> str:
    sep = "" if (current == "" or current.endswith("\n")) else eol
    return current + sep + pointer + eol


def strip_pointer(new_text: str) -> str:
    """Inverse of ``with_pointer`` (for the round-trip check and ``undo``)."""
    for eol in ("\r\n", "\n"):
        if new_text.endswith(eol):
            body = new_text[: -len(eol)]
            idx = body.rfind(eol)
            last = body[idx + len(eol):] if idx != -1 else body
            if last.startswith("<!-- history moved ") and last.endswith(" -->"):
                return body[: idx + len(eol)] if idx != -1 else ""
    return new_text


def _sibling_ends_with(prev: bytes, name: str, history: bytes) -> bool:
    """``prev`` already ends with ``history`` directly under a ``<!-- moved
    from <name> on <any date> -->`` header line."""
    if not history or not prev.endswith(history):
        return False
    head = prev[: len(prev) - len(history)]
    pat = (rb"(?:^|\n)<!-- moved from " + re.escape(name.encode("utf-8"))
           + rb" on \d{4}-\d{2}-\d{2} -->\r?\n\Z")
    return re.search(pat, head) is not None


def _unique_key(client: Any, base: str, history: str) -> tuple[str, bool]:
    for i in range(1, 51):
        key = base if i == 1 else f"{base}-{i}"
        ref = client.get_reference(key)
        if ref is None:
            return key, False
        if ref.get("body") == history:
            return key, True
    raise RuntimeError("no free reference key")


def apply_one(c: Candidate, *, home: Path | str, cwd: Path | str, destination: str,
              client: Any = None, policy: dict[str, Any] | None = None,
              surface_labels: set[str] | None = None, now: float | None = None,
              check_recent: bool = True) -> ApplyResult:
    """Apply the splitter plan for one file. ``destination``: ``sibling``
    (free; no client used), ``memory`` or ``memory+sibling`` (Pro; the caller
    has checked ``automation_allowed`` or the person's explicit choice, and
    the client's own gate refuses a locked device anyway)."""
    home_p, cwd_p = Path(home), Path(cwd)
    now_f = time.time() if now is None else now
    res = ApplyResult(c.label, False, "")
    why = refusal(c, home=home_p, cwd=cwd_p, surface_labels=surface_labels, now=now_f,
                  check_recent=check_recent)
    if why:
        res.reason = why
        return res
    # Work on the file's real location from here on: nothing below writes
    # through a symlinked folder, and the backup slot is the real path's.
    c = dataclasses.replace(c, path=resolved_target(c.path))
    sweep_stale_tmp(c.path, now=now_f)
    read: dict[str, Any] = {}
    try:
        return _apply_checked(c, res, home_p=home_p, cwd_p=cwd_p, destination=destination,
                              client=client, policy=policy, now_f=now_f, read=read)
    finally:
        if not res.applied and res.backup and _source_unchanged(c.path, read.get("st0")):
            # Nothing was rewritten (the source is still the very file that
            # was read): the backup is not needed. Any doubt keeps it.
            discard_backup(res.backup, home_p)
            res.backup = None
        elif res.applied or res.backup:
            prune_backups(_backup_rel(c.path, home_p, cwd_p), home_p)


def _source_unchanged(path: Path, st0: os.stat_result | None) -> bool:
    """True only when ``path`` is verifiably the file read as ``st0``: same
    size, mtime and inode (the atomic rewrite always makes a new inode). A
    missing file, an unreadable one, or no ``st0`` is not verifiable."""
    if st0 is None:
        return False
    try:
        st = path.lstat()
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev) == \
        (st0.st_size, st0.st_mtime_ns, st0.st_ino, st0.st_dev)


def _apply_checked(c: Candidate, res: ApplyResult, *, home_p: Path, cwd_p: Path, destination: str,
                   client: Any, policy: dict[str, Any] | None, now_f: float,
                   read: dict[str, Any] | None = None) -> ApplyResult:
    data, st0 = _read(c.path)
    if data is None or st0 is None:
        res.reason = "refused: unreadable, a symlink, or over 2 MB"
        return res
    if read is not None:
        read["st0"] = st0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        res.reason = "refused: not UTF-8 text"
        return res
    kind = S.kind_for(c.path.name) or "markdown"
    pl = S.split(text, kind=kind, now=datetime.fromtimestamp(now_f),
                 history_after_days=(policy or {}).get("history_after_days", S.HISTORY_AFTER_DAYS),
                 history_heading_regex=(policy or {}).get("history_heading_regex",
                                                          S.HISTORY_HEADING_REGEX))
    res.tok_before = tok(len(data))
    res.sections = list(pl.sections)
    if not pl.safe:
        res.reason = pl.reason
        return res
    to_memory = destination in ("memory", "memory+sibling")
    to_sibling = destination in ("sibling", "memory+sibling")
    if to_memory and client is None:
        res.reason = "refused: no memory client"
        return res
    date = time.strftime("%Y-%m-%d", time.localtime(now_f))
    # 3. backup
    try:
        bpath = backup(c.path, data, home=home_p, cwd=cwd_p, now=now_f)
    except (OSError, RefusedPath) as e:
        res.reason = f"refused: backup failed ({type(e).__name__})"
        return res
    res.backup = str(bpath)
    # 4a. memory
    if to_memory:
        from ..exceptions import CapExceededError, EntitlementError
        base = f"debloat/{c.label}/{date}"
        meta = {"file": c.label, "sections": pl.sections, "tok_before": pl.tok_before,
                "tok_after": pl.tok_after, "backup": _display_home(bpath, home_p),
                "layout": [list(x) for x in pl.layout]}
        try:
            with suppressed():
                key, reused = _unique_key(client, base, pl.history)
                # A reused copy (an earlier apply stored it, then its rewrite
                # failed and that backup was removed) gets this apply's backup
                # path in its metadata: same body, the record stays true.
                client.set_reference(key, pl.history, metadata=meta)
                back = client.get_reference(key)
        except CapExceededError:
            res.reason = "not applied: the store is at the cap (history not stored; file untouched)"
            return res
        except EntitlementError as e:
            res.reason = f"not applied: account ({getattr(e, 'code', 'locked')}); file untouched"
            return res
        except Exception as e:
            res.reason = f"not applied: memory write failed ({type(e).__name__}); file untouched"
            return res
        if back is None or back.get("body") != pl.history:
            res.reason = "not applied: the memory copy did not verify; file untouched"
            return res
        res.key = key
        res.reused_memory_copy = reused
    # 4b. sibling
    if to_sibling:
        sp = sibling_path(c.path)
        try:
            if sp.is_symlink():
                raise RefusedPath("sibling is a symlink", sp)
            prev = b""
            if sp.exists():
                got = read_bounded(sp, 64 * 1024 * 1024)
                if got is None:
                    raise RefusedPath("sibling unreadable", sp)
                prev = got
            eol = _eol(text)
            block = (f"<!-- moved from {c.path.name} on {date} -->{eol}" + pl.history).encode("utf-8")
            # Idempotent: a re-apply after a failed rewrite (the same day or
            # any later one) finds the same history already at the end of the
            # sibling, under a "moved from <this file>" header, and adds
            # nothing. Compared on the history body, not the dated header.
            if not _sibling_ends_with(prev, c.path.name, pl.history.encode("utf-8")):
                lead = eol.encode("utf-8") if (prev and not prev.endswith(b"\n")) else b""
                atomic_write_bytes(sp, prev + lead + block, mode=(st0.st_mode & 0o777) or 0o644)
        except (OSError, RefusedPath) as e:
            res.reason = f"not applied: history file not written ({type(e).__name__}); file untouched"
            return res
        res.sibling = sp.name
    # 5. rewrite the source last, only if it is unchanged since it was read
    try:
        st1 = c.path.lstat()
    except OSError:
        res.reason = "not applied: the file disappeared; nothing changed"
        return res
    if (st1.st_size, st1.st_mtime_ns, st1.st_ino) != (st0.st_size, st0.st_mtime_ns, st0.st_ino) \
            or c.path.is_symlink():
        res.reason = "not applied: the file changed while planning; nothing changed"
        return res
    ptr = pointer_line(key=res.key, sibling=res.sibling, date=date,
                       backup_display=_display_home(bpath, home_p))
    new_text = with_pointer(pl.current, ptr, _eol(text))
    try:
        atomic_write_bytes(c.path, new_text.encode("utf-8"), mode=st0.st_mode & 0o777)
    except (OSError, RefusedPath) as e:
        res.reason = f"not applied: rewrite failed ({type(e).__name__}); file untouched"
        return res
    res.applied = True
    res.reason = "applied"
    res.tok_after = tok(len(new_text.encode("utf-8")))
    return res


def _preferred_backup(prefer: Path | str | None, rel: str, home: Path) -> Path | None:
    """``prefer`` (the backup the last apply recorded) when it is a regular,
    non-symlinked backup of THIS file inside the backups folder."""
    if not prefer:
        return None
    b = Path(prefer)
    d = _stamp_dir(b, home)
    if d is None or d.is_symlink() or os.path.normpath(str(d / rel)) != os.path.normpath(str(b)):
        return None
    try:
        if b.is_file() and not b.is_symlink():
            return b
    except OSError:
        return None
    return None


def undo(c_path: Path | str, *, home: Path | str, cwd: Path | str, now: float | None = None,
         prefer: Path | str | None = None) -> tuple[bool, str, str | None]:
    """Restore a backup of ``c_path``: ``prefer`` (the backup the last apply
    recorded) when it is still there, else the most recent non-undo backup.
    Refused when that backup is byte-identical to the file as it is now
    (nothing to undo). The current file is backed up first (so an undo is
    itself undoable). Memory is never touched. Returns (ok, message,
    restored-from)."""
    home_p, cwd_p = Path(home), Path(cwd)
    if Path(c_path).is_symlink():
        return False, "refused: the file is a symlink", None
    p = resolved_target(c_path)
    if p.is_symlink():
        return False, "refused: the file is a symlink", None
    if not inside_boundary(p, home_p, cwd_p):
        return False, "refused: outside your home and this project", None
    rel = _backup_rel(p, home_p, cwd_p)
    chosen = _preferred_backup(prefer, rel, home_p)
    if chosen is None:
        for b in reversed(backups_of(rel, home_p)):
            if not b.relative_to(backups_root(home_p)).parts[0].endswith("-undo"):
                chosen = b
                break
    if chosen is None:
        return False, "no backup found", None
    data = read_bounded(chosen, MAX_FILE_BYTES)
    if data is None:
        return False, "no backup found", None
    cur = read_bounded(p, MAX_FILE_BYTES) if p.exists() else None
    if cur is not None and cur == data:
        return (False, f"nothing to undo: the backup {_display_home(chosen, home_p)} is identical "
                "to the file as it is now", str(chosen))
    if cur is not None:
        backup(p, cur, home=home_p, cwd=cwd_p, now=now, suffix="-undo")
    mode = 0o644
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        pass
    atomic_write_bytes(p, data, mode=mode)
    prune_backups(rel, home_p)
    return True, f"restored from {_display_home(chosen, home_p)}", str(chosen)


@contextlib.contextmanager
def run_lock(home: Path | str) -> Iterator[bool]:
    """``~/.sibyl-memory/debloat.lock``: one planner/applier at a time across
    processes (MCP + hermes starting together). Yields False when another
    live process holds it; a lock older than 60 s is stale and taken over."""
    d = memory_dir(home)
    got = False
    lp = d / LOCK_NAME
    try:
        ensure_private_dir(d)
        for _attempt in range(2):
            try:
                fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                got = True
                break
            except FileExistsError:
                try:
                    age = time.time() - lp.lstat().st_mtime
                except OSError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    try:
                        lp.unlink()
                    except OSError:
                        pass
                    continue
                break
    except (OSError, RefusedPath):
        got = False
    try:
        yield got
    finally:
        if got:
            try:
                lp.unlink()
            except OSError:
                pass


def journal(home: Path | str, entry: dict[str, Any]) -> None:
    """Append-only weight telemetry: ``~/.sibyl-memory/debloat/history.jsonl``
    (numbers and file labels, never content), rotated to ``history.jsonl.1``
    past 256 KB like ``lifecycle.log``."""
    try:
        d = debloat_dir(home)
        ensure_private_dir(memory_dir(home))
        ensure_private_dir(d)
        p = d / "history.jsonl"
        if p.is_symlink():
            return
        try:
            if p.stat().st_size > MAX_JOURNAL_BYTES:
                os.replace(str(p), str(d / "history.jsonl.1"))
        except OSError:
            pass
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
    except (OSError, RefusedPath):
        pass


__all__ = ["ApplyResult", "Candidate", "MAX_BACKUPS_PER_FILE", "apply_one", "backup", "backup_rel", "backups_of",
           "backups_root", "candidates", "debloat_dir", "discard_backup", "find_candidate", "inside_boundary",
           "journal", "plan", "pointer_line", "prune_backups", "refusal", "resolved_target", "run_lock",
           "sibling_path", "strip_pointer", "sweep_stale_tmp", "trimmable_reason", "undo",
           "verify_backup", "with_pointer"]
