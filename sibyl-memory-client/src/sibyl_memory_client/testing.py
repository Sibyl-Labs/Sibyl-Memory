"""Test support for the Sibyl Memory plugin family (spec account-gated 4.4, D4).

Two things live here and ONLY here:

``NullEntitlementGate``
    An always-``fresh`` gate for unit tests of code that is not about the
    account gate. It is a constructor-injected object
    (``MemoryClient.local(..., entitlement_gate=NullEntitlementGate())``); no
    environment variable, config file or MCP argument can select it. The CLI's
    ``sibyl memory`` / ``sibyl migrate`` use it on purpose: they read the
    person's own local data with no account (D2).

``TestSigner`` + ``install_entitled_home``
    What each repo's ``tests/conftest.py`` uses to exercise the REAL gate: a
    throwaway Ed25519 keypair whose public key the fixture monkeypatches into
    ``sibyl_memory_client._entitlement.TRUSTED_KEYS``, and a writer that puts a
    ``credentials.json`` plus a valid ``entitlement.json`` into an isolated
    fake home. The production pinned key is untouched; a token minted here
    verifies only while a test has patched its key in.

Nothing in this module weakens the gate for a real install: a fork can inject
a null gate exactly as easily as it can delete the module (spec 1.2).
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from ._entitlement import (
    Decision,
    RefreshOutcome,
    STATE_FILENAME,
    _trusted,
    device_hash,
    kid_of,
)


class NullEntitlementGate:
    """Always ``fresh``; the tier comes from the credentials hint (the client
    falls back to it because ``tier`` is None here)."""

    credentials_path = None
    tier = None
    cap_bytes = None
    token = None

    def evaluate(self, op: str | None = None, *, allow_blocking: bool = True) -> Decision:
        return Decision("fresh")

    def prime(self) -> Decision:
        return Decision("fresh")

    def current(self) -> Decision:
        return Decision("fresh")

    def peek(self) -> Decision:
        return Decision("fresh")

    def peek_token(self) -> None:
        return None

    def require(self, op: str | None = None) -> None:
        return None

    def refresh(self, blocking: bool = True) -> RefreshOutcome:
        return RefreshOutcome("shared")

    def notify_server_rejected(self, code: str | None = None) -> None:
        return None

    def trusted_kids(self) -> list[str]:
        return sorted(_trusted())

    def status(self) -> dict[str, Any]:
        return {"state": "fresh", "code": None, "notice": None,
                "kids": self.trusted_kids()}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


TEST_ACCOUNT_ID = "11111111-2222-4333-8444-555555555555"
TEST_SESSION_TOKEN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class TestSigner:
    """A throwaway Ed25519 keypair that mints SE1 tokens for tests."""

    __test__ = False  # not a pytest test class

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._key = Ed25519PrivateKey.generate()
        self.public_raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = kid_of(self.public_raw)

    @property
    def trusted(self) -> dict[str, bytes]:
        return {self.kid: self.public_raw}

    def payload(self, *, account_id: str = TEST_ACCOUNT_ID,
                session_token: str = TEST_SESSION_TOKEN, tenant_id: str | None = None,
                now: float | None = None, **overrides: Any) -> dict[str, Any]:
        iat = int(time.time() if now is None else now)
        p: dict[str, Any] = {
            "v": 1, "kid": self.kid, "sub": account_id,
            "ten": tenant_id or account_id, "dh": device_hash(session_token),
            "tier": "free", "cap": 5242880, "src": "free",
            "iat": iat, "nbf": iat - 300, "exp": iat + 604800,
            "og": 604800, "ra": 86400, "mem": "full",
            "acct": {"complete": True, "email_verified": True, "email_required": False,
                     "grace_until": None, "enforced": False, "purchase_blocked": False},
            "iss": "api.sibyllabs.org",
        }
        p.update(overrides)
        return p

    def sign_payload(self, payload: dict[str, Any]) -> str:
        seg = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        msg = f"SE1.{seg}".encode("ascii")
        return f"SE1.{seg}.{_b64url(self._key.sign(msg))}"

    def mint(self, **kw: Any) -> str:
        return self.sign_payload(self.payload(**kw))

    def refresh_fn(self, **mint_kw: Any):
        """A refresh transport that mints a fresh token for whoever asks (the
        offline stand-in for POST /api/plugin/entitlement)."""
        def _fn(account_id: str, session_token: str) -> RefreshOutcome:
            return RefreshOutcome("ok", token=self.mint(
                account_id=account_id, session_token=session_token, **mint_kw))
        return _fn


def write_credentials(directory: str | Path, *, account_id: str = TEST_ACCOUNT_ID,
                      session_token: str | None = TEST_SESSION_TOKEN,
                      tenant_id: str | None = None, tier: str = "free",
                      **extra: Any) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    creds: dict[str, Any] = {"account_id": account_id, "tenant_id": tenant_id or account_id,
                             "tier": tier, "schema_version": 3}
    if session_token is not None:
        creds["session_token"] = session_token
    creds.update(extra)
    p = d / "credentials.json"
    p.write_text(json.dumps(creds), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def write_entitlement(directory: str | Path, token: str, *, received_at: float | None = None,
                      skew: float | None = None, **extra: Any) -> Path:
    now = time.time() if received_at is None else received_at
    state: dict[str, Any] = {"token": token, "received_at": now,
                             "skew": 0.0 if skew is None else skew, "hwm": now}
    state.update(extra)
    p = Path(directory) / STATE_FILENAME
    p.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(p, 0o600)
    return p


def install_entitled_home(home: str | Path, signer: TestSigner, **mint_kw: Any) -> Path:
    """Write ~/.sibyl-memory/{credentials,entitlement}.json under ``home`` for
    the test account, the token signed by ``signer``. Returns the directory."""
    d = Path(home) / ".sibyl-memory"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_credentials(d)
    write_entitlement(d, signer.mint(**mint_kw))
    return d


def patch_trusted_keys(monkeypatch: Any, signer: TestSigner, *, keep_pinned: bool = False) -> None:
    """Point the gate at ``signer``'s key (pytest ``monkeypatch``), and route
    every refresh through the signer so no test ever reaches the network."""
    from . import _entitlement
    keys = dict(_entitlement.TRUSTED_KEYS) if keep_pinned else {}
    keys.update(signer.trusted)
    monkeypatch.setattr(_entitlement, "TRUSTED_KEYS", keys)
    monkeypatch.setattr(_entitlement, "http_refresh", signer.refresh_fn())


# Every non-loopback connection or name lookup the guard refused, in order.
# The conftests fail any test that adds to it (a refused attempt a test
# swallowed is still a test that tried to reach a live service).
_REFUSED: list[str] = []


def _is_loopback_host(host: str) -> bool:
    """Exact loopback check: an IP literal that is loopback (127/8, ::1) or the
    unspecified address (which connects locally), or the name ``localhost``.
    A prefix test would let ``localhost.example.com`` or ``127.evil.test``
    through."""
    import ipaddress

    h = (host or "").strip().lower().strip("[]")
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def refused_connections() -> list[str]:
    """The hosts the guard refused so far in this process (a copy)."""
    return list(_REFUSED)


def clear_refused_connections(n_from: int = 0) -> None:
    """Forget refusals recorded from index ``n_from`` on (for the guard's own
    self-test, which provokes them on purpose)."""
    del _REFUSED[n_from:]


def forbid_external_network(monkeypatch: Any) -> None:
    """Refuse every non-loopback socket connection AND name lookup for the
    test's duration, so no suite can reach a live service (api.sibyllabs.org
    included) by accident, and record each refusal (``refused_connections``)
    so a test that catches the error still fails. Loopback stays open for the
    in-process HTTP servers the transport tests use."""
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def _host(address: Any) -> str:
        if isinstance(address, tuple) and address:
            host = address[0]
            if isinstance(host, bytes):
                host = host.decode("ascii", "replace")
            return str(host)
        return str(address)

    def _check(address: Any) -> None:
        if isinstance(address, (str, bytes)):  # AF_UNIX path
            return
        host = _host(address)
        if not _is_loopback_host(host):
            _REFUSED.append(host)
            raise ConnectionRefusedError(f"test network guard: refused {host}")

    def connect(self, address):  # type: ignore[no-untyped-def]
        if getattr(self, "family", None) != getattr(socket, "AF_UNIX", object()):
            _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        if getattr(self, "family", None) != getattr(socket, "AF_UNIX", object()):
            _check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if name not in (None, "") and not _is_loopback_host(str(name)):
            _REFUSED.append(str(name))
            raise socket.gaierror(socket.EAI_NONAME,
                                  f"test network guard: refused lookup of {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)


def assert_no_external_attempts(start: int) -> None:
    """Raise AssertionError when refusals were recorded since index ``start``
    (the conftests call this after every test)."""
    new = _REFUSED[start:]
    if new:
        hosts = sorted(set(new))
        del _REFUSED[start:]
        raise AssertionError(
            "test tried to open a non-loopback connection (refused by the test "
            f"network guard): {hosts}")


# ---- real-home guard (save/debloat build, 2026-09-27) -----------------------
# The lifecycle, the skills installer, the hook wiring and the Claude Code /
# Codex / Hermes shims write under the home directory. Tests must only ever
# see a temporary HOME. The conftests assert, per test, that HOME is not the
# account's real home, and at session end that none of the paths this build
# writes changed under the real home.

REAL_HOME_WATCHED = (
    ".sibyl-memory/lifecycle.json", ".sibyl-memory/lifecycle.log", ".sibyl-memory/lifecycle.lock",
    ".sibyl-memory/skills", ".sibyl-memory/debloat", ".sibyl-memory/debloat.json",
    ".sibyl-memory/debloat.lock",
    ".claude/skills/sibyl-save", ".claude/skills/sibyl-debloat", ".claude/settings.json",
    ".codex/AGENTS.md", ".codex/prompts/sibyl-save.md", ".codex/prompts/sibyl-debloat.md",
    ".codex/config.toml", ".hermes/skills/sibyl-save", ".hermes/skills/sibyl-debloat",
)


def real_home() -> Path:
    """The account's home from the password database (never ``$HOME``)."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def real_home_snapshot() -> dict[str, Any]:
    """(exists, size, mtime_ns) per watched path, plus whether the real
    ``~/.claude/settings.json`` mentions a sibyl hook."""
    home = real_home()
    snap: dict[str, Any] = {}
    for rel in REAL_HOME_WATCHED:
        p = home / rel
        try:
            st = p.lstat()
            snap[rel] = (True, st.st_size, st.st_mtime_ns)
        except OSError:
            snap[rel] = (False, 0, 0)
    try:
        txt = (home / ".claude" / "settings.json").read_text(encoding="utf-8", errors="replace")
        snap["settings_has_sibyl_hook"] = "sibyl-hook" in txt or " hook session-start" in txt
    except OSError:
        snap["settings_has_sibyl_hook"] = False
    return snap


def real_home_changes(before: dict[str, Any]) -> list[str]:
    after = real_home_snapshot()
    changed = [k for k in before if before.get(k) != after.get(k)]
    # settings.json belongs to the person's own Claude Code and changes for
    # reasons of its own: only a NEW sibyl hook in it counts against a test.
    changed = [k for k in changed if k != ".claude/settings.json"]
    return changed


def assert_home_is_not_real() -> None:
    home = Path(os.path.expanduser("~")).resolve()
    if home == real_home().resolve():
        raise AssertionError("a test ran with HOME pointing at the real home directory")


__all__ = [
    "NullEntitlementGate", "TestSigner", "assert_home_is_not_real", "assert_no_external_attempts",
    "clear_refused_connections", "forbid_external_network", "real_home", "real_home_changes",
    "real_home_snapshot", "refused_connections",
    "TEST_ACCOUNT_ID", "TEST_SESSION_TOKEN",
    "install_entitled_home", "patch_trusted_keys", "write_credentials",
    "write_entitlement",
]
