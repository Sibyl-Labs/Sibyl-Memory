"""Account gate (0.9.0): spec account-gated-plugin-spec-2026-09-27 sections 9.1 and 9.7.

Every test here drives the REAL EntitlementGate with tokens minted by a test
Ed25519 key that the conftest fixture pins in place of the production key. The
clock is injected (``now_fn``) wherever a band boundary is asserted, and the
refresh transport is a stub (or a loopback HTTP server for the transport
tests), so nothing reaches the network.
"""
from __future__ import annotations

import ast
import base64
import http.server
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest

import sibyl_memory_client
from sibyl_memory_client import DEFAULT_TENANT, EntitlementError, MemoryClient
from sibyl_memory_client import _entitlement as E
from sibyl_memory_client.exceptions import TierGateError, CapExceededError
from sibyl_memory_client.testing import (
    TEST_ACCOUNT_ID,
    TEST_SESSION_TOKEN,
    NullEntitlementGate,
    TestSigner,
    write_credentials,
    write_entitlement,
)

PKG_DIR = Path(sibyl_memory_client.__file__).resolve().parent
# Captured at import (collection), before the conftest routes refreshes to the
# test signer: the transport tests exercise the real HTTP code.
REAL_HTTP_REFRESH = E.http_refresh
T0 = 1_790_000_000.0
DAY = 86400


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Stub:
    """A refresh transport that returns scripted outcomes and counts calls."""

    def __init__(self, *outcomes: E.RefreshOutcome, delay: float = 0.0) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self, account_id: str, session_token: str) -> E.RefreshOutcome:
        with self._lock:
            self.calls += 1
            out = self.outcomes.pop(0) if len(self.outcomes) > 1 else (
                self.outcomes[0] if self.outcomes else E.RefreshOutcome("transient", code="x"))
        if self.delay:
            time.sleep(self.delay)
        return out


TRANSIENT = E.RefreshOutcome("transient", code="unreachable")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "sm"
    d.mkdir()
    write_credentials(d)
    return d


def _gate(home: Path, clock: Clock | None = None, stub=None, **kw) -> E.EntitlementGate:
    return E.EntitlementGate(home / "credentials.json", refresh_fn=stub or Stub(TRANSIENT),
                             now_fn=clock or Clock(), background_refresh=False, **kw)


def _install(home: Path, signer: TestSigner, *, now: float = T0, **mint) -> str:
    tok = signer.mint(now=now, **mint)
    write_entitlement(home, tok, received_at=now, skew=0.0)
    return tok


def _segments(tok: str) -> list[str]:
    return tok.split(".")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ======================================================================
# 1. Format: verify BEFORE parse
# ======================================================================

def _forgeries(signer: TestSigner) -> dict[str, str]:
    good = signer.mint()
    pre, pay, sig = _segments(good)
    other = TestSigner()
    sig_bytes = bytearray(_unb64(sig))
    sig_bytes[5] ^= 0x01
    pay_chars = list(pay)
    pay_chars[10] = "A" if pay_chars[10] != "A" else "B"
    return {
        "wrong_prefix": f"SE2.{pay}.{sig}",
        "unknown_kid": other.mint(),
        "flipped_sig": f"{pre}.{pay}.{_b64(bytes(sig_bytes))}",
        "flipped_payload": f"{pre}.{''.join(pay_chars)}.{sig}",
        "oversize": good + "A" * (E.MAX_TOKEN_BYTES + 1 - len(good)),
        "non_ascii": good[:-1] + "é",
        "missing_segment": f"{pre}.{pay}",
        "padding_present": f"{pre}.{pay}==.{sig}",
        "empty": "",
        "not_string": 12345,
    }


def test_valid_token_verifies(test_signer: TestSigner) -> None:
    ent = E.parse_and_verify(test_signer.mint(tier="pro", cap=None), test_signer.trusted)
    assert ent.tier == "pro" and ent.cap is None and ent.v == 1
    assert ent.kid == test_signer.kid == E.kid_of(test_signer.public_raw)
    assert ent.dh == E.device_hash(TEST_SESSION_TOKEN)


@pytest.mark.parametrize("case", ["wrong_prefix", "unknown_kid", "flipped_sig", "flipped_payload",
                                  "oversize", "non_ascii", "missing_segment", "padding_present",
                                  "empty", "not_string"])
def test_rejected_without_json_loads(case: str, test_signer: TestSigner,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    tok = _forgeries(test_signer)[case]
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("json.loads reached before the signature verified")

    monkeypatch.setattr(E.json, "loads", boom)
    with pytest.raises(E.EntitlementFormatError):
        E.parse_and_verify(tok, test_signer.trusted)
    assert called == []


def test_version_2_is_not_an_entitlement(test_signer: TestSigner) -> None:
    with pytest.raises(E.EntitlementVersionError):
        E.parse_and_verify(test_signer.mint(v=2), test_signer.trusted)


def test_resigned_with_unpinned_test_key_is_rejected(test_signer: TestSigner) -> None:
    rogue = TestSigner()
    tok = rogue.sign_payload(rogue.payload(tier="lifetime", cap=None))
    with pytest.raises(E.EntitlementFormatError) as exc:
        E.parse_and_verify(tok, test_signer.trusted)
    assert exc.value.reason == "signature"


def test_kid_must_name_the_verifying_key(test_signer: TestSigner) -> None:
    tok = test_signer.sign_payload(test_signer.payload(kid="0000000000000000"))
    with pytest.raises(E.EntitlementFormatError) as exc:
        E.parse_and_verify(tok, test_signer.trusted)
    assert exc.value.reason == "kid_mismatch"


def test_format_error_never_carries_token_material(test_signer: TestSigner) -> None:
    tok = _forgeries(test_signer)["flipped_sig"]
    with pytest.raises(E.EntitlementFormatError) as exc:
        E.parse_and_verify(tok, test_signer.trusted)
    assert _segments(tok)[1] not in str(exc.value)


def test_payload_matches_server_signer_layout(test_signer: TestSigner) -> None:
    """Contract with api/_lib/entitlement-signer.js signWith(): payload segment is
    base64url(JSON.stringify(payload)) with no padding, the signature covers the
    ASCII bytes of "SE1.<payload_b64>"."""
    tok = test_signer.mint()
    pre, pay, sig = tok.split(".")
    assert pre == "SE1" and "=" not in pay and "=" not in sig
    assert len(_unb64(sig)) == 64
    payload = json.loads(_unb64(pay))
    for key in ("v", "kid", "sub", "ten", "dh", "tier", "cap", "src", "iat", "nbf",
                "exp", "og", "ra", "mem", "acct", "iss"):
        assert key in payload


# ======================================================================
# 2. Binding
# ======================================================================

@pytest.mark.parametrize("field,value", [
    ("sub", "99999999-2222-4333-8444-555555555555"),   # a real other account
    ("ten", "someone-elses-tenant"),
    ("dh", E.device_hash("ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee")),  # other device
])
def test_binding_mismatch_is_wrong_device(field, value, home, test_signer) -> None:
    _install(home, test_signer, **{field: value})
    g = _gate(home)
    d = g.evaluate()
    assert d.state == "locked" and d.code == "wrong_device"
    with pytest.raises(EntitlementError) as exc:
        g.require("recall")
    assert exc.value.code == "wrong_device"


def test_device_hash_matches_server_canonicalisation() -> None:
    # api plugin-db.sessionRef: sha256 of the trimmed, lowercased token.
    assert E.device_hash("  AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE ") == E.device_hash(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


# ======================================================================
# 3. Bands at the exact boundaries
# ======================================================================

def test_bands_at_exact_boundaries(home, test_signer) -> None:
    exp_off, og = 7 * DAY, 7 * DAY
    _install(home, test_signer, exp=int(T0) + exp_off, og=og)
    clock = Clock(T0)
    g = _gate(home, clock)
    clock.t = T0 + exp_off
    assert g.evaluate().state == "fresh"
    clock.t = T0 + exp_off + 1
    d = g.evaluate()
    assert d.state == "stale" and "keeps working for 7 more days" in d.notice
    clock.t = T0 + exp_off + og
    assert g.evaluate().state == "stale"
    clock.t = T0 + exp_off + og + 1
    d = g.evaluate()
    assert d.state == "locked" and d.code == "expired_offline"


def test_nbf_tolerance(home, test_signer) -> None:
    _install(home, test_signer, nbf=int(T0) + 301)
    d = _gate(home).evaluate()
    assert d.state == "stale" and d.notice == E.CLOCK_NOTICE
    _install(home, test_signer, nbf=int(T0) + 299)
    assert _gate(home).evaluate().state == "fresh"


# ======================================================================
# 4. Skew and the clock high-water mark
# ======================================================================

def test_token_from_a_server_clock_an_hour_ahead_is_fresh(home, test_signer) -> None:
    tok = test_signer.mint(now=T0 + 3600)
    write_entitlement(home, tok, received_at=T0, skew=T0 - (T0 + 3600))
    assert _gate(home, Clock(T0)).evaluate().state == "fresh"


def test_clock_rollback_after_expiry_stays_locked(home, test_signer) -> None:
    _install(home, test_signer)
    clock = Clock(T0 + 14 * DAY + 10)
    g = _gate(home, clock)
    assert g.evaluate().code == "expired_offline"
    clock.t = T0 + 14 * DAY + 10 - 30 * DAY
    assert g.evaluate().code == "expired_offline"
    # A brand-new process (fresh gate object) reads the persisted high-water mark.
    assert _gate(home, clock).evaluate().code == "expired_offline"


def test_clock_rollback_before_expiry_changes_nothing(home, test_signer) -> None:
    _install(home, test_signer)
    clock = Clock(T0 + DAY)
    g = _gate(home, clock)
    assert g.evaluate().state == "fresh"
    clock.t = T0 - 10 * DAY
    assert g.evaluate().state == "fresh"


def test_clock_forward_past_grace_locks(home, test_signer) -> None:
    _install(home, test_signer)
    assert _gate(home, Clock(T0 + 30 * DAY)).evaluate().code == "expired_offline"


def test_successful_refresh_rewrites_skew_and_heals_clock(home, test_signer) -> None:
    _install(home, test_signer)
    clock = Clock(T0 + 30 * DAY)                    # clock jumped forward
    g = _gate(home, clock, stub=Stub(E.RefreshOutcome(
        "ok", token=test_signer.mint(now=T0 + DAY))))
    d = g.evaluate()                                 # past exp+og: blocking refresh
    assert d.state == "fresh"
    st = json.loads((home / "entitlement.json").read_text())
    assert st["skew"] == pytest.approx((T0 + 30 * DAY) - (T0 + DAY))
    clock.t = T0 + 30 * DAY + 60
    assert g.evaluate().state == "fresh"


# ======================================================================
# 5. Refresh outcomes
# ======================================================================

def test_refresh_200_replaces_token_and_clears_denied(home, test_signer) -> None:
    write_entitlement(home, "", received_at=T0, denied={"code": "signed_out", "at": T0 - DAY})
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint(now=T0, tier="pro", cap=None)))
    g = _gate(home, Clock(T0), stub)
    d = g.evaluate()
    assert d.state == "fresh" and d.entitlement.tier == "pro"
    assert "denied" not in json.loads((home / "entitlement.json").read_text())


def test_401_signs_out_immediately_even_inside_grace(home, test_signer) -> None:
    _install(home, test_signer)
    stub = Stub(E.RefreshOutcome("denied", code="signed_out"))
    clock = Clock(T0 + DAY)
    g = _gate(home, clock, stub)
    assert g.evaluate().state == "fresh"
    g.refresh(blocking=True)
    d = g.evaluate()
    assert d.state == "locked" and d.code == "signed_out"
    assert "token" not in json.loads((home / "entitlement.json").read_text())
    calls = stub.calls
    clock.t += 14 * 60
    assert g.evaluate().code == "signed_out"
    assert stub.calls == calls                        # no retry inside 15 minutes
    clock.t += 2 * 60
    g.evaluate()
    assert stub.calls == calls + 1                    # retried after 15 minutes


def test_cleared_denial_unlocks_without_init(home, test_signer) -> None:
    write_entitlement(home, "", received_at=T0, denied={"code": "under_review", "at": T0})
    clock = Clock(T0 + 16 * 60)
    g = _gate(home, clock, Stub(E.RefreshOutcome("ok", token=test_signer.mint(now=clock.t))))
    assert g.evaluate().state == "fresh"


@pytest.mark.parametrize("outcome,code", [
    (E.RefreshOutcome("denied", code="under_review"), "under_review"),
    (E.RefreshOutcome("denied", code="account_deleted"), "account_deleted"),
    (E.RefreshOutcome("denied", code="client_too_old"), "client_too_old"),
])
def test_authoritative_denials(outcome, code, home) -> None:
    d = _gate(home, stub=Stub(outcome)).evaluate()
    assert d.state == "locked" and d.code == code


def test_transient_leaves_state_unchanged(home, test_signer) -> None:
    tok = _install(home, test_signer)
    g = _gate(home, Clock(T0 + 2 * DAY), Stub(TRANSIENT))
    g.refresh(blocking=True)
    st = json.loads((home / "entitlement.json").read_text())
    assert st["token"] == tok and "denied" not in st
    assert g.evaluate().state == "fresh"


def test_retry_after_is_honoured(home) -> None:
    stub = Stub(E.RefreshOutcome("transient", code="http_429", retry_after=600))
    clock = Clock(T0)
    g = _gate(home, clock, stub)
    g.evaluate()                                      # bootstrap: one attempt
    assert stub.calls == 1
    clock.t += 120
    g.evaluate()
    assert stub.calls == 1                            # inside Retry-After
    clock.t += 600
    g.evaluate()
    assert stub.calls == 2


def test_200_with_unverifiable_token_is_ignored(home, test_signer) -> None:
    tok = _install(home, test_signer)
    rogue = TestSigner()
    g = _gate(home, Clock(T0 + 2 * DAY), Stub(E.RefreshOutcome("ok", token=rogue.mint())))
    out = g.refresh(blocking=True)
    assert out.status == "transient"
    st = json.loads((home / "entitlement.json").read_text())
    assert st["token"] == tok and st["last_refresh_error"] == "invalid"


# --- the real transport against a loopback server ----------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    reply: tuple[int, dict, dict] = (200, {}, {})
    seen: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.seen.append((self.path, dict(self.headers), body))
        status, payload, headers = _Handler.reply
        data = json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def loopback(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("SIBYL_API_BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    _Handler.seen = []
    yield _Handler
    srv.shutdown()


@pytest.mark.parametrize("status,body,expect", [
    (401, {"error": "session not found, revoked, or expired"}, ("denied", "signed_out")),
    (403, {"error": "account under review", "reason": "tamper_flag_active"}, ("denied", "under_review")),
    (403, {"code": "client_too_old", "error": "update the plugin"}, ("denied", "client_too_old")),
    (404, {"error": "account not found"}, ("denied", "account_deleted")),
    (404, {"error": "NOT_FOUND"}, ("transient", "endpoint_missing")),   # api without /entitlement (6.2)
    (503, {"code": "not_open"}, ("transient", "not_open")),              # D7 kill switch
])
def test_http_refresh_classifies_server_answers(loopback, status, body, expect) -> None:
    loopback.reply = (status, body, {})
    out = REAL_HTTP_REFRESH(TEST_ACCOUNT_ID, TEST_SESSION_TOKEN)
    assert (out.status, out.code) == expect


def test_http_refresh_request_shape(loopback, test_signer) -> None:
    loopback.reply = (200, {"ok": True, "entitlement": test_signer.mint()}, {})
    out = REAL_HTTP_REFRESH(TEST_ACCOUNT_ID, TEST_SESSION_TOKEN)
    assert out.status == "ok"
    path, headers, body = loopback.seen[-1]
    assert path == "/api/plugin/entitlement"
    assert headers["Authorization"] == f"Bearer {TEST_SESSION_TOKEN}"
    assert headers["User-Agent"].startswith("sibyl-memory-client/")
    assert body["account_id"] == TEST_ACCOUNT_ID
    assert body["client"]["pkg"] == "sibyl-memory-client"
    assert set(body["client"]) == {"pkg", "version", "os_family"}


def test_http_refresh_429_retry_after(loopback) -> None:
    loopback.reply = (429, {"error": "slow down"}, {"Retry-After": "120"})
    out = REAL_HTTP_REFRESH(TEST_ACCOUNT_ID, TEST_SESSION_TOKEN)
    assert out.status == "transient" and out.retry_after == 120


def test_bearer_never_sent_to_a_foreign_api_base(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_API_BASE", "https://evil.example.com")
    out = REAL_HTTP_REFRESH(TEST_ACCOUNT_ID, TEST_SESSION_TOKEN)
    assert (out.status, out.code) == ("transient", "untrusted_api_base")


# ======================================================================
# 6. Bootstrap (legacy credentials, no entitlement)
# ======================================================================

def test_bootstrap_has_no_grace(home) -> None:
    """0.9.0 gate fix (D10 changed to 0): legacy credentials with the server
    unreachable are refused from the first operation, not granted 7 days."""
    clock = Clock(T0)
    g = _gate(home, clock)
    d = g.evaluate()
    assert (d.state, d.code) == ("locked", "setup_incomplete")
    clock.t = T0 + 7 * DAY
    assert g.evaluate().code == "setup_incomplete"


def test_bootstrap_keeps_no_first_sight_anchor(home) -> None:
    _gate(home, Clock(T0)).evaluate()
    st = json.loads((home / "entitlement.json").read_text())
    assert "first_seen_legacy" not in st and "token" not in st
    assert _gate(home, Clock(T0 + 8 * DAY)).evaluate().code == "setup_incomplete"
    assert _gate(home, Clock(T0 + DAY)).evaluate().code == "setup_incomplete"


def test_bootstrap_success_is_fresh(home, test_signer) -> None:
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint(now=T0)))
    g = _gate(home, Clock(T0), stub)
    assert g.evaluate().state == "fresh" and stub.calls == 1


def test_construction_blocks_only_in_bootstrap(tmp_path, test_signer) -> None:
    home = tmp_path / "sm"
    write_credentials(home)
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint()))
    gate = E.EntitlementGate(home / "credentials.json", refresh_fn=stub, background_refresh=False)
    MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=gate)
    assert stub.calls == 1
    MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=gate)   # now fresh
    assert stub.calls == 1


# ======================================================================
# 7. no_account
# ======================================================================

@pytest.mark.parametrize("variant", ["missing", "symlink", "unparsable", "empty_ids",
                                     "no_session_token", "not_a_dict"])
def test_no_account_variants(variant, tmp_path, test_signer) -> None:
    d = tmp_path / "sm"
    d.mkdir()
    cred = d / "credentials.json"
    if variant == "symlink":
        real = tmp_path / "real.json"
        write_credentials(tmp_path)
        (tmp_path / "credentials.json").rename(real)
        cred.symlink_to(real)
    elif variant == "unparsable":
        cred.write_text("{not json")
    elif variant == "empty_ids":
        cred.write_text(json.dumps({"account_id": "", "session_token": ""}))
    elif variant == "no_session_token":
        write_credentials(d, session_token=None)
    elif variant == "not_a_dict":
        cred.write_text("[1, 2]")
    write_entitlement(d, test_signer.mint())
    g = _gate(d)
    assert g.evaluate().state == "no_account"
    with pytest.raises(EntitlementError) as exc:
        g.require("set_entity")
    assert exc.value.code == "no_account"


def test_deleting_credentials_mid_session(tmp_path) -> None:
    c = MemoryClient.local(str(tmp_path / "m.db"))
    c.set_entity("a", "b", {"x": 1})
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    with pytest.raises(EntitlementError) as exc:
        c.get_entity("a", "b")
    assert exc.value.code == "no_account"


# ======================================================================
# 8. Gate placement
# ======================================================================

GATED = [
    ("set_entity", ("c", "n", {"x": 1}), {}),
    ("get_entity", ("c", "n"), {}),
    ("list_entities", (), {}),
    ("delete_entity", ("c", "n"), {}),
    ("set_state", ("k", {"x": 1}), {}),
    ("get_state", ("k",), {}),
    ("write_event", (), {"acted": ["x"]}),
    ("read_events", (), {}),
    ("set_reference", ("k", "body"), {}),
    ("get_reference", ("k",), {}),
    ("archive_entity", ("c", "n"), {}),
    ("search", ("anything",), {}),
    ("search_entities", ("anything",), {}),
    ("learn", (), {}),
    ("learner", (), {}),
    ("lint", (), {}),
    ("list_skill_proposals", (), {}),
    ("accept_skill_proposal", ("p",), {}),
    ("reject_skill_proposal", ("p",), {}),
]


@pytest.fixture
def locked_client(tmp_path):
    c = MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    return c


@pytest.mark.parametrize("name,args,kwargs", GATED, ids=[g[0] for g in GATED])
def test_every_memory_method_is_gated(locked_client, name, args, kwargs) -> None:
    with pytest.raises(EntitlementError):
        getattr(locked_client, name)(*args, **kwargs)


def test_multi_record_and_refine_zero_inherit_the_gate(locked_client) -> None:
    from sibyl_memory_client.multi_record import multi_record_search
    with pytest.raises(EntitlementError):
        multi_record_search(locked_client, "anything at all")


def test_non_memory_methods_stay_open(locked_client) -> None:
    assert locked_client.get_tenant() == DEFAULT_TENANT
    assert locked_client.get_tier() == "lifetime"
    assert locked_client.storage is not None
    assert locked_client.schema_version() is not None
    assert "db_size_bytes" in locked_client.free_tier_status()
    locked_client.set_tenant("other")


def test_every_public_memory_method_carries_track_op() -> None:
    """Coverage guard: a public method that touches storage without the
    decorator would be an ungated path."""
    import inspect
    open_ok = {"get_tenant", "set_tenant", "storage", "schema_version", "get_tier",
               "set_tier", "free_tier_status", "local", "entitlement_gate",
               "require_entitlement", "entitlement_notice", "adoption",
                   "adoption_notice",   # notice accessors: no storage access
                   "enable_lifecycle_counting",  # a flag on the client: no storage access
                   "tier_for_display"}  # Shown-tier fix: reads the gate's tier, no storage access
    import textwrap
    src = inspect.getsource(sibyl_memory_client.client.MemoryClient)
    cls = ast.parse(textwrap.dedent(src)).body[0]
    missing = []
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            decos = [ast.unparse(d) for d in node.decorator_list]
            if node.name in open_ok:
                continue
            if not any(d.startswith("_track_op(") for d in decos):
                missing.append(node.name)
    assert missing == []


# ======================================================================
# 9. _effective_tier
# ======================================================================

def test_entitlement_tier_opens_paid_features(tmp_path, entitle_as) -> None:
    entitle_as("pro")
    c = MemoryClient.local(str(tmp_path / "m.db"), tier="free")
    c.lint()


def test_edited_file_tier_does_not_unlock(tmp_path) -> None:
    c = MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime")   # entitlement: free
    with pytest.raises(TierGateError):
        c.lint()


def test_no_entitlement_no_file_hint(tmp_path) -> None:
    """Pre-fix, bootstrap grace fell back to the credentials file's tier hint.
    With no grace, credentials without a verified entitlement open nothing."""
    (Path.home() / ".sibyl-memory" / "entitlement.json").unlink()
    gate = E.EntitlementGate(Path.home() / ".sibyl-memory" / "credentials.json",
                             refresh_fn=Stub(TRANSIENT), background_refresh=False)
    c = MemoryClient.local(str(tmp_path / "m.db"), tier="lifetime", entitlement_gate=gate)
    assert gate.evaluate().code == "setup_incomplete"
    with pytest.raises(EntitlementError) as ei:
        c.lint()
    assert ei.value.code == "setup_incomplete"


# ======================================================================
# 10. Concurrency
# ======================================================================

def test_two_threads_one_network_call(home, test_signer) -> None:
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint()), delay=0.3)
    g = _gate(home, Clock(time.time()), stub)
    results = []
    ts = [threading.Thread(target=lambda: results.append(g.refresh(blocking=True)))
          for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert stub.calls == 1
    assert sorted(r.status for r in results) == ["ok", "shared"]


def test_two_processes_racing_one_call_both_see_token(home, test_signer) -> None:
    """Two gate objects (as two processes would have) share only the files."""
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint()), delay=0.3)
    clock = Clock(time.time())
    a, b = _gate(home, clock, stub), _gate(home, clock, stub)
    ts = [threading.Thread(target=x.refresh) for x in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert stub.calls == 1
    assert a.evaluate().state == "fresh" and b.evaluate().state == "fresh"


def test_stale_lock_is_stolen(home, test_signer) -> None:
    lock = home / "entitlement.lock"
    lock.write_text("123")
    old = time.time() - 120
    os.utime(lock, (old, old))
    stub = Stub(E.RefreshOutcome("ok", token=test_signer.mint()))
    assert _gate(home, Clock(time.time()), stub).refresh().status == "ok"
    assert stub.calls == 1 and not lock.exists()


# ======================================================================
# 11. Files
# ======================================================================

def test_state_file_is_0600(home, test_signer) -> None:
    _gate(home, Clock(time.time()), Stub(E.RefreshOutcome("ok", token=test_signer.mint()))).refresh()
    mode = stat.S_IMODE(os.stat(home / "entitlement.json").st_mode)
    assert mode == 0o600


def test_symlinked_destination_refused_and_treated_as_absent(home, tmp_path, test_signer) -> None:
    target = tmp_path / "elsewhere.json"
    write_entitlement(tmp_path, test_signer.mint())
    (tmp_path / "entitlement.json").rename(target)
    (home / "entitlement.json").symlink_to(target)
    before = target.read_text()
    store = E.EntitlementStore(home / "entitlement.json")
    assert store.load() == {}
    assert store.store({"token": "x"}) is False
    assert target.read_text() == before
    assert _gate(home).evaluate().code == "setup_incomplete"      # absent, not valid


def test_mkstemp_cleanup_on_failure(home, monkeypatch) -> None:
    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(E.os, "replace", fail)
    assert E.EntitlementStore(home / "entitlement.json").store({"hwm": 1.0}) is False
    assert [p.name for p in home.iterdir() if p.name.endswith(".tmp")] == []


def test_oversized_state_file_rejected_with_bounded_read(home, test_signer, monkeypatch) -> None:
    (home / "entitlement.json").write_bytes(b" " * (5 * 1024 * 1024))
    reads = []
    real_fdopen = os.fdopen

    def spy(fd, *a, **k):
        fh = real_fdopen(fd, *a, **k)
        orig = fh.read

        def read(n=-1):
            reads.append(n)
            return orig(n)
        fh.read = read
        return fh
    monkeypatch.setattr(E.os, "fdopen", spy)
    assert E.EntitlementStore(home / "entitlement.json").load() == {}
    assert reads and all(0 < n <= E.MAX_STATE_BYTES + 1 for n in reads)


# ======================================================================
# 12. Source guards: no switch
# ======================================================================

def _env_keys(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = ast.unparse(node.func)
            if f in ("os.environ.get", "os.getenv", "environ.get") and node.args:
                keys.add(ast.unparse(node.args[0]))
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ"):
            keys.add(ast.unparse(node.slice))
    return keys


def test_entitlement_module_reads_only_api_base() -> None:
    assert _env_keys(PKG_DIR / "_entitlement.py") == {"'SIBYL_API_BASE'"}
    src = (PKG_DIR / "_entitlement.py").read_text()
    assert "getenv" not in src and src.count("os.environ") == 1


def test_no_skip_or_bypass_variable_anywhere_in_the_package() -> None:
    bad = re.compile(r"SIBYL_[A-Z_]*(SKIP|BYPASS|DISABLE|NO_GATE|OFFLINE|UNGATED)[A-Z_]*")
    hits = [p.name for p in PKG_DIR.rglob("*.py") if bad.search(p.read_text())]
    assert hits == []


@pytest.mark.parametrize("var", ["SIBYL_ENTITLEMENT_SKIP", "SIBYL_MEMORY_OFFLINE", "SIBYL_API_BASE"])
def test_environment_cannot_unlock(var, tmp_path, monkeypatch, test_signer) -> None:
    # The real transport runs, pointed at a closed loopback port in every case,
    # so this test can never reach a live api.
    monkeypatch.setenv("SIBYL_API_BASE", "http://127.0.0.1:9")
    if var != "SIBYL_API_BASE":
        monkeypatch.setenv(var, "1")
    # Real transport (no stub): the refresh can only fail.
    monkeypatch.setattr(E, "http_refresh", REAL_HTTP_REFRESH)
    home = tmp_path / "sm"
    write_credentials(home)
    _install(home, test_signer, now=time.time() - 30 * DAY)
    g = E.EntitlementGate(home / "credentials.json", background_refresh=False)
    d = g.evaluate()
    assert d.state == "locked" and d.code == "expired_offline"
    (home / "credentials.json").unlink()
    assert g.evaluate().state == "no_account"


# ======================================================================
# 13. /check-write payload
# ======================================================================

def test_check_write_sends_entitlement_not_the_hmac_claim(tmp_path, entitle_as, monkeypatch) -> None:
    from sibyl_memory_client import _capcheck
    seen = []

    def fake(url, payload, timeout=4.0):
        seen.append(payload)
        return {"ok": True, "tier": "pro", "cap_bytes": None}
    monkeypatch.setattr(_capcheck, "_default_check_write_fn", fake)
    entitle_as("pro")
    c = MemoryClient.local(
        str(tmp_path / "m.db"), tier="pro", account_id=TEST_ACCOUNT_ID,
        session_token=TEST_SESSION_TOKEN,
        credentials_claim={"account_id": TEST_ACCOUNT_ID, "email": "a@b.c", "wallet": "0xabc"},
        credentials_signature="f" * 64)
    c.set_entity("a", "b", {"x": 1})
    assert seen, "the paid hint should reach /check-write"
    keys = set(seen[0])
    assert "entitlement" in keys
    assert not keys & {"credentials_claim", "credentials_signature", "email", "wallet"}
    assert E.parse_and_verify(seen[0]["entitlement"]).tier == "pro"


def test_check_write_header_carries_the_entitlement(monkeypatch) -> None:
    from sibyl_memory_client import _capcheck
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None, context=None):
        captured.update(dict(req.header_items()))
        return Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _capcheck._default_check_write_fn("https://api.sibyllabs.org/x",
                                      {"session_token": "s", "entitlement": "SE1.a.b"})
    assert captured.get("X-sibyl-entitlement") == "SE1.a.b"


@pytest.mark.parametrize("code,needle", [("entitlement_invalid", "sibyl init"),
                                         ("client_too_old", "pip install -U")])
def test_check_write_403_codes_map_to_one_action(code, needle, monkeypatch) -> None:
    import io
    import urllib.error
    from sibyl_memory_client import _capcheck
    from sibyl_memory_client.exceptions import TierAuthError

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                     io.BytesIO(json.dumps({"code": code}).encode()))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(TierAuthError) as exc:
        _capcheck._default_check_write_fn("https://api.sibyllabs.org/x", {"session_token": "s"})
    assert exc.value.server_code == code and needle in str(exc.value)


def test_heartbeat_sends_entitlement_header_to_trusted_host_only(monkeypatch) -> None:
    from sibyl_memory_client._heartbeat import HeartbeatReporter
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None, context=None: sent.append(dict(req.header_items())) or Resp())
    HeartbeatReporter("acct", "tok", entitlement_fn=lambda: "SE1.t.s")._send(3)
    assert sent[-1]["X-sibyl-entitlement"] == "SE1.t.s"
    HeartbeatReporter("acct", "tok", url="https://evil.example.com/h",
                      entitlement_fn=lambda: "SE1.t.s")._send(3)
    assert "X-sibyl-entitlement" not in sent[-1] and "Authorization" not in sent[-1]


# ======================================================================
# 14. NullEntitlementGate is reachable only through .testing
# ======================================================================

def test_null_gate_importable_only_from_testing() -> None:
    import sibyl_memory_client.client as client_mod
    import sibyl_memory_client.testing as testing
    assert hasattr(testing, "NullEntitlementGate")
    for mod in (sibyl_memory_client, E, client_mod):
        assert not hasattr(mod, "NullEntitlementGate")


def test_null_gate_is_constructor_injected(tmp_path) -> None:
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    c = MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=NullEntitlementGate())
    c.set_entity("a", "b", {"x": 1})
    assert c.get_entity("a", "b")["body"] == {"x": 1}


# ======================================================================
# 9.7 adversarial list (the ones not already covered above)
# ======================================================================

def test_replayed_paid_entitlement_after_downgrade(tmp_path, entitle_as, monkeypatch) -> None:
    """A still-valid PAID token after a downgrade gives the local paid path at
    most until exp + og; /check-write carries the token and the server's
    verdict (free, over cap) is a hard denial; the next refresh says free."""
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": False, "tier": "free",
                                                           "cap_bytes": 10})
    entitle_as("pro")
    c = MemoryClient.local(str(tmp_path / "m.db"), account_id=TEST_ACCOUNT_ID,
                           session_token=TEST_SESSION_TOKEN)
    with pytest.raises(CapExceededError):
        c.set_entity("a", "b", {"x": "y" * 100})
    entitle_as("free")
    with pytest.raises(TierGateError):
        c.lint()


def test_default_tenant_without_credentials_is_refused(tmp_path) -> None:
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    c = MemoryClient.local(str(tmp_path / "m.db"), tenant_id=DEFAULT_TENANT)
    with pytest.raises(EntitlementError) as exc:
        c.set_entity("a", "b", {"x": 1})
    assert exc.value.code == "no_account"


def test_constructor_ids_without_a_credentials_file_are_refused(tmp_path) -> None:
    (Path.home() / ".sibyl-memory" / "credentials.json").unlink()
    c = MemoryClient.local(str(tmp_path / "m.db"), account_id=TEST_ACCOUNT_ID,
                           session_token=TEST_SESSION_TOKEN)
    with pytest.raises(EntitlementError):
        c.list_entities()


def test_forged_uncapped_tier_cache_never_unlocks_alone(tmp_path, monkeypatch) -> None:
    """SEC-13 extension: a tier_cache.json claiming cap_bytes null for the REAL
    account_id, with a free entitlement, still goes to the server at the cap."""
    from sibyl_memory_client import _capcheck
    calls = []
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: calls.append(1) or
                        {"ok": False, "tier": "free", "cap_bytes": 100})
    db = tmp_path / "m.db"
    (tmp_path / "tier_cache.json").write_text(json.dumps({
        "account_id": TEST_ACCOUNT_ID, "tier": "lifetime", "checked_at": time.time(),
        "cap_bytes": None}))
    c = MemoryClient.local(str(db), account_id=TEST_ACCOUNT_ID, session_token=TEST_SESSION_TOKEN)
    c._cap_gate._cap = 10
    with pytest.raises(CapExceededError):
        c.set_entity("a", "b", {"x": "y" * 200})
    assert calls


def test_mem_none_and_read_only(home, test_signer) -> None:
    _install(home, test_signer, mem="none")
    assert _gate(home).evaluate().code == "policy_none"
    _install(home, test_signer, mem="read_only")
    g = _gate(home)
    assert g.evaluate("recall").state == "fresh"
    with pytest.raises(EntitlementError) as exc:
        g.require("set_entity")
    assert exc.value.code == "policy_read_only"


def test_version_2_token_is_treated_as_absent(home, test_signer) -> None:
    write_entitlement(home, test_signer.mint(v=2), received_at=T0)
    stub = Stub(TRANSIENT)
    d = _gate(home, Clock(T0), stub).evaluate()
    assert d.code == "setup_incomplete" and stub.calls == 1


def test_deleted_entitlement_mid_session_bootstraps_not_bypasses(home, test_signer) -> None:
    _install(home, test_signer)
    stub = Stub(E.RefreshOutcome("denied", code="signed_out"))
    g = _gate(home, Clock(T0), stub)
    assert g.evaluate().state == "fresh"
    (home / "entitlement.json").unlink()
    d = g.evaluate()
    assert stub.calls == 1 and d.state == "locked" and d.code == "signed_out"


def test_error_copy_is_actionable_and_token_free(home, test_signer) -> None:
    for code in ("no_account", "signed_out", "expired_offline", "setup_incomplete", "under_review",
                 "account_deleted", "invalid_entitlement", "wrong_device",
                 "policy_read_only", "policy_none", "client_too_old"):
        e = EntitlementError(code)
        text = str(e) + e.recovery
        assert "—" not in text                      # no em dashes (house style)
        assert e.code == code and e.surface_code == "ACCOUNT_REQUIRED"
    assert "sibyl init" in str(EntitlementError("no_account"))


def test_notice_throttled_once_per_process_per_day(tmp_path, test_signer) -> None:
    from sibyl_memory_client import client as client_mod
    client_mod._NOTICE_SHOWN.clear()
    home = Path.home() / ".sibyl-memory"
    write_entitlement(home, test_signer.mint(now=time.time() - 8 * DAY),
                      received_at=time.time() - 8 * DAY)
    gate = E.EntitlementGate(home / "credentials.json", refresh_fn=Stub(TRANSIENT),
                             background_refresh=False)
    c = MemoryClient.local(str(tmp_path / "m.db"), entitlement_gate=gate)
    c.list_entities()
    first = c.entitlement_notice()
    assert first and "has not reached the server" in first
    assert c.entitlement_notice() is None


# ======================================================================
# Release gate for the pinned key (placeholder must never ship)
# ======================================================================

def _release_check():
    import importlib.util
    path = PKG_DIR.parent.parent / "scripts" / "check_release_keys.py"
    spec = importlib.util.spec_from_file_location("check_release_keys", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_release_check_blocks_the_placeholder_key() -> None:
    """The shipped _entitlement_keys.py is a PLACEHOLDER until release day: the
    release check must say so (exit 1). This test passes today and keeps
    passing after the swap only if the check then passes too (see below)."""
    from sibyl_memory_client import _entitlement_keys as K
    rc = _release_check().main(["--src", str(PKG_DIR.parent)])
    if set(K.TRUSTED_KEYS) & set(K.PLACEHOLDER_KIDS):
        assert rc == 1
    else:
        assert rc == 0


def test_release_check_compares_production_kids(test_signer) -> None:
    mod = _release_check()
    from sibyl_memory_client import _entitlement_keys as K
    problems = mod.check(str(PKG_DIR.parent), {"0123456789abcdef"})
    assert any("production kids" in p for p in problems)
    assert all(E.kid_of(v) == k for k, v in K.TRUSTED_KEYS.items())


def test_network_guard_is_active() -> None:
    import socket
    from sibyl_memory_client.testing import clear_refused_connections, refused_connections
    start = len(refused_connections())
    s = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError, match="network guard"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()
    clear_refused_connections(start)   # provoked on purpose (the conftest fails any other)


def test_upgrade_before_the_entitlement_refreshes_is_not_blocked(tmp_path, monkeypatch) -> None:
    """The account upgraded; the entitlement still says free. /check-write says
    pro (uncapped): the write lands (the in-transaction recheck trusts the
    server answer this process just got) and the gate is asked to refresh."""
    from sibyl_memory_client import _capcheck
    monkeypatch.setattr(_capcheck, "_default_check_write_fn",
                        lambda url, payload, timeout=4.0: {"ok": True, "tier": "pro",
                                                           "cap_bytes": None})
    c = MemoryClient.local(str(tmp_path / "m.db"), account_id=TEST_ACCOUNT_ID,
                           session_token=TEST_SESSION_TOKEN)
    asked = []
    c._cap_gate._on_auth_denied = asked.append
    c._cap_gate._cap = 10
    c.set_entity("a", "b", {"x": "y" * 100})
    assert asked == ["tier_changed"]


# ======================================================================
# Cross-language vector shared with the api (test/fixtures/entitlement/vector-1.json)
# ======================================================================

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "entitlement-vector-1.json").read_text())


def test_api_vector_verifies_with_its_public_key() -> None:
    raw = base64.b64decode(VECTOR["public_key_raw_b64"])
    assert raw.hex() == VECTOR["public_key_raw_hex"]
    assert E.kid_of(raw) == VECTOR["kid"] == "ea5ac758286ce1d3"
    ent = E.parse_and_verify(VECTOR["token"], {VECTOR["kid"]: raw})
    p = VECTOR["payload"]
    assert (ent.sub, ent.ten, ent.dh, ent.tier, ent.cap, ent.src, ent.iat, ent.nbf, ent.exp,
            ent.og, ent.ra, ent.mem, ent.iss) == (
        p["sub"], p["ten"], p["dh"], p["tier"], p["cap"], p["src"], p["iat"], p["nbf"],
        p["exp"], p["og"], p["ra"], p["mem"], p["iss"])
    assert ent.acct == p["acct"] and set(ent.acct) >= {"claimed", "frozen"}
    assert E.device_hash(VECTOR["session_token"]) == p["dh"]


def test_api_vector_reproduces_byte_for_byte_from_the_seed() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import hashlib
    seed = hashlib.sha256(VECTOR["seed_label"].encode("utf-8")).digest()
    assert seed.hex() == VECTOR["seed_hex"]
    key = Ed25519PrivateKey.from_private_bytes(seed)
    seg = base64.urlsafe_b64encode(VECTOR["payload_json"].encode("utf-8")).rstrip(b"=").decode()
    signing_input = f"SE1.{seg}"
    assert signing_input == VECTOR["signing_input"]
    sig = base64.urlsafe_b64encode(key.sign(signing_input.encode("ascii"))).rstrip(b"=").decode()
    assert f"{signing_input}.{sig}" == VECTOR["token"]


def test_api_vector_is_rejected_by_the_pinned_set() -> None:
    from sibyl_memory_client import _entitlement_keys as K
    assert VECTOR["kid"] not in K.TRUSTED_KEYS
    with pytest.raises(E.EntitlementFormatError):
        E.parse_and_verify(VECTOR["token"], K.TRUSTED_KEYS)


def test_heartbeat_user_agent_carries_the_version(monkeypatch) -> None:
    from sibyl_memory_client._heartbeat import HeartbeatReporter
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None, context=None: sent.append(dict(req.header_items())) or Resp())
    HeartbeatReporter("acct", "tok")._send(1)
    assert re.fullmatch(r"sibyl-memory-client/\S+", sent[-1]["User-agent"])
