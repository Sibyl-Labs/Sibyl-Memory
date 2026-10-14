"""The save and debloat procedures as files every agent can find.

The canonical text ships as package data (``_skills/SAVE.md``,
``_skills/DEBLOAT.md``, vendored from the public MIT repos
github.com/Sibyl-Labs/sibyl-save and github.com/Sibyl-Labs/debloat with the
attribution watermark kept) and is installed to the per-user memory folder::

    ~/.sibyl-memory/skills/
      SAVE.md          the save procedure
      DEBLOAT.md       the debloat procedure
      manifest.json    what the plugin last wrote (sha256 per file and per shim)

Rules (spec plugin-save-debloat 3.2):

* never overwrite a person's edit silently: a file whose bytes differ from what
  the plugin last wrote is kept; a new version goes beside it as
  ``SAVE.md.upstream-<version>`` and ``sibyl skills reset SAVE.md`` takes it;
* files are written 0600 in a 0700 directory, atomically, and a symlinked file
  or a symlinked ``skills/`` directory is refused, never followed;
* reads are bounded (``MAX_SKILL_BYTES``): a planted multi-megabyte file is
  reported, never read past the bound.

The harness shims (``~/.claude/skills/sibyl-save/SKILL.md``, the Codex and
Hermes skills) are thin pointers at the canonical file, written by the CLI
wirers through ``install_shim`` and checksum-managed in the same manifest.

No network, no environment variable, no database access.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ._fsio import RefusedPath, atomic_write_bytes, ensure_private_dir, ensure_dir, read_bounded

SKILL_NAMES: tuple[str, ...] = ("SAVE.md", "DEBLOAT.md")
SHIM_NAMES: dict[str, str] = {"SAVE.md": "sibyl-save", "DEBLOAT.md": "sibyl-debloat"}
MAX_SKILL_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MANIFEST_NAME = "manifest.json"
WATERMARK_BEGIN = "SIBYL-SKILL-WATERMARK-BEGIN"
WATERMARK_END = "SIBYL-SKILL-WATERMARK-END"
SHIM_MARKER = "<!-- managed by sibyl-memory: sibyl skills status shows it; your edits are kept -->"


# ----------------------------------------------------------------------
# Package data
# ----------------------------------------------------------------------

def _pkg_file(name: str):
    if name not in SKILL_NAMES and name != MANIFEST_NAME:
        raise KeyError(name)
    return resources.files("sibyl_memory_client").joinpath("_skills", name)


def shipped_bytes(name: str) -> bytes:
    """The bytes of the procedure as shipped in this package."""
    return _pkg_file(name).read_bytes()


def text(name: str) -> str:
    """The shipped procedure text (``"SAVE.md"`` or ``"DEBLOAT.md"``)."""
    return shipped_bytes(name).decode("utf-8")


def sha256(name: str) -> str:
    return hashlib.sha256(shipped_bytes(name)).hexdigest()


def _package_manifest() -> dict[str, Any]:
    try:
        return json.loads(_pkg_file(MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def version() -> str:
    """The skills version (the watermark ``release`` of the shipped files)."""
    return str(_package_manifest().get("version") or "0")


def _client_version() -> str:
    try:
        from importlib.metadata import version as _v
        return _v("sibyl-memory-client")
    except Exception:
        return "0.0.0+source"


def split_frontmatter(doc: str) -> tuple[str, str]:
    """(frontmatter including its fences, body) for a ``---`` fenced header."""
    if not doc.startswith("---\n"):
        return "", doc
    end = doc.find("\n---\n", 4)
    if end == -1:
        return "", doc
    return doc[: end + 5], doc[end + 5:]


def frontmatter_fields(doc: str) -> dict[str, str]:
    head, _ = split_frontmatter(doc)
    out: dict[str, str] = {}
    for line in head.splitlines()[1:-1]:
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def body(doc: str) -> str:
    """The procedure without its frontmatter (starts with the watermark)."""
    return split_frontmatter(doc)[1]


def watermark_hash(doc: str) -> str:
    """The upstream watermark content hash (scripts/watermark.mjs in
    sibyl-save): sha256 of the frontmatter plus the body with the watermark
    block removed and leading whitespace trimmed."""
    head, rest = split_frontmatter(doc)
    trimmed = rest.lstrip()
    if trimmed.startswith("<!-- " + WATERMARK_BEGIN):
        close = rest.find(WATERMARK_END + " -->")
        if close != -1:
            rest = rest[close + len(WATERMARK_END + " -->"):]
    canonical = re.sub(r"^\s+", "", rest)
    return "sha256:" + hashlib.sha256((head + canonical).encode("utf-8")).hexdigest()


def watermark_fields(doc: str) -> dict[str, str]:
    m = re.search(re.escape("<!-- " + WATERMARK_BEGIN) + r"\n(.*?)\n" + re.escape(WATERMARK_END + " -->"),
                  doc, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# ----------------------------------------------------------------------
# Installed manifest
# ----------------------------------------------------------------------

def skills_dir(home: Path | str) -> Path:
    return Path(home).expanduser() / ".sibyl-memory" / "skills"


def canonical_path(home: Path | str, name: str) -> Path:
    return skills_dir(home) / name


def _load_manifest(d: Path) -> dict[str, Any]:
    raw = read_bounded(d / MANIFEST_NAME, MAX_MANIFEST_BYTES)
    if raw is None:
        return {"files": {}, "shims": {}}
    try:
        m = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"files": {}, "shims": {}}
    if not isinstance(m, dict):
        return {"files": {}, "shims": {}}
    if not isinstance(m.get("files"), dict):
        m["files"] = {}
    if not isinstance(m.get("shims"), dict):
        m["shims"] = {}
    return m


def _store_manifest(d: Path, m: dict[str, Any]) -> None:
    atomic_write_bytes(d / MANIFEST_NAME,
                       (json.dumps(m, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class InstallReport:
    directory: Path
    version: str
    results: dict[str, str] = field(default_factory=dict)   # name -> status
    messages: list[str] = field(default_factory=list)
    ok: bool = True

    @property
    def refused(self) -> bool:
        return any(s.startswith("refused") for s in self.results.values())


def _sidecars(d: Path, name: str) -> list[Path]:
    try:
        return sorted(p for p in d.glob(name + ".upstream-*") if not p.is_symlink())
    except OSError:
        return []


def install(home: Path | str, *, force: bool = False, names: tuple[str, ...] | None = None,
            now: float | None = None) -> InstallReport:
    """Install or refresh the canonical procedure files under ``home``.

    Statuses: ``installed`` (new), ``updated`` (unedited file replaced by a new
    version), ``unchanged``, ``user_modified`` (kept; a new version goes beside
    it as ``<name>.upstream-<version>``), ``replaced`` (``force``: the edited
    file was replaced and the sidecars removed), ``refused_symlink``,
    ``refused_oversize``, ``refused_dir``. Never raises for a refused path."""
    ver = version()
    d = skills_dir(home)
    rep = InstallReport(directory=d, version=ver)
    targets = names or SKILL_NAMES
    try:
        base = Path(home).expanduser() / ".sibyl-memory"
        if base.is_symlink():
            raise RefusedPath("directory is a symlink", base)
        ensure_private_dir(base)
        ensure_private_dir(d)
    except (RefusedPath, OSError) as e:
        rep.ok = False
        for n in targets:
            rep.results[n] = "refused_dir"
        rep.messages.append(f"skills directory refused ({getattr(e, 'reason', type(e).__name__)}): {d}")
        return rep
    manifest = _load_manifest(d)
    stamp = _now_iso(now)
    source = f"sibyl-memory-client {_client_version()}"
    for name in targets:
        new = shipped_bytes(name)
        new_sha = _sha(new)
        target = d / name
        rec = manifest["files"].get(name) if isinstance(manifest["files"].get(name), dict) else None
        recorded = (rec or {}).get("sha256")
        try:
            if target.is_symlink():
                rep.results[name] = "refused_symlink"
                rep.messages.append(f"{name} is a symlink; not followed, not replaced ({target}).")
                rep.ok = False
                continue
            if target.exists() and not target.is_file():
                rep.results[name] = "refused_dir"
                rep.messages.append(f"{name} is not a regular file; left alone ({target}).")
                rep.ok = False
                continue
            if not target.exists():
                atomic_write_bytes(target, new)
                manifest["files"][name] = {"sha256": new_sha, "installed_at": stamp,
                                           "source": source, "user_modified": False}
                rep.results[name] = "installed"
                continue
            cur = read_bounded(target, MAX_SKILL_BYTES)
            if cur is None:
                rep.results[name] = "refused_oversize"
                rep.messages.append(f"{name} is larger than {MAX_SKILL_BYTES // 1024} KB; "
                                    "it was not read and not replaced.")
                rep.ok = False
                continue
            cur_sha = _sha(cur)
            if cur_sha == new_sha:
                manifest["files"][name] = {"sha256": new_sha, "installed_at": (rec or {}).get("installed_at", stamp),
                                           "source": (rec or {}).get("source", source),
                                           "user_modified": False}
                rep.results[name] = "unchanged"
                continue
            if recorded is not None and cur_sha == recorded:
                atomic_write_bytes(target, new)
                for sc in _sidecars(d, name):
                    try:
                        sc.unlink()
                    except OSError:
                        pass
                manifest["files"][name] = {"sha256": new_sha, "installed_at": stamp,
                                           "source": source, "user_modified": False}
                rep.results[name] = "updated"
                continue
            # The person edited it (or it was never ours).
            if force:
                atomic_write_bytes(target, new)
                for sc in _sidecars(d, name):
                    try:
                        sc.unlink()
                    except OSError:
                        pass
                manifest["files"][name] = {"sha256": new_sha, "installed_at": stamp,
                                           "source": source, "user_modified": False}
                rep.results[name] = "replaced"
                continue
            entry = dict(rec or {})
            entry["user_modified"] = True
            entry.setdefault("installed_at", stamp)
            entry.setdefault("source", source)
            if recorded != new_sha:
                side = d / f"{name}.upstream-{ver}"
                atomic_write_bytes(side, new)
                rep.messages.append(
                    f"{name} was edited by you; the new version is beside it as "
                    f"{side.name} (sibyl skills reset {name} to take it).")
                entry["upstream_waiting"] = side.name
            entry.setdefault("sha256", recorded or new_sha)
            manifest["files"][name] = entry
            rep.results[name] = "user_modified"
        except RefusedPath as e:
            rep.results[name] = "refused_symlink" if "symlink" in e.reason else "refused_dir"
            rep.messages.append(f"{name}: {e.reason}; left alone.")
            rep.ok = False
    manifest["version"] = ver
    try:
        _store_manifest(d, manifest)
    except (RefusedPath, OSError) as e:
        rep.ok = False
        rep.messages.append(f"manifest not written ({type(e).__name__}).")
    return rep


def reset(home: Path | str, name: str, *, now: float | None = None) -> InstallReport:
    """Replace ``name`` with the shipped version (after the caller confirmed)."""
    if name not in SKILL_NAMES:
        raise KeyError(name)
    return install(home, force=True, names=(name,), now=now)


def canonical_text(name: str, home: Path | str | None = None) -> str:
    """The procedure an agent should follow: the installed file when present
    (a person's edit is honoured), bounded and symlink-refusing, else the
    shipped text."""
    if home is None:
        home = Path.home()
    raw = read_bounded(canonical_path(home, name), MAX_SKILL_BYTES)
    if raw is not None:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return text(name)


def status(home: Path | str) -> dict[str, Any]:
    """What `sibyl skills status` prints. Never raises, never writes."""
    d = skills_dir(home)
    out: dict[str, Any] = {"directory": str(d), "exists": d.is_dir() and not d.is_symlink(),
                           "package_version": version(), "installed_version": None,
                           "files": {}, "shims": {}}
    if d.is_symlink():
        out["refused"] = "skills directory is a symlink"
        return out
    manifest = _load_manifest(d) if out["exists"] else {"files": {}, "shims": {}}
    out["installed_version"] = manifest.get("version")
    for name in SKILL_NAMES:
        p = d / name
        rec = manifest["files"].get(name) or {}
        info: dict[str, Any] = {"path": str(p)}
        if p.is_symlink():
            info["state"] = "symlink"
        elif not p.exists():
            info["state"] = "missing"
        else:
            raw = read_bounded(p, MAX_SKILL_BYTES)
            if raw is None:
                info["state"] = "oversize"
            else:
                cur = _sha(raw)
                if cur == sha256(name):
                    info["state"] = "ok"
                elif rec.get("sha256") == cur:
                    info["state"] = "outdated"
                else:
                    info["state"] = "edited"
        info["upstream_waiting"] = [s.name for s in _sidecars(d, name)] if out["exists"] else []
        out["files"][name] = info
    for spath, rec in sorted(manifest.get("shims", {}).items()):
        if not isinstance(rec, dict):
            continue
        p = Path(spath)
        if p.is_symlink():
            state = "symlink"
        elif not p.exists():
            state = "missing"
        else:
            raw = read_bounded(p, MAX_SKILL_BYTES)
            state = ("ok" if raw is not None and _sha(raw) == rec.get("sha256") else "edited")
        out["shims"][spath] = {"harness": rec.get("harness"), "skill": rec.get("skill"),
                               "state": state}
    return out


# ----------------------------------------------------------------------
# Harness shims
# ----------------------------------------------------------------------

_SHIM_BODIES = {
    "SAVE.md": (
        "Saves this session into Sibyl Memory: decisions, people, projects, next steps and "
        "priorities, written through the plugin's memory tools. Nothing leaves this computer.",
        "Modes: run (default) | `preview` or `--dry-run` (report only, write nothing) | "
        "`setup` (config) | `headless` (the Pro automatic save asks for it once early, after 5 "
        "memory writes or a compaction, then about every hour of active work: ask nothing) | "
        "`curate` (go through queued decisions and the plugin's skill proposals).",
        "save",
    ),
    "DEBLOAT.md": (
        "Measures the instruction files your agent loads on every start (CLAUDE.md, AGENTS.md, "
        ".cursorrules and the rest) and trims accreted history out of them, never deleting a byte.",
        "Modes: measure (default, `sibyl debloat --json`) | `trim` (you choose each file; history "
        "moves out, a one-line pointer stays).",
        "debloat",
    ),
}


def shim_text(name: str, harness: str) -> str:
    """The thin pointer a harness loads for ``name``: a SKILL.md with
    frontmatter ``name`` + ``description`` for every harness (Claude Code
    ``~/.claude/skills``, Hermes ``$HERMES_HOME/skills``, Codex
    ``$CODEX_HOME/skills``: Codex CLI 0.135.0 has skills, not custom prompts)."""
    if name not in SKILL_NAMES:
        raise KeyError(name)
    fm = frontmatter_fields(text(name))
    desc = fm.get("description", "")
    shim = SHIM_NAMES[name]
    what, modes, prompt = _SHIM_BODIES[name]
    head = f"---\nname: {shim}\ndescription: {desc}\n---\n"
    lines = [
        SHIM_MARKER,
        "",
        f"# /{shim}",
        "",
        what,
        "",
        f"Read `~/.sibyl-memory/skills/{name}` and follow it exactly; `--dry-run` and `preview` "
        "mean the same. That file is the single source of truth and `sibyl update --apply` "
        "refreshes it.",
        "",
        modes,
        "",
        f"If the file is missing, run `sibyl skills install` in a terminal, or use the MCP "
        f"prompt `{prompt}` on the sibyl-memory server.",
        "",
    ]
    return head + "\n".join(lines)


def install_shim(home: Path | str, path: Path | str, content: str, *, harness: str, skill: str,
                 force: bool = False, dry_run: bool = False, now: float | None = None) -> str:
    """Write one harness shim, checksum-managed. Returns ``installed``,
    ``updated``, ``unchanged``, ``user_modified`` (kept, not replaced),
    ``replaced`` (force), ``dry-run``, or ``refused_*``."""
    path = Path(path)
    data = content.encode("utf-8")
    new_sha = _sha(data)
    d = skills_dir(home)
    if dry_run:
        return "dry-run"
    try:
        base = Path(home).expanduser() / ".sibyl-memory"
        if base.is_symlink():
            return "refused_symlink"
        ensure_private_dir(base)
        ensure_private_dir(d)
    except (RefusedPath, OSError):
        return "refused_dir"
    manifest = _load_manifest(d)
    rec = manifest["shims"].get(str(path)) if isinstance(manifest["shims"].get(str(path)), dict) else None
    status_: str
    try:
        if path.is_symlink():
            return "refused_symlink"
        if path.exists() and not path.is_file():
            return "refused_dir"
        if path.parent.is_symlink():
            return "refused_symlink"
        if not path.exists():
            ensure_dir(path.parent)
            atomic_write_bytes(path, data, mode=0o644)
            status_ = "installed"
        else:
            cur = read_bounded(path, MAX_SKILL_BYTES)
            if cur is None:
                return "refused_oversize"
            cur_sha = _sha(cur)
            if cur_sha == new_sha:
                status_ = "unchanged"
            elif rec is not None and rec.get("sha256") == cur_sha:
                atomic_write_bytes(path, data, mode=0o644)
                status_ = "updated"
            elif force:
                atomic_write_bytes(path, data, mode=0o644)
                status_ = "replaced"
            else:
                return "user_modified"
    except RefusedPath as e:
        return "refused_symlink" if "symlink" in e.reason else "refused_dir"
    manifest["shims"][str(path)] = {"sha256": new_sha, "harness": harness, "skill": skill,
                                    "installed_at": (rec or {}).get("installed_at", _now_iso(now))
                                    if status_ == "unchanged" else _now_iso(now)}
    _store_manifest(d, manifest)
    return status_


def remove_shim(home: Path | str, path: Path | str) -> str:
    """Remove a shim only when its bytes are what the plugin wrote. Returns
    ``removed``, ``missing``, ``user_modified`` (kept) or ``refused_symlink``."""
    path = Path(path)
    d = skills_dir(home)
    manifest = _load_manifest(d)
    rec = manifest["shims"].get(str(path))
    if path.is_symlink():
        return "refused_symlink"
    if not path.exists():
        if rec is not None:
            manifest["shims"].pop(str(path), None)
            _store_manifest(d, manifest)
        return "missing"
    cur = read_bounded(path, MAX_SKILL_BYTES)
    if cur is None or not isinstance(rec, dict) or _sha(cur) != rec.get("sha256"):
        return "user_modified"
    path.unlink()
    try:
        path.parent.rmdir()   # only when empty (a shim's own directory)
    except OSError:
        pass
    manifest["shims"].pop(str(path), None)
    _store_manifest(d, manifest)
    return "removed"


# ----------------------------------------------------------------------
# Accepted skill proposals (R1, plan 3.3): a SKILL.md per harness
# ----------------------------------------------------------------------

ACCEPTED_SECTION = "accepted"
SKILL_NAME_MAX = 64
SKILL_DESCRIPTION_MAX = 1024
ALLOWED_FRONTMATTER_KEYS = frozenset({"name", "description", "license", "allowed-tools", "metadata"})
_HYPHEN_CASE = re.compile(r"^[a-z0-9-]+$")


def skill_name(slug: str) -> str:
    """A harness-safe skill name from a proposal slug: hyphen-case, at most 64
    characters, no leading, trailing or double hyphen (Codex's quick_validate
    rules; Claude Code and Hermes accept the same)."""
    n = re.sub(r"[^a-z0-9-]+", "-", str(slug or "").lower())
    n = re.sub(r"-{2,}", "-", n).strip("-")[:SKILL_NAME_MAX].strip("-")
    return n or "sibyl-skill"


def _one_line(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    t = t.replace("<", "(").replace(">", ")")
    return t[:limit].rstrip()


def proposal_skill_text(proposal: Any) -> str:
    """The SKILL.md for an accepted proposal: YAML frontmatter ``name`` +
    ``description`` (double-quoted, no angle brackets, at most 1024
    characters), then the proposal body and where it came from."""
    slug = getattr(proposal, "proposed_slug", None) or "skill"
    name = skill_name(slug)
    title = getattr(proposal, "proposed_title", None) or slug.replace("-", " ")
    kind = str(getattr(proposal, "pattern_kind", "") or "pattern").replace("_", " ")
    desc = _one_line(f"{title}. A {kind} Sibyl Memory found in your sessions; use it when the same "
                     "situation comes up again.", SKILL_DESCRIPTION_MAX)
    body = str(getattr(proposal, "proposed_body", "") or "").strip()
    if body.startswith("---"):
        body = "\n" + body          # never a second frontmatter block
    head = "---\nname: " + name + "\ndescription: " + json.dumps(desc, ensure_ascii=True) + "\n---\n"
    tail = (f"\n\n---\nAccepted from Sibyl Memory skill proposal `{getattr(proposal, 'id', '?')}` "
            f"(reference copy in memory: `skill/{slug}`). Edit this file freely; the plugin "
            "never overwrites your edits.\n")
    return head + "\n" + body + tail


def _parse_frontmatter_yaml(text: str) -> dict[str, Any] | None:
    head, _ = split_frontmatter(text)
    if not head:
        return None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(head[4:-5])
        return data if isinstance(data, dict) else None
    except ImportError:
        pass
    except Exception:
        return None
    out: dict[str, Any] = {}
    for line in head.splitlines()[1:-1]:
        if ":" not in line:
            return None
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                return None
        out[k.strip()] = v
    return out


def validate_skill_text(text: str, dirname: str) -> list[str]:
    """Codex's ``quick_validate.py`` rules (skill-creator, Codex CLI 0.135.0):
    frontmatter present and a mapping; only name / description / license /
    allowed-tools / metadata; ``name`` hyphen-case, at most 64 characters, no
    leading/trailing/double hyphen, equal to the folder name; ``description`` a
    string of at most 1024 characters without angle brackets. Returns the
    problems (empty when valid)."""
    fm = _parse_frontmatter_yaml(text)
    if fm is None:
        return ["frontmatter missing or not a mapping"]
    errs = []
    extra = set(fm) - ALLOWED_FRONTMATTER_KEYS
    if extra:
        errs.append(f"unexpected keys: {sorted(extra)}")
    name = fm.get("name")
    if not isinstance(name, str) or not _HYPHEN_CASE.match(name) or name.startswith("-") \
            or name.endswith("-") or "--" in name or len(name) > SKILL_NAME_MAX:
        errs.append("name is not hyphen-case (at most 64 characters)")
    elif name != dirname:
        errs.append("name differs from the folder name")
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errs.append("description missing")
    elif len(desc) > SKILL_DESCRIPTION_MAX or "<" in desc or ">" in desc:
        errs.append("description too long or has angle brackets")
    return errs


def write_skill(home: Path | str, path: Path | str, content: str, *, harness: str, slug: str,
                now: float | None = None) -> str:
    """Write an accepted skill's SKILL.md, checksum-managed in the manifest's
    ``accepted`` section (``sibyl skills uninstall`` never removes these: they
    are the person's skills). Returns ``installed``, ``unchanged``, ``updated``
    (the plugin's own earlier copy), ``exists`` (a file the plugin did not
    write, or one the person edited: kept) or ``refused_*``."""
    path = Path(path)
    data = content.encode("utf-8")
    new_sha = _sha(data)
    d = skills_dir(home)
    try:
        base = Path(home).expanduser() / ".sibyl-memory"
        if base.is_symlink():
            return "refused_symlink"
        ensure_private_dir(base)
        ensure_private_dir(d)
    except (RefusedPath, OSError):
        return "refused_dir"
    manifest = _load_manifest(d)
    acc = manifest.get(ACCEPTED_SECTION)
    if not isinstance(acc, dict):
        acc = {}
    rec = acc.get(str(path)) if isinstance(acc.get(str(path)), dict) else None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return "refused_symlink"
        if path.exists() and not path.is_file():
            return "refused_dir"
        if not path.exists():
            ensure_dir(path.parent)
            atomic_write_bytes(path, data, mode=0o644)
            status_ = "installed"
        else:
            cur = read_bounded(path, MAX_SKILL_BYTES)
            if cur is None:
                return "refused_oversize"
            if _sha(cur) == new_sha:
                status_ = "unchanged"
            elif rec is not None and rec.get("sha256") == _sha(cur):
                atomic_write_bytes(path, data, mode=0o644)
                status_ = "updated"
            else:
                return "exists"
    except RefusedPath as e:
        return "refused_symlink" if "symlink" in e.reason else "refused_dir"
    except OSError:
        return "refused_dir"
    acc[str(path)] = {"sha256": new_sha, "harness": harness, "slug": slug,
                      "installed_at": _now_iso(now)}
    manifest[ACCEPTED_SECTION] = acc
    _store_manifest(d, manifest)
    return status_


def uninstall(home: Path | str) -> dict[str, str]:
    """Remove every canonical file and shim whose bytes are what the plugin
    wrote; edited ones are kept and reported."""
    d = skills_dir(home)
    out: dict[str, str] = {}
    manifest = _load_manifest(d)
    for spath in list(manifest.get("shims", {})):
        out[spath] = remove_shim(home, spath)
    manifest = _load_manifest(d)
    for name in SKILL_NAMES:
        p = d / name
        rec = manifest["files"].get(name) or {}
        if p.is_symlink() or not p.exists():
            out[name] = "missing" if not p.is_symlink() else "refused_symlink"
            continue
        cur = read_bounded(p, MAX_SKILL_BYTES)
        if cur is not None and _sha(cur) == rec.get("sha256") and not rec.get("user_modified"):
            p.unlink()
            manifest["files"].pop(name, None)
            out[name] = "removed"
        else:
            out[name] = "user_modified"
    if d.is_dir() and not d.is_symlink():
        _store_manifest(d, manifest)
    return out


__all__ = [
    "ACCEPTED_SECTION", "InstallReport", "MAX_SKILL_BYTES", "SHIM_NAMES", "SKILL_NAMES", "body",
    "canonical_path", "canonical_text", "frontmatter_fields", "install", "install_shim",
    "proposal_skill_text", "remove_shim", "reset", "sha256", "shim_text", "shipped_bytes",
    "skill_name", "skills_dir", "split_frontmatter", "status", "text", "uninstall",
    "validate_skill_text", "version", "watermark_fields", "watermark_hash", "write_skill",
]
