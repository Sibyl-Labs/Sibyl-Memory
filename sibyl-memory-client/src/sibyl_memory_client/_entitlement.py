"""Account gate: server-signed entitlement ("SE1") verification and state.

Spec: account-gated-plugin-spec-2026-09-27 sections 2 and 3.1. This module is
the ONE enforcement point for the plugin family: ``MemoryClient._track_op``
calls ``EntitlementGate.require(kind)`` before every public memory operation.

Token (ASCII, at most 4096 bytes)::

    SE1.<base64url(payload JSON, no padding)>.<base64url(64-byte Ed25519 sig)>

The signature covers the exact ASCII bytes of ``SE1.<payload_b64>``. It is
verified against the PINNED public keys in ``_entitlement_keys.TRUSTED_KEYS``
BEFORE the payload JSON is parsed; nothing in the payload is read until a
pinned key has vouched for it.

States (evaluated on every memory operation, cheap: two stats and arithmetic):

  no_account  no usable credentials.json (missing, symlinked, unparsable, or
              without account_id + session_token)            -> refused
  bootstrap   credentials present, no entitlement yet       -> one blocking
              refresh; a verified token -> fresh; anything else -> locked
              (setup_incomplete, or the server's denial). No grace: see
              BOOTSTRAP_GRACE_SECONDS.
  fresh       verified, bound, server_now <= exp            -> allowed
  stale       verified, bound, exp < server_now <= exp + og -> allowed + notice
  locked      past exp + og, authoritative server denial (401/403/404),
              invalid signature, wrong device, mem == "none" -> refused

Nothing here ever reads, moves or deletes local memory. The ONLY environment
variable this module reads is SIBYL_API_BASE (a source-guard test enforces
it); there is no switch that disables the gate. Tests inject a
``NullEntitlementGate`` (``sibyl_memory_client.testing``) at construction, or
mint test tokens against a monkeypatched ``TRUSTED_KEYS``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import platform
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import tiers as _tiers
from ._entitlement_keys import TRUSTED_KEYS  # noqa: F401  (monkeypatch point)
from .exceptions import (
    ENTITLEMENT_MESSAGES,
    SIGNED_OUT_ON_CLAIM,
    SIGNED_OUT_ON_CLAIM_NEXT,
    EntitlementError,
    clean_iso,
    signed_out_on_claim_message,
)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

PREFIX = "SE1"
MAX_TOKEN_BYTES = 4096
# entitlement.json wraps a token of at most MAX_TOKEN_BYTES plus a handful of
# numeric fields; nothing legitimate is larger than this. The file is read
# with a bounded read, so a 5 MB file is rejected after MAX_STATE_BYTES + 1.
MAX_STATE_BYTES = 8192
MAX_CREDENTIALS_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
NBF_TOLERANCE_SECONDS = 300
# D10 changed to 0 (0.9.0 gate fix, a documented deviation from the spec's
# 7 days): the old grace was computed from a first-sight time kept in
# entitlement.json, so fabricated credentials with the api blocked got 7 days,
# and deleting the file restarted them. Legacy credentials now need ONE
# verified token from a successful online /entitlement refresh before memory
# tools work; there is no locally computed grace of any kind before that.
BOOTSTRAP_GRACE_SECONDS = 0
# The largest local-clock lead over the server that a skew READ FROM THE FILE
# may express. The file's skew (or received_at, when it has no skew) is a local
# value; unclamped, an edited skew, or a file holding only a token (skew then
# derived from "now" on every evaluation), moved server_now back without bound
# and kept any once-valid token fresh forever. A skew this process computed
# itself from a successful refresh is trusted as is (kept in memory, never
# re-read from the file). A clock genuinely ahead by more than this, in a new
# process while offline, sees its token expire early by the excess; online, the
# refresh that follows restores the exact skew.
MAX_SKEW_AHEAD_SECONDS = 3600
DENIED_RETRY_SECONDS = 15 * 60        # after an authoritative denial (2.7)
STALE_RETRY_SECONDS = 3600            # stale band: at most hourly (2.1)
FRESH_RETRY_SECONDS = 3600            # a failed background refresh in fresh
LOCKED_RETRY_SECONDS = 60             # blocking recovery attempts, throttled
HWM_WRITE_INTERVAL_SECONDS = 60
LOCK_STALE_SECONDS = 60
LOCK_WAIT_SECONDS = 6.0
HTTP_TIMEOUT_SECONDS = 4.0
MAX_RETRY_AFTER_SECONDS = 86400
DEFAULT_API_BASE = "https://api.sibyllabs.org"
ENTITLEMENT_PATH = "/api/plugin/entitlement"

STATE_FILENAME = "entitlement.json"
LOCK_FILENAME = "entitlement.lock"

# Operations that change stored memory. Only these are refused under
# mem == "read_only" (policy, spec 2.2); reads keep working.
WRITE_OPS = frozenset({
    "set_entity", "set_state", "write_event", "set_reference", "delete_entity",
    "archive_entity", "learn", "accept_skill_proposal", "reject_skill_proposal",
})

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PRINTABLE_ASCII_RE = re.compile(rb"^[\x21-\x7e]+$")

_REQUIRED_STR = ("kid", "sub", "ten", "dh", "tier", "mem", "iss")
_REQUIRED_INT = ("iat", "nbf", "exp", "og", "ra")


# ----------------------------------------------------------------------
# Token format
# ----------------------------------------------------------------------

class EntitlementFormatError(Exception):
    """A token that is not a valid entitlement. ``reason`` never carries token
    material, so it is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EntitlementVersionError(EntitlementFormatError):
    """Signature valid but ``v`` is not 1: not an entitlement this client
    understands. Treated as ABSENT (bootstrap), never as valid (9.7)."""


@dataclass(frozen=True)
class Entitlement:
    """A verified SE1 payload plus the local receipt bookkeeping."""

    v: int
    kid: str
    sub: str
    ten: str
    dh: str
    tier: str
    cap: int | None
    src: Any
    iat: int
    nbf: int
    exp: int
    og: int
    ra: int
    mem: str
    acct: dict[str, Any]
    iss: str
    token: str = field(repr=False)
    received_at: float = 0.0
    skew: float = 0.0


def _b64url_decode(segment: str) -> bytes:
    if not _B64URL_RE.match(segment) or len(segment) % 4 == 1:
        raise EntitlementFormatError("encoding")
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        raise EntitlementFormatError("encoding") from None


_PUBKEY_CACHE: dict[bytes, Ed25519PublicKey] = {}


def _public_key(raw: bytes) -> Ed25519PublicKey | None:
    key = _PUBKEY_CACHE.get(raw)
    if key is None:
        try:
            key = Ed25519PublicKey.from_public_bytes(bytes(raw))
        except (ValueError, TypeError):
            return None
        _PUBKEY_CACHE[bytes(raw)] = key
    return key


def kid_of(raw_public_key: bytes) -> str:
    """kid = first 16 hex chars of sha256(raw 32-byte public key) (spec 2.4)."""
    return hashlib.sha256(bytes(raw_public_key)).hexdigest()[:16]


def device_hash(session_token: str) -> str:
    """dh = sha256 hex of the canonical (trimmed, lowercased) session token.

    Matches the api's ``plugin-db.sessionRef`` and ``bearer_tokens.hashed_bearer``.
    """
    return hashlib.sha256(session_token.strip().lower().encode("utf-8")).hexdigest()


def _trusted() -> Mapping[str, bytes]:
    # Read the module global at call time so a monkeypatched TRUSTED_KEYS
    # (the test suites' fixture) takes effect without re-import.
    return globals()["TRUSTED_KEYS"]


def parse_and_verify(
    token: Any,
    trusted: Mapping[str, bytes] | None = None,
    *,
    received_at: float = 0.0,
    skew: float = 0.0,
) -> Entitlement:
    """Verify then parse a compact SE1 token.

    Order (spec 2.3): size, ASCII, prefix + segments, signature over the exact
    ASCII bytes of ``SE1.<payload_b64>`` against each pinned key (at most two
    during a rotation), THEN ``json.loads``. The payload's ``kid`` must name
    the key that verified. Raises ``EntitlementFormatError`` (``reason`` is
    safe to log; it never contains the token).
    """
    if not isinstance(token, str):
        raise EntitlementFormatError("not_string")
    if len(token) > MAX_TOKEN_BYTES:
        raise EntitlementFormatError("oversize")
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        raise EntitlementFormatError("not_ascii") from None
    if len(raw) > MAX_TOKEN_BYTES:
        raise EntitlementFormatError("oversize")
    if not _PRINTABLE_ASCII_RE.match(raw):
        raise EntitlementFormatError("not_ascii")
    parts = token.split(".")
    if len(parts) != 3:
        raise EntitlementFormatError("segments")
    if parts[0] != PREFIX:
        raise EntitlementFormatError("prefix")
    payload_seg, sig_seg = parts[1], parts[2]
    sig = _b64url_decode(sig_seg)
    if len(sig) != 64:
        raise EntitlementFormatError("signature_length")
    payload_bytes = _b64url_decode(payload_seg)
    msg = f"{PREFIX}.{payload_seg}".encode("ascii")
    keys = _trusted() if trusted is None else trusted
    matched: str | None = None
    for kid, raw_key in keys.items():
        pub = _public_key(raw_key)
        if pub is None:
            continue
        try:
            pub.verify(sig, msg)
        except InvalidSignature:
            continue
        matched = kid
        break
    if matched is None:
        raise EntitlementFormatError("signature")
    # Only now, with a pinned key vouching for these bytes, is the JSON parsed.
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EntitlementFormatError("payload_json") from None
    if not isinstance(payload, dict):
        raise EntitlementFormatError("payload_shape")
    if payload.get("v") != 1 or isinstance(payload.get("v"), bool):
        raise EntitlementVersionError("version")
    if payload.get("kid") != matched:
        raise EntitlementFormatError("kid_mismatch")
    for name in _REQUIRED_STR:
        if not isinstance(payload.get(name), str) or not payload.get(name):
            raise EntitlementFormatError("payload_fields")
    for name in _REQUIRED_INT:
        val = payload.get(name)
        if not isinstance(val, int) or isinstance(val, bool):
            raise EntitlementFormatError("payload_fields")
    if payload["og"] < 0 or payload["ra"] < 0 or payload["exp"] < payload["iat"]:
        raise EntitlementFormatError("payload_fields")
    cap = payload.get("cap", "missing")
    if cap == "missing" or not (cap is None or (isinstance(cap, int) and not isinstance(cap, bool))):
        raise EntitlementFormatError("payload_fields")
    if "src" not in payload:
        raise EntitlementFormatError("payload_fields")
    acct = payload.get("acct")
    if not isinstance(acct, dict):
        raise EntitlementFormatError("payload_fields")
    return Entitlement(
        v=1, kid=payload["kid"], sub=payload["sub"], ten=payload["ten"],
        dh=payload["dh"].lower(), tier=payload["tier"], cap=cap, src=payload["src"],
        iat=payload["iat"], nbf=payload["nbf"], exp=payload["exp"],
        og=payload["og"], ra=payload["ra"], mem=payload["mem"], acct=dict(acct),
        iss=payload["iss"], token=token, received_at=received_at, skew=skew,
    )


# ----------------------------------------------------------------------
# Credentials (identity) reading
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    account_id: str
    session_token: str = field(repr=False)
    tenant_id: str
    dh: str = field(repr=False)


def _bounded_read(path: Path, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes without following a symlink. None when the
    file is absent, a symlink, not a regular file, unreadable or over limit."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data


def read_identity(credentials_path: str | Path) -> Identity | None:
    """The account identity in credentials.json, or None (``no_account``)."""
    data = _bounded_read(Path(credentials_path), MAX_CREDENTIALS_BYTES)
    if data is None:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    account_id = raw.get("account_id")
    session_token = raw.get("session_token")
    if not isinstance(account_id, str) or not account_id.strip():
        return None
    if not isinstance(session_token, str) or not session_token.strip():
        return None
    tenant = raw.get("tenant_id")
    tenant_id = tenant if isinstance(tenant, str) and tenant else account_id
    return Identity(account_id=account_id, session_token=session_token,
                    tenant_id=tenant_id, dh=device_hash(session_token))


TIER_CACHE_FILENAME = "tier_cache.json"


def sync_credentials_tier(credentials_path: str | Path, ent: "Entitlement") -> bool:
    """Shown-tier fix (2026-10-08): after a verified, bound pass arrives, rewrite the
    saved ``tier`` in credentials.json to the pass's tier so every reader of
    the file (status, whoami, the adapters' hint) agrees with the pass that
    drives enforcement. Returns True when the file was rewritten.

    Only the canonical form of a tier this version recognises is written (an
    unrecognised label is treated as Free and is left for ``sibyl update``).
    Nothing is written when the saved tier already normalises to the same
    tier, or when the file is not the same sign-in (account, session) the
    pass is bound to, absent, a symlink, oversize or unparseable. ``tier`` is
    a signed field: ``signature`` and ``signed_at`` are dropped with the edit
    (the CLI's hardening L2 rule), so no stale signature ever sits beside a
    changed tier. Atomic: mkstemp in the same directory, the existing file's
    permission bits, fsync, os.replace. The tier cache beside it (a cached
    server answer about the old tier) is removed when the tier changed.
    Never raises."""
    try:
        new = _tiers.normalize(getattr(ent, "tier", None))
        if new is None:
            return False
        path = Path(credentials_path).expanduser()
        data = _bounded_read(path, MAX_CREDENTIALS_BYTES)
        if data is None:
            return False
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            return False
        tok = raw.get("session_token")
        if (raw.get("account_id") != ent.sub or not isinstance(tok, str) or not tok.strip()
                or device_hash(tok) != ent.dh):
            return False
        if _tiers.normalize(raw.get("tier")) == new:
            return False
        raw["tier"] = new
        raw.pop("signature", None)
        raw.pop("signed_at", None)
        mode = os.lstat(path).st_mode & 0o777
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            os.fchmod(fd, mode)
            os.write(fd, json.dumps(raw, indent=2).encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.close(fd)
        if path.is_symlink():  # swapped for a link while we wrote: never write through it
            os.unlink(tmp)
            return False
        os.replace(tmp, str(path))
        cache = path.parent / TIER_CACHE_FILENAME
        try:
            if cache.is_symlink() or cache.is_file():
                cache.unlink()
        except OSError:
            pass
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# State file (entitlement.json) + cross-process lock
# ----------------------------------------------------------------------

# ``first_seen_legacy`` (the pre-fix bootstrap grace anchor) is no longer
# read or kept: an old file's value is dropped on the next write.
_STATE_NUM_FIELDS = ("received_at", "skew", "hwm", "last_attempt", "retry_after_until")


def _clean_state(raw: Any) -> dict[str, Any]:
    """Keep only well-typed fields; anything else is dropped, never trusted."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    tok = raw.get("token")
    if isinstance(tok, str) and len(tok) <= MAX_TOKEN_BYTES:
        out["token"] = tok
    for name in _STATE_NUM_FIELDS:
        v = raw.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[name] = float(v)
    den = raw.get("denied")
    if (isinstance(den, dict) and isinstance(den.get("code"), str)
            and den.get("code") in ENTITLEMENT_MESSAGES
            and isinstance(den.get("at"), (int, float)) and not isinstance(den.get("at"), bool)):
        out["denied"] = {"code": den["code"], "at": float(den["at"])}
        # F-1: why the api signed this computer out, when it said.
        if den.get("cause") == SIGNED_OUT_ON_CLAIM:
            out["denied"]["cause"] = SIGNED_OUT_ON_CLAIM
            since = clean_iso(den.get("since"))
            if since is not None:
                out["denied"]["since"] = since
    for name in ("last_refresh_error", "attempt_nonce"):
        v = raw.get(name)
        if isinstance(v, str) and len(v) <= 64:
            out[name] = v
    return out


class EntitlementStore:
    """``entitlement.json`` beside the credentials file (spec 3.1).

    Mode 0600 (mkstemp), symlink-refusing load and store, bounded read,
    mkstemp + os.replace write (the TierCache.store pattern), plus an O_EXCL
    ``entitlement.lock`` (stale after LOCK_STALE_SECONDS) so several processes
    (MCP + hermes) never refresh at once.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(LOCK_FILENAME)

    def load(self) -> dict[str, Any]:
        data = _bounded_read(self.path, MAX_STATE_BYTES)
        if data is None:
            return {}
        try:
            return _clean_state(json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            return {}

    def mtime_key(self) -> tuple | None:
        try:
            st = os.lstat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def store(self, state: dict[str, Any]) -> bool:
        """Atomic write at 0600. Refuses a symlinked destination. Returns False
        (never raises) when the write is refused or fails."""
        try:
            if self.path.is_symlink():
                return False
            parent = self.path.parent
            if not parent.is_dir():
                return False
        except OSError:
            return False
        data = json.dumps(_clean_state(state), sort_keys=True).encode("utf-8")
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent),
                                            prefix=self.path.name + ".", suffix=".tmp")
        except OSError:
            return False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, str(self.path))
            return True
        except BaseException as exc:
            # Never leave a stray temp file behind (spec 9.1 item 11).
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if not isinstance(exc, Exception):
                raise
            return False

    def delete(self) -> bool:
        try:
            if self.path.is_symlink() or self.path.exists():
                self.path.unlink()
                return True
        except OSError:
            pass
        return False

    # -- lock ------------------------------------------------------------
    def try_lock(self) -> bool:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.lock_path), flags, 0o600)
        except FileExistsError:
            try:
                age = time.time() - os.lstat(self.lock_path).st_mtime
            except OSError:
                return False
            if age > LOCK_STALE_SECONDS:
                # A crashed holder. Steal it (unlink, then race for O_EXCL again).
                try:
                    os.unlink(self.lock_path)
                except OSError:
                    return False
                try:
                    fd = os.open(str(self.lock_path), flags, 0o600)
                except OSError:
                    return False
            else:
                return False
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        finally:
            os.close(fd)
        return True

    def acquire_lock(self, wait_seconds: float = LOCK_WAIT_SECONDS) -> bool:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            if self.try_lock():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def release_lock(self) -> None:
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Refresh transport
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class RefreshOutcome:
    """What one refresh attempt produced.

    status: "ok" (``token`` set; still verified before use), "denied"
    (authoritative: ``code`` is an EntitlementError code), "transient"
    (network, 5xx, 429, not_open, old api), or "shared" (another thread or
    process refreshed while we waited for the lock; nothing was sent).
    """

    status: str
    token: str | None = field(default=None, repr=False)
    code: str | None = None
    retry_after: float | None = None
    # F-1: the api's own 401 ``code`` when it names the cause
    # (``signed_out_on_claim``) and its ``signed_out_at``. ``code`` stays
    # ``signed_out`` so every surface keyed on it keeps working.
    cause: str | None = None
    since: str | None = None


def _api_base() -> str:
    # The ONLY environment read in this module (source-guard test). Pointing it
    # elsewhere cannot mint a token: the verification key is pinned.
    return (os.environ.get("SIBYL_API_BASE") or DEFAULT_API_BASE).rstrip("/")


_BEARER_HOST = "sibyllabs.org"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _bearer_allowed(url: str) -> bool:
    """The session bearer goes only to https *.sibyllabs.org (or a loopback
    host for local development), never to an arbitrary SIBYL_API_BASE host
    (same rule as the heartbeat's Hardening #12)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    if host in _LOCAL_HOSTS and scheme in ("http", "https"):
        return True
    return scheme == "https" and (host == _BEARER_HOST or host.endswith("." + _BEARER_HOST))


def client_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("sibyl-memory-client")
        except PackageNotFoundError:
            return "0.0.0+source"
    except Exception:  # pragma: no cover
        return "0.0.0+source"


def os_family() -> str:
    try:
        name = platform.system().lower()
    except Exception:  # pragma: no cover
        return "unknown"
    return {"darwin": "macos"}.get(name, name or "unknown")


def _json_body(data: bytes | None) -> dict[str, Any]:
    if not data:
        return {}
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def classify_denial(status: int, body: Mapping[str, Any]) -> RefreshOutcome:
    """Map an HTTP error answer of POST /entitlement to an outcome (2.7).

    401 -> signed_out (with ``cause`` signed_out_on_claim and ``since`` when the
    body's ``code`` says the web email claim did it). 403 tamper (reason tamper_flag_active) -> under_review,
    403 client_too_old -> client_too_old, any other 403 (session/account
    mismatch) -> signed_out. 404 WITH the api's JSON body ``account not
    found`` -> account_deleted; a bare 404 is an api that predates the
    endpoint (rollback, 6.2) and is transient. 429/5xx/other -> transient.
    """
    code = body.get("code") if isinstance(body.get("code"), str) else None
    if status == 401:
        if code == SIGNED_OUT_ON_CLAIM:
            # F-1 (contract C1): signed out by the web email claim.
            return RefreshOutcome("denied", code="signed_out", cause=SIGNED_OUT_ON_CLAIM,
                                  since=clean_iso(body.get("signed_out_at")))
        return RefreshOutcome("denied", code="signed_out")
    if status == 403:
        if body.get("reason") == "tamper_flag_active":
            return RefreshOutcome("denied", code="under_review")
        if code == "client_too_old":
            return RefreshOutcome("denied", code="client_too_old")
        return RefreshOutcome("denied", code="signed_out")
    if status == 404:
        if body.get("error") == "account not found":
            return RefreshOutcome("denied", code="account_deleted")
        return RefreshOutcome("transient", code="endpoint_missing")
    return RefreshOutcome("transient", code=code or f"http_{status}")


def http_refresh(account_id: str, session_token: str) -> RefreshOutcome:
    """Default transport: POST {SIBYL_API_BASE}/api/plugin/entitlement.

    Body ``{account_id, client: {pkg, version, os_family}}``, bearer auth,
    certifi-backed TLS context, 4 s timeout, a single attempt (a transient
    failure is simply retried by the gate's throttle, never in a tight loop).
    """
    import urllib.error
    import urllib.request

    from ._trust import https_context

    url = _api_base() + ENTITLEMENT_PATH
    if not _bearer_allowed(url):
        return RefreshOutcome("transient", code="untrusted_api_base")
    ver = client_version()
    body = json.dumps({
        "account_id": account_id,
        "client": {"pkg": "sibyl-memory-client", "version": ver, "os_family": os_family()},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"sibyl-memory-client/{ver}",
        "Authorization": f"Bearer {session_token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS,
                                    context=https_context()) as resp:
            data = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raw = b""
        outcome = classify_denial(e.code, _json_body(raw[:MAX_RESPONSE_BYTES]))
        if e.code == 429 or outcome.status == "transient":
            ra = None
            try:
                hdr = e.headers.get("Retry-After") if e.headers else None
                if hdr is not None:
                    ra = min(float(int(str(hdr).strip())), float(MAX_RETRY_AFTER_SECONDS))
            except (TypeError, ValueError):
                ra = None
            return RefreshOutcome("transient", code=outcome.code, retry_after=ra)
        return outcome
    except Exception:
        return RefreshOutcome("transient", code="unreachable")
    if len(data) > MAX_RESPONSE_BYTES:
        return RefreshOutcome("transient", code="oversize_response")
    obj = _json_body(data)
    tok = obj.get("entitlement")
    if not isinstance(tok, str):
        return RefreshOutcome("transient", code="no_entitlement")
    return RefreshOutcome("ok", token=tok)


# ----------------------------------------------------------------------
# Decision + gate
# ----------------------------------------------------------------------

@dataclass
class Decision:
    state: str                      # no_account | bootstrap | fresh | stale | locked
    code: str | None = None         # EntitlementError code when refused
    notice: str | None = None
    entitlement: Entitlement | None = None
    server_now: float | None = None
    grace_left_seconds: float | None = None
    renews_in_seconds: float | None = None
    cause: str | None = None        # F-1: signed_out_on_claim, else None
    since: str | None = None        # the api's signed_out_at (ISO), when sent

    @property
    def allowed(self) -> bool:
        return self.state in ("bootstrap", "fresh", "stale")


def _days_ceil(seconds: float) -> int:
    return max(0, int(math.ceil(max(0.0, seconds) / 86400.0)))


def stale_notice(offline_days: int, days_left: int) -> str:
    return (f"Sibyl Memory has not reached the server for {offline_days} days. "
            f"It keeps working for {days_left} more days offline; connect to the "
            "internet once and it renews itself.")


CLOCK_NOTICE = ("Sibyl Memory found this computer's clock far behind the "
                "server's. Check the date and time settings; it renews itself "
                "once the clock is right.")

EMAIL_NAG_NOTICE = ("Your Sibyl Memory account has no confirmed email yet. Run "
                    "sibyl email add you@example.com so receipts and account "
                    "recovery can reach you.")


class EntitlementGate:
    """The account gate for one credentials location.

    Identity is read from ``credentials_path`` on every evaluation (cached on
    its mtime), so deleting credentials.json mid-session locks the next
    operation and ``sibyl init`` on a running process takes effect on the
    next one. The entitlement lives beside it in ``entitlement.json``.

    ``refresh_fn(account_id, session_token) -> RefreshOutcome`` defaults to
    ``http_refresh``. ``now_fn`` is the local wall clock. ``trusted_keys``
    defaults to the pinned ``TRUSTED_KEYS`` (read at call time).
    ``allow_refresh=False`` makes a gate that never refreshes (no network, no
    background thread): it evaluates what the device already holds, exactly
    as strictly. Used by short-lived processes that a harness kills after a
    few seconds (the Claude Code hooks); it can only ever refuse more.
    """

    def __init__(
        self,
        credentials_path: str | Path,
        *,
        refresh_fn: Callable[[str, str], RefreshOutcome] | None = None,
        now_fn: Callable[[], float] = time.time,
        trusted_keys: Mapping[str, bytes] | None = None,
        background_refresh: bool = True,
        allow_refresh: bool = True,
    ) -> None:
        self.credentials_path = Path(credentials_path).expanduser()
        self._store = EntitlementStore(self.credentials_path.parent / STATE_FILENAME)
        self._refresh_fn = refresh_fn
        self._now = now_fn
        self._trusted_override = trusted_keys
        self._background = background_refresh
        self._allow_refresh = bool(allow_refresh)
        self._lock = threading.RLock()
        self._mem_lock = threading.Lock()
        self._ident_key: Any = object()
        self._ident: Identity | None = None
        self._state_key: Any = object()
        self._state: dict[str, Any] = {}
        self._verified: dict[tuple, Any] = {}
        # In-memory mirror of the monotonic bookkeeping, so a refused or failed
        # state write (read-only dir, symlinked file) never lets the process
        # forget the high-water mark.
        self._mem: dict[str, float] = {}
        self._last_hwm_write = -math.inf
        # (token, skew) this process measured on a successful refresh.
        self._trusted_skew: tuple[str, float] | None = None
        self._bg_thread: threading.Thread | None = None
        self._force_refresh = False
        self._suppress_refresh = False

    # -- paths ------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self._store.path

    @property
    def store(self) -> EntitlementStore:
        return self._store

    # -- cached reads -------------------------------------------------------
    def _identity(self) -> Identity | None:
        try:
            st = os.lstat(self.credentials_path)
            key = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)
        except OSError:
            key = None
        if key != self._ident_key:
            self._ident = read_identity(self.credentials_path) if key is not None else None
            self._ident_key = key
        return self._ident

    def _load_state(self) -> dict[str, Any]:
        key = self._store.mtime_key()
        if key != self._state_key:
            self._state = self._store.load() if key is not None else {}
            self._state_key = key
        st = dict(self._state)
        with self._mem_lock:
            if "hwm" in self._mem:
                st["hwm"] = max(st.get("hwm", -math.inf), self._mem["hwm"])
            if "last_attempt" in self._mem:
                st["last_attempt"] = max(st.get("last_attempt", -math.inf), self._mem["last_attempt"])
        return st

    def _verify(self, token: str) -> Entitlement:
        keys = self._trusted_override if self._trusted_override is not None else _trusted()
        ck = (token, tuple(sorted((k, bytes(v)) for k, v in keys.items())))
        hit = self._verified.get(ck)
        if hit is None:
            try:
                hit = parse_and_verify(token, keys)
            except EntitlementFormatError as e:
                hit = e
            if len(self._verified) > 8:
                self._verified.clear()
            self._verified[ck] = hit
        if isinstance(hit, Exception):
            raise hit
        return hit

    @staticmethod
    def _bound(ent: Entitlement, ident: Identity) -> bool:
        return (ent.sub == ident.account_id
                and ent.ten in (ident.tenant_id, ident.account_id)
                and ent.dh == ident.dh)

    # -- state writes -------------------------------------------------------
    def _update_state(self, fn: Callable[[dict[str, Any]], None], *, wait: float = 0.0,
                      have_lock: bool = False) -> bool:
        locked = have_lock or self._store.acquire_lock(wait)
        if not locked:
            return False
        try:
            st = self._store.load()
            fn(st)
            return self._store.store(st)
        finally:
            if not have_lock:
                self._store.release_lock()

    def _note_hwm(self, now: float, stored: float | None) -> float:
        """Raise the in-memory high-water mark; persist it at most once a
        minute AND only when it has advanced a minute past the stored value, so
        a new process (or a rebuilt client) does not rewrite the file."""
        with self._mem_lock:
            hwm = max(self._mem.get("hwm", -math.inf), now)
            self._mem["hwm"] = hwm
        if (now - self._last_hwm_write >= HWM_WRITE_INTERVAL_SECONDS
                and (stored is None or hwm - stored >= HWM_WRITE_INTERVAL_SECONDS)):
            self._last_hwm_write = now

            def _bump(st: dict[str, Any]) -> None:
                st["hwm"] = max(st.get("hwm", -math.inf), hwm)
            self._update_state(_bump)
        return hwm

    # -- refresh ------------------------------------------------------------
    def _may_attempt(self, st: dict[str, Any], now: float, interval: float) -> bool:
        ra_until = st.get("retry_after_until")
        # A stored Retry-After deadline beyond what any server answer could set
        # is a local edit: ignore it (it would suppress every refresh, and with
        # it the server's word on a revoked session, for as long as it says).
        if (ra_until is not None and now < ra_until
                and ra_until - now <= MAX_RETRY_AFTER_SECONDS):
            return False
        last = st.get("last_attempt")
        if last is None or now < last:
            return True
        return now - last >= interval

    def refresh(self, blocking: bool = True) -> RefreshOutcome:
        """One refresh attempt (or a background one when ``blocking`` is False).

        Several threads or processes racing produce ONE network call: the
        loser waits for the lock, sees the winner's attempt nonce and returns
        ``shared``."""
        ident = self._identity()
        if ident is None:
            return RefreshOutcome("denied", code="no_account")
        if not self._allow_refresh:
            return RefreshOutcome("transient", code="refresh_disabled")
        if not blocking:
            self._spawn_background(ident)
            return RefreshOutcome("shared")
        return self._do_refresh(ident)

    def _spawn_background(self, ident: Identity) -> None:
        if self._suppress_refresh or not self._allow_refresh:
            return
        if not self._background:
            self._do_refresh(ident)
            return
        with self._mem_lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return
            t = threading.Thread(target=self._do_refresh_quiet, args=(ident,),
                                 daemon=True, name="sibyl-entitlement-refresh")
            self._bg_thread = t
        try:
            t.start()
        except Exception:
            pass

    def _do_refresh_quiet(self, ident: Identity) -> None:
        try:
            self._do_refresh(ident)
        except Exception:
            pass

    def _do_refresh(self, ident: Identity) -> RefreshOutcome:
        before = self._store.load().get("attempt_nonce")
        if not self._store.acquire_lock(LOCK_WAIT_SECONDS):
            return RefreshOutcome("transient", code="lock_busy")
        try:
            st = self._store.load()
            if st.get("attempt_nonce") != before:
                return RefreshOutcome("shared")
            now = self._now()
            fn = self._refresh_fn or globals()["http_refresh"]
            try:
                outcome = fn(ident.account_id, ident.session_token)
            except Exception:
                outcome = RefreshOutcome("transient", code="refresh_error")
            if not isinstance(outcome, RefreshOutcome):
                outcome = RefreshOutcome("transient", code="refresh_error")
            st = self._store.load()
            synced_ent: Entitlement | None = None
            st["last_attempt"] = now
            st["attempt_nonce"] = secrets.token_hex(8)
            with self._mem_lock:
                self._mem["last_attempt"] = max(self._mem.get("last_attempt", -math.inf), now)
            if outcome.status == "ok":
                try:
                    ent = self._verify(outcome.token or "")
                    good = self._bound(ent, ident)
                except EntitlementFormatError:
                    good = False
                if good:
                    st["token"] = outcome.token
                    st["received_at"] = now
                    st["skew"] = now - ent.iat
                    self._trusted_skew = (outcome.token or "", now - ent.iat)
                    # The server just vouched for the time: a corrected clock
                    # heals itself (2.6), so the high-water mark restarts here.
                    st["hwm"] = now
                    with self._mem_lock:
                        self._mem["hwm"] = now
                    for k in ("denied", "last_refresh_error", "retry_after_until",
                              "first_seen_legacy"):
                        st.pop(k, None)
                    synced_ent = ent
                else:
                    # A 200 with a token we cannot verify or that is not ours:
                    # ignored, the old token is kept (9.1 item 5).
                    st["last_refresh_error"] = "invalid"
                    outcome = RefreshOutcome("transient", code="invalid")
            elif outcome.status == "denied":
                st["denied"] = {"code": outcome.code or "signed_out", "at": now}
                if outcome.cause == SIGNED_OUT_ON_CLAIM:
                    st["denied"]["cause"] = SIGNED_OUT_ON_CLAIM
                    if clean_iso(outcome.since) is not None:
                        st["denied"]["since"] = clean_iso(outcome.since)
                st.pop("token", None)
                st.pop("received_at", None)
                st.pop("skew", None)
                st["last_refresh_error"] = outcome.code or "denied"
            else:
                st["last_refresh_error"] = (outcome.code or "transient")[:64]
                if outcome.retry_after:
                    st["retry_after_until"] = now + float(outcome.retry_after)
            self._store.store(st)
            self._state_key = object()  # force a re-read on the next evaluation
            if synced_ent is not None:
                # Shown-tier fix: the saved tier follows the pass (foreground and
                # background re-checks both land here).
                sync_credentials_tier(self.credentials_path, synced_ent)
            return outcome
        finally:
            self._store.release_lock()

    def notify_server_rejected(self, code: str | None = None) -> None:
        """The api refused the token on another call (403 entitlement_invalid,
        401). Refresh in the background at the next evaluation, ignoring the
        throttle once; the refresh itself is the authority."""
        self._force_refresh = True

    # -- evaluation ---------------------------------------------------------
    def evaluate(self, op: str | None = None, *, allow_blocking: bool = True) -> Decision:
        with self._lock:
            return self._evaluate(op, allow_blocking)

    def _evaluate(self, op: str | None, allow_blocking: bool) -> Decision:
        for attempt in range(2):
            blocking = allow_blocking and attempt == 0 and self._allow_refresh
            now = self._now()
            ident = self._identity()
            if ident is None:
                return Decision("no_account", "no_account")
            st = self._load_state()
            hwm = self._note_hwm(now, self._state.get("hwm"))
            hwm = max(hwm, st.get("hwm", -math.inf))

            denied = st.get("denied")
            if denied is not None:
                if self._may_attempt(st, now, DENIED_RETRY_SECONDS) and (
                        now - denied["at"] >= DENIED_RETRY_SECONDS or now < denied["at"]):
                    if blocking:
                        self._do_refresh(ident)
                        continue
                    self._spawn_background(ident)
                return Decision("locked", denied["code"], cause=denied.get("cause"),
                                since=denied.get("since"))

            token = st.get("token")
            ent: Entitlement | None = None
            problem: str | None = None
            if token is not None:
                try:
                    ent = self._verify(token)
                except EntitlementVersionError:
                    problem = None  # an unknown version is ABSENT, not valid
                except EntitlementFormatError:
                    problem = "invalid_entitlement"
                if ent is not None and not self._bound(ent, ident):
                    ent, problem = None, "wrong_device"

            if ent is None and problem is not None:
                if self._may_attempt(st, now, LOCKED_RETRY_SECONDS) or self._force_refresh:
                    self._force_refresh = False
                    if blocking:
                        self._do_refresh(ident)
                        continue
                    self._spawn_background(ident)
                return Decision("locked", problem)

            if ent is None:
                return self._bootstrap(ident, st, now, hwm, blocking)

            trusted = self._trusted_skew
            if trusted is not None and trusted[0] == token:
                skew = trusted[1]
            else:
                skew = st.get("skew")
                if skew is None:
                    # A writer that stored no skew (the CLI after `sibyl init`):
                    # derive it from the receipt time it did store.
                    skew = st.get("received_at", now) - ent.iat
                # File values are editable: bound the lead (see
                # MAX_SKEW_AHEAD_SECONDS). A clock BEHIND the server (negative
                # skew) is not bounded; that direction only shortens the band.
                skew = min(skew, MAX_SKEW_AHEAD_SECONDS)
            server_now = hwm - skew
            base = dict(entitlement=ent, server_now=server_now)
            due_force = self._force_refresh
            self._force_refresh = False
            if ent.mem == "none":
                # Locked by server policy: ask again (throttled, in the
                # background) like every other locked branch, so a lifted
                # policy (email confirmed) reaches this device without a restart.
                if due_force or self._may_attempt(st, now, LOCKED_RETRY_SECONDS):
                    self._spawn_background(ident)
                return Decision("locked", "policy_none", **base)

            if ent.nbf - server_now > NBF_TOLERANCE_SECONDS:
                if due_force or self._may_attempt(st, now, STALE_RETRY_SECONDS):
                    self._spawn_background(ident)
                return self._with_policy(Decision("stale", notice=CLOCK_NOTICE, **base), op)
            if server_now <= ent.exp:
                if due_force or (server_now - ent.iat >= ent.ra
                                 and self._may_attempt(st, now, FRESH_RETRY_SECONDS)):
                    self._spawn_background(ident)
                d = Decision("fresh", renews_in_seconds=max(0.0, ent.iat + ent.ra - server_now),
                             grace_left_seconds=ent.exp + ent.og - server_now, **base)
                if ent.acct.get("email_required") is True:
                    d.notice = EMAIL_NAG_NOTICE
                return self._with_policy(d, op)
            if server_now <= ent.exp + ent.og:
                if due_force or self._may_attempt(st, now, STALE_RETRY_SECONDS):
                    self._spawn_background(ident)
                left = ent.exp + ent.og - server_now
                d = Decision("stale", grace_left_seconds=left,
                             notice=stale_notice(int(max(0.0, server_now - ent.iat) // 86400),
                                                 _days_ceil(left)), **base)
                return self._with_policy(d, op)
            # Past exp + og: locked until a check-in succeeds.
            if due_force or self._may_attempt(st, now, LOCKED_RETRY_SECONDS):
                if blocking:
                    self._do_refresh(ident)
                    continue
                self._spawn_background(ident)
            return Decision("locked", "expired_offline", **base)
        return Decision("locked", "expired_offline")

    def _with_policy(self, d: Decision, op: str | None) -> Decision:
        ent = d.entitlement
        if ent is not None and ent.mem == "read_only" and op in WRITE_OPS:
            return Decision("locked", "policy_read_only", entitlement=ent,
                            server_now=d.server_now)
        return d

    def _bootstrap(self, ident: Identity, st: dict[str, Any], now: float, hwm: float,
                   blocking: bool) -> Decision:
        """Credentials, no usable entitlement. Leaving this state takes ONE
        verified token from a successful online refresh (D10 = 0): until then
        the answer is ``locked`` / ``setup_incomplete``. Nothing here reads or
        writes a first-sight time, so no local file can start or restart a
        grace period."""
        if self._may_attempt(st, now, LOCKED_RETRY_SECONDS) or self._force_refresh:
            self._force_refresh = False
            if blocking:
                out = self._do_refresh(ident)
                if out.status in ("ok", "denied", "shared"):
                    return self._evaluate_again()
            else:
                self._spawn_background(ident)
        return Decision("locked", "setup_incomplete")

    def _evaluate_again(self) -> Decision:
        # One non-blocking pass after a refresh changed the state file.
        return self._evaluate(None, False)

    # -- public surface -----------------------------------------------------
    def prime(self) -> Decision:
        """Construction-time evaluation: never raises; a BLOCKING refresh only
        while setup is incomplete (credentials, no entitlement: spec 3.1's
        bootstrap), background otherwise."""
        try:
            d = self.evaluate(allow_blocking=False)
            if d.state == "locked" and d.code == "setup_incomplete":
                d = self.evaluate(allow_blocking=True)
            return d
        except Exception:
            return Decision("no_account", "no_account")

    def require(self, op: str | None = None) -> Entitlement | None:
        d = self.evaluate(op)
        if d.state == "no_account":
            raise EntitlementError("no_account")
        if d.state == "locked":
            code = d.code or "invalid_entitlement"
            if code == "policy_read_only" and d.entitlement is not None:
                # R1 (plan 4.2): read-only because of the computer limit, not
                # the email: same code (the MCP `reason` stays stable), the
                # device copy.
                from .exceptions import (DEVICES_READ_ONLY_HUMAN_ACTION, DEVICES_READ_ONLY_NEXT,
                                         DEVICES_READ_ONLY_RECOVERY, devices_read_only_message)
                msg = devices_read_only_message(d.entitlement.acct, d.entitlement.tier)
                if msg:
                    err = EntitlementError(code, message=msg, next=DEVICES_READ_ONLY_NEXT,
                                           human_action=DEVICES_READ_ONLY_HUMAN_ACTION)
                    err.recovery = DEVICES_READ_ONLY_RECOVERY
                    raise err
            if code == "signed_out" and d.cause == SIGNED_OUT_ON_CLAIM:
                raise EntitlementError(code, message=signed_out_on_claim_message(d.since),
                                       next=SIGNED_OUT_ON_CLAIM_NEXT, human_action=None)
            raise EntitlementError(code)
        return d.entitlement

    def peek(self) -> Decision:
        """Evaluation with NO refresh of any kind (no network): for callers
        that only want to know what the device holds right now (the CLI's
        X-Sibyl-Entitlement header, status rows)."""
        with self._lock:
            self._suppress_refresh = True
            try:
                return self._evaluate(None, False)
            except Exception:
                return Decision("no_account", "no_account")
            finally:
                self._suppress_refresh = False

    def peek_token(self) -> str | None:
        """The verified, bound token inside exp + og, without any refresh."""
        d = self.peek()
        if d.entitlement is not None and d.state in ("fresh", "stale"):
            return d.entitlement.token
        return None

    def current(self) -> Decision:
        """Non-blocking evaluation, for read-mostly callers (tier, token)."""
        try:
            return self.evaluate(allow_blocking=False)
        except Exception:
            return Decision("no_account", "no_account")

    @property
    def tier(self) -> str | None:
        d = self.current()
        if d.state in ("fresh", "stale") and d.entitlement is not None:
            return d.entitlement.tier
        return None

    @property
    def cap_bytes(self) -> int | None:
        d = self.current()
        if d.state in ("fresh", "stale") and d.entitlement is not None:
            return d.entitlement.cap
        return None

    @property
    def token(self) -> str | None:
        """The verified, bound token to present to the api, else None."""
        d = self.current()
        if d.entitlement is not None and d.state in ("fresh", "stale"):
            return d.entitlement.token
        return None

    def trusted_kids(self) -> list[str]:
        keys = self._trusted_override if self._trusted_override is not None else _trusted()
        return sorted(keys)

    def status(self) -> dict[str, Any]:
        """Everything ``sibyl status`` / ``sibyl health`` show, token-free.
        No refresh of its own (the caller decides whether to refresh first)."""
        d = self.peek()
        st = self._load_state()
        ent = d.entitlement
        if ent is None and st.get("token"):
            try:
                ent = self._verify(st["token"])
            except EntitlementFormatError:
                ent = None
        out: dict[str, Any] = {
            "state": d.state,
            "code": d.code,
            "notice": d.notice,
            "renews_in_seconds": d.renews_in_seconds,
            "grace_left_seconds": d.grace_left_seconds,
            "received_at": st.get("received_at"),
            "last_attempt": st.get("last_attempt"),
            "last_refresh_error": st.get("last_refresh_error"),
            "kids": self.trusted_kids(),
        }
        if d.cause is not None:
            out["cause"] = d.cause
            out["signed_out_at"] = d.since
        if ent is not None:
            out.update({"tier": ent.tier, "acct": dict(ent.acct), "exp": ent.exp,
                        "iat": ent.iat, "mem": ent.mem, "kid": ent.kid, "cap": ent.cap,
                        "src": ent.src if isinstance(ent.src, str) else None})
            # Tier convention (2026-10-05): whether this version recognises the
            # signed label (an unrecognised one is treated as Free, reason
            # tier_unknown), the tier it is treated as, and the server's
            # canonical plan ({tier, src}) when the token carries one.
            out["tier_known"] = _tiers.known(ent.tier)
            out["tier_effective"] = _tiers.normalize(ent.tier) or _tiers.FREE
            plan = ent.acct.get("plan") if isinstance(ent.acct, Mapping) else None
            if (isinstance(plan, Mapping) and isinstance(plan.get("tier"), str)
                    and isinstance(plan.get("src"), str)):
                out["plan"] = {"tier": plan["tier"], "src": plan["src"]}
                # Plan-end fix (2026-10-08): the paid access end, when the server
                # sends it (additive; older servers send neither).
                for k in ("ends_at", "renews_at"):
                    if isinstance(plan.get(k), str) and 0 < len(plan[k]) <= 40:
                        out["plan"][k] = plan[k]
        return out


__all__ = [
    "Decision", "Entitlement", "EntitlementFormatError", "EntitlementGate",
    "EntitlementStore", "EntitlementVersionError", "Identity", "RefreshOutcome",
    "classify_denial", "device_hash", "http_refresh", "kid_of", "parse_and_verify",
    "read_identity", "sync_credentials_tier",
]
