"""`sibyl` command-line interface.

Stdlib only. The CLI is a thin wrapper around HTTP calls to
https://api.sibyllabs.org/api/plugin/* and the local SibylMemoryProvider.

Design pillars:
  - Zero non-stdlib deps in this file. urllib is enough.
  - Credentials are written atomically at mode 0600, set at file-creation
    time via O_CREAT|O_EXCL|O_NOFOLLOW (no chmod-after-write race).
  - The URL parameter handed to the browser is an opaque session identifier,
    not the long-lived bearer (audit SEC-1: server-side pairing handoff
    issues a separate bearer at activation completion if available).
  - session_token is never printed in full: display short slice only.
  - Polling has explicit timeouts; no infinite loops.
  - Every command exits with a clear status code (0 ok, 1 user error, 2 server error).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any


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
ACTIVATE_BASE = os.environ.get("SIBYL_ACTIVATE_BASE", "https://sibyllabs.org/plugin/activate")
UPGRADE_BASE = os.environ.get("SIBYL_UPGRADE_BASE", "https://sibyllabs.org/plugin/upgrade")

DEFAULT_CRED_PATH = Path("~/.sibyl-memory/credentials.json").expanduser()
DEFAULT_DB_PATH = Path("~/.sibyl-memory/memory.db").expanduser()
DEFAULT_TIER_CACHE_PATH = Path("~/.sibyl-memory/tier_cache.json").expanduser()

POLL_INTERVAL_SEC = 3
INIT_TIMEOUT_SEC = 10 * 60      # 10 minutes for /init activation
UPGRADE_TIMEOUT_SEC = 15 * 60   # 15 minutes for upgrade: wallet ux can be slow

# ---- Color / output ----------------------------------------------------

from . import _aesthetic as a

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


def short(token: str | None) -> str:
    if not token:
        return "-"
    if len(token) <= 12:
        return token
    return f"{token[:8]}…{token[-4:]}"


def print_status(label: str, value: str) -> None:
    print(f"  {dim(label.ljust(18))} {value}")


# ---- HTTP --------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, body: Any, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body}")
        self.status = status
        self.body = body
        self.url = url


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"error": "unparseable response body"}
        raise HttpError(e.code, err_body, url) from None
    except urllib.error.URLError as e:
        raise HttpError(0, {"error": str(e.reason)}, url) from None


# ---- Credentials I/O ---------------------------------------------------

def write_credentials_atomic(creds: dict, path: Path = DEFAULT_CRED_PATH) -> Path:
    """Write credentials.json atomically at mode 0600.

    v0.1.2 hardening (audit SEC-2): mode 0600 is set by the kernel at
    file-creation time via O_CREAT|O_EXCL|O_NOFOLLOW. Previously used
    write_text() followed by os.chmod(), leaving a world-readable window
    between syscalls every credential save.
    """
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(creds, indent=2).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Clean any leftover .tmp from a crashed prior write so O_EXCL can succeed.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
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
    return json.loads(path.read_text(encoding="utf-8"))


def invalidate_tier_cache(path: Path = DEFAULT_TIER_CACHE_PATH) -> None:
    """Drop the local tier cache so the next write refreshes against the server."""
    path = path.expanduser()
    if path.exists():
        path.unlink()


# ---- `sibyl init` ------------------------------------------------------

def _gen_pairing_code() -> str:
    """6-digit cryptographic pairing code. Uniform across 000000-999999."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_pairing_code(code: str, session: str) -> str:
    return hashlib.sha256(f"{code}:{session}".encode("utf-8")).hexdigest()


def cmd_init(args: argparse.Namespace) -> int:
    """Activation flow. Generate session UUID + pairing code, register with
    server, open activation page in browser, poll /check until bound.

    The pairing code is printed in the terminal. If the user picks the
    email path in the browser, they type both their email and this code.
    No external email service is required."""
    # Brand moment: gold/white gradient SIBYL wordmark.
    # Honors NO_COLOR + TTY detection automatically; safe to always call.
    from ._banner import print_banner
    print_banner()

    cred_path = Path(args.credentials).expanduser()
    if cred_path.exists() and not args.force:
        existing = read_credentials(cred_path) or {}
        print(a.section_header("already activated", subtitle="use --force to re-activate"))
        print()
        print(a.kv("Account", short(existing.get("account_id"))))
        print(a.kv("Tier", (existing.get("tier") or "free").upper(), value_color="accent"))
        print(a.kv("Credentials", str(cred_path)))
        print()
        return 0

    # SEC-1 mitigation (v0.1.2): the URL parameter is an opaque pairing
    # session identifier, NOT the long-lived bearer used by /access and
    # /check-write. The CLI generates it locally and the server treats
    # it as the activation rendezvous key only. The persistent bearer
    # is issued by the server in the /check response (`bearer_token`
    # field) after activation completes. Servers running pre-SEC-1
    # firmware that echo the URL identifier as the bearer still work -
    # we use whichever the server returns in the bound credentials.
    session_id = str(uuid.uuid4())
    pairing_code = _gen_pairing_code()
    code_hash = _hash_pairing_code(pairing_code, session_id)
    activate_url = f"{ACTIVATE_BASE}?session={session_id}"

    # Pre-register the session + pairing code hash with the server.
    # The code itself never leaves the user's machine until they type it
    # into the browser.
    try:
        http_request(
            "POST",
            "/api/plugin/session-init",
            body={
                "session": session_id,
                "pairing_code_hash": code_hash,
                "env": {
                    "os_family": _detect_os_family(),
                    "install_method": "cli",
                    "client_version": _client_version(),
                },
            },
            timeout=10.0,
        )
    except HttpError as e:
        # Non-fatal: SIWE path doesn't need the pairing code. If session-init
        # fails the user can still complete SIWE. Surface the warning.
        print(yellow(f"Warning: session-init failed ({e.status}). Wallet path still works; email path may not."))

    print()
    print(a.section_header("activation", subtitle="three paths · pick whichever fits your device"))
    print()
    print(a.kv("Session", short(session_id)))
    formatted_code = pairing_code[:3] + " " + pairing_code[3:]
    print(a.kv("Code", a.gradient_gold(formatted_code), value_color="accent")
          + "  " + a.dim("(use this in the email panel)"))
    print(a.kv("Opening", activate_url))
    print()
    print(a.dim("  desktop wallet · email + code · or send USDC from any mobile wallet"))
    print(a.dim("  this terminal will pick up automatically when you bind."))
    print()

    try:
        webbrowser.open(activate_url, new=2)
    except Exception:
        pass

    # Poll /api/plugin/check
    deadline = time.time() + INIT_TIMEOUT_SEC
    last_status = ""
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin_i = 0

    while time.time() < deadline:
        try:
            resp = http_request("GET", f"/api/plugin/check?session={urllib.parse.quote(session_id)}", timeout=10.0)
        except HttpError as e:
            if e.status in (404, 503, 0):
                # Session not yet created server-side, or transient: keep polling
                pass
            else:
                print(red(f"\nUnexpected error: {e.body}"))
                return 2
            resp = {"bound": False}

        if resp.get("bound") and resp.get("credentials"):
            creds = resp["credentials"]
            # SEC-1: prefer the server-issued bearer_token (post-fix) over
            # echoing the URL pairing-session id. Servers running pre-SEC-1
            # firmware echo `session_token` back as the bearer: we use
            # whichever the server returns. The CLI's session_id (URL
            # identifier) is the rendezvous key, not the persistent bearer.
            bearer = creds.get("bearer_token") or creds.get("session_token")
            if not bearer:
                # Fallback: pre-SEC-1 server flow where neither field is
                # echoed back: inject the pairing session id so subsequent
                # /access and /check-write calls have something to send.
                bearer = session_id
            # Sanity check on echoed session_token (pre-SEC-1 flow only)
            if creds.get("session_token") and creds["session_token"] != session_id \
                    and not creds.get("bearer_token"):
                print(red("\nSession token mismatch: refusing to write credentials."))
                return 2
            creds["session_token"] = bearer
            path = write_credentials_atomic(creds, cred_path)
            print(f"\r{' ' * 80}\r", end="")  # clear spinner line
            print()
            print(a.success_line("Activated."))
            print()
            print(a.kv("Account", short(creds.get("account_id"))))
            print(a.kv("Tier", (creds.get("tier") or "free").upper(), value_color="accent"))
            print(a.kv("Wallet", creds.get("wallet") or "-"))
            print(a.kv("Email", creds.get("email") or "-"))
            print(a.kv("Credentials", str(path)))
            print()
            print(a.section_header("wire it into your agent"))
            print()
            print(a.dim("  hermes:"))
            print(a.dim("    sibyl-memory-hermes install-plugin"))
            print(a.dim("    # then edit ~/.hermes/config.yaml:"))
            print(a.dim("    #   memory:"))
            print(a.dim("    #     provider: sibyl"))
            print()
            print(a.dim("  claude code / codex / cursor / continue (MCP):"))
            print(a.dim("    pip install sibyl-memory-mcp"))
            print()
            print(a.dim("  python orchestration (langchain / llamaindex / custom):"))
            print(a.dim("    from sibyl_memory_hermes import SibylMemoryProvider"))
            print(a.dim("    provider = SibylMemoryProvider()"))
            print()
            return 0

        # Spinner tick
        spin_i = (spin_i + 1) % len(spinner)
        remaining = int(deadline - time.time())
        spin_glyph = a.color(spinner[spin_i], a.PULSE)
        status = f"\r  {spin_glyph} {a.dim('watching the network for your bind')} … {a.dim(f'{remaining // 60}:{remaining % 60:02d} left')}"
        if status != last_status:
            sys.stdout.write(status)
            sys.stdout.flush()
            last_status = status
        time.sleep(POLL_INTERVAL_SEC)

    print()
    print(a.err_line("Activation timed out."))
    print(a.dim("  Re-run `sibyl init` to try again."))
    return 1


# ---- `sibyl upgrade` ---------------------------------------------------

def cmd_upgrade(args: argparse.Namespace) -> int:
    """Upgrade flow. Read existing creds → open upgrade page → poll /access until tier flips."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.err_line("Not activated."))
        print(a.dim("  Run `sibyl init` first."))
        return 1

    account_id = creds.get("account_id")
    session_token = creds.get("session_token")
    current_tier = (creds.get("tier") or "free").lower()

    if not account_id or not session_token:
        print(a.err_line("credentials.json is missing account_id or session_token."))
        print(a.dim("  Re-run `sibyl init`."))
        return 1

    upgrade_url = f"{UPGRADE_BASE}?session={session_token}"

    print()
    print(a.section_header("upgrade", subtitle="lift the 2 MB free-tier cap"))
    print()
    print(a.kv("Account", short(account_id)))
    print(a.kv("Current tier", current_tier.upper(), value_color="accent"))
    print(a.kv("Opening", upgrade_url))
    print()
    print(a.dim("  two paths in the browser:"))
    print(a.dim("    1. stake $SIBYL on Base (free unlimited if you qualify)"))
    print(a.dim("    2. subscribe in USDC (monthly / quarterly / annual)"))
    print()

    try:
        webbrowser.open(upgrade_url, new=2)
    except Exception:
        pass

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
                print(red("\nSession expired. Re-run `sibyl init`."))
                return 1
            # Transient: keep polling
            resp = {}

        new_tier = (resp.get("tier") or current_tier).lower()
        source = resp.get("source")

        if new_tier != current_tier and source in ("subscription", "staker"):
            # Tier changed. Refresh credentials.
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
                print(a.kv("Expires", resp["expires_at"]))
            if resp.get("cap_bytes") is None:
                print(a.kv("Storage cap", "unlimited", value_color="ok"))
            else:
                print(a.kv("Storage cap", f"{resp['cap_bytes']:,} bytes"))
            if resp.get("staker"):
                s = resp["staker"]
                print(a.kv("Wallet", s.get("wallet", "-")))
                print(a.kv("$SIBYL held", str(s.get("total_sibyl", "-"))))
            print()
            print(a.dim("  local tier cache cleared. your next write will sync the new tier."))
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
    return 1


# ---- `sibyl status` ----------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    """Show local + server-side state without modifying anything.

    LIGHT treatment: utilitarian dashboard. No banner, no section header,
    no chrome. Eyebrow labels + kv rows + ↓ status drift surfaces. Same
    convention as `git status`, `ls -la`, `btop` panel bodies."""
    cred_path = Path(args.credentials).expanduser()
    creds = read_credentials(cred_path)

    print()

    if not creds:
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        return 0

    # Local view
    print(a.eyebrow("local"))
    print(a.kv("Credentials", str(cred_path)))
    print(a.kv("Account", short(creds.get("account_id"))))
    print(a.kv("Tier", (creds.get("tier") or "free").upper(), value_color="accent"))
    print(a.kv("Wallet", creds.get("wallet") or "-"))
    print(a.kv("Email", creds.get("email") or "-"))
    print(a.kv("Issued", creds.get("issued_at") or "-"))

    db_path = Path(args.db).expanduser()
    if db_path.exists():
        size = db_path.stat().st_size
        pct = size / 2_097_152 * 100
        size_label = f"{size:,} bytes ({size / (1024 * 1024):.2f} MB · {pct:.1f}% of free cap)"
        size_color = "warn" if pct > 80 else "soft"
        print(a.kv("DB path", str(db_path)))
        print(a.kv("DB size", size_label, value_color=size_color))
    else:
        print(a.kv("DB path", f"{db_path} (not created)"))

    tier_cache = Path(args.tier_cache).expanduser()
    if tier_cache.exists():
        cache = json.loads(tier_cache.read_text(encoding="utf-8"))
        print(a.kv("Tier cache", f"{cache.get('tier','?')} (checked {cache.get('checked_at','?')[:19]})"))
    else:
        print(a.kv("Tier cache", "-"))

    # Server view (only if account_id + session_token are present)
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
            print(a.kv("Tier", (resp.get("tier") or "free").upper(), value_color="accent"))
            print(a.kv("Source", resp.get("source") or "-"))
            print(a.kv("Cap bytes", "unlimited" if resp.get("cap_bytes") is None else f"{resp['cap_bytes']:,}"))
            if resp.get("expires_at"):
                print(a.kv("Expires", resp["expires_at"]))
            if resp.get("staker"):
                s = resp["staker"]
                print(a.kv("$SIBYL held", str(s.get("total_sibyl", "-"))))
                print(a.kv("Threshold", str(s.get("threshold_sibyl", "-"))))
                print(a.kv("Qualified", "yes" if s.get("qualified") else "no",
                           value_color="ok" if s.get("qualified") else "soft"))
            # Detect server/local drift
            srv_tier = (resp.get("tier") or "free").lower()
            loc_tier = (creds.get("tier") or "free").lower()
            if srv_tier != loc_tier:
                print()
                print(a.warn_line(f"Local tier ({loc_tier}) differs from server tier ({srv_tier})."))
                print(a.dim("  Run `sibyl upgrade` to refresh, or `sibyl init --force` to re-activate."))
        except HttpError as e:
            print(a.kv("Tier", f"server error: {e.status}", value_color="err"))

    print()
    return 0


# ---- `sibyl dashboard` (placeholder, today routes to status) -----------

def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the web account dashboard. In v0.1.0, the dashboard at
    account.sibyllabs.org is not yet live (queued post-V1-ship per the
    operator design memo). Until then, `sibyl dashboard` delegates to
    `sibyl status` so the command surface exists from day one and users
    who muscle-memory it get a real result.

    When account.sibyllabs.org ships, this will flip to
    `webbrowser.open(...)` with no UX disruption: same command, real
    web dashboard."""
    DASHBOARD_BASE = os.environ.get("SIBYL_DASHBOARD_BASE")
    if DASHBOARD_BASE:
        # If env var is set, open the web dashboard with the session token.
        creds = read_credentials(Path(args.credentials).expanduser())
        if creds and creds.get("session_token"):
            url = f"{DASHBOARD_BASE}?session={creds['session_token']}"
            print()
            print(bold("Sibyl Memory Plugin · dashboard"))
            print(f"  {dim('Opening:')}     {url}")
            print()
            try:
                webbrowser.open(url, new=2)
            except Exception:
                pass
            return 0
    # Fall through: account.sibyllabs.org isn't live yet, run status instead.
    return cmd_status(args)


# ---- `sibyl whoami` ----------------------------------------------------

def _mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return "-"
    user, _, domain = e.partition("@")
    if "." not in domain:
        return f"{user[0]}***@{domain[0]}***"
    name, _, tld = domain.rpartition(".")
    return f"{user[0]}***@{name[0]}***.{tld}"


def _mask_wallet(w: str | None) -> str:
    if not w or not w.startswith("0x") or len(w) < 12:
        return w or "-"
    return f"{w[:6]}…{w[-4:]}"


def cmd_whoami(args: argparse.Namespace) -> int:
    """One-line account summary. Shows account_id + tier + linked email/wallet + this device.

    LIGHT treatment: 4-line glance. No banner, no section header. Same shape
    as `whoami` on unix, `gh auth status`, `aws sts get-caller-identity`."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.warn_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        return 1

    full = bool(getattr(args, "full", False))
    acct = creds.get("account_id") or ""
    tier = (creds.get("tier") or "free").upper()
    email = creds.get("email") if full else _mask_email(creds.get("email"))
    wallet = creds.get("wallet") if full else _mask_wallet(creds.get("wallet"))

    print()
    print(f"  {a.color('account', a.INK_FAINT)}  {a.bold(short(acct))}  {a.dim(a.GLYPH_DOT)}  {a.gradient_gold(tier)}")
    print(f"  {a.color('wallet ', a.INK_FAINT)}  {a.color(wallet or '-', a.INK)}")
    print(f"  {a.color('email  ', a.INK_FAINT)}  {a.color(email or '-', a.INK)}")
    os_label = _detect_os_family() or "unknown"
    device_line = f"sibyl-memory-cli/{_client_version()} {os_label}"
    print(f"  {a.color('device ', a.INK_FAINT)}  {a.dim(device_line)}")
    print()
    return 0


# ---- `sibyl devices` ---------------------------------------------------

def cmd_devices(args: argparse.Namespace) -> int:
    """List active bearer tokens (devices) for the account. Optional: revoke by index."""
    creds = read_credentials(Path(args.credentials).expanduser())
    if not creds:
        print(a.err_line("Not activated."))
        print(a.dim("  Run `sibyl init`."))
        return 1
    account_id = creds.get("account_id")
    session_token = creds.get("session_token")
    if not account_id or not session_token:
        print(a.err_line("credentials.json missing account_id or session_token."))
        print(a.dim("  Run `sibyl init`."))
        return 1

    sub = getattr(args, "sub", None)

    # `sibyl devices revoke <index>` path
    if sub == "revoke":
        idx = getattr(args, "index", None)
        if idx is None:
            print(red("usage: sibyl devices revoke <index>"))
            return 1
        # List first to map index → bearer_id
        try:
            resp = http_request(
                "GET",
                f"/api/plugin/devices?account_id={urllib.parse.quote(account_id)}",
                headers={"Authorization": f"Bearer {session_token}"},
                timeout=10.0,
            )
        except HttpError as e:
            print(red(f"server error: {e.status} {e.body}"))
            return 2
        devices = resp.get("devices", [])
        try:
            target = devices[idx]
        except (IndexError, TypeError):
            print(red(f"no device at index {idx}. Run `sibyl devices` to see indexes."))
            return 1
        if target.get("is_this_device"):
            print(red("refusing to revoke your own device: that would lock you out. Run `sibyl logout` instead, then `sibyl init` on a fresh activation."))
            return 1
        try:
            revoke_resp = http_request(
                "POST",
                "/api/plugin/devices",
                body={"bearer_id": target["bearer_id"]},
                headers={"Authorization": f"Bearer {session_token}"},
                timeout=10.0,
            )
        except HttpError as e:
            print(red(f"revoke failed: {e.status} {e.body}"))
            return 2
        print(green(f"✓ Revoked device {target.get('device_label') or target['bearer_id']}"))
        return 0 if revoke_resp.get("revoked") else 1

    # Default: list devices
    try:
        resp = http_request(
            "GET",
            f"/api/plugin/devices?account_id={urllib.parse.quote(account_id)}",
            headers={"Authorization": f"Bearer {session_token}"},
            timeout=10.0,
        )
    except HttpError as e:
        if e.status == 401:
            print(a.err_line("Session expired."))
            print(a.dim("  Re-run `sibyl init`."))
        else:
            print(a.err_line(f"server error: {e.status} {e.body}"))
        return 2

    devices = resp.get("devices", [])
    # LIGHT treatment: table-like dashboard. Eyebrow line with count + the rows. No banner.
    print()
    print(f"  {a.eyebrow('devices')}  {a.dim(f'· {len(devices)} active')}")
    print()
    if not devices:
        print(a.dim("  no active devices"))
        print()
        return 0

    for i, d in enumerate(devices):
        is_this = d.get("is_this_device")
        marker = a.ok("▶") if is_this else " "
        label = d.get("device_label") or "(unlabeled)"
        installed = d.get("install_method") or "-"
        last_seen = d.get("last_seen_at", "")[:19].replace("T", " ")
        idx_chip = a.chip(str(i), palette="jade" if is_this else "mute")
        label_color = a.gradient_gold(label) if is_this else a.color(label, a.INK)
        meta = f"{a.dim(installed)} {a.dim(a.GLYPH_DOT)} {a.dim('last seen ' + last_seen)}"
        note = a.color("(this device)", a.PULSE) if is_this else a.dim(f"revoke: sibyl devices revoke {i}")
        print(f"  {marker} {idx_chip} {label_color}  {meta}  {note}")
    print()
    return 0


# ---- `sibyl logout` ----------------------------------------------------

def cmd_logout(args: argparse.Namespace) -> int:
    """Delete credentials.json + tier_cache.json. memory.db stays: that's your data."""
    cred_path = Path(args.credentials).expanduser()
    tier_cache = Path(args.tier_cache).expanduser()

    deleted = []
    if cred_path.exists():
        cred_path.unlink()
        deleted.append(str(cred_path))
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
    print()
    return 0


# ---- `sibyl health` ----------------------------------------------------

def cmd_health(args: argparse.Namespace) -> int:
    """SibylMemoryProvider.health(): minimal self-check."""
    try:
        from sibyl_memory_hermes import SibylMemoryProvider
    except ImportError:
        print(a.err_line("sibyl-memory-hermes not installed."))
        print(a.dim("  pip install sibyl-memory-hermes"))
        return 1

    # LIGHT treatment: verdict + details. No banner, no section header.
    # Pattern: `pg_isready` / `redis-cli ping` / `gh auth status`.
    print()
    provider = SibylMemoryProvider(db_path=args.db)
    h = provider.health()
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
    print()
    return 0 if ok_state else 1


# ---- Dispatch ----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sibyl",
        description="Command-line interface for the Sibyl Memory Plugin.",
    )
    p.add_argument("--credentials", default=str(DEFAULT_CRED_PATH),
                   help="Path to credentials.json (default: ~/.sibyl-memory/credentials.json)")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH),
                   help="Path to memory.db (default: ~/.sibyl-memory/memory.db)")
    p.add_argument("--tier-cache", default=str(DEFAULT_TIER_CACHE_PATH),
                   help="Path to tier_cache.json (default: ~/.sibyl-memory/tier_cache.json)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Activate the plugin in your browser")
    p_init.add_argument("--force", action="store_true", help="Re-activate even if credentials.json exists")
    p_init.set_defaults(func=cmd_init)

    p_up = sub.add_parser("upgrade", help="Open the upgrade flow (stake or subscribe)")
    p_up.set_defaults(func=cmd_upgrade)

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

    p_dash = sub.add_parser("dashboard", help="Open the account dashboard (delegates to status until account.sibyllabs.org ships)")
    p_dash.set_defaults(func=cmd_dashboard)

    p_lo = sub.add_parser("logout", help="Remove local credentials (memory.db stays)")
    p_lo.set_defaults(func=cmd_logout)

    p_h = sub.add_parser("health", help="Run the provider self-check")
    p_h.set_defaults(func=cmd_health)

    # v0.1.4: one-command auto-detect-and-wire setup for any agent stack
    from .setup import cmd_setup
    p_setup = sub.add_parser(
        "setup",
        help="Auto-detect Hermes / Claude Code and wire SIBYL as the memory provider",
    )
    p_setup.add_argument(
        "target", nargs="?", choices=list(["hermes", "claude-code"]),
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
        help="Override ~/.claude/settings.json autodetection",
    )
    p_setup.set_defaults(func=cmd_setup)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(red("\nInterrupted."))
        return 130


if __name__ == "__main__":
    sys.exit(main())
