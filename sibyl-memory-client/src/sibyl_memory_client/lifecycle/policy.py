"""``~/.sibyl-memory/debloat.json``: the standing debloat policy (spec 5.4).

A superset of the public ``.debloat.json`` (same key names where they
overlap). Defaults: ``mode: "propose"``, empty ``auto_files``, destination
``memory``. Nothing here grants anything: the policy only narrows what the
Pro automation may do; a free tier never applies whatever the file says.
A damaged or oversized file is treated as the defaults (and ``mode`` can
only ever be one of off / propose / auto).

``auto_files`` holds one entry per FILE the person authorised with ``sibyl
debloat auto <file>``: the file's REAL absolute path (its folder resolved with
``os.path.realpath``, the final name kept), a file under the real home
directory written as ``~/<rel>`` (``auth_id``). The same file reached through
a symlinked folder (``/home -> var/home``, ``~/.claude -> ~/dotfiles/claude``,
a project opened through a link) has one identity. A bare name such as
``AGENTS.md`` is never an authorisation (it would match that name in every
project the person opens): builds before this one stored bare names, and
those entries are dropped, never guessed at, with a one-time notice
(``take_legacy_notice``). Path entries written before identities were real
paths are re-resolved once when their file exists, and dropped with the same
notice when it does not.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from .._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir, read_bounded
from .splitter import HISTORY_AFTER_DAYS, HISTORY_HEADING_REGEX
from .state import memory_dir

POLICY_NAME = "debloat.json"
MAX_POLICY_BYTES = 64 * 1024
MODES = ("off", "propose", "auto")
DESTINATIONS = ("memory", "memory+sibling")

DEFAULT_POLICY: dict[str, Any] = {
    "surface": None,
    "thresholds": {"watch": 45000, "heavy": 55000},
    "perFileSoft": {"CLAUDE.md": 6000, "AGENTS.md": 6000},
    "mode": "propose",
    "auto_files": [],
    "history_after_days": HISTORY_AFTER_DAYS,
    "history_heading_regex": HISTORY_HEADING_REGEX,
    "destination": "memory",
    "never": ["*.json", "*.jsonl", "*.toml", "*.yaml", "*.yml", "*.env*"],
    "journal": True,
}


def policy_path(home: Path | str | None = None) -> Path:
    return memory_dir(home) / POLICY_NAME


def auth_id(path: Path | str, home: Path | str) -> str:
    """The identity an automatic-trim authorisation is stored and matched
    under: the file's real absolute path (its folder resolved, every symlink
    in it followed; the final component kept, a symlinked file is never
    followed), ``~/<rel>`` for a file under the real ``home``."""
    p = Path(os.path.abspath(str(path)))
    ap = os.path.join(os.path.realpath(str(p.parent)), p.name)
    hp = os.path.realpath(os.path.abspath(str(home)))
    if ap != hp and ap.startswith(hp.rstrip(os.sep) + os.sep):
        return "~/" + Path(ap[len(hp.rstrip(os.sep)) + 1:]).as_posix()
    return Path(ap).as_posix()


def id_path(entry: str, home: Path | str) -> Path:
    """The file a stored ``auth_id`` names (``~/<rel>`` under ``home``)."""
    if entry.startswith("~/"):
        return Path(os.path.abspath(str(home))) / entry[2:]
    return Path(entry)


def is_path_id(entry: Any) -> bool:
    """A stored ``auto_files`` entry that names one file (``~/<rel>`` or an
    absolute path), as opposed to a bare, repo-independent name."""
    if not isinstance(entry, str) or not entry:
        return False
    if entry.startswith("~/"):
        rel = entry[2:]
        return bool(rel) and not os.path.isabs(rel) and ".." not in Path(rel).parts
    return os.path.isabs(entry)


def _norm_id(entry: str) -> str:
    if entry.startswith("~/"):
        return "~/" + Path(os.path.normpath(entry[2:])).as_posix()
    return Path(os.path.normpath(entry)).as_posix()


def _clean(raw: Any) -> dict[str, Any]:
    p = copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(raw, dict):
        return p
    s = raw.get("surface")
    if isinstance(s, list):
        p["surface"] = [x for x in s if isinstance(x, str) and x][:100]
    th = raw.get("thresholds")
    if isinstance(th, dict):
        for k in ("watch", "heavy"):
            v = th.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                p["thresholds"][k] = v
    pfs = raw.get("perFileSoft")
    if isinstance(pfs, dict):
        p["perFileSoft"] = {str(k): v for k, v in pfs.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    if raw.get("mode") in MODES:
        p["mode"] = raw["mode"]
    af = raw.get("auto_files")
    if isinstance(af, list):
        p["auto_files"] = sorted({_norm_id(x) for x in af if is_path_id(x)})[:100]
    d = raw.get("history_after_days")
    if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 3650:
        p["history_after_days"] = d
    rx = raw.get("history_heading_regex")
    if isinstance(rx, str) and 0 < len(rx) <= 500:
        try:
            re.compile(rx)
            p["history_heading_regex"] = rx
        except re.error:
            pass
    if raw.get("destination") in DESTINATIONS:
        p["destination"] = raw["destination"]
    nv = raw.get("never")
    if isinstance(nv, list):
        # The defaults always stay: a policy can add patterns, never remove
        # the config-file exclusions.
        p["never"] = sorted(set(DEFAULT_POLICY["never"]) | {x for x in nv if isinstance(x, str) and x})
    if isinstance(raw.get("journal"), bool):
        p["journal"] = raw["journal"]
    return p


def load(home: Path | str | None = None) -> dict[str, Any]:
    raw = read_bounded(policy_path(home), MAX_POLICY_BYTES)
    if raw is None:
        return copy.deepcopy(DEFAULT_POLICY)
    try:
        return _clean(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return copy.deepcopy(DEFAULT_POLICY)


def save(policy: dict[str, Any], home: Path | str | None = None) -> bool:
    p = policy_path(home)
    try:
        ensure_private_dir(p.parent)
        atomic_write_bytes(p, (json.dumps(_clean(policy), indent=2, sort_keys=True) + "\n").encode("utf-8"))
        return True
    except (RefusedPath, OSError):
        return False


def legacy_auto_entries(home: Path | str | None = None) -> list[str]:
    """``auto_files`` entries in the file on disk that name no single file (a
    bare name written by an earlier build). ``load`` already ignores them."""
    raw = read_bounded(policy_path(home), MAX_POLICY_BYTES)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    af = data.get("auto_files") if isinstance(data, dict) else None
    if not isinstance(af, list):
        return []
    return sorted({x for x in af if isinstance(x, str) and x and not is_path_id(x)})[:100]


def _migrate_ids(entries: list[str], home: Path | str) -> tuple[list[str], list[str]]:
    """(kept ids, dropped ids): each stored path entry re-resolved to the
    real-path identity when its file exists (a regular file, not a symlink),
    dropped when it does not."""
    kept: set[str] = set()
    dropped: list[str] = []
    for e in entries:
        p = id_path(e, home)
        try:
            ok = p.is_file() and not p.is_symlink()
        except OSError:
            ok = False
        if ok:
            kept.add(auth_id(p, home))
        else:
            dropped.append(e)
    return sorted(kept), dropped


def take_legacy_notice(home: Path | str | None = None) -> str | None:
    """Once: when the policy still holds name-only ``auto_files`` entries, or
    path entries not in the real-path form (an earlier build stored the
    logical path), rewrite it: name-only entries are dropped (those files go
    back to manual; nothing is guessed), path entries are re-resolved to the
    real path when the file exists and dropped when it does not. Returns the
    one line that names what was dropped (None when nothing was dropped, and
    after the rewrite, so the line is given out once)."""
    legacy = legacy_auto_entries(home)
    pol = load(home)
    kept, gone = _migrate_ids(pol["auto_files"], home if home is not None else Path.home())
    if not legacy and kept == pol["auto_files"]:
        return None
    pol["auto_files"] = kept
    if not pol["auto_files"] and pol["mode"] == "auto":
        pol["mode"] = "propose"
    if not save(pol, home):
        return None
    parts = []
    if legacy:
        names = ", ".join(legacy)
        one = len(legacy) == 1
        parts.append(f"context trim: automatic trim is now set per file path, not per file name; "
                     f"{names} {'was' if one else 'were'} set by name only and "
                     f"{'is' if one else 'are'} back to manual. Run `sibyl debloat auto <file>` in the "
                     "project whose file you mean.")
    if gone:
        names = ", ".join(gone[:5]) + (f" (+{len(gone) - 5} more)" if len(gone) > 5 else "")
        one = len(gone) == 1
        parts.append(f"context trim: {names} {'was' if one else 'were'} set to automatic but "
                     f"{'is' if one else 'are'} no longer there; back to manual.")
    return " ".join(parts) if parts else None


def set_auto(file_id: str, on: bool, home: Path | str | None = None) -> dict[str, Any]:
    """Add (``on``) or remove one FILE from ``auto_files``. ``file_id`` is the
    file's ``auth_id`` (``~/<rel>`` or an absolute path); a bare name is
    refused (ValueError). Adding a file also moves ``mode`` from propose to
    auto (the person has just named a file); removing the last one moves it
    back to propose. ``off`` stays off."""
    if not is_path_id(file_id):
        raise ValueError(f"not a file path: {file_id!r}")
    file_id = _norm_id(file_id)
    pol = load(home)
    files = set(pol["auto_files"])
    if on:
        files.add(file_id)
        if pol["mode"] == "propose":
            pol["mode"] = "auto"
    else:
        files.discard(file_id)
        if not files and pol["mode"] == "auto":
            pol["mode"] = "propose"
    pol["auto_files"] = sorted(files)
    save(pol, home)
    return pol


def set_mode(mode: str, home: Path | str | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(mode)
    pol = load(home)
    pol["mode"] = mode
    save(pol, home)
    return pol


__all__ = ["DEFAULT_POLICY", "DESTINATIONS", "MODES", "auth_id", "id_path", "is_path_id", "legacy_auto_entries",
           "load", "policy_path", "save", "set_auto", "set_mode", "take_legacy_notice"]
