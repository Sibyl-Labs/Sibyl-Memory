"""Hard-cap enforcement with server-authoritative tier verification.

Design (v0.3.0):

    1. Every write call (set_entity, write_event, set_state, set_reference)
       calls _check_write_allowed(proposed_delta_bytes).
    2. Three fast paths skip the server call:
       a) tier in PAID_TIERS (locally cached, refreshed weekly)
       b) db_size + delta would still be well under the cap
       c) we have a recent cached server result that says we're under-cap
    3. The slow path (only fires at the cap boundary) hits the server endpoint
       POST /api/plugin/check-write with current_size + proposed_delta. The
       server is the authoritative source for tier: credentials.json
       tampering is detected here because the server looks up the real tier
       from the server-side account database.
    4. Server response is cached for 7 days. After that, the next write at the
       cap forces a refresh. Users who go offline keep working under the
       cached result; if their cached tier says paid, they keep their grant
       for up to a week.
    5. Offline at the cap boundary with NO cache: hard block with a clear
       error pointing at the upgrade URL.

The local-first promise is preserved: no memory content ever crosses the
network. Only (account_id, current_size_bytes, proposed_delta_bytes) is sent
to the check-write endpoint.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# v0.4.0: CapExceededError + TierVerificationError live in exceptions.py
# (canonical exception module). _capcheck imports them back so existing
# callers that import from `sibyl_memory_client._capcheck` still resolve.
# The MCP server (sibyl-memory-mcp >= 0.1.2) imports from the canonical
# `.exceptions` path; this re-export keeps the historical path alive too.
from .exceptions import (  # noqa: F401  (re-exported for backwards compat)
    CapExceededError,
    SibylMemoryError,
    TierVerificationError,
)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

FREE_TIER_CAP_BYTES = 2 * 1024 * 1024  # 2 MB
GRACE_PERIOD_SECONDS = 7 * 24 * 60 * 60  # 7 days
PAID_TIERS = frozenset({"sync", "team", "lifetime", "stake", "enterprise"})

DEFAULT_CHECK_WRITE_URL = "https://api.sibyllabs.org/api/plugin/check-write"
DEFAULT_UPGRADE_URL = "https://docs.sibyllabs.org/memory/tiers"
DEFAULT_CACHE_PATH = "~/.sibyl-memory/tier_cache.json"

# Network timeout for the check-write call. Short to keep latency tolerable
# on the user's first write at the cap.
HTTP_TIMEOUT_SECONDS = 4.0


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------

@dataclass
class TierCacheEntry:
    """A single tier-check result cached on disk.

    Fields:
        account_id, tier, checked_at, cap_bytes, last_known_size: original
            v0.3.0 schema fields.
        grace_seconds: legacy local grace window (default 7d).
        server_expires_at: T1-4 anchor (v0.3.2+). The server-supplied
            subscription expiry (epoch seconds). When set, this is the
            authoritative end-of-validity. Cache is honored only while
            `now < min(checked_at + grace_seconds, server_expires_at)`.
            For staker/free tier this is None (cache uses grace_seconds only).
        cache_token: T1-2-lite (v0.3.2+). Opaque token issued by the
            server (currently a copy of `credentials.signature`). Sent back
            on every cap-check so the server can detect tampering of the
            cache file. Authoritative cap decision still comes from the
            server-side tier lookup.
    """
    account_id: str
    tier: str
    checked_at: float          # epoch seconds when we got the result
    cap_bytes: int | None      # None = uncapped (paid)
    last_known_size: int = 0   # the size we reported when we made the check
    grace_seconds: int = GRACE_PERIOD_SECONDS
    server_expires_at: float | None = None
    cache_token: str | None = None

    @property
    def expires_at(self) -> float:
        local = self.checked_at + self.grace_seconds
        if self.server_expires_at is not None:
            return min(local, self.server_expires_at)
        return local

    @property
    def is_fresh(self) -> bool:
        return time.time() < self.expires_at


class TierCache:
    """File-backed tier cache. Mode 0600. Single entry per file."""

    def __init__(self, path: str | Path = DEFAULT_CACHE_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load(self) -> TierCacheEntry | None:
        """Load the cache entry. v0.3.3 hardens against symlink swapping:
        refuses to follow symlinks (SEC-11). Returns None on missing,
        symlinked, corrupted, or unreadable cache."""
        if not self.path.exists():
            return None
        try:
            # SEC-11: reject symlinks. A low-privilege attacker who once had
            # write to ~/.sibyl-memory could symlink the cache to /dev/null
            # or another sensitive file.
            if self.path.is_symlink():
                return None
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            server_exp = raw.get("server_expires_at")
            return TierCacheEntry(
                account_id=raw["account_id"],
                tier=raw["tier"],
                checked_at=float(raw["checked_at"]),
                cap_bytes=raw.get("cap_bytes"),
                last_known_size=int(raw.get("last_known_size", 0)),
                grace_seconds=int(raw.get("grace_seconds", GRACE_PERIOD_SECONDS)),
                server_expires_at=(float(server_exp) if server_exp is not None else None),
                cache_token=raw.get("cache_token"),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None  # corrupted cache, treat as missing

    def store(self, entry: TierCacheEntry) -> None:
        """Atomic store with mode 0o600 set at creation (not after the fact).

        SEC-2 hardening (v0.3.3): the previous write_text() + chmod() pattern
        left a world-readable window between the syscalls. Now we open with
        O_CREAT|O_EXCL|O_WRONLY and mode 0o600: the kernel sets mode at the
        moment of creation, no race window."""
        payload = {
            "account_id": entry.account_id,
            "tier": entry.tier,
            "checked_at": entry.checked_at,
            "cap_bytes": entry.cap_bytes,
            "last_known_size": entry.last_known_size,
            "grace_seconds": entry.grace_seconds,
            "server_expires_at": entry.server_expires_at,
            "cache_token": entry.cache_token,
        }
        data = json.dumps(payload, indent=2).encode("utf-8")
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        # Clean any leftover tmp from a crashed prior write so O_EXCL can succeed.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        # Atomic create-with-mode. O_NOFOLLOW rejects symlink targets.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(self.path))

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# ----------------------------------------------------------------------
# Server check
# ----------------------------------------------------------------------

def _default_check_write_fn(
    url: str,
    payload: dict[str, Any],
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Default network transport for the check-write call.

    Pure stdlib (urllib) to keep the SDK zero-dependency. If the call
    fails (timeout, network error, non-2xx), raises TierVerificationError.
    Callers can pass in a custom fn for testing or for using their own
    HTTP client.
    """
    import urllib.request
    import urllib.error
    body = json.dumps(payload).encode("utf-8")
    # User-Agent sourced from installed metadata so version drift is impossible.
    try:
        from importlib.metadata import version as _pkg_version, PackageNotFoundError
        try:
            _ua_ver = _pkg_version("sibyl-memory-client")
        except PackageNotFoundError:
            _ua_ver = "0.0.0+source"
    except Exception:
        _ua_ver = "0.0.0+source"
    # v0.4.1 (auth-redesign wave 1 step 15): forward-compat with the
    # server bearer model. If the payload carries a bearer_token (new server protocol)
    # OR session_token (v1 backward compat where bearer == session), send it
    # as `Authorization: Bearer <token>` in addition to the body field. Server
    # accepts either path; this aligns the SDK to the new protocol without
    # breaking older servers that only read the body.
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"sibyl-memory-client/{_ua_ver}",
        "Accept": "application/json",
    }
    auth_value = payload.get("bearer_token") or payload.get("session_token")
    if auth_value:
        headers["Authorization"] = f"Bearer {auth_value}"
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # T2-3 fix: do NOT synthesize a fake "free tier" decision on HTTP
        # error. Previously a transient 502 would write `{tier:free, cap_bytes:2MB}`
        # into the cache for a legitimately paid user, locking them out
        # for up to 7 days. Now we raise TierVerificationError: the
        # caller (_refresh_and_check) will fall back to a recent cache
        # if one exists, or hard-cap if no cache.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        # SEC-9 (v0.3.3): strip the server-side `error` string from the
        # surfaced message to avoid echoing verbose internal detail (or
        # potential PII) into user logs.
        raise TierVerificationError(
            f"Sibyl Labs returned HTTP {e.code} while verifying your account. "
            f"Retry shortly.",
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TierVerificationError(
            f"Could not reach Sibyl Labs to verify your account: {type(e).__name__}",
        ) from e


# ----------------------------------------------------------------------
# Cap gate
# ----------------------------------------------------------------------

class CapGate:
    """Orchestrates the cap check across the SDK write paths.

    Args:
        account_id: the account_id from credentials.json (None for
            unactivated users; the gate behaves as free tier with no
            server check capability)
        session_token: bearer token sent with the check-write call
        db_size_fn: callable returning the current SQLite db size in bytes
        local_tier_hint: initial tier from credentials.json (advisory; the
            server's answer always wins when we have one)
        cache: TierCache instance (defaults to ~/.sibyl-memory/tier_cache.json)
        check_url: full URL to the check-write endpoint
        check_fn: pluggable transport (default: stdlib urllib)
    """

    def __init__(
        self,
        *,
        account_id: str | None,
        session_token: str | None,
        db_size_fn: Callable[[], int],
        local_tier_hint: str = "free",
        cache: TierCache | None = None,
        check_url: str = DEFAULT_CHECK_WRITE_URL,
        check_fn: Callable[..., dict[str, Any]] | None = None,
        cap_bytes: int = FREE_TIER_CAP_BYTES,
        credentials_claim: dict[str, Any] | None = None,
        credentials_signature: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.session_token = session_token
        self._db_size_fn = db_size_fn
        self._local_hint = local_tier_hint
        self._cache = cache if cache is not None else TierCache()
        self._check_url = check_url
        self._check_fn = check_fn or _default_check_write_fn
        self._cap = cap_bytes
        # HMAC signature + the claim it commits to. When both are present,
        # the server can verify and log mismatches as tamper-suspected
        # telemetry. Authoritative tier always comes from the DB regardless
        #: these fields are advisory, defense in depth only.
        self._credentials_claim = credentials_claim
        self._credentials_signature = credentials_signature

    # ------------------------------------------------------------------
    # Public entry point: called by every write path
    # ------------------------------------------------------------------
    def check(self, proposed_delta_bytes: int = 0) -> None:
        """Verify that the proposed write is permitted. Raises
        CapExceededError if not."""
        # Fast path 1: locally hinted as paid AND we have a fresh cache
        # that agrees → allow without network.
        cached = self._cache.load()
        if cached and cached.is_fresh and cached.account_id == self.account_id:
            if cached.cap_bytes is None:
                # Cached as paid (uncapped) within grace window: allow
                return
            # Cached as free with a cap. Enforce locally.
            new_size = self._db_size_fn() + proposed_delta_bytes
            if new_size <= cached.cap_bytes:
                return
            # Over the cached cap. Try to refresh (user may have upgraded).
            return self._refresh_and_check(proposed_delta_bytes)

        # No fresh cache. Use the credentials.json hint as a fast path
        # for the "obviously under cap" case to avoid a server call for
        # every brand-new user's first writes.
        current = self._db_size_fn()
        new_size = current + proposed_delta_bytes
        if self._local_hint in PAID_TIERS:
            # Credentials say paid; verify with server then cache the result.
            # If user genuinely paid: server confirms, we cache, done. If
            # credentials are tampered: server says free, we cache, enforce.
            return self._refresh_and_check(proposed_delta_bytes)
        if new_size <= self._cap:
            # Free + under cap. Trust the credentials hint, no server call.
            return
        # Free + at/past cap → must call server.
        return self._refresh_and_check(proposed_delta_bytes)

    # ------------------------------------------------------------------
    # Network refresh
    # ------------------------------------------------------------------
    def _refresh_and_check(self, proposed_delta_bytes: int) -> None:
        if not self.account_id or not self.session_token:
            # Pre-activation user trying to write past the cap. They never
            # had a binding; we can't verify a tier they don't have.
            current = self._db_size_fn()
            new_size = current + proposed_delta_bytes
            if new_size <= self._cap:
                return
            raise CapExceededError(
                "You're at the 2 MB free-tier cap and your account isn't "
                "activated. Run `sibyl init` to activate, or stay under "
                "the cap.",
                current_size=current,
                cap=self._cap,
                proposed_delta=proposed_delta_bytes,
            )

        current = self._db_size_fn()
        payload = {
            "account_id": self.account_id,
            "session_token": self.session_token,
            "current_size_bytes": current,
            "proposed_delta_bytes": proposed_delta_bytes,
        }
        # Attach signed-credentials claim if we have one. Server uses it for
        # tamper telemetry; the decision itself is unaffected.
        if self._credentials_signature and self._credentials_claim:
            payload["credentials_signature"] = self._credentials_signature
            payload["credentials_claim"] = self._credentials_claim

        try:
            resp = self._check_fn(self._check_url, payload)
        except TierVerificationError:
            # Offline. Fall back to the most recent cache if we have one,
            # even if technically expired (within an extended grace window
            # of double the normal period: i.e., 14 days for tier=free).
            #
            # T1-4 fix: respect server-supplied subscription expiry on the
            # offline path. The cache can no longer be honored past the
            # actual subscription end-of-validity even if the user
            # blackholes /api/plugin/check-write. Subscription expiry is
            # authoritative, not a refresh-able grace window.
            cached = self._cache.load()
            if cached and cached.account_id == self.account_id:
                now = time.time()
                if cached.server_expires_at is not None and now >= cached.server_expires_at:
                    raise  # subscription already expired per server's own record
                age = now - cached.checked_at
                if age < 2 * GRACE_PERIOD_SECONDS:
                    # Honor the cached result a bit longer for honest
                    # offline users.
                    if cached.cap_bytes is None:
                        return
                    new_size = current + proposed_delta_bytes
                    if new_size <= cached.cap_bytes:
                        return
            raise  # re-raise TierVerificationError; SDK will surface to caller

        # Got a response. Update the cache.
        ok = bool(resp.get("ok"))
        tier = resp.get("tier", "free")
        cap_bytes = resp.get("cap_bytes") if "cap_bytes" in resp else (
            None if tier in PAID_TIERS else self._cap
        )
        # T1-4 anchor: capture server-supplied subscription expiry so the
        # cache cannot be honored past actual end-of-validity, even if the
        # user blackholes the network. Server returns ISO string; parse if
        # present.
        server_expires_at: float | None = None
        raw_exp = resp.get("expires_at")
        if raw_exp:
            try:
                from datetime import datetime, timezone
                server_expires_at = datetime.fromisoformat(
                    raw_exp.replace("Z", "+00:00")
                ).astimezone(timezone.utc).timestamp()
            except (ValueError, TypeError):
                server_expires_at = None
        entry = TierCacheEntry(
            account_id=self.account_id,
            tier=tier,
            checked_at=time.time(),
            cap_bytes=cap_bytes,
            last_known_size=current,
            server_expires_at=server_expires_at,
            # Cache token = the credentials signature we hold (defense-in-depth
            # link between cache and credentials.json identity). Server can
            # cross-check on next /check-write call.
            cache_token=self._credentials_signature,
        )
        self._cache.store(entry)

        if ok:
            return  # server permitted the write
        # Server rejected: typically free tier over cap.
        raise CapExceededError(
            f"Your {tier} tier doesn't permit this write. "
            f"Current memory size: {current / 1024:.1f} KB. "
            f"Cap: {(cap_bytes or self._cap) / 1024:.1f} KB.",
            current_size=current,
            cap=cap_bytes or self._cap,
            proposed_delta=proposed_delta_bytes,
            upgrade_url=resp.get("upgrade_url", DEFAULT_UPGRADE_URL),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def invalidate_cache(self) -> None:
        """Forget any cached tier result. Next write at the cap will refetch."""
        self._cache.clear()

    def current_cap(self) -> int | None:
        """Return the current effective cap. None = uncapped."""
        cached = self._cache.load()
        if cached and cached.is_fresh:
            return cached.cap_bytes
        if self._local_hint in PAID_TIERS:
            return None
        return self._cap
