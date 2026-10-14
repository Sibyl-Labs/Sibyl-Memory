"""Typed exception hierarchy for sibyl-memory-client.

Every error has a stable `code` for programmatic handling and a `recovery`
string suggesting what the caller should try next.

v0.4.0 (2026-05-18): `CapExceededError` + `TierVerificationError` relocated
here from `_capcheck.py` so they are importable from the canonical
`sibyl_memory_client.exceptions` submodule path (external test bug report against
sibyl-memory-mcp 0.1.1: server imported these from `.exceptions` but they
only lived on `._capcheck`).
"""
from __future__ import annotations

import re
from typing import Any

from . import tiers as _tiers


# Default upgrade URL for cap / tier-related errors. Kept here as a string
# literal so the exceptions module has no dependency on _capcheck (avoids the
# circular import that motivated the v0.4.0 reorganization).
_DEFAULT_UPGRADE_URL = "https://docs.sibyllabs.org/memory/tiers"


class SibylMemoryError(Exception):
    """Base for all sibyl-memory-client errors."""

    code: str = "SIBYL_MEMORY_ERROR"
    recovery: str = "See exception message for details."

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        super().__init__(message)
        if recovery is not None:
            self.recovery = recovery


def format_handoff(next: str, human_action: str | None = None) -> str:
    """The agent handoff block (spec 2026-10-03 section 1.1) without the
    status line: an optional ``ask your human:`` line, then ``next:``, which
    is always the last line. Plain text, column 0, no glyph."""
    lines = []
    if human_action:
        lines.append(f"ask your human: {human_action}")
    lines.append(f"next: {next}")
    return "\n".join(lines)


class _HandoffMixin:
    """``next`` / ``human_action`` class defaults for the cap and tier errors
    (rows C14 to C17). ``str()`` of these is unchanged; agent surfaces read the
    two attributes."""

    next: str = "done"
    human_action: str | None = None

    def handoff_lines(self) -> str:
        return format_handoff(self.next, self.human_action)


class StorageError(SibylMemoryError):
    code = "STORAGE_ERROR"
    recovery = "Check disk space and file permissions on ~/.sibyl-memory/."


class SchemaError(SibylMemoryError):
    code = "SCHEMA_ERROR"
    recovery = "The schema file is missing or corrupt. Re-install sibyl-memory-client."


class TenantError(SibylMemoryError):
    code = "TENANT_ERROR"
    recovery = "Set a tenant before calling write/read operations: client.set_tenant(uuid)."


class NotFoundError(SibylMemoryError):
    code = "NOT_FOUND"
    recovery = "The requested entity / state / reference does not exist."


class ConflictError(SibylMemoryError):
    code = "CONFLICT"
    recovery = "An entity with this (tenant_id, category, name) already exists. Use update_entity() instead."


class ValidationError(SibylMemoryError):
    code = "VALIDATION_ERROR"
    recovery = "Body must be a JSON-serializable dict / list / primitive."


class TierGateError(_HandoffMixin, SibylMemoryError):
    """Raised when a free-tier user invokes a paid-tier-only feature.

    Carries the user's current tier + an upgrade URL so callers can render
    a clean prompt. Self-learning + memory linter are both gated by this
    on the free tier; upgrading to any paid tier unlocks both.
    """

    code = "TIER_GATE"
    next = "sibyl upgrade"
    human_action = "this feature is on Pro; a purchase, so it is your call"
    recovery = (
        "Upgrade to Pro to unlock this feature. See "
        "https://sibyllabs.org/plugin#tier for options."
    )

    def __init__(
        self,
        message: str,
        *,
        feature: str,
        current_tier: str = "free",
        upgrade_url: str = "https://sibyllabs.org/plugin#tier",
    ) -> None:
        super().__init__(message)
        self.feature = feature
        self.current_tier = current_tier
        self.upgrade_url = upgrade_url


class CapExceededError(_HandoffMixin, SibylMemoryError):
    """Raised when a free-tier user tries to write past the 5 MB cap.

    Carries the upgrade URL so callers (CLIs, IDEs, agent frameworks) can
    render a clean upgrade prompt.

    v0.4.0: moved from `_capcheck.py` to `exceptions.py` so the canonical
    `sibyl_memory_client.exceptions` submodule path exports it. The class
    contract (code, recovery, current_size, cap, proposed_delta, upgrade_url
    attributes) is unchanged.
    """

    code = "CAP_EXCEEDED"
    next = "sibyl upgrade"
    human_action = ("the free 5 MB memory cap is full; Pro is a purchase, so it is your call; "
                    "nothing was deleted")
    recovery = (
        "Upgrade to Pro to remove the 5 MB cap. See "
        "https://docs.sibyllabs.org/memory/tiers for options."
    )

    def __init__(
        self,
        message: str,
        *,
        current_size: int,
        cap: int,
        proposed_delta: int = 0,
        upgrade_url: str = _DEFAULT_UPGRADE_URL,
    ) -> None:
        super().__init__(message)
        self.current_size = current_size
        self.cap = cap
        self.proposed_delta = proposed_delta
        self.upgrade_url = upgrade_url


class TierVerificationError(_HandoffMixin, SibylMemoryError):
    """Raised when the SDK can't verify the user's tier and has no cached
    grace period to fall back on (offline at the cap with no recent
    successful check).

    v0.4.0: moved from `_capcheck.py` to `exceptions.py` so the canonical
    `sibyl_memory_client.exceptions` submodule path exports it. The class
    contract (code, recovery) is unchanged.
    """

    code = "TIER_VERIFY_FAILED"
    next = "retry"
    human_action = "connect this computer to the internet once"
    recovery = (
        "Connect to the internet so the SDK can verify your account, or "
        "stay under the 5 MB free-tier cap until you're online."
    )


class TierAuthError(TierVerificationError):
    """Raised when the tier-verification server authoritatively refuses the
    request (HTTP 401/403): a bad, expired, forged, or revoked token.

    CAP-5 / CORE-2 (2026-06-25 pre-launch audit): this is a SUBCLASS of
    TierVerificationError so existing ``except TierVerificationError`` handlers
    still catch it, but the cap gate handles it distinctly — an auth refusal is
    authoritative ("not entitled"), so the gate enforces the free cap and NEVER
    fails open. Only a genuine reachability failure (timeout, connection error,
    5xx) is eligible for the bounded fail-open concession.
    """

    code = "TIER_AUTH_FAILED"
    next = "sibyl init --force"
    human_action = None
    recovery = (
        "Your account could not be authorized (token invalid, expired, or "
        "revoked). Re-run `sibyl init` to refresh credentials. The free 5 MB "
        "cap is enforced until your account is re-verified."
    )


# ----------------------------------------------------------------------
# Account gate (0.9.0, spec account-gated-plugin 2026-09-27)
# ----------------------------------------------------------------------
# The copy below is the section 8 text. Agent surfaces (MCP, hermes) relay it
# verbatim, so it is written for a person: one action, plain words, no jargon,
# and it always says the local data is kept.

ACCOUNT_URL = "https://account.sibyllabs.org"

_UPDATE_COMMAND = (
    "pip install -U sibyl-memory-client sibyl-memory-mcp sibyl-memory-hermes "
    "sibyl-memory-cli"
)

ENTITLEMENT_MESSAGES: dict[str, str] = {
    "no_account": (
        "Sibyl Memory needs an account before it can store or recall anything. "
        "Open a terminal and run: sibyl init\n"
        "It takes about a minute: you sign in with your email (or a wallet) and "
        "this computer is linked to your account.\n"
        "Your existing local memory is kept and becomes available again as soon "
        "as sign-in finishes."
    ),
    "signed_out": (
        "This computer was signed out of your Sibyl Memory account (from the "
        "account page, sibyl devices, or because the sign-in expired after 30 "
        "days without contact). Run sibyl init to sign this computer in again.\n"
        "Your local memory is kept and will be available after you sign in."
    ),
    "expired_offline": (
        "Sibyl Memory has been offline for more than 14 days and needs to check "
        "in with your account.\n"
        "Connect to the internet and try again; it renews automatically. If it "
        "still does not, run: sibyl init\n"
        "Nothing was deleted. Your local memory is available again as soon as "
        "the check-in succeeds."
    ),
    "setup_incomplete": (
        "Sibyl Memory needs to reach your account once to finish setting up on "
        "this computer, and it could not connect.\n"
        "Connect to the internet and try again; setup finishes by itself. If it "
        "still does not, run: sibyl init\n"
        "Nothing was deleted. Your local memory is available as soon as setup "
        "finishes."
    ),
    "under_review": (
        "Your Sibyl Memory account is under review. Email sibyl@sibyllabs.org "
        "and we will sort it out. Your local memory is kept."
    ),
    "account_deleted": (
        "This Sibyl Memory account was deleted. Run sibyl init to create or sign "
        "in to an account. Your local memory is kept."
    ),
    "invalid_entitlement": (
        "Sibyl Memory's sign-in file on this computer is damaged or belongs to "
        "another computer. Run sibyl init to sign in again. Your local memory "
        "is kept."
    ),
    "wrong_device": (
        "Sibyl Memory's sign-in file on this computer is damaged or belongs to "
        "another computer. Run sibyl init to sign in again. Your local memory "
        "is kept."
    ),
    "policy_read_only": (
        "Your account needs a confirmed email to save new memories. Reading "
        "still works. Run: sibyl email add you@example.com"
    ),
    "policy_none": (
        "Your account needs a confirmed email before Sibyl Memory can be used. "
        "Run: sibyl email add you@example.com. Your local memory is kept."
    ),
    "client_too_old": (
        "This version of Sibyl Memory is too old for your account. "
        f"update the plugin: {_UPDATE_COMMAND}. Your local memory is kept."
    ),
}

ENTITLEMENT_RECOVERY: dict[str, str] = {
    "no_account": "Run sibyl init in a terminal, then retry.",
    "signed_out": "Run sibyl init in a terminal, then retry.",
    "expired_offline": "Connect to the internet and retry. If it still fails, run sibyl init.",
    "setup_incomplete": "Connect to the internet once and retry. If it still fails, run sibyl init.",
    "under_review": "Email sibyl@sibyllabs.org.",
    "account_deleted": "Run sibyl init in a terminal, then retry.",
    "invalid_entitlement": "Run sibyl init in a terminal, then retry.",
    "wrong_device": "Run sibyl init in a terminal, then retry.",
    "policy_read_only": "Run sibyl email add you@example.com, then retry.",
    "policy_none": "Run sibyl email add you@example.com, then retry.",
    "client_too_old": f"Run {_UPDATE_COMMAND}, then retry.",
}

# Agent handoff (spec agent-handoff-messages 2026-10-03, rows C1 to C11): the
# exact command an agent runs next, and the sentence it relays to its person
# (None when no person has to act). Keyed by the same codes as above.
_OFFLINE_HUMAN = "connect this computer to the internet once; nothing was deleted"

ENTITLEMENT_NEXT: dict[str, str] = {
    "no_account": "sibyl init",
    "signed_out": "sibyl init",
    "expired_offline": "sibyl refresh",
    "setup_incomplete": "sibyl refresh",
    "under_review": "done",
    "account_deleted": "sibyl init",
    "invalid_entitlement": "sibyl init --force",
    "wrong_device": "sibyl init --force",
    "policy_read_only": "sibyl email add <address>",
    "policy_none": "sibyl email add <address>",
    "client_too_old": _UPDATE_COMMAND,
}

ENTITLEMENT_HUMAN_ACTION: dict[str, str | None] = {
    "no_account": None,
    "signed_out": None,
    "expired_offline": _OFFLINE_HUMAN,
    "setup_incomplete": _OFFLINE_HUMAN,
    "under_review": "your account is under review; email sibyl@sibyllabs.org and we will sort it out",
    "account_deleted": "this account was deleted; signing in creates or picks an account",
    "invalid_entitlement": None,
    "wrong_device": None,
    "policy_read_only": "an email address to confirm for the account; reading still works",
    "policy_none": "an email address to confirm for the account",
    "client_too_old": None,
}

ENTITLEMENT_CODES = frozenset(ENTITLEMENT_MESSAGES)

# F-1 (contract C1): the api's 401 for a sign-in that was revoked when the
# account's email was first confirmed on the account page (the web email claim
# signs out every computer set up before it). The code stays ``signed_out`` (the
# MCP ``reason`` is stable); the cause rides beside it and changes the copy and
# the next step.
SIGNED_OUT_ON_CLAIM = "signed_out_on_claim"
SIGNED_OUT_ON_CLAIM_NEXT = "sibyl init --force"
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?"
                     r"(Z|[+-]\d{2}:?\d{2})?$")


def clean_iso(value: Any) -> str | None:
    """``value`` when it is a plausible ISO-8601 timestamp (at most 40
    characters), else None. Never trusted beyond display."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    v = value.strip()
    return v if _ISO_RE.match(v) else None


def utc_minute(value: Any) -> str | None:
    """'2026-10-09T21:08:12+02:00' -> '2026-10-09 19:08' (UTC; a value with no
    offset is read as UTC), or None when ``value`` is not an ISO timestamp."""
    import calendar
    v = clean_iso(value)
    if v is None:
        return None
    m = _ISO_RE.match(v)
    try:
        y, mo, d, hh, mi = (int(m.group(i)) for i in range(1, 6))
        ss = int(m.group(6) or 0)
        epoch = calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0))
        off = m.group(7)
        if off and off != "Z":
            sign = -1 if off[0] == "-" else 1
            digits = off[1:].replace(":", "")
            epoch -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
        import time as _time
        return _time.strftime("%Y-%m-%d %H:%M", _time.gmtime(epoch))
    except (ValueError, OverflowError, OSError):
        return None


def signed_out_on_claim_message(signed_out_at: Any = None) -> str:
    """The section 8 copy for a computer signed out by the web email claim."""
    when = utc_minute(signed_out_at)
    at = f" ({when} UTC)" if when else ""
    return ("This computer was signed out of your Sibyl Memory account when your email was "
            f"confirmed on the account page{at}. Run sibyl init --force and approve this computer "
            "on the account page to sign it in again.\n"
            "Your local memory is kept and will be available after you sign in.")

SECURITY_URL = f"{ACCOUNT_URL}/security"
# The same page as people read it in copy: no scheme.
_SECURITY_PAGE = SECURITY_URL.split("://", 1)[-1]
DEVICES_READ_ONLY_RECOVERY = (
    f"Sign the other computers out at {_SECURITY_PAGE} (or run sibyl devices), "
    "or get Pro (sibyl upgrade); reading works meanwhile.")
# Row C12: the computer-limit read-only handoff (same code policy_read_only).
DEVICES_READ_ONLY_NEXT = "sibyl devices"
DEVICES_READ_ONLY_HUMAN_ACTION = (
    "more computers are signed in than the plan allows; sign one out at "
    f"{_SECURITY_PAGE}, or get Pro; reading works meanwhile")


def device_limit_info(acct: Any) -> dict[str, Any] | None:
    """``acct.devices`` of a verified entitlement (plan 4.2: ``{n, limit,
    over}``), shape-checked, or None. ``limit`` None means no limit."""
    if not isinstance(acct, dict):
        return None
    d = acct.get("devices")
    if not isinstance(d, dict):
        return None
    n, limit = d.get("n"), d.get("limit")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        return None
    return {"n": n, "limit": limit, "over": d.get("over") is True}


def devices_read_only_message(acct: Any, tier: Any = None) -> str | None:
    """The read-only copy when the server set ``mem: read_only`` because the
    account has more computers signed in than its plan allows (``acct.devices.
    over``). None when that is not the reason (the email copy applies)."""
    info = device_limit_info(acct)
    if info is None or not info["over"]:
        return None
    free = not _tiers.is_paid(tier)
    lim = info["limit"]
    plan = ("Free is one computer" if free and lim == 1
            else f"your plan allows {lim}" if lim is not None else "your plan's limit is lower")
    tail = " or get Pro" if free else ""
    return (f"Your account has {info['n']} computers signed in; {plan}. Reading works everywhere; "
            "new memories save only on the computer that signed in most recently. Sign the others out at "
            f"{_SECURITY_PAGE}{tail}. Nothing was deleted.")


class EntitlementError(SibylMemoryError):
    """The account gate refused a memory operation (0.9.0).

    ``code`` is the fine state (spec 3.1): one of ``no_account``,
    ``signed_out``, ``expired_offline``, ``setup_incomplete``, ``under_review``,
    ``account_deleted``,
    ``invalid_entitlement``, ``wrong_device``, ``policy_read_only``,
    ``policy_none``, ``client_too_old``; ``reason`` is the same value (the name
    the MCP envelope uses), and ``surface_code`` is the stable class code
    ``ACCOUNT_REQUIRED`` agent surfaces key on. ``summary`` is the section 8
    copy a person can act on; ``str(e)`` is that copy followed by the agent
    handoff lines (``ask your human:`` when a person must act, then ``next:``,
    always last; spec 2026-10-03), read individually as ``next`` and
    ``human_action``. It never contains a token or a session value.

    Local data is never touched by the gate: the escape hatch is the CLI
    (``sibyl memory list|search|recall``), which reads local data with no
    account.
    """

    code = "no_account"
    surface_code = "ACCOUNT_REQUIRED"
    recovery = "Run sibyl init in a terminal, then retry."

    def __init__(self, reason: str, *, message: str | None = None,
                 next: str | None = None, human_action: str | None = None) -> None:
        if reason not in ENTITLEMENT_CODES:
            reason = "invalid_entitlement"
        super().__init__(
            message or ENTITLEMENT_MESSAGES[reason],
            recovery=ENTITLEMENT_RECOVERY[reason],
        )
        self.code = reason
        self.reason = reason
        self.account_url = ACCOUNT_URL
        # Agent handoff (spec 2026-10-03 section 1.6). ``summary`` is the
        # section 8 copy (what str(e) was before); str(e) now ends with the
        # handoff lines so every surface relaying the text carries them.
        self.summary = message or ENTITLEMENT_MESSAGES[reason]
        self.next = next if next is not None else ENTITLEMENT_NEXT[reason]
        self.human_action = (human_action if next is not None or human_action is not None
                             else ENTITLEMENT_HUMAN_ACTION[reason])

    def handoff_lines(self) -> str:
        """The convention block without the status line: ``ask your human:``
        (when a person must act), then ``next:`` (always last)."""
        return format_handoff(self.next, self.human_action)

    def __str__(self) -> str:
        return f"{self.summary}\n{self.handoff_lines()}"
