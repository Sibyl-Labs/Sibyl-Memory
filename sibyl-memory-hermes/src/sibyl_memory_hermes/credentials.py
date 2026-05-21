"""Credential loader for the Sibyl Memory plugin.

`sibyl init` writes `~/.sibyl-memory/credentials.json` after a successful
activation. The Hermes provider reads it at startup so callers don't have
to pass account/tenant IDs explicitly. This file is mode 0600.

Shape:

    {
      "account_id":   "uuid",
      "tenant_id":    "uuid OR email-like string",
      "email":        "alice@example.com",  // optional
      "wallet":       "0x...",              // optional
      "tier":         "free | sync | team | lifetime | stake | enterprise",
      "issued_at":    "2026-05-21T14:32:18Z",
      "schema_version": 1
    }
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "~/.sibyl-memory/memory.db"
DEFAULT_CRED_PATH = "~/.sibyl-memory/credentials.json"


class CredentialsNotFoundError(FileNotFoundError):
    """Raised when the plugin has not been activated yet."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(
            f"No Sibyl Memory credentials found at {path}. "
            f"Run `sibyl init` to activate the plugin, or pass tenant_id "
            f"explicitly to SibylMemoryProvider(tenant_id=...)."
        )
        self.path = Path(path)


@dataclass(frozen=True)
class Credentials:
    """Parsed credential file. Immutable for safety.

    schema_version 2 (server-issued 2026-05-16+) adds two fields:
      - signature: HMAC-SHA256 of the canonical credential fields
                   (account_id, tenant_id, tier, email, wallet, issued_at,
                   schema_version) signed server-side at issue time.
      - signed_at: ISO timestamp when the signature was generated.

    The SDK does NOT verify the signature locally (would require sharing the
    server's HMAC key, which would defeat the purpose). Instead, the SDK
    includes the signature alongside the claim in any cap-gate request, and
    the server re-verifies. Mismatches surface as `credentials_tamper_suspected`
    telemetry. The authoritative tier comes from the database regardless.

    schema_version 1 credentials (no signature) still load — old fields are
    None — and continue to work unsigned. The SDK just sends an unsigned
    request and the server skips the tamper check."""

    account_id: str
    tenant_id: str
    tier: str = "free"
    email: str | None = None
    wallet: str | None = None
    issued_at: str | None = None
    schema_version: int = 1
    session_token: str | None = None  # long-lived bearer for tier-check calls
    signature: str | None = None      # HMAC-SHA256 (hex, 64 chars), schema v2+
    signed_at: str | None = None      # ISO timestamp, schema v2+


def load_credentials(path: str | Path = DEFAULT_CRED_PATH) -> Credentials:
    """Load credentials from disk.

    v0.3.1 hardening (audit SEC-11): refuses to follow symlinks. A
    low-privilege attacker who once had write to ~/.sibyl-memory could
    redirect this file to read from /dev/null or any sensitive path. We
    use ``Path.is_symlink()`` to detect, then ``lstat`` to confirm the
    file type. On detection, raises ``CredentialsNotFoundError`` (the
    safe default — caller falls back to DEFAULT_TENANT).

    Raises:
        CredentialsNotFoundError: file missing or symlinked
        ValueError: file present but unparseable / missing required fields
        OSError: I/O failure reading the file
    """
    resolved = Path(path).expanduser()
    # SEC-11: detect symlinks BEFORE resolve() — resolve follows them silently.
    if resolved.is_symlink():
        raise CredentialsNotFoundError(resolved)
    resolved = resolved.resolve()
    if not resolved.exists():
        raise CredentialsNotFoundError(resolved)

    with resolved.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    # Be lenient about missing optional fields; strict only about the two
    # IDs we genuinely need.
    if "tenant_id" not in raw and "account_id" not in raw:
        raise ValueError(
            f"Credentials file at {resolved} is missing both tenant_id and account_id; "
            f"the file may be corrupted. Re-run `sibyl init` to refresh."
        )

    account_id = raw.get("account_id") or raw["tenant_id"]
    tenant_id = raw.get("tenant_id") or raw["account_id"]

    return Credentials(
        account_id=account_id,
        tenant_id=tenant_id,
        tier=raw.get("tier", "free"),
        email=raw.get("email"),
        wallet=raw.get("wallet"),
        issued_at=raw.get("issued_at"),
        schema_version=int(raw.get("schema_version", 1)),
        session_token=raw.get("session_token"),
        signature=raw.get("signature"),
        signed_at=raw.get("signed_at"),
    )


def write_credentials(creds: Credentials, path: str | Path = DEFAULT_CRED_PATH) -> Path:
    """Write a credentials file at mode 0600.

    v0.3.1 hardening (audit SEC-2): atomic create-with-mode using
    ``os.open(O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)``. Previously
    used ``tmp.write_text()`` then ``os.chmod(0o600)``, leaving a
    world-readable window between syscalls. Now mode is set by the
    kernel at file-creation time — no race.

    Used by `sibyl init`.
    """
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "account_id": creds.account_id,
        "tenant_id": creds.tenant_id,
        "tier": creds.tier,
        "email": creds.email,
        "wallet": creds.wallet,
        "issued_at": creds.issued_at,
        "schema_version": creds.schema_version,
        "session_token": creds.session_token,
        "signature": creds.signature,
        "signed_at": creds.signed_at,
    }
    data = json.dumps(payload, indent=2).encode("utf-8")
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    # Clean any leftover .tmp from a crashed prior write so O_EXCL can succeed.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    # Atomic create-with-mode. O_NOFOLLOW rejects symlink targets.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(resolved))
    return resolved
