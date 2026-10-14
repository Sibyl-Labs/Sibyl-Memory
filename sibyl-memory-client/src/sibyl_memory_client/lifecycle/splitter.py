"""The safe splitter: a pure, deterministic HISTORY / CURRENT classification of
an instruction file (spec plugin-save-debloat 5.4). No I/O.

A line is HISTORY only when one of these rules matches:

1. it is inside ``<!-- sibyl:history -->`` ... ``<!-- /sibyl:history -->``
   (the marker lines move with the region);
2. it is in a section whose heading matches ``history_heading_regex``
   (a section runs from its heading to the next heading of the same or a
   higher level);
3. it is in a section whose heading, or first non-empty line, carries an ISO
   date (``YYYY-MM-DD``) older than ``history_after_days`` against the local
   clock, and the section holds no unresolved marker (``TODO``, ``[ ]``,
   ``NEXT``, ``open``; case-insensitive);
4. it is below a ``<!-- sibyl:trim-below -->`` marker.

Everything else is CURRENT. Lines inside ``<!-- sibyl:keep -->`` or
``<!-- sibyl:immutable -->`` regions (an unclosed one runs to the end of the
file) are CURRENT whatever else matches. Headings and markers inside fenced
code blocks are ignored. ``.cursorrules`` (``kind="paragraph"``) uses
blank-line paragraphs as sections; their first line plays the heading.

A plan that would move less than 10 % or more than 90 % of the file (by
UTF-8 bytes), or leave fewer than 5 non-blank lines, is "no safe trim".

Loss-free by construction: the file is split into maximal runs of same-label
lines; ``layout`` records every run's label and length, so
``reassemble(current, history, layout)`` returns the original text exactly
(CRLF and a UTF-8 BOM included).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

HISTORY_HEADING_REGEX = r"^(changelog|history|log|session notes|archive|done)\b"
HISTORY_AFTER_DAYS = 30
MIN_FRACTION = 0.10
MAX_FRACTION = 0.90
MIN_CURRENT_LINES = 5

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_OPEN_MARKERS = re.compile(r"TODO|\[ \]|\bNEXT\b|\bopen\b", re.IGNORECASE)

_M_HISTORY_OPEN = "<!-- sibyl:history -->"
_M_HISTORY_CLOSE = "<!-- /sibyl:history -->"
_M_TRIM_BELOW = "<!-- sibyl:trim-below -->"
_PROTECT = (("<!-- sibyl:keep -->", "<!-- /sibyl:keep -->"),
            ("<!-- sibyl:immutable -->", "<!-- /sibyl:immutable -->"))


@dataclass
class Plan:
    safe: bool
    reason: str
    layout: list[tuple[str, int]] = field(default_factory=list)   # ("C"|"H", chars)
    current: str = ""
    history: str = ""
    sections: list[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_history: int = 0

    @property
    def tok_before(self) -> int:
        from .measure import tok
        return tok(self.bytes_before)

    @property
    def tok_history(self) -> int:
        from .measure import tok
        return tok(self.bytes_history)

    @property
    def tok_after(self) -> int:
        from .measure import tok
        return tok(self.bytes_before - self.bytes_history)

    def as_dict(self) -> dict[str, Any]:
        return {"safe": self.safe, "reason": self.reason, "sections": list(self.sections),
                "tok_before": self.tok_before, "tok_history": self.tok_history,
                "tok_after": self.tok_after, "layout": [list(x) for x in self.layout]}


def _strip(line: str) -> str:
    return line.rstrip("\r\n")


def _marker(line: str, marker: str) -> bool:
    return _strip(line).strip() == marker


def _parse_date(s: str) -> datetime | None:
    for m in _DATE.finditer(s):
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def _sections_markdown(lines: list[str], in_fence: list[bool]) -> list[tuple[int, int, str, int]]:
    """[(start, end_exclusive, heading text, level)] for every heading."""
    heads: list[tuple[int, int, str]] = []
    for i, ln in enumerate(lines):
        if in_fence[i]:
            continue
        m = _HEADING.match(_strip(ln))
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out = []
    for k, (i, lvl, txt) in enumerate(heads):
        end = len(lines)
        for j, lvl2, _t in heads[k + 1:]:
            if lvl2 <= lvl:
                end = j
                break
        out.append((i, end, txt, lvl))
    return out


def _sections_paragraph(lines: list[str]) -> list[tuple[int, int, str, int]]:
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if not _strip(lines[i]).strip():
            i += 1
            continue
        j = i
        while j < n and _strip(lines[j]).strip():
            j += 1
        out.append((i, j, _strip(lines[i]).strip(), 1))
        i = j
    return out


def split(text: str, *, kind: str = "markdown", now: datetime | None = None,
          history_after_days: int = HISTORY_AFTER_DAYS,
          history_heading_regex: str = HISTORY_HEADING_REGEX) -> Plan:
    """Classify ``text``; never raises for any input string."""
    now = now or datetime.now()
    lines = text.splitlines(keepends=True)
    n = len(lines)
    total_bytes = len(text.encode("utf-8"))
    if n == 0:
        return Plan(False, "empty file", [], "", "", [], 0, 0)
    try:
        heading_re = re.compile(history_heading_regex, re.IGNORECASE)
    except re.error:
        heading_re = re.compile(HISTORY_HEADING_REGEX, re.IGNORECASE)
    cutoff = now - timedelta(days=max(0, int(history_after_days)))

    # Fenced code (markdown only).
    in_fence = [False] * n
    if kind == "markdown":
        fence: str | None = None
        for i, ln in enumerate(lines):
            m = _FENCE.match(ln)
            if fence is None:
                if m:
                    fence = m.group(1)
                    in_fence[i] = True
            else:
                in_fence[i] = True
                if m and m.group(1) == fence:
                    fence = None

    hist = [False] * n
    protected = [False] * n
    # Protected regions (unclosed = to the end of the file).
    for open_m, close_m in _PROTECT:
        i = 0
        while i < n:
            if not in_fence[i] and _marker(lines[i], open_m):
                j = i
                while j < n:
                    protected[j] = True
                    if j > i and not in_fence[j] and _marker(lines[j], close_m):
                        break
                    j += 1
                i = j + 1
            else:
                i += 1
    # Rule 1: explicit history regions (an unclosed one moves nothing).
    i = 0
    while i < n:
        if not in_fence[i] and _marker(lines[i], _M_HISTORY_OPEN):
            j = i + 1
            while j < n and not (not in_fence[j] and _marker(lines[j], _M_HISTORY_CLOSE)):
                j += 1
            if j < n:
                for k in range(i, j + 1):
                    hist[k] = True
                i = j + 1
                continue
        i += 1
    # Rule 4: trim-below (the marker line itself stays).
    for i in range(n):
        if not in_fence[i] and _marker(lines[i], _M_TRIM_BELOW):
            for k in range(i + 1, n):
                hist[k] = True
            break
    # Rules 2 and 3: sections.
    sections = (_sections_markdown(lines, in_fence) if kind == "markdown"
                else _sections_paragraph(lines))
    moved_headings: list[str] = []
    for start, end, title, _lvl in sections:
        is_hist = False
        if heading_re.search(title):
            is_hist = True
        else:
            first = ""
            for k in range(start + 1, end):
                if _strip(lines[k]).strip():
                    first = _strip(lines[k])
                    break
            d = _parse_date(title) or _parse_date(first)
            if d is not None and d < cutoff:
                body = "".join(lines[start:end])
                if not _OPEN_MARKERS.search(body):
                    is_hist = True
        if is_hist:
            for k in range(start, end):
                hist[k] = True
    # Protection wins.
    for i in range(n):
        if protected[i]:
            hist[i] = False
    # Headings of moved sections (for the digest), in order, deduped.
    for start, _end, title, _lvl in sections:
        if hist[start] and title not in moved_headings:
            moved_headings.append(title[:80])

    layout: list[tuple[str, int]] = []
    cur_parts: list[str] = []
    his_parts: list[str] = []
    for i, ln in enumerate(lines):
        lab = "H" if hist[i] else "C"
        (his_parts if hist[i] else cur_parts).append(ln)
        if layout and layout[-1][0] == lab:
            layout[-1] = (lab, layout[-1][1] + len(ln))
        else:
            layout.append((lab, len(ln)))
    current = "".join(cur_parts)
    history = "".join(his_parts)
    hbytes = len(history.encode("utf-8"))
    plan = Plan(False, "", layout, current, history, moved_headings, total_bytes, hbytes)
    if hbytes == 0:
        plan.reason = "nothing classified as history"
        return plan
    frac = hbytes / total_bytes if total_bytes else 0.0
    current_lines = sum(1 for ln in cur_parts if _strip(ln).strip())
    if frac < MIN_FRACTION:
        plan.reason = f"no safe trim: would move under {int(MIN_FRACTION * 100)} % of the file"
        return plan
    if frac > MAX_FRACTION:
        plan.reason = f"no safe trim: would move over {int(MAX_FRACTION * 100)} % of the file"
        return plan
    if current_lines < MIN_CURRENT_LINES:
        plan.reason = f"no safe trim: would leave fewer than {MIN_CURRENT_LINES} lines"
        return plan
    plan.safe = True
    plan.reason = "ok"
    return plan


def reassemble(current: str, history: str, layout: list[tuple[str, int]] | list[list[Any]]) -> str:
    """Rebuild the original text from the two projections and the layout."""
    out: list[str] = []
    ci = hi = 0
    for lab, length in layout:
        if lab == "C":
            out.append(current[ci:ci + length])
            ci += length
        else:
            out.append(history[hi:hi + length])
            hi += length
    return "".join(out)


def kind_for(path_name: str) -> str | None:
    """``markdown`` for .md/.mdc/.markdown/.txt, ``paragraph`` for .cursorrules,
    None (not trimmable) for anything else."""
    low = path_name.lower()
    if low.endswith(".cursorrules") or low == ".cursorrules":
        return "paragraph"
    if low.endswith((".md", ".mdc", ".markdown", ".txt")):
        return "markdown"
    return None


__all__ = ["HISTORY_AFTER_DAYS", "HISTORY_HEADING_REGEX", "Plan", "kind_for", "reassemble", "split"]
