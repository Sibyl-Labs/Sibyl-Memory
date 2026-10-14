"""Deterministic measurement of the always-load context surface.

A Python port of ``context-hygiene.mjs`` from the public MIT repo
github.com/Sibyl-Labs/debloat (v1.0.0, commit f0bf26d): ``tok = round(bytes /
4)`` (JavaScript ``Math.round``, half up), the ``watch`` / ``heavy``
thresholds, ``perFileSoft``, the target rules (``file_heavy``, else
``file_largest`` when the total reaches ``watch``), the weight label, the
``--json`` record, the ``--fyi`` line and the default report. Stat only: no
file body is ever read here. No network, no environment variable.

Two surfaces:

* **public** (``public_defaults=True`` or a ``surface`` list in
  ``.debloat.json``): exactly the upstream behaviour, entries relative to the
  root; the conformance test compares this against the Node script;
* **plugin default** (no ``surface`` configured): ``CONTEXT_FILES`` resolved
  against the project root AND the home directory, ``.cursor/rules`` expanded to
  the files inside it; home entries are labelled ``~/<rel>``.

One deliberate difference from upstream, in both modes: an entry that resolves
outside the root and the home directory (``../../other/file.md``, an absolute
path elsewhere) is ignored and listed under ``ignored``.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

from .._fsio import read_bounded

# The instruction files an agent loads on every session start (spec 6.1). The
# CLI's migrate.CONTEXT_FILES is this same set; CONFIG_FILES are backed up and
# extracted from, never measured or trimmed.
CONTEXT_FILES_ORDERED: tuple[str, ...] = (
    "CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md", ".codex/AGENTS.md",
    "MEMORY.md", "memory.md", ".cursorrules", ".cursor/rules",
)
CONTEXT_FILES: frozenset[str] = frozenset(CONTEXT_FILES_ORDERED)
CONFIG_FILES: frozenset[str] = frozenset({
    ".claude/settings.json", ".codex/config.toml", ".hermes/config.yaml", ".hermes/memory",
})

PUBLIC_DEFAULT_SURFACE: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
DEFAULT_THRESHOLDS = {"watch": 45000, "heavy": 55000}
MAX_CONFIG_BYTES = 64 * 1024
MAX_RULES_FILES = 200
PUBLIC_HINT = " - /debloat to trim"
PLUGIN_HINT = " - /sibyl-debloat to trim"


def tok(nbytes: int) -> int:
    """JavaScript ``Math.round(bytes / 4)`` for non-negative sizes."""
    return int(math.floor(nbytes / 4 + 0.5))


def _to_fixed(x: float, digits: int) -> str:
    """JavaScript ``Number.prototype.toFixed`` (half up on the exact binary value)."""
    q = Decimal(1).scaleb(-digits)
    return str(Decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def _js_num(x: float) -> str:
    """JavaScript ``String(number)`` for the small values printed here."""
    if float(x).is_integer():
        return str(int(x))
    return repr(float(x))


def _iso_ms(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def load_config(root: Path, config_path: Path | None = None) -> tuple[dict[str, Any], str | None, bool]:
    """(config, error message or None, surface_configured). Mirrors upstream
    ``loadConfig``: the parsed object is merged over the defaults, thresholds
    and perFileSoft are merged key by key; an unparsable file prints a message
    and the defaults are used."""
    cfg: dict[str, Any] = {"surface": list(PUBLIC_DEFAULT_SURFACE),
                           "thresholds": dict(DEFAULT_THRESHOLDS),
                           "perFileSoft": {}, "journal": None}
    path = Path(config_path) if config_path is not None else Path(root) / ".debloat.json"
    if not path.exists():
        return cfg, None, False
    raw = read_bounded(path, MAX_CONFIG_BYTES)
    if raw is None:
        return cfg, f"debloat: could not read {path} (missing, a symlink or over 64 KB)", False
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("top level is not an object")
    except (ValueError, UnicodeDecodeError) as e:
        return cfg, f"debloat: could not parse {path}: {e}", False
    surface_configured = isinstance(parsed.get("surface"), list)
    for k, v in parsed.items():
        if k in ("thresholds", "perFileSoft"):
            continue
        cfg[k] = v
    th = parsed.get("thresholds")
    if isinstance(th, dict):
        for k, v in th.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cfg["thresholds"][k] = v
    pfs = parsed.get("perFileSoft")
    if isinstance(pfs, dict):
        for k, v in pfs.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cfg["perFileSoft"][k] = v
    if not isinstance(cfg.get("surface"), list):
        cfg["surface"] = list(PUBLIC_DEFAULT_SURFACE)
    cfg["surface"] = [s for s in cfg["surface"] if isinstance(s, str) and s]
    return cfg, None, surface_configured


def _lexically_within(p: Path, bases: Iterable[Path]) -> bool:
    """The entry's own path (symlinks NOT followed) is under a base. Measuring a
    symlinked instruction file is fine (stat only); an entry that names a path
    outside the project and home is not."""
    import os
    ap = Path(os.path.normpath(str(p)))
    for b in bases:
        bp = Path(os.path.normpath(str(b)))
        try:
            if ap == bp or ap.is_relative_to(bp):
                return True
        except ValueError:
            continue
    return False


def resolve_surface(root: Path, *, home: Path | None, cfg: dict[str, Any], surface_configured: bool,
                    includes: Iterable[str] = (), public_defaults: bool = False
                    ) -> tuple[list[tuple[str, Path, str]], list[str]]:
    """[(label, absolute path, soft-lookup key)], [ignored entries]."""
    root = Path(root)
    bases = [root] + ([Path(home)] if home is not None else [])
    out: list[tuple[str, Path, str]] = []
    ignored: list[str] = []
    seen: set[str] = set()

    def add(label: str, p: Path, key: str) -> None:
        if label in seen:
            return
        seen.add(label)
        out.append((label, p, key))

    if surface_configured or public_defaults:
        entries = list(dict.fromkeys([*cfg.get("surface", []), *[i for i in includes if i]]))
        for rel in entries:
            p = Path(rel) if Path(rel).is_absolute() else root / rel
            if not _lexically_within(p, bases):
                ignored.append(rel)
                continue
            add(rel, p, rel)
        return out, ignored

    roots: list[tuple[Path, str]] = [(root, "")]
    if home is not None:
        try:
            same = Path(home).resolve() == root.resolve()
        except OSError:
            same = False
        if not same:
            roots.append((Path(home), "~/"))
    for base, prefix in roots:
        for rel in CONTEXT_FILES_ORDERED:
            p = base / rel
            if rel == ".cursor/rules":
                try:
                    if p.is_dir() and not p.is_symlink():
                        files = sorted(f for f in p.rglob("*") if f.is_file())[:MAX_RULES_FILES]
                        for f in files:
                            sub = f.relative_to(base).as_posix()
                            add(prefix + sub, f, sub)
                except OSError:
                    pass
                continue
            add(prefix + rel, p, rel)
    for inc in includes:
        if not inc:
            continue
        p = Path(inc) if Path(inc).is_absolute() else root / inc
        if not _lexically_within(p, bases):
            ignored.append(inc)
            continue
        add(inc, p, inc)
    return out, ignored


def measure(root: Path | str | None = None, *, home: Path | str | None = None,
            config_path: Path | str | None = None, includes: Iterable[str] = (),
            public_defaults: bool = False, surface: list[str] | None = None,
            thresholds: dict[str, Any] | None = None,
            per_file_soft: dict[str, Any] | None = None, write_journal: bool = False,
            now: datetime | None = None) -> dict[str, Any]:
    """The ``--json`` record. ``thresholds`` / ``per_file_soft`` (the plugin
    policy) apply UNDER a project ``.debloat.json`` (the project file wins for
    its keys). ``write_journal`` honours the config's ``journal`` path (the
    public telemetry line), relative to the root."""
    import os
    root = Path(os.path.abspath(str(root if root is not None else Path.cwd())))
    home_p = Path(home) if home is not None else None
    cfg, err, configured = load_config(root, Path(config_path) if config_path else None)
    if not configured and isinstance(surface, list):
        # The plugin policy's surface list (the project file wins when it has one).
        cfg["surface"] = [s for s in surface if isinstance(s, str) and s]
        configured = True
    if thresholds:
        for k, v in thresholds.items():
            if k not in _explicit_keys(root, config_path, "thresholds"):
                cfg["thresholds"][k] = v
    if per_file_soft:
        for k, v in per_file_soft.items():
            cfg["perFileSoft"].setdefault(k, v)
    surface, ignored = resolve_surface(root, home=home_p, cfg=cfg, surface_configured=configured,
                                       includes=includes, public_defaults=public_defaults)
    plugin_mode = not (configured or public_defaults)
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for label, p, key in surface:
        try:
            st = p.stat()
        except OSError:
            continue
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode):
            continue
        nbytes = st.st_size
        total_bytes += nbytes
        t = tok(nbytes)
        soft = cfg["perFileSoft"].get(label)
        if soft is None and plugin_mode:
            soft = cfg["perFileSoft"].get(key)
        files.append({"file": label, "bytes": nbytes, "tok": t, "soft": soft,
                      "heavy": soft is not None and t > soft, "path": str(p)})
    total_tok = tok(total_bytes)
    watch = cfg["thresholds"]["watch"]
    heavy_th = cfg["thresholds"]["heavy"]
    targets: list[dict[str, Any]] = []
    for f in files:
        if f["heavy"]:
            targets.append({"file": f["file"], "kind": "file_heavy", "tok": f["tok"], "soft": f["soft"],
                            "suggestion": f"{f['file']} is {f['tok']} tok (soft {_js_num(f['soft'])}). "
                                          "Move accreted history to a *-history file; keep current "
                                          "state + a pointer."})
    if not targets and files and total_tok >= watch:
        biggest = sorted(files, key=lambda f: -f["tok"])[0]
        targets.append({"file": biggest["file"], "kind": "file_largest", "tok": biggest["tok"],
                        "soft": biggest["soft"],
                        "suggestion": f"{biggest['file']} is the heaviest at {biggest['tok']} tok. "
                                      "If it carries accreted history, extract it to a *-history file."})
    weight = "light"
    if total_tok >= heavy_th:
        weight = "heavy"
    elif total_tok >= watch or targets:
        weight = "watch"
    record: dict[str, Any] = {
        "ts": _iso_ms(now), "root": str(root), "total_tok": total_tok, "total_bytes": total_bytes,
        "ref": {"watch": watch, "heavy": heavy_th}, "weight": weight,
        "files": [{"file": f["file"], "tok": f["tok"], "soft": f["soft"], "heavy": f["heavy"]}
                  for f in files],
        "targets": [{"file": t["file"], "kind": t["kind"], "tok": t["tok"], "soft": t["soft"],
                     "suggestion": t["suggestion"]} for t in targets],
    }
    # Plugin extras (never part of the public shape's required keys).
    record["_paths"] = {f["file"]: f["path"] for f in files}
    record["_surface"] = [label for label, _p, _k in surface]
    if ignored:
        record["ignored"] = ignored
    if err:
        record["config_error"] = err
    if write_journal and isinstance(cfg.get("journal"), str) and cfg["journal"]:
        jp = Path(cfg["journal"])
        jp = jp if jp.is_absolute() else root / jp
        if _lexically_within(jp, [root] + ([home_p] if home_p else [])):
            try:
                jp.parent.mkdir(parents=True, exist_ok=True)
                if not jp.is_symlink():
                    with open(jp, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": record["ts"], "total_tok": total_tok,
                                             "weight": weight, "targets": len(targets)},
                                            separators=(",", ":")) + "\n")
            except OSError:
                pass
    return record


def _explicit_keys(root: Path, config_path: Any, section: str) -> set[str]:
    path = Path(config_path) if config_path else Path(root) / ".debloat.json"
    raw = read_bounded(path, MAX_CONFIG_BYTES)
    if raw is None:
        return set()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return set()
    sec = parsed.get(section) if isinstance(parsed, dict) else None
    return set(sec) if isinstance(sec, dict) else set()


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """The record without the plugin's private extras (the upstream shape)."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


def fyi(record: dict[str, Any], *, hint: str = PLUGIN_HINT) -> str:
    heavy = [f"{f['file'].split('/')[-1]} {f['tok']}t" for f in record["files"] if f["heavy"]]
    spots = f" | heavy: {', '.join(heavy)}" if heavy else ""
    h = "" if record["weight"] == "light" else hint
    return (f"context: ~{_to_fixed(record['total_tok'] / 1000, 1)}k tok always-load "
            f"({record['weight']}){spots}{h}")


def report(record: dict[str, Any], *, surface_list: list[str] | None = None) -> str:
    """The upstream default report, line for line."""
    bar = "=" * 60
    lines = [bar, f"CONTEXT WEIGHT  {record['ts']}", bar]
    files = record["files"]
    if not files:
        looked = surface_list if surface_list is not None else record.get("_surface", [])
        lines.append("  no always-load files found.")
        lines.append(f"  looked for: {', '.join(looked) or '(none)'}")
        lines.append('  set "surface" in .debloat.json or pass --include <file>.')
        lines.append(bar)
        return "\n".join(lines)
    watch, heavy = record["ref"]["watch"], record["ref"]["heavy"]
    lines.append(f"  always-load surface: {record['total_tok']:,} tok  "
                 f"(~{_to_fixed(record['total_bytes'] / 1024, 0)} KB)   [MEASURED~ chars/4]")
    lines.append(f"  reference: light <{_js_num(watch / 1000)}k / watch <{_js_num(heavy / 1000)}k / "
                 f"heavy >={_js_num(heavy / 1000)}k")
    lines.append(f"  weight: {record['weight']}")
    lines.append("")
    lines.append("  file weights (heaviest first):")
    for f in sorted(files, key=lambda f: -f["tok"]):
        if f["heavy"]:
            mark = f"  <-- heavy (soft {_js_num(f['soft'])})"
        elif f["soft"]:
            mark = f"  (soft {_js_num(f['soft'])})"
        else:
            mark = ""
        lines.append(f"    {str(f['tok']).rjust(6)} tok  {f['file']}{mark}")
    if record["targets"]:
        lines.append("")
        lines.append("  suggested trim targets (you choose what moves):")
        for t in record["targets"]:
            lines.append(f"    * {t['suggestion']}")
    lines.append("")
    lines.append("  Light. Nothing to do." if record["weight"] == "light" else
                 "  Trim: move accreted history out of the heavy files into *-history files, "
                 "keep current state + a pointer.")
    lines.append(bar)
    return "\n".join(lines)


__all__ = ["CONFIG_FILES", "CONTEXT_FILES", "CONTEXT_FILES_ORDERED", "fyi", "load_config",
           "measure", "public_record", "report", "resolve_surface", "tok"]
