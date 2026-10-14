"""`sibyl` command-line interface.

A wrapper around HTTP calls to https://api.sibyllabs.org/api/plugin/* (plus
pypi.org for `sibyl update`) and the local Sibyl Memory store.

Design pillars:
  - HTTP is stdlib urllib. The package depends on sibyl-memory-client,
    sibyl-memory-hermes and pyyaml; some client imports stay lazy (the
    `sibyl hook` fast path, and `sibyl update` / `sibyl setup`, which must
    still run on a half-upgraded install).
  - Credentials are written atomically at mode 0600: a mkstemp temp file
    (created O_CREAT|O_EXCL) is fchmod'ed to 0600 before any byte is written,
    fsynced, then renamed over the target.
  - The URL handed to the browser is an opaque session identifier, never the
    long-lived bearer (audit SEC-1).
  - session_token is never printed in full: display a short slice only.
  - Polling has explicit timeouts; no infinite loops.
  - Exit codes are per command; see each `cmd_*` docstring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any


from ._timefmt import utc_minute as _utc_minute  # times carry a zone


def _client_version() -> str:
    """Return the installed package version from metadata, never hardcoded."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        try:
            return _v("sibyl-memory-cli")
        except PackageNotFoundError:
            return "0.0.0+source"
    except Exception:
        return "0.0.0+source"

# ---- Defaults ----------------------------------------------------------

API_BASE = os.environ.get("SIBYL_API_BASE", "https://api.sibyllabs.org")
# Dedicated short-URL auth subdomain (2026-05-20). Trust + phishing resistance:
# the URL the user sees in their terminal + browser is purpose-specific and
# short enough to read at a glance. Legacy URL `sibyllabs.org/plugin/activate
# ?session=<uuid>` still resolves so older CLI installs continue to work.
ACTIVATE_BASE = os.environ.get("SIBYL_ACTIVATE_BASE", "https://auth.sibyllabs.org")
UPGRADE_BASE = os.environ.get("SIBYL_UPGRADE_BASE", "https://sibyllabs.org/plugin/upgrade")
# The account page (0.5.0). Every URL the CLI prints or opens on this origin is
# checked against it first, so a server string can never steer the browser
# somewhere else. Never carries a token.
ACCOUNT_BASE = os.environ.get("SIBYL_ACCOUNT_BASE", "https://account.sibyllabs.org").rstrip("/")
# The account origin as people read it in copy (no scheme), and its security page.
ACCOUNT_HOST = ACCOUNT_BASE.split("://", 1)[-1]
ACCOUNT_SECURITY_PAGE = f"{ACCOUNT_HOST}/security"
# Where a card purchase lands; the checkout code for `sibyl claim` is shown there.
CLAIM_PAGE = "sibyllabs.org/pro/welcome"

DEFAULT_CRED_PATH = Path("~/.sibyl-memory/credentials.json").expanduser()
DEFAULT_DB_PATH = Path("~/.sibyl-memory/memory.db").expanduser()
DEFAULT_TIER_CACHE_PATH = Path("~/.sibyl-memory/tier_cache.json").expanduser()

POLL_INTERVAL_SEC = 3
# v0.3.5 fix: the CLI no longer carries its own activation deadline. The
# server's /session-init response includes pairing_ttl_seconds — the CLI
# polls until that timestamp, deferring to the server as the single source
# of truth. The constants below are fallbacks only, used when session-init
# fails to return a value (network error, schema drift). Drift between CLI
# and server is now impossible by construction; the prior 10min/15min
# silent-success gap can't recur because there is no CLI-side number to
# diverge from the server's.
INIT_TIMEOUT_FALLBACK_SEC = 30 * 60   # used only if session-init returns no TTL
UPGRADE_TIMEOUT_SEC = 30 * 60         # upgrade flow uses local constant — no server handshake to defer to

# ---- Color / output ----------------------------------------------------

from . import _aesthetic as a
from . import _account_gate as _gate
from . import _handoff as _h

# The credentials path of the running command (root --credentials). The
# X-Sibyl-Entitlement header reads the entitlement.json beside it (0.5.0,
# spec account-gated 5.6). Set in main(); defaults to the standard location.
_ENT_CRED_PATH: Path = DEFAULT_CRED_PATH
# Set by read_credentials when credentials.json is corrupt (row I19): the
# command's handoff then ends `next: sibyl init --force`.
_CREDS_CORRUPT = False


def _init_next() -> str:
    """`sibyl init`, or `sibyl init --force` when credentials.json is corrupt."""
    return "sibyl init --force" if _CREDS_CORRUPT else "sibyl init"


# Agent handoff sentences (spec agent-handoff-messages 2026-10-03, section 3).
WINDOWS_HUMAN = ("install Sibyl Memory inside WSL2 and run sibyl init there; native Windows "
                 "is not supported")
EMAIL_ADDRESS_HUMAN = "an email address for the account (receipts, security notices, purchases)"
OFFLINE_HUMAN = "connect this computer to the internet"


def _code_human(masked: Any, mins: Any = None) -> str:
    valid = f" (valid {mins} minutes)" if mins else ""
    return f"check {masked or 'your inbox'} for a six digit code from Sibyl Labs{valid} and tell me the code"


def _verify_human(masked: Any) -> str:
    return f"the six digit code we will email to {masked or 'the address on file'} in the next step"


def _email_web_human() -> str:
    """Row E11: the browser step, when the api does not take email
    confirmation from the terminal (``features.cli_email_verify`` not true)."""
    return (f"add and confirm your email at {ACCOUNT_BASE} (sign in with the email or wallet "
            "used here)")

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def c(code: str, s: str) -> str:
    if _NO_COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


def dim(s: str) -> str: return c("2", s)
def bold(s: str) -> str: return c("1", s)
def green(s: str) -> str: return c("32", s)
def yellow(s: str) -> str: return c("33", s)
def red(s: str) -> str: return c("31", s)
def cyan(s: str) -> str: return c("36", s)


def _detect_os_family() -> str | None:
    p = sys.platform
    if p == "darwin": return "macos"
    if p.startswith("linux"): return "linux"
    if p.startswith("win"): return "windows"
    return None


# ---- platform support policy (2026-09-04) ------------------------------
#
# Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows
# through WSL2. Native Windows is not supported. WSL2 is Linux to Python and
# to us, so it needs no special case beyond the label.
#
# "block" refuses `sibyl init` and `sibyl setup` on native Windows. Every
# other command keeps working on machines that already activated there, so an
# existing install never loses `status`, `upgrade`, `claim`, `devices`,
# `logout`, `health`, `memory` or `update`.
NATIVE_WINDOWS_POLICY = "block"
NATIVE_WINDOWS_ESCAPE_ENV = "SIBYL_ALLOW_NATIVE_WINDOWS"
WSL_DOCS_URL = "https://docs.sibyllabs.org/memory/install#windows"


def _platform_info() -> tuple[str, str, bool]:
    """(os_family, human label, supported). WSL2 is Linux to Python and to us.

    The os_family element is deliberately the same vocabulary
    `_detect_os_family()` returns (`linux|macos|windows`), because telemetry
    consumers key on it. `unknown` is the one value that function reports as
    None instead.
    """
    p = sys.platform
    arch = _platform.machine() or "unknown"
    if p == "darwin":
        ver = _platform.mac_ver()[0] or ""
        return "macos", f"macOS {ver} ({arch})".replace("  ", " "), True
    if p.startswith("linux"):
        rel = _platform.release().lower()
        wsl = "microsoft" in rel or bool(os.environ.get("WSL_DISTRO_NAME"))
        return "linux", ("Linux, WSL2" if wsl else "Linux") + f" ({arch})", True
    if p.startswith("win"):
        return "windows", f"Windows {_platform.release()} native ({arch})", False
    return "unknown", p, False


def _is_wsl() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    return "microsoft" in _platform.release().lower() or bool(os.environ.get("WSL_DISTRO_NAME"))


def _native_windows_override() -> bool:
    """True when the user has explicitly opted into the unsupported path."""
    v = (os.environ.get(NATIVE_WINDOWS_ESCAPE_ENV) or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def print_platform_line(*, gate: bool = False) -> int:
    """Print the platform line. Returns 0 to continue, 1 to refuse.

    `gate=True` is passed by `sibyl init` and `sibyl setup` only. Those are the
    two commands that stand up a NEW install, and they are the only two the
    native-Windows block applies to.
    """
    family, label, supported = _platform_info()
    if supported:
        print(dim(f"  platform  {label} \u00b7 supported"))
        return 0

    blocked = (
        gate
        and family == "windows"
        and NATIVE_WINDOWS_POLICY == "block"
        and not _native_windows_override()
    )
    paint = red if blocked else yellow
    print(paint(f"  platform  {label} \u00b7 not supported"))

    if family != "windows":
        print(yellow("  Sibyl Memory is built and tested on Linux and macOS. This platform is"))
        print(yellow("  neither, so nothing here is tested or fixed. Continuing anyway."))
        return 0

    if blocked:
        print(red("  Sibyl Memory is not supported on native Windows. Install it inside WSL2 and"))
        print(red(f"  run this command again there: {WSL_DOCS_URL}"))
        print(red("  Already activated here? status, upgrade, claim, devices and logout keep working."))
        print(red(f"  To continue unsupported anyway, set {NATIVE_WINDOWS_ESCAPE_ENV}=1."))
        return 1

    print(yellow("  Sibyl Memory is not supported on native Windows. Run it inside WSL2:"))
    print(yellow(f"  {WSL_DOCS_URL}"))
    print(yellow("  Continuing anyway. Nothing on this platform is tested or fixed."))
    return 0


def short(token: str | None) -> str:
    if not token:
        return "—"
    if len(token) <= 12:
        return token
    return f"{token[:8]}…{token[-4:]}"


def _fmt_cap_bytes(cap: Any) -> str:
    """Render a server-supplied cap_bytes value defensively.

    CLI-16: `cap_bytes` is None for unlimited, otherwise an int. A non-int,
    non-None value (e.g. a string from a buggy/old server) would raise on the
    `:,` format spec. Coerce to int when possible; show the raw value rather
    than crash when it can't be coerced."""
    if cap is None:
        return "unlimited"
    try:
        return f"{int(cap):,}"
    except (TypeError, ValueError):
        return str(cap)


# ---- HTTP --------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, body: Any, url: str, headers: dict | None = None) -> None:
        super().__init__(f"HTTP {status} for {url}: {body}")
        self.status = status
        self.body = body
        self.url = url
        # Response headers (0.5.0, for Retry-After). Empty for network errors.
        self.headers = headers or {}

    @property
    def code(self) -> str | None:
        """The server's machine-readable `code`, when the body carries one."""
        if isinstance(self.body, dict) and isinstance(self.body.get("code"), str):
            return self.body["code"]
        return None

    @property
    def message(self) -> str | None:
        """The server's human `error` string, when the body carries one."""
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), str):
            return self.body["error"]
        return None


def _claim_signout_line(e: "HttpError | None") -> str | None:
    """F-1 (contract C1): the line for the api's 401 ``signed_out_on_claim``
    (this computer was signed out when the email was confirmed on the account
    page), or None for any other answer."""
    if e is None or e.status != 401 or e.code != _gate.SIGNED_OUT_ON_CLAIM:
        return None
    return _gate.signed_out_on_claim_line(e.body.get("signed_out_at") if isinstance(e.body, dict) else None)


def _https_context() -> ssl.SSLContext:
    """TLS context for every CLI network call (api.sibyllabs.org, pypi.org).

    Prefers the shared certifi-backed trust context from sibyl-memory-client,
    the fix for macOS framework-build Pythons that ship an empty OpenSSL trust
    store (the `session-init failed (0)` / `Bind failed` / `server error: 0`
    class of report). Degrades to the stdlib default context when the client
    package is absent, mirroring the `_ver_lt` packaging fallback: the CLI
    stays runnable stdlib-only, just without the Darwin trust fix. Verification
    is fully on in both branches; nothing here ever weakens it.
    """
    try:
        from sibyl_memory_client._trust import https_context
    except ImportError:
        return ssl.create_default_context()
    return https_context()


def http_request(  # noqa: D401
    method: str,
    path: str,
    *,
    body: dict | None = None,
    timeout: float = 15.0,
    headers: dict | None = None,
) -> dict:
    """Single source of truth for HTTP calls. Returns parsed JSON or raises HttpError."""
    url = f"{API_BASE}{path}"
    data = None
    full_headers = {"Accept": "application/json", "User-Agent": f"sibyl-memory-cli/{_client_version()}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        full_headers["Content-Type"] = "application/json"
    if headers:
        full_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=full_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"error": "unparseable response body"}
        try:
            hdrs = {k: v for k, v in (e.headers.items() if e.headers else [])}
        except Exception:
            hdrs = {}
        raise HttpError(e.code, err_body, url, hdrs) from None
    except urllib.error.URLError as e:
        raise HttpError(0, {"error": str(e.reason)}, url) from None


# ---- Credentials I/O ---------------------------------------------------

# CLI-15: credentials.json is built from an explicit allowlist of known
# fields, never the raw server blob. Contract T: tenant_id is persisted so
# mcp/hermes/langgraph resolve the same tenant.
CRED_FIELDS = ("account_id", "tenant_id", "tier", "wallet", "email", "issued_at",
               "bearer_token", "expires_at")

# Rule (release-blocker F1): a field the server's credential signature commits
# to never changes under an old signature. Server-issued credentials replace
# the file (email confirm); a local tier/wallet hint (upgrade, claim) drops the
# signature and signed_at first (hardening L2), and the server stays the tier
# authority.


def _allowlisted_credentials(raw: dict) -> dict:
    """The persisted subset of a server-issued credentials blob (CLI-15)."""
    return {k: raw[k] for k in CRED_FIELDS if k in raw}


def _credentials_from_server(raw: Any, current: dict) -> dict | None:
    """Credentials to write after a server re-issued them for THIS account
    (POST /email-verify/confirm, release-blocker F1): the allowlisted server
    fields, plus this computer's own session_token / bearer_token / expires_at
    (the server does not send the bearer back). None when `raw` is not a
    credentials object for the same account."""
    if not isinstance(raw, dict) or not raw.get("account_id") or raw.get("account_id") != current.get("account_id"):
        return None
    new = _allowlisted_credentials(raw)
    for k in ("session_token", "bearer_token", "expires_at"):
        if k not in new and current.get(k):
            new[k] = current[k]
    return new


def _unsign_for_local_edit(creds: dict) -> dict:
    """Prepare a local edit of a signed field (the tier / wallet hint after an
    upgrade or a claim, hardening L2): drop `signature` and `signed_at`, which
    are not in CRED_FIELDS and which `sibyl init` never persists. An unsigned
    file makes the adapters send no credentials claim, so the edit cannot read
    as tampering, and the file never keeps a stale tier next to a newer
    server tier. Mutates and returns `creds`."""
    creds.pop("signature", None)
    creds.pop("signed_at", None)
    return creds


def write_credentials_atomic(creds: dict, path: Path = DEFAULT_CRED_PATH) -> Path:
    """Write credentials.json atomically at mode 0600.

    v0.1.2 hardening (audit SEC-2): no world-readable window. The temp file
    is created O_CREAT|O_EXCL by mkstemp and fchmod'ed to 0600 before any
    byte is written (see CLI-2 below). Previously used write_text() followed
    by os.chmod(), leaving a world-readable window between syscalls every
    credential save.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is ignored when the dir already exists (bug, dor_alpha 2026-06-01):
    # a pre-existing 0755 ~/.sibyl-memory left credentials world-readable. Tighten
    # explicitly to cover the pre-existing-directory case.
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    data = json.dumps(creds, indent=2).encode("utf-8")
    # v0.3.17 hardening (audit CLI-2): unique per-process temp via
    # tempfile.mkstemp instead of the fixed `<name>.tmp` + unlink dance.
    # The old approach unlinked a leftover temp and then re-created it with
    # O_EXCL — a TOCTOU window where two concurrent writers (or an attacker
    # who recreated the path between unlink and open) could collide. mkstemp
    # picks a name no other process holds and opens it O_CREAT|O_EXCL itself,
    # so no unlink is needed. We still enforce mode 0600 (fchmod, since mkstemp
    # honors the process umask) and fsync before the atomic replace.
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp, str(path))
    return path


def read_credentials(path: Path = DEFAULT_CRED_PATH) -> dict | None:
    """Read credentials.json.

    v0.1.2 hardening (audit SEC-11): refuses to follow symlinks.
    Returns None if the file is a symlink or doesn't exist."""
    path = path.expanduser()
    if not path.exists():
        return None
    if path.is_symlink():
        return None
    # v0.3.17 hardening (audit CLI-1 / submitter D1): a corrupt or unreadable
    # credentials.json must surface a clean one-liner, not a raw traceback.
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        global _CREDS_CORRUPT
        _CREDS_CORRUPT = True
        print(red("credentials.json is corrupt or unreadable. Run `sibyl init --force` to re-activate."))
        return None


def _tier_display(value: Any) -> str:
    """Display word for a tier label: Free / Pro / Elite (tier convention,
    2026-10-05). Legacy labels show as the tier they alias (stake -> Pro);
    an unrecognised label shows as Free. Falls back to the raw label when the
    client (which owns the one tier module) is not importable."""
    try:
        from sibyl_memory_client.tiers import display
    except Exception:  # pragma: no cover - client absent or pre-0.9.0
        return str(value or "free").capitalize()
    return display(value or "free")


def invalidate_tier_cache(path: Path = DEFAULT_TIER_CACHE_PATH) -> None:
    """Drop the local tier cache so the next write refreshes against the server."""
    path = path.expanduser()
    if path.exists():
        path.unlink()


def is_sqlite_db(path: Path) -> bool:
    """Lightweight check that `path` is a real SQLite database.

    v0.3.17 (audit CLI-3 / submitter D3): `sibyl status --db <garbage>` used to
    report a non-SQLite file as if it were a normal DB. We check the 16-byte
    SQLite header magic, then confirm the file opens and answers a trivial
    PRAGMA. A 0-byte file is a valid (empty) SQLite database, so it passes.
    Returns False on any read/parse error rather than raising."""
    import sqlite3

    try:
        if not path.exists() or not path.is_file():
            return False
        size = path.stat().st_size
        if size == 0:
            return True  # empty file is a valid, freshly-created SQLite DB
        with open(path, "rb") as fh:
            header = fh.read(16)
        if header != b"SQLite format 3\x00":
            return False
        con = sqlite3.connect(str(path))
        try:
            con.execute("PRAGMA schema_version")
        finally:
            con.close()
        return True
    except (OSError, sqlite3.Error):
        return False


# ---- account plumbing (0.5.0) ------------------------------------------
#
# Shared by init / status / email / claim / upgrade / dashboard. Everything
# here degrades: an OLD api returns no `features` and no `account`, and the
# callers then print exactly what 0.4.1 printed.

def _account_url(path: str = "") -> str:
    return f"{ACCOUNT_BASE}/{path.lstrip('/')}" if path else ACCOUNT_BASE


def _bearer_headers(creds: dict, *, entitlement: bool = True) -> dict:
    """Bearer auth for a session-authenticated call, plus the device's
    entitlement as X-Sibyl-Entitlement (spec 5.6) when entitlement.json holds a
    token that verifies, is bound to this computer and is inside exp + og.
    With no usable token, ONE blocking /entitlement refresh first (the same
    ensure-fresh path as the security calls), so a device back from offline
    or finishing setup does not hit the cutoff's 403. A token the api would
    refuse is never sent; with none the api accepts the call until
    min_client_version is set."""
    token = creds.get("bearer_token") or creds.get("session_token")
    if not token:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    if entitlement:
        ent = _gate.ensure_fresh_token(_ENT_CRED_PATH)
        if ent:
            headers[_gate.HEADER] = ent
    return headers


def _security_request(method: str, path: str, creds: dict, *, body: dict | None = None,
                      timeout: float = 10.0) -> Any:
    """A session call for device management and logout (spec 5.6, api O-5).

    A device offline past exp + og holds an expired token, and these are the
    calls a person makes to recover or to sign out, so: with no usable token,
    try one /entitlement refresh first; send whatever usable token exists; on
    a 403 entitlement_invalid retry ONCE without the header (the api accepts an
    absent token until the cutoff). Never blocks logout or revoke on the gate."""
    token = creds.get("bearer_token") or creds.get("session_token")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    ent = _gate.ensure_fresh_token(_ENT_CRED_PATH) if token else None
    headers = dict(auth)
    if ent:
        headers[_gate.HEADER] = ent
    try:
        return http_request(method, path, body=body, headers=headers, timeout=timeout)
    except HttpError as e:
        if ent and e.status == 403 and e.code == "entitlement_invalid":
            return http_request(method, path, body=body, headers=auth, timeout=timeout)
        raise


def _fetch_access_ex(creds: dict) -> tuple[dict | None, HttpError | None]:
    """POST /api/plugin/access with the bearer. (body, None) or (None, error)."""
    token = creds.get("bearer_token") or creds.get("session_token")
    try:
        resp = http_request(
            "POST",
            "/api/plugin/access",
            body={"account_id": creds.get("account_id"), "session_token": token},
            headers=_bearer_headers(creds),
            timeout=10.0,
        )
    except HttpError as e:
        return None, e
    return (resp if isinstance(resp, dict) else {}), None


def _fetch_access(creds: dict) -> dict | None:
    """The /access body, or None on any HttpError (callers degrade)."""
    return _fetch_access_ex(creds)[0]


def _features_of(body: Any) -> dict:
    f = body.get("features") if isinstance(body, dict) else None
    return f if isinstance(f, dict) else {}


def _account_of(body: Any) -> dict:
    acc = body.get("account") if isinstance(body, dict) else None
    return acc if isinstance(acc, dict) else {}


def _is_interactive() -> bool:
    try:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
    return bool(tty) and not os.environ.get("SIBYL_NONINTERACTIVE")


def _on_account_origin(url: Any) -> bool:
    """True only for the account page origin itself or a path under it.

    A plain startswith() would also accept https://account.sibyllabs.org.evil.example,
    so the origin must be followed by nothing, `/`, or `?`.
    """
    if not isinstance(url, str):
        return False
    return url == ACCOUNT_BASE or url.startswith(ACCOUNT_BASE + "/") or url.startswith(ACCOUNT_BASE + "?")


_APPROVE_PREFIX_PATH = "/activate?r="


def _approve_url_ok(url: Any) -> bool:
    """Print an approval link only when it is ours and carries the ?r= handle.

    The handle is sha256 of the session id (64 lowercase hex). A legacy
    `?s=<session uuid>` link is refused: the session id becomes the bearer.
    """
    if not isinstance(url, str) or not url.startswith(ACCOUNT_BASE + _APPROVE_PREFIX_PATH):
        return False
    return re.fullmatch(r"[0-9a-f]{64}", url.split("?r=", 1)[1]) is not None


def _session_ref(session_id: str) -> str:
    """Same value as the server's db.sessionRef(): sha256 hex of the lowercase uuid."""
    return hashlib.sha256(session_id.strip().lower().encode("utf-8")).hexdigest()


def _retry_minutes(e: HttpError) -> str:
    raw = None
    for k, v in (e.headers or {}).items():
        if str(k).lower() == "retry-after":
            raw = v
            break
    if raw is None and isinstance(e.body, dict):
        raw = e.body.get("retry_after_seconds")
    try:
        secs = int(float(raw))
    except (TypeError, ValueError):
        return "a few"
    return str(max(1, -(-secs // 60)))


def _email_error_message(e: HttpError) -> str:
    """One plain line for an email-verify failure (spec 4.3 error map)."""
    code = e.code
    msg = e.message
    gate_line = _gate.gate_http_line(e.status, code, cred_path=_ENT_CRED_PATH, body=e.body)
    if gate_line:
        return gate_line
    if code == "not_open":
        return (f"email confirmation from the terminal is not open yet. add your email at "
                f"{ACCOUNT_BASE} (sign in with the email or wallet you used here).")
    if code == "wrong_code":
        left = e.body.get("attempts_remaining") if isinstance(e.body, dict) else None
        left_s = str(left) if isinstance(left, int) and not isinstance(left, bool) else "a few"
        return f"that code is not right ({left_s} attempts left)."
    if code in ("already_verified", "email_taken", "mail_unavailable", "expired",
                "identity_frozen", "email_required_in_body") and msg:
        return msg
    if code == "expired":
        return "that code expired. run sibyl email verify for a new one."
    if e.status == 401:
        return "session expired. re-run sibyl init."
    if e.status == 403 and isinstance(e.body, dict) and e.body.get("reason") == "tamper_flag_active":
        return "this account is under review. email sibyl@sibyllabs.org."
    if e.status == 404:
        # An api from before 0.5.0 has no email-verify endpoints.
        return (f"email confirmation is not available on this server yet. add your email at "
                f"{ACCOUNT_BASE} instead.")
    if e.status == 429:
        return f"too many requests. try again in {_retry_minutes(e)} minutes."
    if e.status == 0:
        return "could not reach the server."
    if code and msg:
        return msg
    return f"server error: {e.status}"


def _email_rc(e: HttpError) -> int:
    """Exit code for an email-verify failure: 2 server side, 1 otherwise."""
    return 2 if (e.status == 0 or e.status >= 500 or e.status == 404) else 1


def _retry_in(e: HttpError) -> str:
    mins = _retry_minutes(e)
    return _h.retry_in(mins if mins.isdigit() else 5)


def _email_error_handoff(e: HttpError, masked: Any = None) -> tuple:
    """(next, human) for an email-verify failure: rows E11 to E23, the same
    branch order as ``_email_error_message``."""
    code = e.code
    gate = _gate.gate_http_handoff(e.status, code, cred_path=_ENT_CRED_PATH, body=e.body)
    if gate is not None:
        return gate
    not_open = ("sibyl email", _email_web_human())
    if code == "not_open":
        return not_open
    if code == "wrong_code":
        left = e.body.get("attempts_remaining") if isinstance(e.body, dict) else None
        left_s = str(left) if isinstance(left, int) and not isinstance(left, bool) else "a few"
        return ("sibyl email confirm <code>", f"re-read the six digit code ({left_s} attempts left)")
    if code == "already_verified":
        return ("sibyl status", None)
    if code == "email_taken":
        return ("sibyl email add <address>", "that address belongs to another account; give a "
                f"different address, or sign in at {ACCOUNT_BASE} with it")
    if code == "mail_unavailable":
        return (_h.retry_in(5), None)
    if code == "expired":
        return ("sibyl email verify", f"a new code will be sent to {masked or 'the address on file'}")
    if code == "identity_frozen":
        return ("done", "this account's identity is frozen; email sibyl@sibyllabs.org")
    if code == "email_required_in_body":
        return ("sibyl email add <address>", EMAIL_ADDRESS_HUMAN)
    if e.status == 401:
        return ("sibyl init", None)
    if e.status == 403 and isinstance(e.body, dict) and e.body.get("reason") == "tamper_flag_active":
        return _gate.code_handoff("under_review")
    if e.status == 404:
        return not_open
    if e.status == 429:
        return (_retry_in(e), None)
    if e.status == 0:
        return (_h.RETRY, OFFLINE_HUMAN)
    return (_h.RETRY, None)


def _harness_wired() -> bool:
    """True when an agent harness on this computer already has Sibyl wired
    (file checks only, never a subprocess). Picks `sibyl status` over
    `sibyl setup` as the next step after an email is confirmed (row E24)."""
    try:
        from .setup import CodexWirer, HermesWirer
        for w in (HermesWirer(), CodexWirer()):
            if w.current_state().get("wired_with_sibyl"):
                return True
    except Exception:
        pass
    home = Path.home()
    for p in (home / ".claude.json", home / ".claude" / "settings.json"):
        try:
            if p.is_file() and '"sibyl-memory"' in p.read_text(encoding="utf-8"):
                return True
        except OSError:
            pass
    return False


def _after_confirm_next() -> str:
    return "sibyl status" if _harness_wired() else "sibyl setup"


def _email_request(creds: dict, address: str | None) -> dict:
    """POST /api/plugin/email-verify/request. Raises HttpError."""
    body: dict = {"account_id": creds.get("account_id")}
    if address:
        body["email"] = address
    return http_request(
        "POST",
        "/api/plugin/email-verify/request",
        body=body,
        headers=_bearer_headers(creds),
        timeout=15.0,
    )


def _normalize_code(code: str | None) -> str | None:
    c = "".join(ch for ch in (code or "") if not ch.isspace() and ch != "-")
    return c if re.fullmatch(r"\d{6}", c) else None


def _email_confirm_once_h(creds: dict, code: str, cred_path: Path, *,
                          then: str | None = None) -> tuple:
    """One confirm call. (exit code, server error code or None, handoff)."""
    norm = _normalize_code(code)
    if norm is None:
        print(a.warn_line("a code is six digits, from the email we sent."))
        return 1, "bad_format", ("sibyl email confirm <code>",
                                 "the six digit code from the email (digits only)")
    try:
        resp = http_request(
            "POST",
            "/api/plugin/email-verify/confirm",
            body={"code": norm},
            headers=_bearer_headers(creds),
            timeout=15.0,
        )
    except HttpError as e:
        print(a.warn_line(_email_error_message(e)))
        return _email_rc(e), (e.code or str(e.status)), _email_error_handoff(e, _mask_email(creds.get("email")))
    resp = resp if isinstance(resp, dict) else {}
    handoff = (then or _after_confirm_next(), None)
    # Release-blocker F1: email is a SIGNED field. Write the credentials the
    # server re-signed (through the CLI-15 allowlist); an api that sends none
    # leaves the file exactly as it is (never edit a signed field locally).
    fresh = _credentials_from_server(resp.get("credentials"), creds)
    if fresh is not None:
        try:
            write_credentials_atomic(fresh, cred_path)
            creds.clear()
            creds.update(fresh)
        except OSError:
            print(a.warn_line("confirmed on the server, but credentials.json could not be updated."))
            handoff = ("sibyl init --force", None)
    # 0.5.0 (spec 3.4, 5.2): the confirm answer carries a re-issued entitlement
    # (its `acct` must not lag a week). None sent = the old file stays.
    if resp.get("entitlement") is not None:
        _gate.write_entitlement_atomic(resp.get("entitlement"), cred_path)
    try:
        invalidate_tier_cache()  # a pending purchase may have attached
    except OSError:
        pass
    print(a.success_line("Email confirmed. Your account is complete."))
    attached = resp.get("claims_attached")
    if isinstance(attached, int) and not isinstance(attached, bool) and attached > 0:
        print(a.dim(f"  {attached} purchase(s) attached"))
    revoked = resp.get("sessions_revoked")
    if isinstance(revoked, int) and not isinstance(revoked, bool) and revoked > 0:
        print(a.dim(f"  {revoked} other computer(s) signed out"))
    return 0, None, handoff


def _email_confirm_prompt_h(creds: dict, cred_path: Path, *, masked: Any = None,
                            mins: Any = None, then: str | None = None) -> tuple:
    """Ask for the emailed code. Three tries on a wrong code; Enter skips.
    (exit code, (next, human))."""
    waiting_code = ("sibyl email confirm <code>", _code_human(masked, mins))
    for _ in range(3):
        try:
            answer = input("  code from the email: ").strip()
        except EOFError:
            answer = ""
        if not answer:
            print(a.dim("  skipped. when the email arrives: sibyl email confirm <code>"))
            return 1, waiting_code
        rc, err, h = _email_confirm_once_h(creds, answer, cred_path, then=then)
        if rc == 0:
            return 0, h
        if err not in ("wrong_code", "bad_format"):
            return rc, h
    print(a.dim("  run sibyl email verify for a new code."))
    return 1, ("sibyl email verify", "the code was wrong three times; a new one will be sent")


def _email_request_and_prompt_h(creds: dict, address: str | None, cred_path: Path,
                                *, interactive: bool, then: str | None = None) -> tuple:
    """Request a code, report where it went, then prompt (TTY) or explain the
    next step. (exit code, (next, human))."""
    try:
        resp = _email_request(creds, address)
    except HttpError as e:
        print(a.warn_line(_email_error_message(e)))
        return _email_rc(e), _email_error_handoff(e, _mask_email(address or creds.get("email")))
    resp = resp if isinstance(resp, dict) else {}
    masked = resp.get("to_masked") if isinstance(resp.get("to_masked"), str) else _mask_email(address or creds.get("email"))
    secs = resp.get("expires_in_seconds")
    n_mins = max(1, int(secs) // 60) if isinstance(secs, (int, float)) and not isinstance(secs, bool) and secs > 0 else None
    mins = f" it expires in {n_mins} minutes." if n_mins else ""
    print(a.dim(f"  code sent to {masked}.{mins}"))
    if interactive:
        return _email_confirm_prompt_h(creds, cred_path, masked=masked, mins=n_mins, then=then)
    print(a.dim("  then run: sibyl email confirm <code>"))
    return 0, ("sibyl email confirm <code>", _code_human(masked, n_mins))


def _looks_like_email(s: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s or "") is not None


# ---- `sibyl init` ------------------------------------------------------

def _gen_pairing_code() -> str:
    """6-digit cryptographic pairing code. Uniform across 000000-999999."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_pairing_code(code: str, session: str) -> str:
    return hashlib.sha256(f"{code}:{session}".encode("utf-8")).hexdigest()


# F-1a (contract C2): an email typed in the browser at `sibyl init`
# (pairing code + address) proves terminal and browser, never the mailbox. The
# first sign-in on the account page with that address is the mailbox proof, and
# it signs out every computer set up before it, this one included. Said on every
# path that leaves the email unconfirmed.
CLAIM_SIGNOUT_WARNING = ("until you confirm your email here, signing in on the account page signs "
                         "this computer out. confirm later with: sibyl email verify")


def _usdc_bind_offered(init_resp: Any) -> bool:
    """True when session-init minted a USDC-send bind for this session."""
    if not isinstance(init_resp, dict):
        return False
    amt = init_resp.get("bind_amount_micros")
    bus = init_resp.get("bind_bus_wallet")
    return (isinstance(amt, int) and not isinstance(amt, bool) and amt > 0
            and isinstance(bus, str) and bool(bus.strip()))


def _init_email_step(creds: dict, cred_path: Path, features: dict, account: dict,
                     *, no_email: bool) -> tuple:
    """After `Activated.`: nag for a missing / unconfirmed email and, on a TTY,
    offer to fix it now. Never fails init; every error prints and moves on.

    Silent against an old api (no `account` in /check and no `cli_email_verify`
    feature), so 0.5.0 on a 0.4-era server prints what 0.4.1 printed.

    Returns the init handoff (rows I11 to I15): (next, human, later).

    F7 (onboarding friction 2026-10-08): the installer runs `sibyl setup`
    before `sibyl init`, so an agent is usually wired already; then the step
    after activation is `sibyl status`, and `sibyl setup` is not queued.
    """
    after = _after_confirm_next()
    later_setup = ("sibyl setup",) if after == "sibyl setup" else ()
    done = (after, None, ())
    server_knows = bool(account) or ("cli_email_verify" in features)
    if not server_knows or account.get("email_verified") is True:
        return done
    required = account.get("email_required") is True
    if features.get("cli_email_verify") is not True:
        # The api does not take confirmation from the terminal (row E11):
        # `sibyl email add` would only fail with not_open, so hand the
        # browser step to the person instead of asking for an address.
        print(a.warn_line("confirm your email to finish your account" if creds.get("email")
                          else "add an email to finish your account"))
        print(a.dim(f"    add and confirm it at {ACCOUNT_BASE}. local memory works either way."))
        print(a.dim(f"    {CLAIM_SIGNOUT_WARNING}"))
        print()
        return (after, _email_web_human(), ("sibyl email",))
    can_prompt = _is_interactive() and not no_email
    if not creds.get("email"):
        add = (("sibyl email add <address>", EMAIL_ADDRESS_HUMAN, later_setup) if required
               else (after, EMAIL_ADDRESS_HUMAN, ("sibyl email add <address>",)))
        print(a.warn_line("add an email to finish your account"))
        print(a.dim("    receipts, security notices and purchases need one. local memory works either way."))
        print(a.dim("    sibyl email add you@example.com"))
        print()
        if not can_prompt:
            return add
        try:
            answer = input("  add an email now? (address, or Enter to skip): ").strip()
        except EOFError:
            answer = ""
        if not answer:
            print()
            return add
        if not _looks_like_email(answer):
            print(a.warn_line("that does not look like an email address. later: sibyl email add <address>"))
            print()
            return add
        rc, h = _email_request_and_prompt_h(creds, answer, cred_path, interactive=True, then=after)
        print()
        return done if rc == 0 else (h[0], h[1], later_setup)
    if account.get("email_verified") is False:
        masked = _mask_email(creds.get("email"))
        verify = (("sibyl email verify", _verify_human(masked), later_setup) if required
                  else (after, None, ("sibyl email verify",)))
        print(a.dim("  confirm your email to finish your account: sibyl email verify"))
        if can_prompt:
            # F-1a (C2): no "confirm it now?" question; the code goes out
            # and the only prompt is for the code (Enter still skips).
            rc, h = _email_request_and_prompt_h(creds, None, cred_path, interactive=True,
                                                then=after)
            if rc != 0:
                print(a.dim(f"  {CLAIM_SIGNOUT_WARNING}"))
            print()
            return done if rc == 0 else (h[0], h[1], later_setup)
        print(a.dim(f"  {CLAIM_SIGNOUT_WARNING}"))
        print()
        return verify
    return done


# ---- computer limit (R1, plan 4.2 / 4.4) --------------------------------
# The api counts active sign-ins (bearer rows) per account. At the limit it
# does not bind a new computer: /check answers bound:false with a `limit`
# object, and bind / email-bind answer 409 with the same object as the body:
#   {code: "device_limit", limit: <int>, tier: <str>,
#    devices: [{label, os_family, last_seen_at}], manage_url, upgrade_url}
# (labels only, never tokens). Re-activating a computer that already holds
# credentials sends replace: {session_token} on session-init, so its old
# sign-in is revoked in the same transaction and no slot is used. An optional
# integer `pro_limit` in the object is the paid plans' limit, named in the Free
# upgrade line only when the api sends it (never a CLI constant there).

# Plan 4.4's `sibyl status` / `sibyl devices` row ("1 of 1 (Free) · Pro: up to 10").
PRO_DEVICE_LIMIT_HINT = 10
_LABEL_MAX = 60


def _clean_label(v: Any) -> str:
    t = "".join(ch for ch in str(v or "") if ch.isprintable())
    t = " ".join(t.split())[:_LABEL_MAX]
    return t or "(unlabeled)"


def _device_limit_of(obj: Any) -> dict | None:
    """The computer-limit object of a /check body (``limit``) or of a 409 body
    (the object itself), shape-checked; None when it is not one."""
    if not isinstance(obj, dict):
        return None
    lim = obj if obj.get("code") == "device_limit" else obj.get("limit")
    if not isinstance(lim, dict) or lim.get("code") != "device_limit":
        return None
    devices = []
    raw = lim.get("devices")
    for d in (raw if isinstance(raw, list) else [])[:50]:
        if not isinstance(d, dict):
            continue
        seen = d.get("last_seen_at")
        devices.append({"label": _clean_label(d.get("label")),
                        "os_family": _clean_label(d.get("os_family")) if d.get("os_family") else "",
                        "last_seen_at": _utc_minute(seen) or ""})
    n = lim.get("limit")
    limit = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
    # Optional: the paid plans' limit, for the Free upgrade line. Plan 4.2's
    # object does not carry it; without it the line names no number.
    p = lim.get("pro_limit")
    pro_limit = p if isinstance(p, int) and not isinstance(p, bool) and p > 0 else None
    tier = lim.get("tier") if isinstance(lim.get("tier"), str) else None
    manage = lim.get("manage_url") if _on_account_origin(lim.get("manage_url")) else _account_url("security")
    up = lim.get("upgrade_url")
    if not (isinstance(up, str) and (_on_account_origin(up) or up.startswith("https://sibyllabs.org/"))):
        up = UPGRADE_BASE
    return {"limit": limit, "tier": tier, "devices": devices, "manage_url": manage, "upgrade_url": up,
            "pro_limit": pro_limit}


def _handle_device_limit(lim: dict, *, interactive: bool, window: Any = None) -> str:
    """Print the computers holding the slots and what to do. Returns "wait"
    (the person chose to sign one out on the account page; keep polling) or
    "stop". Nothing is revoked from this terminal: a new computer holds no
    credentials for the others; the Security page does it after sign-in."""
    n = len(lim["devices"])
    free = (lim["tier"] or "free") == "free"
    plan = ("Free is one computer" if free and lim["limit"] == 1
            else f"its plan allows {lim['limit']}" if lim["limit"] is not None else "its plan's limit is reached")
    print(f"\r{' ' * 80}\r", end="")
    print()
    print(a.warn_line(f"This account already has {n or lim['limit'] or 'its'} computer"
                      f"{'' if n == 1 else 's'} signed in; {plan}."))
    for d in lim["devices"]:
        meta = " · ".join(x for x in (d["os_family"], f"last seen {d['last_seen_at']}" if d["last_seen_at"] else "")
                          if x)
        print(a.kv(d["label"], meta or "—"))
    print()
    if interactive:
        who = lim["devices"][0]["label"] if n == 1 else "one of them"
        print(a.dim("  This terminal cannot sign another computer out; that is done on the account page."))
        try:
            ans = input(f"Open the account page to sign {who} out? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans in ("y", "yes"):
            print(a.dim(f"  Opening {lim['manage_url']}. Next: choose Sign out next to {who} there, then"))
            print(a.dim("  finish the sign-in on the activation page in your browser. This terminal keeps waiting."))
            _h.waiting("for the bind", window if window is not None else INIT_TIMEOUT_FALLBACK_SEC,
                       human=f"on the account page choose Sign out next to {who}, then finish the "
                             "sign-in in the activation tab")
            try:
                webbrowser.open(lim["manage_url"], new=2)
            except Exception:
                pass
            return "wait"
    print(a.dim(f"  This computer is not signed in. To use it, sign one of those out at {lim['manage_url']}"))
    print(a.dim("  (or run `sibyl devices revoke <index>` on a signed-in computer), then run `sibyl init` again."))
    if free:
        up_to = f" (up to {lim['pro_limit']})" if lim.get("pro_limit") else ""
        print(a.dim(f"  Pro works on every computer you use{up_to}: {lim['upgrade_url']}"))
    print(a.dim("  Nothing was deleted; the computers already signed in keep working."))
    or_pro = " (or get Pro)" if free else ""
    _h.handoff("sibyl init", later=("sibyl upgrade",) if free else (),
               human=f"your account is at its computer limit; sign one out at {lim['manage_url']}"
                     f"{or_pro}, then tell me")
    return "stop"


def cmd_init(args: argparse.Namespace) -> int:
    """Activation flow. Generate session UUID + pairing code, register with
    server, open activation page in browser, poll /check until bound.

    The pairing code is printed in the terminal. If the user picks the
    email path in the browser, they type both their email and this code.
    Afterwards it offers to add or confirm the account email (the server
    sends that verification code by email; --no-email skips it)."""
    # Brand moment — gold/white gradient SIBYL wordmark.
    # Honors NO_COLOR + TTY detection automatically; safe to always call.
    from ._banner import print_banner
    print_banner()

    # Platform line first, so it shows on --force re-activation too.
    if print_platform_line(gate=True) != 0:
        _h.handoff("done", human=WINDOWS_HUMAN)
        return 1

    cred_path = Path(args.credentials).expanduser()
    if getattr(args, "replace", False):
        args.force = True
    if cred_path.exists() and not args.force:
        existing = read_credentials(cred_path) or {}
        print(a.section_header("already activated", subtitle="use --force to re-activate"))
        print()
        print(a.kv("Account", existing.get("account_id") or "—"))
        print(a.kv("Tier", _tier_display(_gate.peek_tier(cred_path, existing.get("tier"))).upper(),
                   value_color="accent"))
        print(a.kv("Credentials", str(cred_path)))
        print()
        if _CREDS_CORRUPT:
            _h.handoff("sibyl init --force")
        else:
            _h.handoff("sibyl status", later=("sibyl init --force",))
        return 0

    # SEC-1 mitigation (v0.1.2): the URL parameter is an opaque pairing
    # session identifier, NOT the long-lived bearer used by /access and
    # /check-write. The CLI generates it locally and the server treats
    # it as the activation rendezvous key only. The persistent bearer
    # is issued by the server in the /check response (`bearer_token`
    # field) after activation completes. Servers running pre-SEC-1
    # firmware that echo the URL identifier as the bearer still work —
    # we use whichever the server returns in the bound credentials.
    session_id = str(uuid.uuid4())
    pairing_code = _gen_pairing_code()
    code_hash = _hash_pairing_code(pairing_code, session_id)
    # Path-based URL on the dedicated auth subdomain (2026-05-20).
    # auth.sibyllabs.org/<uuid> reads cleaner in the terminal than the old
    # query-string form and aligns the wallet popup's "X wants you to sign in"
    # header with the browser URL bar.
    if ACTIVATE_BASE.rstrip("/").endswith(".sibyllabs.org") or ACTIVATE_BASE.rstrip("/").endswith("/auth"):
        activate_url = f"{ACTIVATE_BASE.rstrip('/')}/{session_id}"
    else:
        # Legacy fallback: anyone with SIBYL_ACTIVATE_BASE pointing at the old
        # /plugin/activate path keeps the query-string shape.
        activate_url = f"{ACTIVATE_BASE}?session={session_id}"

    # Pre-register the session + pairing code hash with the server.
    # The code itself never leaves the user's machine until they type it
    # into the browser.
    #
    # v0.3.5: capture pairing_ttl_seconds from the response and use it as
    # the activation deadline. Server is the single source of truth — if
    # the server-side TTL ever changes, the CLI adopts the new value
    # automatically without a re-publish. INIT_TIMEOUT_FALLBACK_SEC only
    # applies when the call fails entirely (network error) or the response
    # is missing the field (schema drift).
    pairing_ttl_seconds = None
    # 0.5.0: the server advertises what it supports. An old api sends no
    # `features`, and then nothing below prints or asks anything new.
    features: dict = {}
    init_body: dict = {
        "session": session_id,
        "pairing_code_hash": code_hash,
        "env": {
            "os_family": _detect_os_family(),
            "os_version": _platform.release(),
            "arch": _platform.machine(),
            "wsl": _is_wsl(),
            "install_method": "cli",
            "client_version": _client_version(),
        },
    }
    # R1 (plan 4.2): re-activation never costs a computer slot. The sign-in
    # this computer already holds is named so the api revokes it in the same
    # transaction as the new bind (an api without the limit ignores the key).
    prior = read_credentials(cred_path) if cred_path.exists() else None
    prior_token = (prior or {}).get("session_token")
    if isinstance(prior_token, str) and prior_token:
        init_body["replace"] = {"session_token": prior_token}
    interactive = _is_interactive()
    # the USDC-from-a-mobile-wallet path is offered only when the
    # api minted this session's USDC-send amount (session-init answers
    # bind_amount_micros + bind_bus_wallet; both null while USDC sign-in is off,
    # and with no answer at all there is no amount to send).
    usdc_on = False
    try:
        init_resp = http_request("POST", "/api/plugin/session-init", body=init_body, timeout=10.0)
        if isinstance(init_resp, dict):
            v = init_resp.get("pairing_ttl_seconds")
            if isinstance(v, (int, float)) and v > 0:
                pairing_ttl_seconds = int(v)
            features = _features_of(init_resp)
            usdc_on = _usdc_bind_offered(init_resp)
    except HttpError as e:
        lim = _device_limit_of(e.body) if e.status == 409 else None
        if lim is not None:
            if _handle_device_limit(lim, interactive=interactive,
                                    window=pairing_ttl_seconds or INIT_TIMEOUT_FALLBACK_SEC) == "stop":
                return 2
        else:
            # Non-fatal: SIWE path doesn't need the pairing code. If session-init
            # fails the user can still complete SIWE. Surface the warning.
            print(yellow(f"Warning: session-init failed ({e.status}). Wallet path still works; email path may not."))

    activation_window_sec = pairing_ttl_seconds if pairing_ttl_seconds else INIT_TIMEOUT_FALLBACK_SEC

    print()
    print(a.section_header("activation", subtitle=("three paths" if usdc_on else "two paths")
                                                   + " · pick whichever fits your device"))
    print()
    print(a.kv("Session", short(session_id)))
    formatted_code = pairing_code[:3] + " " + pairing_code[3:]
    print(a.kv("Code", a.gradient_gold(formatted_code), value_color="accent")
          + "  " + a.dim("(use this in the email panel)"))
    if features.get("device_approval") is True:
        # The ?r= handle is sha256(session id): it names this request without
        # carrying the session id, which becomes the bearer on approval.
        # P1 (web-first spec 9.4): the account may exist only on the website.
        print(a.dim(f"  already have a Sibyl Memory account (website or another computer)? approve "
                    f"this one at {_account_url('activate')}?r={_session_ref(session_id)} instead "
                    f"(sign in there, then type this code)"))
    print(a.kv("Opening", activate_url))
    print()
    print(a.dim("  desktop wallet · email + code · or send USDC from any mobile wallet" if usdc_on
                else "  desktop wallet · or email + code"))
    print(a.dim("  this terminal will pick up automatically when you bind."))
    print()
    if features.get("device_approval") is True:
        # F12 (onboarding friction 2026-10-08): the likeliest path first, so an
        # agent relays one decision instead of four equal options.
        paths = ("that code, a desktop wallet, or USDC from a mobile wallet" if usdc_on
                 else "that code or a desktop wallet")
        bind_human = (f"if you already have a Sibyl Memory account, approve this computer at "
                      f"{_account_url('activate')}?r={_session_ref(session_id)} with the code "
                      f"{formatted_code}; otherwise open {activate_url} and sign in with email plus "
                      f"{paths}; I pick it up here")
    else:
        paths = (f"with a desktop wallet, with email plus the code {formatted_code}, or by sending "
                 f"USDC from a mobile wallet" if usdc_on
                 else f"with a desktop wallet or with email plus the code {formatted_code}")
        bind_human = f"open {activate_url} and sign in {paths}; I pick it up here"
    _h.waiting(f"for the bind at {urllib.parse.urlparse(activate_url).netloc or 'the activation page'}",
               activation_window_sec, human=bind_human)

    try:
        webbrowser.open(activate_url, new=2)
    except Exception:
        pass

    # Poll /api/plugin/check
    deadline = time.time() + activation_window_sec
    last_status = ""
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin_i = 0
    approve_url_printed = False
    limit_shown = False

    while time.time() < deadline:
        lim = None
        try:
            resp = http_request("GET", f"/api/plugin/check?session={urllib.parse.quote(session_id)}", timeout=10.0)
        except HttpError as e:
            if e.status in (404, 503, 0):
                # Session not yet created server-side, or transient — keep polling
                resp = {"bound": False}
            elif e.status == 409 and _device_limit_of(e.body) is not None:
                resp, lim = {"bound": False}, _device_limit_of(e.body)
            else:
                print(red(f"\nUnexpected error: {e.body}"))
                _h.handoff("sibyl init --force")
                return 2

        # R1 (plan 4.2): at the computer limit the api does not bind; /check
        # says why (bound:false + `limit`). Shown once; an old api never sends it.
        if lim is None and isinstance(resp, dict) and not resp.get("bound"):
            lim = _device_limit_of(resp)
        if lim is not None and not limit_shown:
            limit_shown = True
            if _handle_device_limit(lim, interactive=interactive,
                                    window=int(deadline - time.time())) == "stop":
                return 2

        if resp.get("bound") and resp.get("credentials"):
            raw_creds = resp["credentials"]
            # CLI-15: never trust the server payload shape blindly. A non-dict
            # `credentials` (string, list, null) would otherwise crash on .get
            # or persist garbage. Treat it as "not yet bound" and keep polling.
            if not isinstance(raw_creds, dict):
                print(f"\r{' ' * 80}\r", end="")
                print(red("\nServer returned malformed credentials. Re-run `sibyl init --force`."))
                _h.handoff("sibyl init --force")
                return 2
            # SEC-1: prefer the server-issued bearer_token (post-fix) over
            # echoing the URL pairing-session id. Servers running pre-SEC-1
            # firmware echo `session_token` back as the bearer — we use
            # whichever the server returns. The CLI's session_id (URL
            # identifier) is the rendezvous key, not the persistent bearer.
            bearer = raw_creds.get("bearer_token") or raw_creds.get("session_token")
            if not bearer:
                # Fallback: pre-SEC-1 server flow where neither field is
                # echoed back — inject the pairing session id so subsequent
                # /access and /check-write calls have something to send.
                bearer = session_id
            # Sanity check on echoed session_token (pre-SEC-1 flow only)
            if raw_creds.get("session_token") and raw_creds["session_token"] != session_id \
                    and not raw_creds.get("bearer_token"):
                print(red("\nSession token mismatch — refusing to write credentials."))
                _h.handoff("sibyl init --force")
                return 2
            # CLI-15: build the persisted dict from an explicit allowlist of
            # known fields, not the raw server blob. This keeps unexpected /
            # hostile server-supplied keys out of credentials.json.
            # Contract T (tenant resolution, Real #1): persist the server-issued
            # tenant_id so mcp/hermes/langgraph all resolve the SAME tenant from
            # this credentials.json. Without it the CLI dropped tenant_id on
            # activation and every surface silently fell back to DEFAULT_TENANT.
            creds = _allowlisted_credentials(raw_creds)
            creds["session_token"] = bearer
            path = write_credentials_atomic(creds, cred_path)
            # 0.5.0 account gate (spec 3.4, 5.2): /check bound:true carries
            # `entitlement` NEXT TO `credentials`. A token bound to a previous
            # sign-in is useless, so it goes first; an api that sends none
            # leaves no file and the client bootstraps on first use.
            _gate.remove_entitlement(cred_path)
            ent_written = None
            if resp.get("entitlement") is not None:
                ent_written = _gate.write_entitlement_atomic(resp.get("entitlement"), cred_path)
            # 0.5.0: account state rides NEXT TO the signed credentials (never
            # inside them). Old apis send none.
            account = _account_of(resp)
            print(f"\r{' ' * 80}\r", end="")  # clear spinner line
            print()
            print(a.success_line("Activated."))
            print()
            print(a.kv("Account", creds.get("account_id") or "—"))
            print(a.kv("Tier", _tier_display(_gate.peek_tier(path, creds.get("tier"))).upper(),
                       value_color="accent"))
            print(a.kv("Wallet", creds.get("wallet") or "—"))
            print(a.kv("Email", creds.get("email") or "—"))
            print(a.kv("Credentials", str(path)))
            rows_left = False
            if ent_written is not None:
                print(a.kv("Access", "active", value_color="ok"))
                rows_left = _adopt_after_sign_in(Path(args.db).expanduser(), cred_path)
                _install_skills_quietly()
            print()
            nxt, human, later = _init_email_step(creds, path, features, account,
                                                 no_email=bool(getattr(args, "no_email", False)))
            # Wiring is `sibyl setup` (installer, then setup, then init): no
            # hand-edited config and no separate pip step. Already wired: the
            # handoff says `sibyl status` and nothing is printed here.
            if not _harness_wired():
                print(a.dim("  wire it into your agent: sibyl setup (wires Claude Code, Codex and Hermes)"))
                print()
            _h.handoff(nxt, human=human,
                       later=(("sibyl memory adopt",) if rows_left else ()) + tuple(later))
            return 0

        # 0.5.0: web device approval. The email already has an owner, so the
        # owner approves this computer at the account page.
        approval = resp.get("approval") if isinstance(resp, dict) else None
        waiting_approval = (isinstance(approval, dict) and approval.get("required") is True
                            and not approval.get("approved") and not approval.get("declined"))
        if isinstance(approval, dict) and approval.get("declined") is True:
            print(f"\r{' ' * 80}\r", end="")
            print(a.err_line("This computer was not approved (Not me was chosen)."))
            print(a.dim("  Re-run `sibyl init --force` if that was a mistake."))
            _h.handoff("done", human="this computer was not approved on the account page; if that "
                                     "was a mistake, say so and I will start again")
            return 2
        if waiting_approval and not approve_url_printed:
            url = approval.get("approve_url")
            # Only our own origin with the ?r= handle, never a server string
            # pointing anywhere else and never a ?s=<session> link.
            if _approve_url_ok(url):
                print(f"\r{' ' * 80}\r", end="")
                print(a.dim(f"  approve this computer at {url}"))
                _h.waiting(f"for approval at {ACCOUNT_HOST}", int(deadline - time.time()),
                           human=f"on a computer already signed in, open {url}, sign in, and type "
                                 f"the code {formatted_code} to approve this one")
                approve_url_printed = True

        # Spinner tick
        spin_i = (spin_i + 1) % len(spinner)
        remaining = int(deadline - time.time())
        spin_glyph = a.color(spinner[spin_i], a.PULSE)
        label = f"waiting for approval at {ACCOUNT_HOST}" if waiting_approval else "watching the network for your bind"
        status = f"\r  {spin_glyph} {a.dim(label)} … {a.dim(f'{remaining // 60}:{remaining % 60:02d} left')}"
        if status != last_status:
            sys.stdout.write(status)
            sys.stdout.flush()
            last_status = status
        time.sleep(POLL_INTERVAL_SEC)

    print()
    print(a.err_line("Activation timed out."))
    print(a.dim("  Re-run `sibyl init --force` to try again."))
    print()
    print(a.dim("  If your browser already showed 'Activation successful',"))
    print(a.dim("  your bind landed server-side but didn't reach this terminal."))
    print(a.dim("  Running `sibyl init --force` again will start a fresh handshake;"))
    print(a.dim("  bind through the same browser to write credentials locally."))
    _h.handoff("sibyl init --force", human="did the browser show Activation successful? either way "
                                          "the next command starts a fresh handshake")
    return 1


# ---- `sibyl upgrade` ---------------------------------------------------

def _legacy_upgrade_url(session_token: str) -> str:
    """The pre-2026-05-25 upgrade link. Only for an api with no upgrade-nonce
    endpoint (404). It carries the bearer in the URL, which is why every other
    path avoids it."""
    return f"{UPGRADE_BASE}?session={session_token}"


SESSION_EXPIRED = "session-expired"
# The gate handoff of a refused upgrade-nonce call (a 403 gate line), if any.
_UPGRADE_GATE_HANDOFF: tuple | None = None
PURCHASE_HUMAN = ("Pro is a purchase, so it is your call: buy it on the account page (card, or USDC) "
                  "or stake $SIBYL; I watch for the plan change here")


def _upgrade_exchange_url(creds: dict, account_id: str) -> str | None:
    """POST /api/plugin/upgrade-nonce and return its exchange_url.

    404 (an api from before the nonce existed) falls back to the legacy link.
    Any other failure returns None; the caller prints and exits 2.
    """
    token = creds.get("bearer_token") or creds.get("session_token")
    try:
        resp = http_request(
            "POST",
            "/api/plugin/upgrade-nonce",
            body={"account_id": account_id, "session_token": token},
            headers=_bearer_headers(creds),
            timeout=10.0,
        )
    except HttpError as e:
        # A route-level 404 (no such endpoint). "account not found" is a real
        # answer from a current api, and the legacy page would fail the same way.
        if e.status == 404 and e.message != "account not found":
            return _legacy_upgrade_url(token)
        gate_line = _gate.gate_http_line(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
        if gate_line:
            print(a.warn_line(gate_line))
            global _UPGRADE_GATE_HANDOFF
            _UPGRADE_GATE_HANDOFF = _gate.gate_http_handoff(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
            return None
        if e.status == 401:
            return SESSION_EXPIRED
        return None
    url = resp.get("exchange_url") if isinstance(resp, dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Upgrade flow. Read existing creds → open upgrade page → poll /access until tier flips."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.err_line("Not activated."))
        print(a.dim("  Run `sibyl init` first."))
        _h.handoff(_init_next(), later=("sibyl upgrade",))
        return 1

    account_id = creds.get("account_id")
    session_token = creds.get("session_token")
    # Shown-tier fix: the tier of the current pass (the saved tier only without one).
    current_tier = str(_gate.peek_tier(Path(args.credentials).expanduser(), creds.get("tier"))
                       or "free").lower()

    if not account_id or not session_token:
        print(a.err_line("credentials.json is missing account_id or session_token."))
        print(a.dim("  Re-run `sibyl init`."))
        _h.handoff("sibyl init --force")
        return 1

    # 0.5.0: the bearer never rides a URL. Either the account billing page
    # (sign in there; no token) or the one-time upgrade-nonce exchange.
    access, access_err = _fetch_access_ex(creds)
    if access_err is not None and access_err.status == 401:
        # Release-blocker F7: say what fixes it, never "try again".
        claim_line = _claim_signout_line(access_err)
        print(a.err_line(claim_line or "session expired. re-run sibyl init."))
        _h.handoff(_gate.SIGNED_OUT_ON_CLAIM_NEXT if claim_line else "sibyl init")
        return 1
    features = _features_of(access)
    account = _account_of(access)
    dash = features.get("web_dashboard_url")

    print()
    print(a.section_header("upgrade", subtitle="lift the 5 MB free-tier cap"))
    print()
    print(a.kv("Account", account_id or "—"))
    print(a.kv("Current tier", _tier_display(current_tier).upper(), value_color="accent"))

    # Release-blocker F7: warn whenever the purchase rails would refuse (nag
    # with the purchase gate on, or enforce past grace), not only on enforced.
    purchase_human = PURCHASE_HUMAN
    if account.get("purchase_blocked") is True or (account.get("email_required") is True and account.get("enforced") is True):
        print(a.warn_line("a confirmed email is required before buying: sibyl email add you@example.com"))
        purchase_human += ("; the account needs a confirmed email before buying; if the page refuses, "
                           "stop and I will run sibyl email add")

    if features.get("web_checkout") is True and _on_account_origin(dash):
        upgrade_url = f"{dash.rstrip('/')}/billing"
        print(a.kv("Opening", upgrade_url))
        print()
        print(a.dim("  buy Pro on the account page (sign in first; card, or USDC from the plugin page)."))
        print(a.dim("  this terminal keeps watching for the tier change."))
        print()
    else:
        global _UPGRADE_GATE_HANDOFF
        _UPGRADE_GATE_HANDOFF = None
        upgrade_url = _upgrade_exchange_url(creds, account_id)
        if upgrade_url == SESSION_EXPIRED:
            print(a.err_line("session expired. re-run sibyl init."))
            _h.handoff("sibyl init")
            return 1
        if upgrade_url is None:
            print(a.err_line("could not start the upgrade. try again."))
            gate_h = _UPGRADE_GATE_HANDOFF
            if gate_h is not None:
                _h.handoff(gate_h[0], human=gate_h[1], later=("sibyl upgrade",))
            else:
                _h.handoff(_h.RETRY)
            return 2
        # Never print the opened URL: the exchange code is single-use but still
        # a credential for 15 minutes, and the legacy fallback holds the bearer.
        print(a.kv("Opening", UPGRADE_BASE))
        print()
        print(a.dim("  two paths in the browser:"))
        print(a.dim("    1. Pro in USDC: $12 for one month or $108 for one year (a fixed period)"))
        print(a.dim("    2. stake 100,000 $SIBYL on Base: Pro while it stays staked (holding does not count)"))
        print(a.dim("  by card: $12/month or $108/year plus applicable tax at sibyllabs.org/checkout, then sibyl claim <cs_code>"))
        print()

    try:
        webbrowser.open(upgrade_url, new=2)
    except Exception:
        pass

    _h.waiting("for the plan change", UPGRADE_TIMEOUT_SEC, human=purchase_human)

    # Poll /api/plugin/access until tier changes
    deadline = time.time() + UPGRADE_TIMEOUT_SEC
    last_status = ""
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin_i = 0

    while time.time() < deadline:
        try:
            resp = http_request(
                "POST",
                "/api/plugin/access",
                body={"account_id": account_id, "session_token": session_token},
                timeout=10.0,
            )
        except HttpError as e:
            if e.status == 401:
                claim_line = _claim_signout_line(e)
                print(red("\n" + claim_line) if claim_line else red("\nSession expired. Re-run `sibyl init`."))
                _h.handoff(_gate.SIGNED_OUT_ON_CLAIM_NEXT if claim_line else "sibyl init")
                return 1
            # Transient — keep polling
            resp = {}

        new_tier = (resp.get("tier") or current_tier).lower()
        source = resp.get("source")

        if new_tier != current_tier and source in ("subscription", "staker"):
            # Tier changed. Refresh the local hint. Signed fields never change
            # under an old signature (release-blocker F1): the signature and
            # signed_at are dropped with the edit (hardening L2).
            _unsign_for_local_edit(creds)
            creds["tier"] = new_tier
            if resp.get("staker") and resp["staker"].get("wallet"):
                creds["wallet"] = resp["staker"]["wallet"]
            write_credentials_atomic(creds, Path(args.credentials).expanduser())
            invalidate_tier_cache()

            print(f"\r{' ' * 80}\r", end="")
            print()
            print(a.success_line(f"Upgraded to {new_tier.upper()} via {source}."))
            print()
            print(a.kv("Source", source))
            if resp.get("expires_at"):
                print(a.kv("Expires", _utc_minute(resp["expires_at"]) or str(resp["expires_at"])))
            if resp.get("cap_bytes") is None:
                print(a.kv("Storage cap", "unlimited", value_color="ok"))
            else:
                print(a.kv("Storage cap", f"{_fmt_cap_bytes(resp.get('cap_bytes'))} bytes"))
            if resp.get("staker"):
                s = resp["staker"]
                print(a.kv("Wallet", s.get("wallet", "—")))
                print(a.kv("$SIBYL held", str(s.get("total_sibyl", "—"))))
            print()
            print(a.dim("  local tier cache cleared. your next write will sync the new tier."))
            _h.handoff("sibyl refresh")
            return 0

        spin_i = (spin_i + 1) % len(spinner)
        remaining = int(deadline - time.time())
        spin_glyph = a.color(spinner[spin_i], a.PULSE)
        tier_glyph = a.color(current_tier.upper(), a.ACCENT)
        status = f"\r  {spin_glyph} {a.dim('waiting for browser upgrade')} · current: {tier_glyph}  {a.dim(f'{remaining // 60}:{remaining % 60:02d} left')}"
        if status != last_status:
            sys.stdout.write(status)
            sys.stdout.flush()
            last_status = status
        time.sleep(POLL_INTERVAL_SEC)

    print()
    print(a.err_line("Upgrade timed out. Tier unchanged."))
    print(a.dim("  Re-run `sibyl upgrade` to retry."))
    _h.handoff("sibyl refresh", later=("sibyl claim <cs_code>",),
               human="did the payment complete? if you paid by card, give me the cs_ code from the "
                     "welcome page")
    return 1


# ---- `sibyl status` ----------------------------------------------------

def _discover_stores(primary_db: Path) -> list[dict[str, Any]]:
    """Enumerate every memory.db an agent on this machine might resolve.

    Beta reports (2026-06-11) showed split-brain storage: the SDK / CLI /
    MCP default (``~/.sibyl-memory/memory.db``), the Hermes adapter
    (``$HERMES_HOME/sibyl/memory.db``), per-profile DBs
    (``$HERMES_HOME/sibyl/profiles/<p>/memory.db``), and an MCP
    ``SIBYL_MEMORY_DB`` override can each hold a disjoint set of memories, so a
    user switching entry points sees memory "vanish". This surfaces all of
    them in one place. Read-only: it never creates or moves anything (path
    unification is a separate, migration-gated change).

    Returns one dict per DISTINCT existing store, resolved + deduped:
    ``{"label", "path", "size"}``.
    """
    candidates: list[tuple[str, Path]] = [("default (SDK/CLI/MCP)", primary_db)]

    hermes_home_env = os.environ.get("HERMES_HOME")
    hermes_home = Path(hermes_home_env).expanduser() if hermes_home_env else (Path.home() / ".hermes")
    candidates.append(("hermes adapter", hermes_home / "sibyl" / "memory.db"))
    profiles_dir = hermes_home / "sibyl" / "profiles"
    if profiles_dir.is_dir():
        # #15 hygiene: iterdir() raises PermissionError on a restricted
        # profiles directory (e.g. a 0700 dir owned by another user). Skip the
        # whole profiles sweep gracefully rather than crashing `sibyl status`.
        try:
            profiles = sorted(profiles_dir.iterdir())
        except (PermissionError, OSError):
            profiles = []
        for prof in profiles:
            db = prof / "memory.db"
            if db.exists():
                candidates.append((f"hermes profile · {prof.name}", db))

    mcp_override = os.environ.get("SIBYL_MEMORY_DB")
    if mcp_override:
        candidates.append(("MCP SIBYL_MEMORY_DB", Path(mcp_override).expanduser()))

    seen: set[str] = set()
    stores: list[dict[str, Any]] = []
    for label, path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        stores.append({"label": label, "path": str(path), "size": size})
    return stores


def _print_account_rows(resp: Any, creds: dict) -> None:
    """Email / Account / Dashboard rows (0.5.0). Nothing when the api sends no
    `account` / `features`, so status on an old api is unchanged."""
    account = _account_of(resp)
    if account:
        verified = account.get("email_verified")
        label = account.get("email_masked") if isinstance(account.get("email_masked"), str) else None
        if label is None and creds.get("email"):
            label = _mask_email(creds.get("email"))
        if verified is True:
            print(a.kv("Email", f"confirmed \u00b7 {label}" if label else "confirmed", value_color="ok"))
        elif label:
            print(a.kv("Email", "not confirmed \u00b7 run sibyl email verify", value_color="warn"))
        else:
            print(a.kv("Email", "none \u00b7 run sibyl email add you@example.com", value_color="warn"))
        if account.get("email_required") is True:
            grace = account.get("grace_until")
            value = f"incomplete \u00b7 email required by {grace[:10]}" if isinstance(grace, str) and grace else "incomplete"
            if account.get("enforced") is True:
                value += " \u00b7 purchases need a confirmed email (local memory unaffected)"
            print(a.kv("Account", value, value_color="warn"))
    dash = _features_of(resp).get("web_dashboard_url")
    if _on_account_origin(dash):
        print(a.kv("Dashboard", dash))


def _print_version_row() -> None:
    """Version row (2026-10-08): the first `sibyl status` row, in both branches, so a report
    pasted from status always carries the build."""
    client = _installed_version("sibyl-memory-client") or "not installed"
    print(a.eyebrow("plugin"))
    print(a.kv("Version", f"cli {_client_version()} \u00b7 client {client}"))


def cmd_status(args: argparse.Namespace) -> int:
    """Show local + server-side state. Runs one access check-in when online,
    which may renew entitlement.json and the saved tier; nothing else changes.

    LIGHT treatment: utilitarian dashboard. No banner, no section header,
    no chrome. Eyebrow labels + kv rows + ↓ status drift surfaces. Same
    convention as `git status`, `ls -la`, `btop` panel bodies."""
    cred_path = Path(args.credentials).expanduser()
    creds = read_credentials(cred_path)

    print()

    if not creds:
        _print_version_row()
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        print(a.dim("  agents cannot use memory until you do. your local data is kept."))
        pre = _default_tenant_hint(Path(args.db).expanduser(), None)
        _h.handoff(_init_next(), later=("sibyl memory adopt",) if pre else ())
        return 0

    # 0.5.0 account gate (spec 3.4): one refresh attempt when online (the
    # refresh IS the status check), then the state from the entitlement.
    # Shown-tier fix (2026-10-08): taken BEFORE the local rows, so the Tier row shows
    # the current verified pass (the refresh also rewrites the saved tier),
    # falling back to the saved tier only without a valid pass.
    ent_info = _gate.status_info(cred_path, refresh=True)
    creds = read_credentials(cred_path) or creds
    tier_shown = _gate.display_tier(ent_info, creds.get("tier"))

    _print_version_row()

    # Local view
    print(a.eyebrow("local"))
    print(a.kv("Credentials", str(cred_path)))
    print(a.kv("Account", creds.get("account_id") or "—"))
    print(a.kv("Tier", _tier_display(tier_shown), value_color="accent"))
    print(a.kv("Wallet", creds.get("wallet") or "—"))
    print(a.kv("Email", creds.get("email") or "—"))
    print(a.kv("Issued", _utc_minute(creds.get("issued_at")) or creds.get("issued_at") or "—"))
    _family, plat_label, plat_ok = _platform_info()
    print(a.kv("Platform", plat_label if plat_ok else f"{plat_label} \u00b7 unsupported",
               value_color="ink" if plat_ok else "warn"))

    db_path = Path(args.db).expanduser()
    if db_path.exists():
        # CLI-3 / D3: a path that exists but is not a SQLite DB is labeled
        # explicitly instead of being reported as a normal memory store.
        if not is_sqlite_db(db_path):
            # Not a DB: report the raw file size, since the logical SQLite
            # measure does not apply to an arbitrary file.
            size = db_path.stat().st_size
            print(a.kv("DB path", str(db_path)))
            print(a.kv("DB size", f"{size:,} bytes (not a SQLite database)", value_color="err"))
        else:
            # B001 (audit #13): report the same WAL-inclusive logical footprint
            # the cap gate enforces (sibyl_memory_client.storage.db_size_bytes),
            # not the raw memory.db st_size. The raw file under-reports during a
            # write burst (committed pages still in memory.db-wal), so the
            # displayed size/percentage would otherwise disagree with the gate.
            from sibyl_memory_client.storage import db_size_bytes
            from sibyl_memory_client._capcheck import FREE_TIER_CAP_BYTES
            from sibyl_memory_client.tiers import is_paid

            size = db_size_bytes(db_path)
            # Panel fix 2026-09-04: the divisor was a hardcoded pre-2026-08-06
            # 2 MiB; the free cap is FREE_TIER_CAP_BYTES (5 MiB). And a paid
            # tier has no cap, so no percentage is shown for it.
            tier_now = tier_shown or "free"
            if is_paid(tier_now):
                size_label = f"{size:,} bytes ({size / (1024 * 1024):.2f} MB · uncapped)"
                size_color = "soft"
            else:
                pct = size / FREE_TIER_CAP_BYTES * 100
                size_label = f"{size:,} bytes ({size / (1024 * 1024):.2f} MB · {pct:.1f}% of free cap)"
                size_color = "warn" if pct > 80 else "soft"
            print(a.kv("DB path", str(db_path)))
            print(a.kv("DB size", size_label, value_color=size_color))
    else:
        print(a.kv("DB path", f"{db_path} (not created)"))
    pre_rows = _default_tenant_hint(db_path, creds)

    print()
    print(a.eyebrow("access"))
    if ent_info is None:
        print(a.kv("Access", "unknown · sibyl-memory-client is not installed", value_color="warn"))
        nxt, human = "pip install -U sibyl-memory-client", None
    else:
        nxt, human = _gate.access_handoff(ent_info)
        row = _gate.access_row(ent_info)
        color = ("ok" if ent_info.get("state") == "fresh"
                 else "warn" if ent_info.get("state") in ("stale", "bootstrap") else "err")
        print(a.kv("Access", row, value_color=color))
        plan = _gate.plan_row(ent_info)
        if plan:
            print(a.kv("Plan", plan, value_color="warn" if " \u00b7 ends " in plan else "ink"))
        comp = _computers_label(cred_path)
        if comp:
            print(a.kv("Computers", comp,
                       value_color="warn" if "over the limit" in comp else "ink"))

    # All resolvable stores on this machine (split-brain visibility floor,
    # beta report 2026-06-11). Read-only: lists what exists, moves
    # nothing. A divergence warning fires when more than one store holds data,
    # because that is exactly when an agent "loses" memory by switching the
    # entry point it reads from.
    stores = _discover_stores(db_path)
    split_n = 0
    if len(stores) > 1:
        print()
        print(a.eyebrow("memory stores"))
        for s in stores:
            mb = s["size"] / (1024 * 1024)
            print(a.kv(s["label"], f"{s['path']}  ({s['size']:,} bytes · {mb:.2f} MB)"))
        with_data = [s for s in stores if s["size"] > 0]
        if len(with_data) > 1:
            split_n = len(with_data)
            print()
            print(a.warn_line("Multiple memory stores hold data on this machine."))
            print(a.dim("  Memory is NOT shared across these paths. An agent reads only the store"))
            print(a.dim("  for its entry point (SDK/CLI vs Hermes vs profile vs MCP), so memory can"))
            print(a.dim("  look 'missing' when you switch. Point every entry point at one path via"))
            print(a.dim("  --db / SIBYL_MEMORY_DB, or back up and consolidate before relying on recall."))

    tier_cache = Path(args.tier_cache).expanduser()
    if tier_cache.exists():
        # CLI-4: a corrupt tier_cache.json must not crash `sibyl status` —
        # same crash class as D1, separate call site. Degrade to empty dict.
        try:
            cache = json.loads(tier_cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        # checked_at is written by _capcheck.py as epoch seconds (float), but
        # older caches / future formats may carry an ISO string. Render both;
        # never index a float (TypeError on every `sibyl status` run with a
        # populated tier cache; Discord report 2026-06-10).
        checked = cache.get("checked_at")
        checked = _utc_minute(checked) or (str(checked)[:19] if checked else "?")
        cached_tier = cache.get("tier", "?")
        if (_gate.has_valid_pass(ent_info)
                and _tier_display(cached_tier) != _tier_display(tier_shown)):
            # Shown-tier fix: one tier per screen; the pass decides, the cache is old.
            print(a.kv("Tier cache", f"superseded by the access pass (checked {checked})"))
        else:
            print(a.kv("Tier cache", f"{cached_tier} (checked {checked})"))
    else:
        print(a.kv("Tier cache", "—"))

    # Server view (only if account_id + session_token are present)
    drift = False
    server_err: int | None = None
    if creds.get("account_id") and creds.get("session_token"):
        print()
        print(a.eyebrow("server"))
        try:
            resp = http_request(
                "POST",
                "/api/plugin/access",
                body={"account_id": creds["account_id"], "session_token": creds["session_token"]},
                timeout=10.0,
            )
            print(a.kv("Tier", _tier_display(resp.get("tier")), value_color="accent"))
            print(a.kv("Source", resp.get("source") or "—"))
            print(a.kv("Cap bytes", _fmt_cap_bytes(resp.get("cap_bytes"))))
            if resp.get("expires_at"):
                print(a.kv("Expires", _utc_minute(resp["expires_at"]) or str(resp["expires_at"])))
            if resp.get("staker"):
                s = resp["staker"]
                print(a.kv("$SIBYL held", str(s.get("total_sibyl", "—"))))
                print(a.kv("Threshold", str(s.get("threshold_sibyl", "—"))))
                print(a.kv("Qualified", "yes" if s.get("qualified") else "no",
                           value_color="ok" if s.get("qualified") else "soft"))
            _print_account_rows(resp, creds)
            # Detect server/local drift
            # Compared as display words, so a legacy local label (stake) and
            # the server's canonical one (pro) are not reported as drift.
            srv_tier = _tier_display(resp.get("tier"))
            loc_tier = _tier_display(tier_shown)
            if srv_tier != loc_tier:
                print()
                print(a.warn_line(f"Local tier ({loc_tier}) differs from server tier ({srv_tier})."))
                print(a.dim("  Run `sibyl refresh` to pick up the server plan, or `sibyl init --force` to re-activate."))
                drift = True
        except HttpError as e:
            server_err = e.status
            claim_line = _claim_signout_line(e)
            if claim_line:
                print(a.kv("Tier", "signed out", value_color="err"))
                print(a.dim(f"  {claim_line}"))
            else:
                print(a.kv("Tier", f"server error: {e.status}", value_color="err"))
            if e.status == 0 and ent_info and isinstance(ent_info.get("acct"), dict):
                # Offline: the last known account state, from the entitlement.
                when = ent_info.get("received_at")
                as_of = _utc_minute(when) or "?"
                print(a.dim(f"  as of {as_of} (last check-in):"))
                _print_account_rows({"account": ent_info["acct"]}, creds)

    # Save and debloat (spec plugin-save-debloat 4.3): local only, no network.
    from . import lifecycle_cli as _lc
    _lc.status_rows(args, creds)

    print()
    # Agent handoff (rows S3 to S12): the access state first; a healthy state
    # gives way to an unreachable server, then tier drift, then split stores.
    if nxt == "done":
        if server_err is not None:
            nxt, human = "sibyl refresh", (OFFLINE_HUMAN if server_err == 0 else None)
        elif drift:
            nxt = "sibyl refresh"
        elif split_n:
            human = (f"memory is split across {split_n} stores (listed above); choose which path "
                     "every agent should use")
    _h.handoff(nxt, human=human, later=("sibyl memory adopt",) if pre_rows else ())
    return 0


def _adopt_after_sign_in(db_path: Path, cred_path: Path) -> bool:
    """First sign-in on this computer: memory written before sign-in
    (DEFAULT_TENANT) moves into the account, in one local transaction (client
    _adopt, the same rule the agents apply on their first operation). Never
    fails `sibyl init`; a store that has held another account is left for
    `sibyl memory adopt`, which asks first. Returns True when rows were left
    for that command (row I14: `later: sibyl memory adopt`)."""
    try:
        res = _gate.adopt_pre_signin(db_path, cred_path, trigger="auto")
    except Exception:
        return False
    if res is None:
        return False
    if res.status == "moved":
        print(a.kv("Local memory", f"{res.moved} rows from before sign-in moved into your account",
                   value_color="ok"))
        if res.renamed:
            print(a.dim(f"  {len(res.renamed)} kept under a new name ending in (before sign-in): "
                        "your account already had that key"))
    elif res.status == "skipped" and res.reason == "other_account":
        print(a.warn_line("This computer's memory store also holds another account's memory."))
        print(a.dim("  Rows written before sign-in were not moved. " + _gate.ADOPT_HINT
                    + " (it asks first)."))
        return True
    return False


def _install_skills_quietly() -> None:
    """Save/debloat build (spec 3.2): the save and debloat procedures land in
    ~/.sibyl-memory/skills/ the moment an account exists (never overwriting a
    person's edit). One line; never fails `sibyl init`."""
    try:
        from sibyl_memory_client import skills as _sk
        rep = _sk.install(Path.home())
    except Exception:
        return
    state = "ready" if rep.ok else "see sibyl skills status"
    print(a.kv("Save + debloat", f"procedures v{rep.version} {state} · wire them with sibyl setup"))
    for m in rep.messages:
        print(a.dim("  " + m))


def _default_tenant_hint(db_path: Path, creds: dict | None) -> int:
    """Spec 3.5: a one-line hint when the default store holds entities written
    under DEFAULT_TENANT before an account existed (a COUNT only, no content,
    read-only sqlite, no gate, no network). Returns the count (row S12)."""
    try:
        from sibyl_memory_client import DEFAULT_TENANT
    except Exception:
        return 0
    active = (creds or {}).get("tenant_id") or (creds or {}).get("account_id")
    if active == DEFAULT_TENANT or not db_path.exists() or not is_sqlite_db(db_path):
        return 0
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM entities WHERE tenant_id = ?",
                            (DEFAULT_TENANT,)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    if n:
        print(a.dim(f"  {n} entities from before sign-in are kept under the default tenant; "
                    f"agents signed in to your account do not see them yet."))
        if active:
            print(a.dim("  " + _gate.ADOPT_HINT + "."))
    return int(n or 0)


# ---- `sibyl dashboard` -------------------------------------------------

def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the account page the server advertises (features.web_dashboard_url).

    No token rides the URL, ever: you sign in on the page with your confirmed
    email or your wallet. 0.4.x opened SIBYL_DASHBOARD_BASE with the bearer in
    the query string, which the account page never read; that path is gone.
    When the server advertises no account page (or cannot be reached), this
    falls through to `sibyl status` as before.
    """
    creds = read_credentials(Path(args.credentials).expanduser())
    access = _fetch_access(creds) if creds and creds.get("account_id") and \
        (creds.get("bearer_token") or creds.get("session_token")) else None
    url = _features_of(access).get("web_dashboard_url")
    if _on_account_origin(url):
        account = _account_of(access)
        print()
        print(a.kv("Opening", url))
        print(a.dim("  sign in with your confirmed email (or your wallet)."))
        dash_human = "sign in on the account page with your confirmed email or wallet"
        if account.get("email_verified") is False:
            print(a.dim("  your account has no confirmed email yet: sign in with your wallet and add one there,"))
            print(a.dim("  or run sibyl email add."))
            dash_human += "; add an email there or tell me one"
        print()
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
        _h.handoff("done", human=dash_human)
        return 0
    if access is not None:
        print()
        print(a.dim("  the account page is not open yet."))
    return cmd_status(args)


# ---- `sibyl whoami` ----------------------------------------------------

def _mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return "—"
    user, _, domain = e.partition("@")
    if "." not in domain:
        return f"{user[0]}***@{domain[0]}***"
    name, _, tld = domain.rpartition(".")
    return f"{user[0]}***@{name[0]}***.{tld}"


def _mask_wallet(w: str | None) -> str:
    if not w or not w.startswith("0x") or len(w) < 12:
        return w or "—"
    return f"{w[:6]}…{w[-4:]}"


def cmd_whoami(args: argparse.Namespace) -> int:
    """One-line account summary. Shows account_id + tier + linked email/wallet + this device.

    LIGHT treatment: 4-line glance. No banner, no section header. Same shape
    as `whoami` on unix, `gh auth status`, `aws sts get-caller-identity`."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        _h.handoff(_init_next())
        return 1

    full = bool(getattr(args, "full", False))
    acct = creds.get("account_id") or ""
    tier = _tier_display(_gate.peek_tier(Path(args.credentials).expanduser(),
                                         creds.get("tier"))).upper()
    email = creds.get("email") if full else _mask_email(creds.get("email"))
    wallet = creds.get("wallet") if full else _mask_wallet(creds.get("wallet"))

    print()
    print(f"  {a.color('account', a.INK_FAINT)}  {a.bold(acct or "—")}  {a.dim(a.GLYPH_DOT)}  {a.gradient_gold(tier)}")
    print(f"  {a.color('wallet ', a.INK_FAINT)}  {a.color(wallet or '—', a.INK)}")
    print(f"  {a.color('email  ', a.INK_FAINT)}  {a.color(email or '—', a.INK)}")
    _family, plat_label, plat_ok = _platform_info()
    device_line = f"sibyl-memory-cli/{_client_version()} {plat_label}"
    if not plat_ok:
        device_line += " \u00b7 unsupported"
    print(f"  {a.color('device ', a.INK_FAINT)}  {a.dim(device_line)}")
    print()
    _h.handoff("done")
    return 0


# ---- `sibyl claim` -----------------------------------------------------

def _looks_like_checkout_code(cs: str) -> bool:
    """Mirror of the server's CS_ID_RE without importing re: cs_ prefix,
    optional test_/live_ marker, then 10..240 alphanumerics."""
    if not cs.startswith("cs_"):
        return False
    rest = cs[3:]
    for marker in ("test_", "live_"):
        if rest.startswith(marker):
            rest = rest[len(marker):]
            break
    return 10 <= len(rest) <= 240 and rest.isalnum()


def cmd_claim(args: argparse.Namespace) -> int:
    """Link a card purchase (Stripe checkout) to this account.

    Buy-before-install: the page after checkout shows the buyer their
    checkout code (cs_...). This redeems it onto the account activated on
    this machine — email or wallet activation both work. The bearer rides
    along as session_token so the server verifies the caller controls the
    destination account, not just the receipt.
    """
    cs = (getattr(args, "checkout_code", "") or "").strip()
    if not _looks_like_checkout_code(cs):
        print(a.warn_line("That does not look like a checkout code."))
        print(a.dim("  It starts with cs_ and appears on the page you landed on after paying"))
        print(a.dim(f"  ({CLAIM_PAGE}). Copy the whole code."))
        _h.handoff("sibyl claim <cs_code>", human="the whole cs_ code from the page after paying "
                                                  f"({CLAIM_PAGE})")
        return 1

    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds or not creds.get("account_id"):
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init` first, then `sibyl claim` again."))
        _h.handoff(_init_next(), later=(f"sibyl claim {cs}",))
        return 1
    account_id = creds["account_id"]

    body: dict = {"cs": cs, "account_id": account_id}
    token = creds.get("bearer_token") or creds.get("session_token")
    if token:
        body["session_token"] = token

    try:
        resp = http_request("POST", "/api/plugin/stripe-claim", body=body,
                            headers=_bearer_headers(creds))
    except HttpError as e:
        msg = e.body.get("error") if isinstance(e.body, dict) else None
        print(a.warn_line(f"Claim failed ({e.status or 'network'})."))
        gate_line = _gate.gate_http_line(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
        if gate_line:
            print(a.dim(f"  {gate_line}"))
            gnext, ghuman = _gate.gate_http_handoff(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
            _h.handoff(gnext, human=ghuman, later=(f"sibyl claim {cs}",))
            return 1
        if msg:
            print(a.dim(f"  {msg}"))
        again = (f"sibyl claim {cs}",)
        if e.code in ("unclaimed_account", "email_required"):
            if creds.get("email"):
                claim_h = ("sibyl email verify", _verify_human(_mask_email(creds.get("email"))))
            else:
                claim_h = ("sibyl email add <address>", "the email address you used to buy")
        elif e.status == 403:
            claim_h = (_init_next(), None)
        elif e.status == 0:
            claim_h = (_h.RETRY, OFFLINE_HUMAN)
            again = ()
        else:
            claim_h = ("sibyl claim <cs_code>", f"the purchase could not be linked ({msg or e.status}); "
                                                "check the code on the welcome page")
            again = ()
        # 0.5.0: branch on the server's machine code, not the status alone.
        if e.code == "unclaimed_account":
            if creds.get("email"):
                print(a.dim("  fastest path: sibyl email verify (confirms the email you used here and keeps"))
                print(a.dim(f"  this computer signed in), then sibyl claim {cs} again."))
            else:
                # Release-blocker F7: no email on file, so there is nothing to verify.
                print(a.dim("  fastest path: sibyl email add you@example.com (confirms an email from this"))
                print(a.dim(f"  computer and keeps it signed in), then sibyl claim {cs} again."))
        elif e.code == "email_required":
            print(a.dim(f"  then run sibyl claim {cs} again."))
        elif e.status == 403:
            print(a.dim("  This session does not match the account. Run `sibyl init`, then retry."))
        elif e.status == 0:
            print(a.dim("  Could not reach the server. Check your connection and retry."))
        _h.handoff(claim_h[0], human=claim_h[1], later=again)
        return 1

    tier = _tier_display(resp.get("tier") or "pro").upper()
    print()
    print(a.success_line(f"Purchase linked. Tier: {tier}"))
    if resp.get("expires_at"):
        print(a.kv("Covers through", _utc_minute(resp["expires_at"]) or str(resp["expires_at"])))
    print(a.dim("  Run the next command and agents pick it up on their next call."))
    print()
    _h.handoff("sibyl refresh")

    # Sync local state so the very next write sees the new tier. Signed fields
    # never change under an old signature (release-blocker F1): the signature
    # and signed_at are dropped with the edit (hardening L2). The server is the
    # tier authority either way.
    try:
        _unsign_for_local_edit(creds)
        creds["tier"] = resp.get("tier") or "pro"
        write_credentials_atomic(creds, Path(args.credentials).expanduser())
    except Exception:
        pass  # server is the source of truth; local hint refreshes on next access check
    try:
        invalidate_tier_cache()
    except Exception:
        pass
    return 0


# ---- `sibyl email` -----------------------------------------------------

def cmd_email(args: argparse.Namespace) -> int:
    """Show, add or confirm the account's email (0.5.0).

        sibyl email                  state: confirmed / not confirmed / none
        sibyl email add <address>    send a code to a new address, then confirm it
        sibyl email verify           send a code to the address on file, then confirm it
        sibyl email confirm <code>   confirm a code sent earlier

    Confirming from this computer keeps it signed in and signs out every other
    computer on the account that was set up before (someone who paired your
    address first loses theirs; you keep yours).
    """
    cred_path = Path(args.credentials).expanduser()
    creds = read_credentials(cred_path)
    sub = getattr(args, "email_cmd", None)

    if not creds or not creds.get("account_id") or not (creds.get("bearer_token") or creds.get("session_token")):
        print()
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        print()
        asked = " ".join(x for x in ("sibyl email", sub, (getattr(args, "address", "") or "").strip()
                                     if sub == "add" else (getattr(args, "code", "") or "").strip()
                                     if sub == "confirm" else "") if x) if sub else ""
        _h.handoff(_init_next() if not creds or _CREDS_CORRUPT else "sibyl init --force",
                   later=(asked,) if asked else ())
        return 0 if sub is None else 1

    print()
    if sub is None:
        access, err = _fetch_access_ex(creds)
        if access is None:
            if err is not None and err.status == 401:
                claim_line = _claim_signout_line(err)
                print(a.warn_line(claim_line or "session expired. re-run sibyl init."))
                print()
                _h.handoff(_gate.SIGNED_OUT_ON_CLAIM_NEXT if claim_line else "sibyl init")
                return 1
            offline = err is None or err.status == 0
            print(a.warn_line("could not reach the server." if offline
                              else f"server error: {err.status}"))
            print()
            _h.handoff(_h.RETRY, human=OFFLINE_HUMAN if offline else None)
            return 2
        account = _account_of(access)
        if not account:
            print(a.dim("  this server does not report email state yet."))
            print()
            _h.handoff("done")
            return 0
        masked = account.get("email_masked") if isinstance(account.get("email_masked"), str) else None
        if masked is None and creds.get("email"):
            masked = _mask_email(creds.get("email"))
        web_only = _features_of(access).get("cli_email_verify") is not True
        if account.get("email_verified") is True:
            print(a.kv("Email", f"confirmed {masked or ''}".rstrip(), value_color="ok"))
            state_h = ("done", None)
        elif web_only:
            # Row E11: the terminal cannot add or confirm on this api, so
            # `sibyl email add` / `verify` would loop on not_open.
            print(a.kv("Email", f"not confirmed {masked}" if masked else "none", value_color="warn"))
            print(a.dim(f"  add and confirm it at {ACCOUNT_BASE}"))
            state_h = ("done", _email_web_human())
        elif masked:
            print(a.kv("Email", f"not confirmed {masked}", value_color="warn"))
            print(a.dim("  run sibyl email verify"))
            state_h = ("sibyl email verify", _verify_human(masked))
        else:
            print(a.kv("Email", "none", value_color="warn"))
            print(a.dim("  run sibyl email add <address>"))
            state_h = ("sibyl email add <address>", "an email address for the account")
        print()
        _h.handoff(state_h[0], human=state_h[1])
        return 0

    interactive = _is_interactive()
    if sub == "add":
        address = (getattr(args, "address", "") or "").strip()
        if not _looks_like_email(address):
            print(a.warn_line("that does not look like an email address."))
            print(a.dim("  usage: sibyl email add you@example.com"))
            print()
            _h.handoff("sibyl email add <address>",
                       human="a valid email address (the one given was not one)")
            return 1
        rc, hand = _email_request_and_prompt_h(creds, address, cred_path, interactive=interactive)
    elif sub == "verify":
        rc, hand = _email_request_and_prompt_h(creds, None, cred_path, interactive=interactive)
    elif sub == "confirm":
        rc, _err, hand = _email_confirm_once_h(creds, getattr(args, "code", "") or "", cred_path)
    else:
        print(a.warn_line("usage: sibyl email [add <address> | verify | confirm <code>]"))
        rc, hand = 1, ("sibyl email", None)
    print()
    _h.handoff(hand[0], human=hand[1])
    return rc


# ---- `sibyl devices` ---------------------------------------------------

def _devices_error_line(e: HttpError) -> str:
    """Readable devices/revoke failure. The tamper 403 gets its own copy
    instead of a raw body dump."""
    if e.status == 403 and isinstance(e.body, dict) and e.body.get("reason") == "tamper_flag_active":
        return "this account is under review. email sibyl@sibyllabs.org."
    gate_line = _gate.gate_http_line(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
    if gate_line:
        return gate_line
    if e.status == 0:
        return "could not reach the server."
    return f"server error: {e.status} {e.message or e.body}"


def _devices_error_handoff(e: HttpError) -> tuple:
    """(next, human) for a devices / revoke failure (row V4), same branch
    order as ``_devices_error_line``."""
    if e.status == 403 and isinstance(e.body, dict) and e.body.get("reason") == "tamper_flag_active":
        return _gate.code_handoff("under_review")
    gate = _gate.gate_http_handoff(e.status, e.code, cred_path=_ENT_CRED_PATH, body=e.body)
    if gate is not None:
        return gate
    if e.status == 0:
        return (_h.RETRY, OFFLINE_HUMAN)
    return (_h.RETRY, None)


def cmd_devices(args: argparse.Namespace) -> int:
    """List active bearer tokens (devices) for the account. Optional: revoke by index."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.err_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        _h.handoff(_init_next())
        return 1
    account_id = creds.get("account_id")
    session_token = creds.get("session_token")
    if not account_id or not session_token:
        print(a.err_line("credentials.json missing account_id or session_token."))
        print(a.dim("  Run `sibyl init`."))
        _h.handoff("sibyl init --force")
        return 1

    sub = getattr(args, "sub", None)

    # `sibyl devices revoke <index>` path
    if sub == "revoke":
        idx = getattr(args, "index", None)
        if idx is None:
            print(red("usage: sibyl devices revoke <index>"))
            _h.handoff("sibyl devices")
            return 1
        # CLI-5: reject negative indexes. Python's negative indexing means
        # `revoke -1` would silently target the LAST device — a footgun that
        # could revoke the wrong (or your own) device. Require an explicit
        # non-negative index from `sibyl devices` output.
        if idx < 0:
            print(red(f"invalid index {idx}. Use a non-negative index from `sibyl devices`."))
            _h.handoff("sibyl devices")
            return 1
        # List first to map index → bearer_id
        try:
            resp = _security_request(
                "GET", f"/api/plugin/devices?account_id={urllib.parse.quote(account_id)}", creds)
        except HttpError as e:
            print(red(_devices_error_line(e)))
            dn, dh = _devices_error_handoff(e)
            _h.handoff(dn, human=dh)
            return 2
        devices = resp.get("devices", [])
        try:
            target = devices[idx]
        except (IndexError, TypeError):
            print(red(f"no device at index {idx}. Run `sibyl devices` to see indexes."))
            _h.handoff("sibyl devices")
            return 1
        if not isinstance(target, dict):
            print(red(f"malformed device record at index {idx}. Run `sibyl devices`."))
            _h.handoff("sibyl devices")
            return 2
        if target.get("is_this_device"):
            print(red("refusing to revoke your own device — that would lock you out. Run `sibyl logout` instead, then `sibyl init` on a fresh activation."))
            _h.handoff("sibyl logout", human="that is this computer; signing it out is sibyl logout, "
                                             "which removes local credentials; confirm")
            return 1
        # CLI-5: bail cleanly if the server payload lacks bearer_id instead of
        # raising KeyError on hostile/malformed data.
        bearer_id = target.get("bearer_id")
        if not bearer_id:
            print(red(f"device at index {idx} has no bearer_id. Run `sibyl devices` to refresh."))
            _h.handoff("sibyl devices")
            return 2
        try:
            revoke_resp = _security_request(
                "POST", "/api/plugin/devices", creds, body={"bearer_id": bearer_id})
        except HttpError as e:
            print(red(f"revoke failed: {_devices_error_line(e)}"))
            dn, dh = _devices_error_handoff(e)
            _h.handoff(dn, human=dh)
            return 2
        name = target.get('device_label') or bearer_id
        if isinstance(revoke_resp, dict) and revoke_resp.get("already_revoked") is True:
            print(a.dim(f"  {name}: already revoked"))
            _h.handoff("sibyl devices")
            return 0
        print(green(f"✓ Revoked device {name}"))
        _h.handoff("sibyl devices")
        return 0 if isinstance(revoke_resp, dict) and revoke_resp.get("revoked") else 1

    # Default: list devices
    try:
        resp = _security_request(
            "GET", f"/api/plugin/devices?account_id={urllib.parse.quote(account_id)}", creds)
    except HttpError as e:
        if _claim_signout_line(e):
            print(a.err_line(_claim_signout_line(e)))
            _h.handoff(_gate.SIGNED_OUT_ON_CLAIM_NEXT)
        elif e.status == 401:
            print(a.err_line("Session expired."))
            print(a.dim("  Re-run `sibyl init`."))
            _h.handoff("sibyl init")
        else:
            print(a.err_line(_devices_error_line(e)))
            dn, dh = _devices_error_handoff(e)
            _h.handoff(dn, human=dh)
        return 2

    devices = resp.get("devices", [])
    # LIGHT treatment: table-like dashboard. Eyebrow line with count + the rows. No banner.
    print()
    limit_note = _computers_label(Path(args.credentials).expanduser(), resp)
    print(f"  {a.eyebrow('devices')}  {a.dim(f'· {len(devices)} active')}"
          + (f"  {a.dim('· ' + limit_note)}" if limit_note else ""))
    print()
    if not devices:
        print(a.dim("  no active devices"))
        print()
        _h.handoff("sibyl init")
        return 0

    for i, d in enumerate(devices):
        is_this = d.get("is_this_device")
        marker = a.ok("▶") if is_this else " "
        label = d.get("device_label") or "(unlabeled)"
        installed = d.get("install_method") or "—"
        last_seen = _utc_minute(d.get("last_seen_at")) or "unknown"
        idx_chip = a.chip(str(i), palette="jade" if is_this else "mute")
        label_color = a.gradient_gold(label) if is_this else a.color(label, a.INK)
        meta = f"{a.dim(installed)} {a.dim(a.GLYPH_DOT)} {a.dim('last seen ' + last_seen)}"
        note = a.color("(this device)", a.PULSE) if is_this else a.dim(f"revoke: sibyl devices revoke {i}")
        print(f"  {marker} {idx_chip} {label_color}  {meta}  {note}")
    print()
    comp = _computers_info(Path(args.credentials).expanduser(), resp)
    if comp is not None and comp["over"]:
        from sibyl_memory_client.tiers import is_paid
        _h.handoff("sibyl devices revoke <index>",
                   later=() if is_paid(comp["tier"]) else ("sibyl upgrade",),
                   human="which computer to sign out (the index is the number in the row), or get Pro")
    else:
        _h.handoff("done")
    return 0


def _computers_info(cred_path: Path, devices_resp: Any = None) -> dict | None:
    """``{n, limit, over, tier}`` for this account: ``acct.devices`` of the
    verified entitlement (plan 4.2; read locally, no network), with the tier
    from the same token. A /devices response's own ``limit`` (an int), when
    an api sends one, fills a missing limit. None when unknown."""
    try:
        info = _gate.status_info(cred_path, refresh=False) or {}
    except Exception:
        info = {}
    from sibyl_memory_client.exceptions import device_limit_info
    d = device_limit_info(info.get("acct"))
    if d is None:
        return None
    if d["limit"] is None and isinstance(devices_resp, dict):
        lim = devices_resp.get("limit")
        if isinstance(lim, int) and not isinstance(lim, bool) and lim >= 0:
            d["limit"] = lim
    d["tier"] = info.get("tier")
    return d


def _computers_label(cred_path: Path, devices_resp: Any = None) -> str | None:
    """`1 of 1 (Free) · Pro: up to 10` (plan 4.4), or None when the api has not
    sent the computer count yet."""
    d = _computers_info(cred_path, devices_resp)
    if d is None:
        return None
    from sibyl_memory_client.tiers import is_paid
    paid = is_paid(d["tier"])
    if d["limit"] is None:
        text = f"{d['n']} signed in"
    else:
        text = f"{d['n']} of {d['limit']} ({_tier_display(d['tier'])})"
    if not paid:
        text += f" · Pro: up to {PRO_DEVICE_LIMIT_HINT}"
    if d["over"]:
        text += " · over the limit: new memories save only on the computer that signed in most recently"
    return text


# ---- `sibyl logout` ----------------------------------------------------

# Real #4 (audit): the same offline caveat wherever a logout revoke can't be
# confirmed — mirrors the `sibyl devices revoke` remediation path.
_LOGOUT_REVOKE_CAVEAT = (
    "remote session may still be active; run `sibyl devices revoke` from another device"
)


def _logout_revoke_status(creds: dict) -> tuple[str, str | None]:
    """Best-effort revoke THIS device's server bearer before local logout.

    Real #4 (audit): `sibyl logout` used to unlink local credentials only, so
    the bearer (which has no server-side expiry) stayed valid forever after
    logout. This revokes it first, reusing the EXACT endpoint + auth shape of
    `sibyl devices revoke` (no new endpoint invented):

        GET  /api/plugin/devices?account_id=...   (Authorization: Bearer <token>)
        POST /api/plugin/devices  {"bearer_id": ...}  (same Bearer auth)

    credentials.json stores only the bearer TOKEN, not its server-side
    bearer_id, so we list devices to find THIS one (``is_this_device``) and
    revoke it by id, identical to the interactive revoke flow.

    Returns ("revoked" | "nothing" | "unconfirmed", caveat or None): the
    caveat is None on a confirmed revoke (or when there is nothing to revoke).
    0.5.0: lets logout say "signed out on the server too." only when the
    server confirmed. Network failure is swallowed but reported (never
    crashes logout).
    """
    account_id = creds.get("account_id")
    session_token = creds.get("session_token")
    if not account_id or not session_token:
        # Pre-activation / malformed creds: nothing server-side to revoke.
        return "nothing", None
    try:
        resp = _security_request(
            "GET", f"/api/plugin/devices?account_id={urllib.parse.quote(account_id)}", creds)
        devices = resp.get("devices", []) if isinstance(resp, dict) else []
        this = next(
            (d for d in devices if isinstance(d, dict) and d.get("is_this_device")),
            None,
        )
        bearer_id = this.get("bearer_id") if this else None
        if not bearer_id:
            # Couldn't identify this device's server record — can't confirm.
            return "unconfirmed", _LOGOUT_REVOKE_CAVEAT
        revoke_resp = _security_request(
            "POST", "/api/plugin/devices", creds, body={"bearer_id": bearer_id})
        if isinstance(revoke_resp, dict) and revoke_resp.get("revoked"):
            return "revoked", None
        return "unconfirmed", _LOGOUT_REVOKE_CAVEAT
    except Exception:
        # Best-effort: swallow ANY network/HTTP failure, but report it so the
        # user knows the remote bearer may still be live.
        return "unconfirmed", _LOGOUT_REVOKE_CAVEAT


def cmd_logout(args: argparse.Namespace) -> int:
    """Delete credentials.json + tier_cache.json. memory.db stays — that's your data."""
    cred_path = Path(args.credentials).expanduser()
    tier_cache = Path(args.tier_cache).expanduser()

    # Real #4: revoke THIS device's server bearer BEFORE unlinking local creds
    # (once credentials.json is gone we no longer have the token to authorize
    # the revoke). Best-effort; a failure only produces a printed caveat.
    revoke_caveat = None
    revoke_state = "nothing"
    creds = read_credentials(cred_path)
    if creds:
        revoke_state, revoke_caveat = _logout_revoke_status(creds)

    deleted = []
    if cred_path.exists():
        cred_path.unlink()
        deleted.append(str(cred_path))
    # 0.5.0: the entitlement goes with the credentials (spec 3.4).
    deleted.extend(_gate.remove_entitlement(cred_path))
    if tier_cache.exists():
        tier_cache.unlink()
        deleted.append(str(tier_cache))

    # LIGHT treatment: quick confirmation. No banner, no section header.
    print()
    if not deleted:
        print(a.warn_line("Nothing to remove."))
        print(a.dim("  Already logged out."))
    else:
        print(a.success_line("Logged out."))
        for path in deleted:
            print(f"  {a.dim('removed')} {a.color(path, a.INK)}")
        print()
        print(a.dim("  memory.db untouched. run `sibyl init` to activate a fresh account."))
        print(a.dim("  agents cannot use memory until you sign in again. `sibyl memory` still reads it."))
    if revoke_state == "revoked":
        print(a.dim("  signed out on the server too."))
    if revoke_caveat:
        print(a.warn_line(revoke_caveat))
    print()
    # Rows O1 to O3: the person asked to sign out, so the agent stops here;
    # signing in again stays available as a later step, never the next one.
    _h.handoff("done", human=("this computer's sign-in may still be active on the server; on "
                              "another signed-in computer run sibyl devices and revoke it, or "
                              f"sign it out at {ACCOUNT_SECURITY_PAGE}")
               if revoke_caveat else None, later=("sibyl init",))
    return 0


# ---- `sibyl health` ----------------------------------------------------

def cmd_health(args: argparse.Namespace) -> int:
    """SibylMemoryProvider.health() — minimal self-check."""
    try:
        from sibyl_memory_hermes import SibylMemoryProvider
    except ImportError:
        print(a.err_line("sibyl-memory-hermes not installed."))
        print(a.dim("  pip install sibyl-memory-hermes"))
        _h.handoff("pip install sibyl-memory-hermes")
        return 1

    # LIGHT treatment: verdict + details. No banner, no section header.
    # Pattern: `pg_isready` / `redis-cli ping` / `gh auth status`.
    print()
    # CLI-6: expanduser the db path (a leading ~ was passed through literally),
    # and wrap provider construction + health() so a bad DB / provider error
    # prints a clean line instead of a traceback.
    db_path = Path(args.db).expanduser()
    try:
        provider = SibylMemoryProvider(db_path=str(db_path))
        h = provider.health()
    except Exception as e:
        print(a.err_line(f"Health check failed: {type(e).__name__}: {e}"))
        print()
        _h.handoff("sibyl status")
        return 1
    if not isinstance(h, dict):
        print(a.err_line("Health check returned an unexpected result."))
        print()
        _h.handoff("sibyl status")
        return 1
    ok_state = bool(h.get("ok"))
    if ok_state:
        print(a.success_line("All green."))
    else:
        print(a.err_line("Health check reports issues."))
    print()
    for k, v in h.items():
        if k == "ok":
            continue
        val = str(v)
        print(a.kv(k, val, value_color="ok" if v is True else ("soft" if v else "warn")))
    # 0.5.0 account gate (spec 3.4): the local state and the pinned kids. A
    # report, not a check: it never changes the exit code.
    info = _gate.status_info(Path(args.credentials).expanduser(), refresh=False)
    if info is not None:
        state = info.get("state") or "?"
        if info.get("code") and state == "locked":
            state = f"locked ({info['code']})"
        print(a.kv("entitlement", state, value_color="ok" if state == "fresh" else "warn"))
    print(a.kv("pinned kids", ", ".join(_gate.pinned_kids()) or "none"))
    print()
    if info is not None:
        hn, hh = _gate.access_handoff(info)
    else:
        hn, hh = "pip install -U sibyl-memory-client", None
    if hn == "done" and not ok_state:
        hn = "sibyl status"
    _h.handoff(hn, human=hh)
    return 0 if ok_state else 1


# ---- `sibyl update` ----------------------------------------------------

# Every Sibyl package `sibyl update` knows about. The installer
# (sibyllabs.org/install) installs `sibyl-memory-cli[mcp]`, so the MCP server
# (what Claude Code talks to) is present on every standard install; langgraph
# is installed only by people who use it. `sibyl update` checks and upgrades
# the ones that are INSTALLED and never installs a missing one. The [mcp]
# extra only sets a version floor, so upgrading the CLI alone would leave the
# MCP server behind: it is named in the same pip call instead.
UPDATE_PACKAGES = (
    "sibyl-memory-cli",
    "sibyl-memory-hermes",
    "sibyl-memory-client",
    "sibyl-memory-mcp",
    "sibyl-memory-langgraph",
)


def _installed_version(pkg: str) -> str | None:
    """Return the locally-installed version of a package, or None if not installed."""
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
        try:
            return _v(pkg)
        except PackageNotFoundError:
            return None
    except Exception:
        return None


def _pypi_latest(pkg: str, timeout: float = 4.0) -> str | None:
    """Hit PyPI's JSON endpoint for the latest published version. Best-effort."""
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"sibyl-memory-cli/{_client_version()}"})
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (data.get("info") or {}).get("version")
    except Exception:
        return None


def _ver_tuple(v: str) -> tuple:
    """Lenient version tuple for comparison. Splits on '.', tolerates non-numeric tails."""
    out = []
    for part in (v or "").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _ver_lt(installed: str, latest: str) -> bool:
    """True if `installed` is strictly older than `latest`.

    CLI-13: prefer packaging.version.parse for PEP 440 correctness (rc/dev/post
    tags handled, 1.2 == 1.2.0). If packaging is unavailable (stdlib-only
    environments), fall back to a length-normalized numeric-tuple compare so
    1.2 vs 1.2.0 no longer mis-orders (the old raw-tuple compare made (1,2) <
    (1,2,0), reporting a spurious update)."""
    try:
        from packaging.version import InvalidVersion, parse as _parse
        try:
            return _parse(installed) < _parse(latest)
        except InvalidVersion:
            pass  # fall through to tuple compare on unparseable input
    except ImportError:
        pass
    a_t, b_t = _ver_tuple(installed), _ver_tuple(latest)
    width = max(len(a_t), len(b_t))
    a_t = a_t + (0,) * (width - len(a_t))
    b_t = b_t + (0,) * (width - len(b_t))
    return a_t < b_t


def _detect_install_method() -> str:
    """Best-guess of how the CLI was installed — pipx / venv / system-pip / pep668-blocked."""
    exe = sys.executable
    if "/pipx/" in exe or "/.local/pipx/" in exe:
        return "pipx"
    if exe and ("venv" in exe.lower() or "virtualenv" in exe.lower() or os.environ.get("VIRTUAL_ENV")):
        return "venv"
    # Look for PEP 668 marker file
    for parent in Path(exe).resolve().parents:
        marker = parent / "lib" / "EXTERNALLY-MANAGED"
        if marker.exists():
            return "pep668"
        marker2 = parent / "EXTERNALLY-MANAGED"
        if marker2.exists():
            return "pep668"
        if str(parent) in ("/", "/home", "/usr"):
            break
    return "system"


PEP668_HUMAN = ("this Python is externally managed; choose a venv or allow "
                "--break-system-packages")


def _update_handoff(rows: list, any_outdated: bool) -> tuple:
    """(next, human) for `sibyl update` (rows Z1, Z2, Z5)."""
    if not any_outdated:
        if any(r["latest"] is None for r in rows):
            return (_h.RETRY, None)
        return ("done", None)
    method = _detect_install_method()
    if method == "pipx":
        return ("pipx upgrade " + " ".join(r["pkg"] for r in rows if r["outdated"]), None)
    if method == "pep668":
        return ("sibyl update --apply", PEP668_HUMAN)
    return ("sibyl update --apply", None)


def cmd_update(args: argparse.Namespace) -> int:
    """Check installed package versions against PyPI, optionally apply upgrade."""
    rows = []
    any_outdated = False
    for pkg in UPDATE_PACKAGES:
        installed = _installed_version(pkg)
        if installed is None:
            # Not installed: not shown, not checked, never installed by --apply.
            continue
        latest = _pypi_latest(pkg)
        outdated = False
        if installed and latest:
            outdated = _ver_lt(installed, latest)
        rows.append({"pkg": pkg, "installed": installed, "latest": latest, "outdated": outdated})
        if outdated:
            any_outdated = True

    if args.json:
        doc = {"packages": rows, "any_outdated": any_outdated}
        un, uh = _update_handoff(rows, any_outdated)
        print(json.dumps(_h.json_handoff(doc, un, uh), indent=2))
        return 0 if not any_outdated else 2

    # ASCII output — keep it small and readable, follow `sibyl status` style.
    print()
    if any_outdated:
        print(a.err_line("Updates available."))
    else:
        # Distinguish "all current" from "could not reach PyPI"
        any_unreachable = any(r["latest"] is None for r in rows)
        if any_unreachable:
            print(a.dim("Could not reach PyPI for one or more packages — showing what we know."))
        else:
            print(a.success_line("All packages current."))
    print()

    name_w = max((len(r["pkg"]) for r in rows), default=0)
    for r in rows:
        installed = r["installed"]
        latest = r["latest"] or "(unreachable)"
        if r["outdated"]:
            line = f"  {yellow(r['pkg'].ljust(name_w))}  {installed}  →  {green(latest)}"
        else:
            line = f"  {r['pkg'].ljust(name_w)}  {a.dim(installed)}"
        print(line)
    print()

    if not any_outdated:
        un, uh = _update_handoff(rows, any_outdated)
        _h.handoff(un, human=uh)
        return 0

    pip_cmd_pkgs = " ".join(r["pkg"] for r in rows if r["outdated"])
    method = _detect_install_method()

    if args.apply:
        # Best-effort in-process pip invocation
        import subprocess
        pip_args = [sys.executable, "-m", "pip", "install", "-U", *pip_cmd_pkgs.split()]
        if method == "pep668":
            pip_args.append("--break-system-packages")
        if method == "pipx":
            # pipx is a separate tool; we can't drive it via `pip install`.
            print(a.err_line("Detected pipx install. Run instead:"))
            print(f"    pipx upgrade {' '.join(r['pkg'] for r in rows if r['outdated'])}")
            _h.handoff(f"pipx upgrade {' '.join(r['pkg'] for r in rows if r['outdated'])}")
            return 2
        print(a.dim("Running: ") + " ".join(pip_args))
        try:
            rc = subprocess.call(pip_args)
        except FileNotFoundError:
            print(a.err_line("pip not found at " + sys.executable + " -m pip"))
            _h.handoff("done", human=f"pip is missing for {sys.executable}; install pip or point me "
                                     "at the right Python")
            return 2
        if rc == 0:
            print()
            print(a.success_line("Upgrade complete. Re-run `sibyl update` to confirm."))
            # Save/debloat build (spec 3.2): refresh the installed procedures
            # from the NEW package (a fresh interpreter, so the upgraded code
            # and text are the ones used). Never overwrites a person's edit.
            try:
                subprocess.call([sys.executable, "-c",
                                 "from sibyl_memory_cli.cli import main; main(['skills', 'install'])"])
            except Exception:
                pass
            _h.handoff("sibyl update", human="restart any running agent so it loads the new version")
        else:
            # A failed pip run (permissions, PEP 668, a resolver conflict,
            # offline) fails the same way again, so a person reads pip's
            # output instead of the agent re-running it.
            _h.handoff("done", human=f"pip exited {rc} upgrading the packages (output above); "
                                     "tell me what it said")
        return rc

    # Default: print the command, do not execute
    print(a.dim("To upgrade, run:"))
    if method == "pipx":
        print(f"    pipx upgrade {pip_cmd_pkgs}")
    elif method == "pep668":
        print(f"    pip install --break-system-packages -U {pip_cmd_pkgs}")
        print()
        print(a.dim("  (Your Python flags itself as externally-managed under PEP 668.)"))
        print(a.dim("  (Cleanest: install inside a venv. See https://beta.sibyllabs.org for the recommended path.)"))
    else:
        print(f"    pip install -U {pip_cmd_pkgs}")
    print()
    print(a.dim("Or let sibyl run it:") + "  sibyl update --apply")
    print()
    un, uh = _update_handoff(rows, any_outdated)
    _h.handoff(un, human=uh)
    return 2  # exit 2 signals "outdated" without being a hard error


# ---- Guided migration (sibyl migrate) ----------------------------------

def _migrate_io():
    """Interactive IO for the guided flow: prints narration live and reads real
    stdin for pauses/confirms. Subclasses the testable GuidedIO seam in migrate.py
    (whose .say() only buffers, for non-interactive tests)."""
    from .migrate import GuidedIO

    class _PrintingIO(GuidedIO):
        def say(self, s: str = "") -> None:
            super().say(s)
            print(s)

    return _PrintingIO()


def cmd_migrate(args: argparse.Namespace) -> int:
    """`sibyl migrate` — guided onboarding. Backs up existing memory/agent files
    FIRST, wires Sibyl into every detected harness, hands the semantic extraction
    to the user's own agent (it holds the memory tools; Sibyl Labs never sees the
    files), verifies what landed, then optionally trims the originals — only on an
    explicit confirm and only because a verified backup exists."""
    from . import migrate as M

    home = Path.home()
    cwd = Path.cwd()
    db_path = Path(args.db).expanduser()
    backup_parent = Path(args.backup_dir).expanduser() if getattr(args, "backup_dir", None) else home

    print()
    print(bold("Sibyl Memory — guided migration"))
    print(dim("Back up existing memory, populate Sibyl Memory, optionally slim the originals."))
    print()
    print(yellow("Your files are copied to a timestamped backup FIRST and are never modified"))
    print(yellow("except by an explicit, confirmed trim at the very end. You run the extraction"))
    print(yellow("in your own agent — Sibyl Labs never sees your files or memory."))
    print(dim("No warranty: keep your backup. Sibyl Labs is not responsible for data loss."))
    print()

    # 0.5.0: the extraction runs in the person's own agent, whose memory tools
    # need an account. Reading and backing up need none.
    signed_in = _gate.has_account_access(
        Path(getattr(args, "credentials", None) or DEFAULT_CRED_PATH).expanduser())
    if not signed_in:
        print(dim(_gate.NO_ACCOUNT_MEMORY_LINE))
        print(dim("Run `sibyl init` before the extraction step so your agent can write memory."))
        print()

    files = M.scan_memory_files(home, cwd)
    if not files:
        print(yellow("No memory/agent files found in your home or current project."))
        print(dim("Looked for CLAUDE.md, AGENTS.md, .codex/config.toml, .hermes/*, and similar."))
        print(dim("If your files live elsewhere, run this from that project directory."))
        _h.handoff("sibyl setup", human="where your CLAUDE.md, AGENTS.md or Hermes files live, if anywhere")
        return 0

    print(dim("Will back up (originals untouched):"))
    for f in files:
        kind = "dir " if f.is_dir else "file"
        print(f"  {kind}  {f.rel}  {dim(f'({f.size} bytes)')}")
    print()
    print(dim("After Sibyl is wired, if your agent was already open, restart it (or"))
    print(dim("reconnect the sibyl-memory MCP) before running the extraction prompt."))
    print()

    if not args.yes:
        try:
            ans = input("Proceed? [Y/n]: ").strip().lower()
        except EOFError:
            ans = ""
            print("no terminal: proceeding (pass --yes to say so)")
        if ans.startswith("n"):
            print(dim("Aborted. Nothing was changed."))
            _h.handoff("done")
            return 0
    print()

    io = _migrate_io()
    report = M.run_guided_setup(
        home=home, cwd=cwd, db_path=db_path, backup_parent=backup_parent,
        io=io, debloat=not args.no_debloat, force=getattr(args, "force", False),
    )

    ph = report.get("phases", {})
    print()
    print(bold("Summary"))
    bk = ph.get("backup", {})
    if bk:
        print(f"  {green('backup')}    {bk.get('files', 0)} files")
        print(f"  {dim('location')}  {bk.get('dir', '')}")
    wire = ph.get("wire", {})
    if wire:
        wired = ", ".join(f"{n} ({s})" for n, s in wire.items())
        print(f"  {green('wired')}     {wired}")
    v = ph.get("verify", {})
    if v:
        cats = ", ".join(f"{k}:{n}" for k, n in (v.get("by_category") or {}).items())
        print(f"  {green('extracted')} {v.get('new_total', 0)} new entries" + (f"  {dim(cats)}" if cats else ""))
    trims = ph.get("debloat")
    if isinstance(trims, dict):          # older report shape
        trims = [dict(trims, file=trims.get("file", "CLAUDE.md"))]
    for rec in trims or []:
        if not rec.get("written"):
            continue
        saved = max(0, rec.get("before", 0) - rec.get("after", 0))
        print(f"  {green('trimmed')}   {rec.get('file', '?')}  {dim(f'(-{saved} bytes; full copy in backup)')}")

    later = list(report.get("later") or [])
    if not signed_in:
        later.insert(0, "sibyl init")
    if not report.get("ok"):
        print()
        print(yellow("Migration did not complete. Your originals and backup are intact."))
        fail = report.get("handoff") or {"next": "sibyl status", "human": None}
        _h.handoff(fail["next"], human=fail.get("human"), later=later)
        return 1
    print()
    if (v or {}).get("new_total", 0) == 0:
        # Finding 5.3.3 (handoff spec 2026-10-03): with nothing extracted yet
        # (a run without a terminal verifies at once) "Done." was misleading.
        print(green("Backed up and wired. Extraction has not run yet."))
    else:
        print(green("Done. Your memory now lives in Sibyl and is recalled on demand."))
    if bk:
        print(dim(f"Backup retained at {bk.get('dir','')} — delete it once you've confirmed everything."))
    agents = [n for n, st in (wire or {}).items() if st in ("wired", "already")]
    names = {"claude-code": "Claude Code", "codex": "Codex", "hermes": "Hermes"}
    who = " / ".join(names.get(n, n) for n in agents) or "your agent"
    human = (f"restart {who} if it was open while wiring; the backup at {bk.get('dir', '')} can be "
             "deleted once you have checked")
    if not signed_in:
        human = "this computer is not signed in yet, so run sibyl init first (listed above); " + human
    _h.handoff("sibyl status", human=human, later=later)
    return 0


# ---- Dispatch ----------------------------------------------------------

# `sibyl --version` order and short names. The CLI is always
# shown; the others only when installed.
VERSION_PACKAGES = (
    ("cli", "sibyl-memory-cli"),
    ("client", "sibyl-memory-client"),
    ("mcp", "sibyl-memory-mcp"),
    ("hermes", "sibyl-memory-hermes"),
    ("langgraph", "sibyl-memory-langgraph"),
)


def version_line() -> str:
    parts = []
    for short_name, pkg in VERSION_PACKAGES:
        v = _client_version() if pkg == "sibyl-memory-cli" else _installed_version(pkg)
        if v:
            parts.append(f"{short_name} {v}")
    return "sibyl " + " \u00b7 ".join(parts)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help="show the version of every installed Sibyl package and exit"):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0,
                         help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print(version_line())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sibyl",
        description="Command-line interface for the Sibyl Memory Plugin.",
    )
    # Version row (2026-10-08): `sibyl --version` exits 0 before the required-subcommand check
    # and touches no file. One line naming every installed Sibyl package,
    # `sibyl cli 0.5.0 · client 0.9.0 · mcp 0.3.0` (read when the flag is used).
    p.add_argument("--version", action=_VersionAction)
    p.add_argument("--credentials", default=str(DEFAULT_CRED_PATH),
                   help="Path to credentials.json (default: ~/.sibyl-memory/credentials.json)")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH),
                   help="Path to memory.db (default: ~/.sibyl-memory/memory.db)")
    p.add_argument("--tier-cache", default=str(DEFAULT_TIER_CACHE_PATH),
                   help="Path to tier_cache.json (default: ~/.sibyl-memory/tier_cache.json)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Activate the plugin in your browser")
    p_init.add_argument("--force", action="store_true", help="Re-activate even if credentials.json exists")
    p_init.add_argument("--no-email", action="store_true", help="Skip the email prompt after activation")
    p_init.add_argument("--replace", action="store_true",
                        help=("Re-activate this computer in place of its earlier sign-in (implies --force; "
                              "done automatically whenever credentials exist), and when the account is at "
                              "its computer limit, offer to sign another computer out"))
    p_init.set_defaults(func=cmd_init)

    p_up = sub.add_parser("upgrade", help="Open the upgrade flow (account billing page, or stake / subscribe)")
    p_up.set_defaults(func=cmd_upgrade)

    p_claim = sub.add_parser("claim", help=f"Link a card purchase to this account (code from {CLAIM_PAGE})")
    p_claim.add_argument("checkout_code", help="The cs_... checkout code shown after paying")
    p_claim.set_defaults(func=cmd_claim)

    p_em = sub.add_parser("email", help="Show, add or confirm your account email (receipts, security notices)")
    em_sub = p_em.add_subparsers(dest="email_cmd")
    p_ema = em_sub.add_parser("add", help="Send a confirmation code to a new address")
    p_ema.add_argument("address", help="The email address to add")
    p_ema.set_defaults(func=cmd_email)
    p_emv = em_sub.add_parser("verify", help="Send a confirmation code to the address on file")
    p_emv.set_defaults(func=cmd_email)
    p_emc = em_sub.add_parser("confirm", help="Confirm the six-digit code from the email")
    p_emc.add_argument("code", help="The six-digit code")
    p_emc.set_defaults(func=cmd_email)
    p_em.set_defaults(func=cmd_email)

    p_st = sub.add_parser("status", help="Show local + server tier / DB stats")
    p_st.set_defaults(func=cmd_status)

    p_who = sub.add_parser("whoami", help="One-line account summary (masked by default)")
    p_who.add_argument("--full", action="store_true", help="Show full email + wallet (no masking)")
    p_who.set_defaults(func=cmd_whoami)

    p_dev = sub.add_parser("devices", help="List devices (active bearer tokens) for the account")
    dev_sub = p_dev.add_subparsers(dest="sub")
    p_rev = dev_sub.add_parser("revoke", help="Revoke a device by index (run `sibyl devices` for indexes)")
    p_rev.add_argument("index", type=int, help="Index from `sibyl devices` output")
    p_dev.set_defaults(func=cmd_devices)
    p_rev.set_defaults(func=cmd_devices)

    p_dash = sub.add_parser("dashboard", help="Open the account page (no token in the URL; falls back to status while it is not open)")
    p_dash.set_defaults(func=cmd_dashboard)

    p_lo = sub.add_parser("logout", help="Remove local credentials (memory.db stays)")
    p_lo.set_defaults(func=cmd_logout)

    p_h = sub.add_parser("health", help="Run the provider self-check")
    p_h.set_defaults(func=cmd_health)

    p_mem = sub.add_parser("memory", help="Inspect your memory store (list / search / recall), or adopt memory from before sign-in")
    mem_sub = p_mem.add_subparsers(dest="mem_cmd")
    p_ml = mem_sub.add_parser("list", help="List entities (optionally filtered by category)")
    p_ml.add_argument("category", nargs="?", default=None, help="Optional category to filter by")
    p_ml.add_argument("--limit", type=int, default=50, help="Max rows (default 50)")
    p_ml.set_defaults(func=cmd_memory)
    p_ms = mem_sub.add_parser("search", help="Full-text search across entities + state + reference + journal")
    p_ms.add_argument("query", help="Search query (matches stored text, not meaning)")
    p_ms.add_argument("--limit", type=int, default=20, help="Max hits (default 20)")
    p_ms.set_defaults(func=cmd_memory)
    p_mr = mem_sub.add_parser("recall", help="Recall one entity by category + name")
    p_mr.add_argument("category", help="Entity category")
    p_mr.add_argument("name", help="Entity name")
    p_mr.set_defaults(func=cmd_memory)
    p_ma = mem_sub.add_parser(
        "adopt",
        help="Move memory written on this computer before sign-in into your account "
             "(local, one transaction, nothing deleted)")
    p_ma.add_argument("--yes", action="store_true",
                      help="Do not ask when this store also holds another account's memory")
    p_ma.set_defaults(func=cmd_memory)
    p_mem.set_defaults(func=cmd_memory)

    p_update = sub.add_parser(
        "update",
        help="Check for newer sibyl-memory-* releases on PyPI (use --apply to upgrade)",
    )
    p_update.add_argument("--apply", action="store_true", help="Run pip install -U for the outdated packages")
    p_update.add_argument("--json", action="store_true", help="Machine-readable output")
    p_update.set_defaults(func=cmd_update)

    # v0.1.4: one-command auto-detect-and-wire setup for any agent stack
    from .setup import cmd_setup
    p_setup = sub.add_parser(
        "setup",
        help="Auto-detect Hermes / Claude Code / Codex and wire SIBYL as the memory provider",
    )
    p_setup.add_argument(
        "target", nargs="?", choices=["hermes", "claude-code", "codex"],
        help="Wire just this framework (default: detect all)",
    )
    p_setup.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip prompts, accept defaults (still respects destructive-default-NO unless --force)",
    )
    p_setup.add_argument(
        "--force", action="store_true",
        help="Overwrite existing non-SIBYL memory provider configs",
    )
    p_setup.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing",
    )
    p_setup.add_argument(
        "--hermes-home", default=None,
        help="Override HERMES_HOME autodetection",
    )
    p_setup.add_argument(
        "--claude-settings", default=None,
        help="Path to Claude Code's settings.json (default: ~/.claude/settings.json)",
    )
    p_setup.add_argument(
        "--codex-config", default=None,
        help="Override ~/.codex/config.toml autodetection",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_migrate = sub.add_parser(
        "migrate",
        help="Guided: back up existing memory/agent files, wire Sibyl, populate Sibyl Memory, optionally slim the originals",
    )
    p_migrate.add_argument(
        "--backup-dir", default=None,
        help="Where to write the timestamped backup (default: your home directory)",
    )
    p_migrate.add_argument(
        "--no-debloat", action="store_true",
        help="Skip the optional trim step (back up + wire + extract + verify only)",
    )
    p_migrate.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the initial confirm (the trim step still always asks separately)",
    )
    p_migrate.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing non-sibyl memory provider when wiring a harness "
             "(without this, migrate stops at that harness and tells you to re-run with --force)",
    )
    p_migrate.set_defaults(func=cmd_migrate)

    # Save and debloat (spec plugin-save-debloat 2026-09-27).
    from . import lifecycle_cli as _lc
    p_save = sub.add_parser(
        "save",
        help="Save to Sibyl Memory: one fact now, a session capsule (Pro), or how to run the save procedure")
    p_save.add_argument("--fact", default=None, help="Write one fact (category 'fact')")
    p_save.add_argument("--capsule", action="store_true",
                        help="Write a session capsule now (Pro; counts only, no content)")
    p_save.add_argument("--preview", action="store_true", help="Say what would be written; write nothing")
    p_save.add_argument("--status", action="store_true", help="Last save, writes since, decisions queued")
    p_save.set_defaults(func=_lc.cmd_save)

    p_deb = sub.add_parser(
        "debloat",
        help="Measure the instruction files your agent loads every start; plan, apply, or undo a context trim")
    p_deb.add_argument("action", nargs="?", choices=["plan", "apply", "auto", "manual", "undo"],
                       help="plan | apply [<file>] | auto <file> | manual <file> | undo [<file>]")
    p_deb.add_argument("file", nargs="?", default=None, help="An instruction file (as `sibyl debloat` names it)")
    p_deb.add_argument("--fyi", action="store_true", help="One line")
    p_deb.add_argument("--json", action="store_true", help="Machine-readable record")
    p_deb.add_argument("--root", default=None, help="Project directory (default: the current directory)")
    p_deb.add_argument("--config", default=None, help="A .debloat.json to use instead of <root>/.debloat.json")
    p_deb.add_argument("--include", action="append", default=[], help="Also measure this file (repeatable)")
    p_deb.add_argument("--yes", "-y", action="store_true", help="Answer yes to the confirmation")
    p_deb.set_defaults(func=_lc.cmd_debloat)

    p_sk = sub.add_parser("skills", help="The save and debloat procedures installed for your agents")
    sk_sub = p_sk.add_subparsers(dest="skills_cmd")
    p_ski = sk_sub.add_parser("install", help="Install or refresh them (never overwrites your edits)")
    p_ski.add_argument("--force", action="store_true", help="Replace files you edited too")
    p_ski.set_defaults(func=_lc.cmd_skills)
    p_sks = sk_sub.add_parser("status", help="Version, checksums, shims and hooks")
    p_sks.set_defaults(func=_lc.cmd_skills)
    p_skr = sk_sub.add_parser("reset", help="Replace one file with the shipped version")
    p_skr.add_argument("name", help="SAVE.md or DEBLOAT.md")
    p_skr.add_argument("--yes", "-y", action="store_true")
    p_skr.set_defaults(func=_lc.cmd_skills)
    p_sku = sk_sub.add_parser(
        "uninstall",
        help=("Remove what this plugin installed: the procedures, shims, Claude Code hooks and the "
              "Codex block (edited files are kept). Run it BEFORE pip uninstall: the hooks in "
              "~/.claude/settings.json point at this package's sibyl command"),
        description=("Removes the save/debloat procedures, the harness shims, the five Claude Code "
                     "hooks in ~/.claude/settings.json and the block in Codex's AGENTS.md. Files you "
                     "edited are kept. Run this before `pip uninstall sibyl-memory-cli`: the hooks "
                     "call this package's `sibyl` command (they exit quietly if it is gone, but "
                     "they stay in your settings until removed)."))
    p_sku.add_argument("--yes", "-y", action="store_true")
    p_sku.set_defaults(func=_lc.cmd_skills)
    p_skp = sk_sub.add_parser(
        "proposals",
        help="Pro: skills Sibyl proposes from your sessions (list, show, accept, reject, mute, unmute)",
        description=("Skill proposals are mined locally from your journal on Pro. Nothing is created "
                     "without an accept: `accept <id>` writes a SKILL.md for Claude Code, Codex and Hermes "
                     "(the folders present on this computer, or --to <dir>) and a reference copy in memory; "
                     "`reject <id>` keeps it from coming back for 30 days (twice rejected: muted until "
                     "`unmute <slug>`); `mute <kind>` stops a kind of proposal."))
    p_skp.add_argument("proposal_action", nargs="?", default="list",
                       choices=["list", "show", "accept", "reject", "mute", "unmute"])
    p_skp.add_argument("target", nargs="?", help="proposal id (or its first 6+ characters), kind or slug")
    p_skp.add_argument("--to", help="accept: write <dir>/<name>/SKILL.md instead of the harness folders")
    p_skp.add_argument("--note", help="accept / reject: a note kept with the decision")
    p_skp.add_argument("--days", type=int, help="mute: how many days (default 30)")
    p_skp.set_defaults(func=_lc.cmd_skills)
    p_sk.set_defaults(func=_lc.cmd_skills)

    p_as = sub.add_parser("autosave", help=("Pro automatic save: status, on, off, or every <minutes> of "
                                            "active work (30 to 240; default 60)"))
    p_as.add_argument("action", nargs="?", choices=["status", "on", "off", "every"], default="status")
    p_as.add_argument("minutes", nargs="?", type=int)
    p_as.set_defaults(func=_lc.cmd_autosave)

    p_ref = sub.add_parser("refresh", help=("Check your account now (picks up an upgrade or a plan change "
                                            "at once) and refresh the procedures, shims and hooks"))
    p_ref.set_defaults(func=_lc.cmd_refresh)

    p_rem = sub.add_parser("reminders", help="Save and debloat reminders: on, off, or quiet (Pro reports)")
    p_rem.add_argument("state", nargs="?", choices=["on", "off", "quiet", "status"], default="status")
    p_rem.set_defaults(func=_lc.cmd_reminders)

    p_hook = sub.add_parser("hook", help="Claude Code hook entry point (installed by sibyl setup; not for manual use)")
    p_hook.add_argument("event", choices=["session-start", "prompt", "stop", "pre-compact", "session-end"])
    p_hook.set_defaults(func=_lc.cmd_hook)

    return p


def cmd_memory(args: argparse.Namespace) -> int:
    """Read-only inspection of the local memory store (PKG-4, beta reports).

        sibyl memory list [category]    list entities
        sibyl memory search <query>     full-text search across tiers
        sibyl memory recall <cat> <nm>  recall one entity by category + name

    Opens the resolved DB read-only via the SDK; never writes. Respects --db so
    you can inspect any split-brain store that `sibyl status` surfaces.
    """
    from sibyl_memory_client import DEFAULT_TENANT, MemoryClient
    # THE canonical cause vocabulary (stage 3, 2026-08-31). Imported from the
    # client, never re-declared here — the CLI prints causes, it does not own
    # them.
    from sibyl_memory_client.verdicts import refine_zero

    db_path = Path(args.db).expanduser()
    print()
    if not db_path.exists():
        print(a.warn_line(f"No memory store at {db_path}."))
        print(a.dim("  Run `sibyl status` to see every store on this machine."))
        _h.handoff("sibyl status")
        return 1
    if getattr(args, "mem_cmd", None) == "adopt":
        return _cmd_memory_adopt(args, db_path)
    # F1 (Polish-language eval 2026-08-12): resolve the ACTIVATED tenant exactly like
    # the MCP server does (Contract T ladder: tenant_id -> account_id ->
    # DEFAULT_TENANT), so `sibyl memory list/search/recall` reads the same tenant
    # the MCP writes. Without this the CLI always read DEFAULT_TENANT and an
    # activated account saw "(no entities)" for a perfectly healthy store. `--credentials`
    # is a root parser arg so args.credentials is present on the real path; a
    # direct cmd_memory(Namespace(...)) call (CLI-7 unit tests) may omit it, so we
    # fall back to the parser default resolved at call time.
    cred_arg = getattr(args, "credentials", None) or str(DEFAULT_CRED_PATH)
    creds = read_credentials(Path(cred_arg).expanduser()) or {}
    tenant_id = creds.get("tenant_id") or creds.get("account_id") or DEFAULT_TENANT
    # 0.5.0 ESCAPE HATCH (spec 3.4, D2): the person's own data is never held
    # hostage to the account. This read-only command builds the client with a
    # NullEntitlementGate ON PURPOSE (constructor-injected; no env var, no
    # config) and passes no account ids, so it never calls the network. Agents
    # (MCP, hermes, langgraph) stay gated.
    from sibyl_memory_client.testing import NullEntitlementGate
    client = MemoryClient.local(path=db_path, tenant_id=tenant_id,
                                entitlement_gate=NullEntitlementGate())
    signin_later: tuple = ()
    if not _gate.has_account_access(Path(cred_arg).expanduser()):
        print(a.dim(_gate.NO_ACCOUNT_MEMORY_LINE))
        signin_later = ("sibyl init",)
    op = getattr(args, "mem_cmd", None)
    if op == "list":
        rows = client.list_entities(category=args.category, limit=args.limit)
        if not rows:
            print(a.dim("(no entities)"))
            _h.handoff("done", later=signin_later)
            return 0
        print(a.eyebrow(f"entities ({len(rows)})"))
        for r in rows:
            # CLI-7: tolerate SDK rows missing category/name keys.
            cat = r.get("category", "?")
            name = r.get("name", "?")
            print(a.kv(f"{cat}/{name}", r.get("status") or "-"))
        _h.handoff("done", later=signin_later)
        return 0
    if op == "search":
        # THE VERDICT CONTRACT (stage 3, 2026-08-31). This used to print
        # "(no matches)" and stop — the terminal-shaped version of the defect
        # this stage closes: a zero that cannot explain itself. The SDK now
        # returns a cause on every call, and `refine_zero` pays one COUNT on the
        # zero path so a brand-new user is told their store is EMPTY rather than
        # left to conclude their query was wrong.
        hits = refine_zero(client, client.search(args.query, limit=args.limit))
        if not hits:
            print(a.dim(f"(no matches for {args.query!r})"))
            verdict = getattr(hits, "verdict", None)
            if verdict is not None:
                # Plain language, generated from the enum and never from stored
                # text, so it is always safe to print to a terminal.
                print(a.warn_line(verdict.explain()))
                print(a.dim(f"cause: {verdict.code.value}"))
            _h.handoff("done", later=signin_later)
            return 0
        print(a.eyebrow(f"matches ({len(hits)})"))
        for h in hits:
            snip = (h.get("snippet") or "").replace("\n", " ")[:100]
            print(a.kv(f"[{h.get('tier') or '-'}] {h.get('key') or '-'}", snip))
        _h.handoff("done", later=signin_later)
        return 0
    if op == "recall":
        try:
            ent = client.get_entity(args.category, args.name)
        except Exception as e:
            print(a.warn_line(str(e)))
            _h.handoff("sibyl memory list", later=signin_later)
            return 1
        # CLI-7: tolerate SDK rows missing category/name keys.
        print(a.eyebrow(f"{ent.get('category', args.category)}/{ent.get('name', args.name)}"))
        print(a.kv("status", ent.get("status") or "-"))
        print(a.kv("updated", _utc_minute(ent.get("updated_at")) or ent.get("updated_at") or "-"))
        body = ent.get("body")
        print(body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False))
        _h.handoff("done", later=signin_later)
        return 0
    print(a.warn_line("Usage: sibyl memory {list|search|recall|adopt} ..."))
    _h.handoff("sibyl memory list", later=signin_later)
    return 1


def _cmd_memory_adopt(args: argparse.Namespace, db_path: Path) -> int:
    """`sibyl memory adopt`: move this store's rows written before sign-in
    (DEFAULT_TENANT) into the signed-in account. Local only: one SQLite
    transaction, nothing deleted, conflicting keys kept under a new name, an
    audit row in the store. Needs a verified sign-in (sibyl init). When the
    store has held another account it asks first (or --yes)."""
    from sibyl_memory_client import EntitlementError
    from sibyl_memory_client._adopt import AdoptionRefused

    cred_path = Path(getattr(args, "credentials", None) or DEFAULT_CRED_PATH).expanduser()

    def _run(confirm: bool):
        return _gate.adopt_pre_signin(db_path, cred_path, trigger="command",
                                      confirm_other_accounts=confirm)
    try:
        try:
            res = _run(bool(getattr(args, "yes", False)))
        except AdoptionRefused as e:
            if e.reason != "other_account":
                raise
            print(a.warn_line("This store also holds another account's memory."))
            print(a.dim("  The rows written before sign-in may not be yours. Moving them makes"))
            print(a.dim("  them part of the account signed in now. Nothing is deleted either way."))
            if not sys.stdin.isatty():
                print(a.dim("  Not a terminal: re-run with --yes to move them."))
                _h.handoff("sibyl memory adopt --yes",
                           human="this store also holds another account's memory; confirm the pre "
                                 "sign-in rows are yours before I move them (nothing is deleted either way)")
                return 1
            answer = input("  Move them into this account? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print(a.dim("  Nothing moved."))
                _h.handoff("done")
                return 1
            res = _run(True)
    except EntitlementError as e:
        print(a.warn_line(str(e).splitlines()[0]))
        print(a.dim("  Sign in first: sibyl init. Nothing moved."))
        en, eh = _h.from_exception(e, default="sibyl init")
        _h.handoff(en, human=eh, later=("sibyl memory adopt",))
        return 1
    except AdoptionRefused as e:
        print(a.warn_line(str(e)))
        print(a.dim("  Nothing moved."))
        _h.handoff("sibyl status")
        return 1
    if res is None or res.status == "nothing_to_move":
        print(a.dim("Nothing to move: no memory from before sign-in in this store."))
        _h.handoff("done")
        return 0
    print(a.success_line(f"Moved {res.moved} rows from before sign-in into your account."))
    for table, n in sorted(res.counts.items()):
        print(a.kv(table, str(n)))
    if res.renamed:
        print(a.dim(f"  {len(res.renamed)} kept under a new name ending in (before sign-in): "
                    "your account already had that key."))
    _h.handoff("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    global _ENT_CRED_PATH
    try:
        _ENT_CRED_PATH = Path(getattr(args, "credentials", None) or DEFAULT_CRED_PATH).expanduser()
    except (TypeError, ValueError):
        _ENT_CRED_PATH = DEFAULT_CRED_PATH
    global _CREDS_CORRUPT
    _CREDS_CORRUPT = False
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(red("\nInterrupted."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
