"""The one gate check every piece of save/debloat automation goes through
(spec plugin-save-debloat 2.2).

Rules: (a) automation never runs without a VERIFIED entitlement in the
``fresh`` or ``stale`` band (locked, no_account and setup_incomplete: never);
(b) the tier is the entitlement's own ``tier`` claim, never the credentials
hint, the tier cache, ``lifecycle.json`` or ``debloat.json``; a gate that holds
no verified entitlement (the test/escape-hatch ``NullEntitlementGate``) never
automates; (c) every write the automation makes still goes through the gated
``MemoryClient``, so the account gate is applied twice; (d) files can turn
automation OFF, nothing can turn it ON for a free tier; (e) when this says no,
callers exit quietly and the free reminders take over.

``peek()`` is used: no network, no blocking.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tiers import FREE, PAID_TIERS, UNKNOWN_REASON, is_paid, normalize  # noqa: F401  (PAID_TIERS re-exported)


def decision_allows(dec: Any) -> tuple[bool, str]:
    state = getattr(dec, "state", None)
    if state not in ("fresh", "stale"):
        return False, str(getattr(dec, "code", None) or state or "no_account")
    ent = getattr(dec, "entitlement", None)
    tier = getattr(ent, "tier", None)
    if ent is None or not isinstance(tier, str) or not tier:
        return False, "no_entitlement"
    # Shared tier module (2026-10-05): a paid claim (any case, any legacy
    # alias) answers with its canonical tier; an unrecognised label is treated
    # as free and says so with the reason code, which the hooks log and
    # ``sibyl status`` shows.
    canonical = normalize(tier)
    if canonical is None:
        return False, UNKNOWN_REASON
    if is_paid(canonical):
        return True, canonical
    return False, FREE


def gate_allows(gate: Any) -> tuple[bool, str]:
    if gate is None:
        return False, "no_gate"
    try:
        dec = gate.peek()
    except Exception:
        return False, "gate_error"
    return decision_allows(dec)


def automation_allowed(client: Any) -> tuple[bool, str]:
    """(allowed, tier-or-reason) for a ``MemoryClient``."""
    return gate_allows(getattr(client, "entitlement_gate", None))


def automation_allowed_for_credentials(credentials_path: Path | str) -> tuple[bool, str]:
    """Same decision for a process that has no client (the Claude Code hooks):
    a gate over ``credentials_path`` with background refresh off, peeked."""
    try:
        from .._entitlement import EntitlementGate
        gate = EntitlementGate(Path(credentials_path).expanduser(), background_refresh=False)
    except Exception:
        return False, "gate_error"
    return gate_allows(gate)


__all__ = ["PAID_TIERS", "automation_allowed", "automation_allowed_for_credentials",
           "decision_allows", "gate_allows"]
