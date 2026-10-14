"""The one place the client spells a tier name (tier convention, 2026-10-05).

The canonical product tiers are three: ``free``, ``pro`` and ``elite``.
Older servers, older credentials and historical subscription rows may still
carry a legacy label (``sync``, ``team``, ``lifetime``, ``stake``,
``enterprise``). Those are accepted on INPUT only, as aliases: every reader
normalises through :func:`normalize` and nothing in the client writes a legacy
label again.

An unrecognised label (a typo, a future plan name, an empty value) is treated
as Free, with the reason code :data:`UNKNOWN_REASON`, so a server/client skew
fails closed on both sides. The server still holds the authoritative cap, so a
write is never lost: at the cap the client asks the server, which answers.

Stdlib only and import-free on purpose: ``exceptions``, ``lint`` and
``_capcheck`` import it at module level, and the cli hooks mirror
:data:`PAID_TIERS` as a literal (a test asserts the two are equal).
"""

from __future__ import annotations

FREE, PRO, ELITE = "free", "pro", "elite"
CANONICAL = (FREE, PRO, ELITE)
PAID = frozenset({PRO, ELITE})

# Tokens and credentials from older servers. Input only, never written.
LEGACY_ALIASES = {
    "sync": PRO,
    "team": PRO,
    "lifetime": PRO,
    "stake": PRO,
    "enterprise": ELITE,
}

# The ONE paid set: canonical paid tiers plus every legacy alias (all of which
# map to a paid tier). Kept as a set of raw labels so a membership test on an
# already-lowercased label still works for callers that cannot normalise.
PAID_TIERS = frozenset(PAID) | frozenset(LEGACY_ALIASES)

UNKNOWN_REASON = "tier_unknown"

_DISPLAY = {FREE: "Free", PRO: "Pro", ELITE: "Elite"}

# Local soft caps per canonical tier. None means uncapped.
SOFT_CAPS: dict[str, int | None] = {
    FREE: 5 * 1024 * 1024,  # 5 MiB (raised 2026-08-06)
    PRO: None,
    ELITE: None,
}


def normalize(value) -> str | None:
    """Canonical tier for ``value``, or None when it is not recognised.

    Strings only: stripped and lowercased; a canonical name maps to itself, a
    legacy alias to its canonical tier. Anything else (None, a number, an
    empty string, a typo) is None. Never raises."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in CANONICAL:
        return v
    return LEGACY_ALIASES.get(v)


def known(value) -> bool:
    return normalize(value) is not None


def is_paid(value) -> bool:
    """True for pro, elite and every legacy paid alias. Free and unknown: False."""
    return normalize(value) in PAID


def display(value) -> str:
    """Display word: "Free" | "Pro" | "Elite". Unknown displays as "Free"."""
    return _DISPLAY[normalize(value) or FREE]


def soft_cap(value) -> int | None:
    """Local soft cap for ``value``. Unknown gets the free cap."""
    return SOFT_CAPS[normalize(value) or FREE]


__all__ = [
    "CANONICAL", "ELITE", "FREE", "LEGACY_ALIASES", "PAID", "PAID_TIERS", "PRO",
    "SOFT_CAPS", "UNKNOWN_REASON", "display", "is_paid", "known", "normalize",
    "soft_cap",
]
