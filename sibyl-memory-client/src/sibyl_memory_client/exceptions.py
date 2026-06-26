"""Typed exception hierarchy for sibyl-memory-client.

Every error has a stable `code` for programmatic handling and a `recovery`
string suggesting what the caller should try next.

v0.4.0 (2026-05-18): `CapExceededError` + `TierVerificationError` relocated
here from `_capcheck.py` so they are importable from the canonical
`sibyl_memory_client.exceptions` submodule path (KAPPA bug report against
sibyl-memory-mcp 0.1.1: server imported these from `.exceptions` but they
only lived on `._capcheck`).
"""
from __future__ import annotations


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


class TierGateError(SibylMemoryError):
    """Raised when a free-tier user invokes a paid-tier-only feature.

    Carries the user's current tier + an upgrade URL so callers can render
    a clean prompt. Self-learning + memory linter are both gated by this
    on the free tier; upgrading to any paid tier unlocks both.
    """

    code = "TIER_GATE"
    recovery = (
        "Upgrade your plugin tier to unlock this feature. See "
        "https://sibyllabs.org/plugin#tier for options "
        "(Sibyl Stake / Sync / Lifetime / Enterprise)."
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


class CapExceededError(SibylMemoryError):
    """Raised when a free-tier user tries to write past the 2 MB cap.

    Carries the upgrade URL so callers (CLIs, IDEs, agent frameworks) can
    render a clean upgrade prompt.

    v0.4.0: moved from `_capcheck.py` to `exceptions.py` so the canonical
    `sibyl_memory_client.exceptions` submodule path exports it. The class
    contract (code, recovery, current_size, cap, proposed_delta, upgrade_url
    attributes) is unchanged.
    """

    code = "CAP_EXCEEDED"
    recovery = (
        "Upgrade to remove the 2 MB cap. See "
        "https://docs.sibyllabs.org/memory/tiers for options "
        "(Sibyl Stake / Sync / Lifetime / Enterprise)."
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


class TierVerificationError(SibylMemoryError):
    """Raised when the SDK can't verify the user's tier and has no cached
    grace period to fall back on (offline at the cap with no recent
    successful check).

    v0.4.0: moved from `_capcheck.py` to `exceptions.py` so the canonical
    `sibyl_memory_client.exceptions` submodule path exports it. The class
    contract (code, recovery) is unchanged.
    """

    code = "TIER_VERIFY_FAILED"
    recovery = (
        "Connect to the internet so the SDK can verify your account, or "
        "stay under the 2 MB free-tier cap until you're online."
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
    recovery = (
        "Your account could not be authorized (token invalid, expired, or "
        "revoked). Re-run `sibyl init` to refresh credentials. The free 2 MB "
        "cap is enforced until your account is re-verified."
    )
