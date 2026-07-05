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
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# v0.4.0: CapExceededError + TierVerificationError live in exceptions.py
# (canonical exception module). _capcheck imports them back so existing
# callers that import from `sibyl_memory_client._capcheck` still resolve.
# The MCP server (sibyl-memory-mcp >= 0.1.2) imports from the canonical
# `.exceptions` path; this re-export keeps the historical path alive too.
from .exceptions import (  # noqa: F401  (re-exported for backwards compat)
    CapExceededError,
    SibylMemoryError,
    TierAuthError,
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

# Bounded retry for transient verification failures (v0.4.14). Under sustained
# free-tier write volume the server can return a rate-limit-shaped 429; a couple
# of short backoff retries clears the transient case before we treat the call as
# unreachable. Kept small so worst-case added latency stays ~1.2s.
#
# CAP-5 / CORE-2 (2026-06-25 pre-launch audit): 401 and 403 are NO LONGER
# retryable and must NEVER route into the fail-open path. A 401/403 is the
# server's authoritative "you are not entitled" (bad/expired/forged token, or
# tier revoked) — retrying then failing open would let a forged token write past
# the cap. _refresh_and_check now treats any TierAuthError (raised on 401/403)
# as a hard denial: enforce the free cap, never fail open. Genuine rate limiting
# must be a 429 (still retryable), not a 401.
RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Authoritative "not entitled" codes: hard-deny, never fail-open.
AUTH_DENY_HTTP_CODES = frozenset({401, 403})
CHECK_WRITE_MAX_RETRIES = 2
CHECK_WRITE_RETRY_BACKOFF = 0.4  # seconds, exponential: 0.4, 0.8

# Fail-open safety ceiling (v0.4.14). When tier verification is unreachable and
# there is no usable cache, the write is allowed to avoid silent data loss
# (durability > cap enforcement during an outage; the server reconciles on the
# next reachable check). This is bounded: a permanently offline free user can
# still only grow to FAIL_OPEN_CEILING_MULT x the cap before hard-blocking, so
# the concession can't be abused indefinitely.
FAIL_OPEN_CEILING_MULT = 4


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
# Account-level size aggregation (v0.4.18)
# ----------------------------------------------------------------------

def aggregate_db_size(primary_db: str | Path) -> int:
    """Total WAL-inclusive bytes across every memory.db an agent on this
    machine resolves.

    The FREE-tier cap is per ACCOUNT, not per DB file. Sizing only the store
    being written to lets N stores yield N x 2 MB on one free account
    (Discord report 2026-06-11: 6.29 MB across 9 stores). This walks every
    store an agent on this machine can resolve and sums them:

      - ``primary_db`` (the store the current client is writing to)
      - ``~/.sibyl-memory/memory.db`` (SDK default location)
      - ``$HERMES_HOME/sibyl/memory.db`` (Hermes adapter; ``HERMES_HOME``
        defaults to ``~/.hermes``)
      - ``$HERMES_HOME/sibyl/profiles/<p>/memory.db`` for each profile dir
      - ``$SIBYL_MEMORY_DB`` override, when set

    Candidates are deduped by resolved path. Missing or unreadable
    candidates contribute 0; this function never raises.

    COMPOSITION WITH CAP-1 (0.4.15): each existing candidate is sized with
    ``storage.db_size_bytes`` — the SQLite *logical* size (``page_count x
    page_size``, falling back to main + -wal + -shm file bytes) — NOT a
    plain ``st_size``. Committed data still living in a store's -wal
    journal therefore counts toward the account footprint. Summing raw
    ``st_size`` per file here would silently regress CAP-1 for every store
    in the walk.
    """
    # Local import: keeps the storage<->_capcheck edge lazy so there is no
    # circular-import risk at module load time.
    from .storage import db_size_bytes

    candidates: list[Path] = [
        Path(primary_db).expanduser(),
        Path.home() / ".sibyl-memory" / "memory.db",
    ]
    if os.environ.get("HERMES_HOME"):
        hermes_home = Path(os.environ["HERMES_HOME"]).expanduser()
    else:
        hermes_home = Path.home() / ".hermes"
    candidates.append(hermes_home / "sibyl" / "memory.db")
    profiles_dir = hermes_home / "sibyl" / "profiles"
    try:
        if profiles_dir.is_dir():
            for prof in sorted(profiles_dir.iterdir()):
                candidates.append(prof / "memory.db")
    except OSError:
        pass
    if os.environ.get("SIBYL_MEMORY_DB"):
        candidates.append(Path(os.environ["SIBYL_MEMORY_DB"]).expanduser())

    seen: set[str] = set()
    total = 0
    for path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if path.is_file():
                # WAL-inclusive per-store size (CAP-1), never plain st_size.
                total += db_size_bytes(path)
        except OSError:
            continue
    return total


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
    # v0.4.14: bounded retry on transient verification failures. Under sustained
    # free-tier write volume the server can return a rate-limit-shaped 401/429
    # (the silent-write-loss path reported in beta). A couple of short backoff
    # retries clears the transient case before the caller treats verification as
    # unreachable. A clean non-retryable HTTP error (e.g. 400/403/404) still
    # raises immediately so we don't add latency to genuine failures.
    for attempt in range(CHECK_WRITE_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # CAP-5 / CORE-2: 401/403 are authoritative "not entitled". Do not
            # retry and do not let them reach the generic TierVerificationError
            # branch (which is fail-open eligible). Raise the distinct
            # TierAuthError so _refresh_and_check hard-denies at the free cap.
            if e.code in AUTH_DENY_HTTP_CODES:
                raise TierAuthError(
                    f"Sibyl Labs refused to authorize this account "
                    f"(HTTP {e.code}). Re-activate to continue.",
                ) from e
            if e.code in RETRYABLE_HTTP_CODES and attempt < CHECK_WRITE_MAX_RETRIES:
                time.sleep(CHECK_WRITE_RETRY_BACKOFF * (2 ** attempt))
                continue
            # T2-3 fix: do NOT synthesize a fake "free tier" decision on HTTP
            # error. Previously a transient 502 would write `{tier:free,
            # cap_bytes:2MB}` into the cache for a legitimately paid user,
            # locking them out for up to 7 days. Now we raise
            # TierVerificationError: the caller (_refresh_and_check) falls back
            # to a recent cache if one exists, or fails open for no-cache writes.
            # SEC-9 (v0.3.3): do not echo the server-side `error` string (verbose
            # internal detail / potential PII) into user logs.
            raise TierVerificationError(
                f"Sibyl Labs returned HTTP {e.code} while verifying your account. "
                f"Retry shortly.",
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < CHECK_WRITE_MAX_RETRIES:
                time.sleep(CHECK_WRITE_RETRY_BACKOFF * (2 ** attempt))
                continue
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
                # Cached as paid (uncapped) within grace window: allow — but
                # ONLY for a real account. A free/pre-activation user has
                # account_id=None; a forged tier_cache.json with
                # account_id:null + cap_bytes:null matches that null state and
                # would otherwise spoof an uncapped account (SEC-13). A
                # legitimately uncapped tier always carries a real account_id.
                if self.account_id is not None:
                    return
                # Forged/null-account uncapped claim: distrust the cache and
                # fall through to the credentials-hint + server path below.
            else:
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

    def check_total(self, total_size_bytes: int) -> None:
        """Gate on an ABSOLUTE resulting footprint (CAP-2).

        ``check()`` gates on ``db_size_fn() + proposed_delta`` *before* the
        write, so it can never see the true post-write size (WAL lag + estimate
        error). This entry point takes the actual resulting total — measured by
        the caller immediately before COMMIT, inside the same transaction (via
        SQLite ``page_count * page_size``, which already reflects the pending
        INSERT) — and decides allow/deny against the effective cap.

        It reuses the same trust ladder as ``check()``: a fresh account-matched
        paid cache short-circuits to allow; an over-cap total triggers the
        server refresh path (which hard-denies on auth failure and applies the
        CAP-4 fail-open rules). The only difference is the size is the real total
        rather than current + estimate, expressed as a zero-delta refresh so all
        the size comparisons in _refresh_and_check operate on it directly.
        """
        cached = self._cache.load()
        if cached and cached.is_fresh and cached.account_id == self.account_id:
            if cached.cap_bytes is None:
                if self.account_id is not None:
                    return  # account-matched paid cache: uncapped
            elif total_size_bytes <= cached.cap_bytes:
                return
            else:
                return self._refresh_and_check_total(total_size_bytes)

        if self._local_hint in PAID_TIERS:
            return self._refresh_and_check_total(total_size_bytes)
        if total_size_bytes <= self._cap:
            return
        return self._refresh_and_check_total(total_size_bytes)

    def _effective_cap_local(self) -> int | None:
        """The cap to enforce WITHOUT any network call. None = uncapped.

        Used by the in-transaction recheck (check_total_local), which runs under
        the BEGIN IMMEDIATE write lock where a urlopen would starve concurrent
        writers (2026-06-25 audit blocker). The authoritative tier/cap decision
        was already made by the pre-write check() BEFORE the transaction (and it
        populated the cache); here we only read that result locally:
          - a real account's paid grant in the cache (cap_bytes is None) -> uncapped
          - a fresh cached free cap -> that cap
          - otherwise -> the free default cap
        The bare credentials paid-hint is NOT trusted here (check() already
        server-verified it); a null-account uncapped cache is distrusted (SEC-13).
        """
        cached = self._cache.load()
        if (cached and cached.account_id == self.account_id
                and self.account_id is not None):
            if cached.cap_bytes is None:
                return None              # account-matched paid grant -> uncapped
            if cached.is_fresh:
                return cached.cap_bytes  # fresh cached free cap
        return self._cap                 # free default

    def check_total_local(self, total_size_bytes: int) -> None:
        """Local-only absolute-footprint gate for the in-transaction recheck.

        MUST NOT make a network call — it runs inside the BEGIN IMMEDIATE write
        lock. Enforces the true post-stage footprint against the cap that the
        pre-write check() already established (via _effective_cap_local). Raises
        CapExceededError if over, so the surrounding transaction rolls back.
        """
        cap = self._effective_cap_local()
        if cap is None or total_size_bytes <= cap:
            return
        raise CapExceededError(
            f"This write would bring stored memory to "
            f"{total_size_bytes / 1024:.1f} KB, over the "
            f"{cap / 1024:.1f} KB free-tier cap.",
            current_size=total_size_bytes,
            cap=cap,
            proposed_delta=0,
        )

    def _refresh_and_check_total(self, total_size_bytes: int) -> None:
        """Server-authoritative recheck of an absolute footprint (CAP-2).

        WARNING: this may perform a network call — do NOT call it while holding
        a SQLite write lock. The in-transaction recheck uses check_total_local()
        instead. Retained for any out-of-transaction absolute-footprint check.

        Temporarily overrides db_size_fn so _refresh_and_check evaluates the
        provided absolute total (delta 0). This keeps the auth-deny / fail-open /
        cache logic single-sourced rather than duplicated.
        """
        prev = self._db_size_fn
        self._db_size_fn = lambda: total_size_bytes
        try:
            self._refresh_and_check(0)
        finally:
            self._db_size_fn = prev

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
        except TierAuthError as e:
            # CAP-5 / CORE-2: authoritative "not entitled" (401/403). NEVER fail
            # open: the token is bad/expired/forged/revoked, so the account is
            # treated as free and the free cap is enforced. A recent PAID cache
            # is not honored here — a 401 means the server is actively refusing,
            # which supersedes a stale grant. The over-cap state is surfaced to
            # the caller as a raised CapExceededError, not just a log line.
            new_size = current + proposed_delta_bytes
            if new_size <= self._cap:
                return
            raise CapExceededError(
                "Your account could not be authorized and you're past the "
                "2 MB free-tier cap. Re-run `sibyl init` to refresh "
                "credentials, or stay under the cap.",
                current_size=current,
                cap=self._cap,
                proposed_delta=proposed_delta_bytes,
                upgrade_url=DEFAULT_UPGRADE_URL,
            ) from e
        except TierVerificationError as e:
            # Verification unreachable (timeout / connection error / 5xx — NOT an
            # auth refusal; those are TierAuthError, handled above). First, honor
            # a recent cache if we have one (within an extended 2x grace window),
            # respecting any server-supplied subscription expiry.
            #
            # T1-4 fix: respect server-supplied subscription expiry on the
            # offline path. The cache can no longer be honored past the
            # actual subscription end-of-validity even if the user
            # blackholes /api/plugin/check-write. Subscription expiry is
            # authoritative, not a refresh-able grace window.
            cached = self._cache.load()
            had_paid_grant = False
            if cached and cached.account_id == self.account_id:
                now = time.time()
                if cached.server_expires_at is not None and now >= cached.server_expires_at:
                    raise  # subscription already expired per server's own record
                # CAP-4: a paid grant in the cache (cap_bytes is None) is the
                # evidence that gates the bounded fail-open concession below.
                had_paid_grant = cached.cap_bytes is None
                age = now - cached.checked_at
                if age < 2 * GRACE_PERIOD_SECONDS:
                    # Honor the cached result a bit longer for honest
                    # offline users.
                    if cached.cap_bytes is None:
                        return
                    new_size = current + proposed_delta_bytes
                    if new_size <= cached.cap_bytes:
                        return

            # CAP-4 + CORE-1: fail-open is for HONEST PAID users riding out an
            # extended outage — NOT for free / never-paid accounts. A user who
            # never had a verified paid grant (no cache, or a cache that says
            # free) must fail CLOSED at the free cap; the old code let ANY
            # account, including a blackholed free user with no cache, grow to
            # 4x the cap. The size read here is WAL-inclusive (db_size_fn now
            # sums the -wal/-shm sidecars per CAP-1), so this is the true
            # cumulative footprint, not a per-write delta.
            new_size = current + proposed_delta_bytes
            if not had_paid_grant:
                # Free / no-grant account, unreachable verification. Enforce the
                # free cap and surface the over-cap state to the caller.
                if new_size <= self._cap:
                    return
                raise CapExceededError(
                    "You're past the 2 MB free-tier cap and Sibyl Labs can't be "
                    "reached to verify a paid tier. Reconnect to continue, or "
                    "upgrade.",
                    current_size=current,
                    cap=self._cap,
                    proposed_delta=proposed_delta_bytes,
                    upgrade_url=DEFAULT_UPGRADE_URL,
                ) from e

            # v0.4.14 FAIL-OPEN (paid-grant evidenced only): verification is
            # unreachable but the cache shows this account HELD a paid grant.
            # Allow continued writes to preserve durability during the outage;
            # the server reconciles tier/cap on the next reachable check.
            # Bounded by a safety ceiling so even a paid grant gone permanently
            # offline can't grow without limit.
            ceiling = self._cap * FAIL_OPEN_CEILING_MULT
            if new_size <= ceiling:
                logger.warning(
                    "Sibyl tier verification unreachable (%s); allowing write for "
                    "a previously-paid account to avoid data loss "
                    "(size=%.1fKB, reconciles when reachable).",
                    type(e).__name__, new_size / 1024,
                )
                return
            # Past the fail-open ceiling: hard-block to bound the concession.
            raise CapExceededError(
                "Memory is past the offline safety ceiling and Sibyl Labs can't "
                "be reached to verify your tier. Reconnect to continue, or upgrade.",
                current_size=current,
                cap=ceiling,
                proposed_delta=proposed_delta_bytes,
                upgrade_url=DEFAULT_UPGRADE_URL,
            ) from e

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
        """Return the current effective cap. None = uncapped.

        CAP-6 (2026-06-25 pre-launch audit): the cached entry is only trusted
        when its account_id matches this gate's account_id, mirroring the guard
        in check(). Without it, a tier_cache.json belonging to (or forged for) a
        different account — including a null-account forged uncapped entry —
        could be read as this account's cap, reporting uncapped for a free user.
        """
        cached = self._cache.load()
        if (cached and cached.is_fresh
                and cached.account_id == self.account_id):
            # SEC-13: never honor a null-account "uncapped" entry
            # (cap_bytes=None, account_id=None) for a free/unactivated user. A
            # genuine uncapped tier always carries a real account_id, so this
            # would let a forged tier_cache.json spoof "uncapped" in status.
            # Mirrors the guard in check() / check_total().
            if not (cached.cap_bytes is None and self.account_id is None):
                return cached.cap_bytes
        if self._local_hint in PAID_TIERS:
            return None
        return self._cap
