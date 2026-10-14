"""Times shown to people: always with a zone.

Every clock time the CLI prints is UTC and says so: ``2026-10-09 19:08 UTC``.
A bare ``2026-10-09 19:08:12`` read as local time by one person and as server
time by another; the zone removes the guess. Stdlib only.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?"
                     r"(Z|[+-]\d{2}:?\d{2})?$")


def _epoch_of(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or len(value) > 40:
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    try:
        y, mo, d, hh, mi = (int(m.group(i)) for i in range(1, 6))
        epoch = calendar.timegm((y, mo, d, hh, mi, int(m.group(6) or 0), 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    off = m.group(7)
    if off and off != "Z":
        sign = -1 if off[0] == "-" else 1
        digits = off[1:].replace(":", "")
        epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return float(epoch)


def utc_minute(value: Any) -> str | None:
    """Epoch seconds or an ISO-8601 string (no offset = UTC) ->
    ``'2026-10-09 19:08 UTC'``; None when ``value`` is neither."""
    epoch = _epoch_of(value)
    if epoch is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch)) + " UTC"
    except (ValueError, OverflowError, OSError):
        return None


def utc_hm(value: Any) -> str | None:
    """Epoch seconds -> ``'19:08 UTC'`` (a time of day, for today's rows)."""
    epoch = _epoch_of(value)
    if epoch is None:
        return None
    try:
        return time.strftime("%H:%M", time.gmtime(epoch)) + " UTC"
    except (ValueError, OverflowError, OSError):
        return None
