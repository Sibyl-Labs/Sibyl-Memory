"""CLI side of the account gate (spec account-gated-plugin 2026-09-27, 3.4 and 5.6).

The gate itself lives in sibyl-memory-client (``_entitlement``). This module is
the CLI's thin layer over it:

  * write / remove ``entitlement.json`` beside the credentials file
    (``sibyl init``, ``sibyl email confirm``, ``sibyl logout``);
  * the ``X-Sibyl-Entitlement`` header the CLI adds to every
    session-authenticated call (only a token that verifies, is bound to this
    computer and is inside ``exp + og``; never a token it knows the api would
    refuse);
  * the ``access`` status row and the ``health`` line;
  * one-line copy for the api's 403 ``entitlement_invalid`` / ``client_too_old``.

Nothing here prints or logs a token or a session value.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

try:  # the CLI stays importable without the client (update / setup paths)
    from sibyl_memory_client import _entitlement as _E
except Exception:  # pragma: no cover - client absent or pre-0.9.0
    _E = None

ENTITLEMENT_FILENAME = "entitlement.json"
HEADER = "X-Sibyl-Entitlement"

CLIENT_TOO_OLD_LINE = ("update the plugin: pip install -U sibyl-memory-client sibyl-memory-mcp "
                       "sibyl-memory-hermes sibyl-memory-cli")
ENTITLEMENT_INVALID_LINE = ("this computer's sign-in file is damaged, expired or belongs to "
                            "another computer. run sibyl init to sign in again. your local "
                            "memory is kept.")
NO_ACCOUNT_MEMORY_LINE = "reading local data without an account. agents need sibyl init."
# 403 client_too_old while this computer holds no usable token: the plugin is
# current, the api refused an ABSENT token (after the min_client_version
# cutoff) because setup never finished. Same words as the setup_incomplete
# access row.
SETUP_INCOMPLETE_LINE = ("setup is not finished on this computer. connect to the internet once "
                         "to finish setup. nothing was deleted.")


# F-1 (contract C1): the api's 401 ``code`` for a computer that was signed
# out when the account's email was confirmed on the account page.
SIGNED_OUT_ON_CLAIM = "signed_out_on_claim"
SIGNED_OUT_ON_CLAIM_NEXT = "sibyl init --force"


def signed_out_on_claim_line(signed_out_at: Any = None) -> str:
    """'signed out: this computer was signed out when your email was confirmed
    on the account page (2026-10-08 18:34 UTC). run: sibyl init --force'. The
    parenthesis is left out when the api sent no usable time."""
    from ._timefmt import utc_minute
    when = utc_minute(signed_out_at) if isinstance(signed_out_at, str) else None
    at = f" ({when})" if when else ""
    return ("signed out: this computer was signed out when your email was confirmed on the "
            f"account page{at}. run: {SIGNED_OUT_ON_CLAIM_NEXT}")


def entitlement_path(cred_path: Path) -> Path:
    return Path(cred_path).expanduser().parent / ENTITLEMENT_FILENAME


def _shape_ok(token: Any) -> bool:
    """Cheap shape check before writing (spec 9.5 item 24): a string, at most
    4096 bytes, printable ASCII, three segments, SE1 prefix. The signature is
    verified by the gate on every use, never trusted from here."""
    if not isinstance(token, str) or not token or len(token) > 4096:
        return False
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        return False
    if any(b < 0x21 or b > 0x7E for b in raw):
        return False
    parts = token.split(".")
    return len(parts) == 3 and parts[0] == "SE1" and all(parts[1:])


def write_entitlement_atomic(token: Any, cred_path: Path) -> Path | None:
    """Write entitlement.json (0600, mkstemp + os.replace, symlink-refusing)
    beside ``cred_path``. Returns the path, or None when the token is
    malformed (ignored, nothing written) or the write was refused."""
    if _E is None or not _shape_ok(token):
        return None
    store = _E.EntitlementStore(entitlement_path(cred_path))
    now = time.time()
    ok = store.store({"token": token, "received_at": now, "hwm": now})
    return store.path if ok else None


def remove_entitlement(cred_path: Path) -> list[str]:
    """Delete entitlement.json (and a leftover lock). Returns what was removed."""
    removed: list[str] = []
    for name in (ENTITLEMENT_FILENAME, "entitlement.lock"):
        p = Path(cred_path).expanduser().parent / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
                if name == ENTITLEMENT_FILENAME:
                    removed.append(str(p))
        except OSError:
            pass
    return removed


def gate_for(cred_path: Path, *, background: bool = False):
    if _E is None:
        return None
    return _E.EntitlementGate(Path(cred_path).expanduser(), background_refresh=background)


def header_token(cred_path: Path) -> str | None:
    """The token to present on a session-authenticated call, or None.

    Read-only: no refresh here. A token past exp + og, unverifiable, or bound
    to another session is never sent (the api would answer 403
    entitlement_invalid); with no token the api accepts the call until
    min_client_version is set."""
    gate = gate_for(cred_path)
    if gate is None:
        return None
    try:
        return gate.peek_token()
    except Exception:
        return None


def has_account_access(cred_path: Path) -> bool:
    """True when an agent on this computer could use memory right now
    (fresh, stale or bootstrap grace), judged locally with no network."""
    gate = gate_for(cred_path)
    if gate is None:
        return False
    try:
        return gate.peek().state in ("fresh", "stale", "bootstrap")
    except Exception:
        return False


def ensure_fresh_token(cred_path: Path) -> str | None:
    """Security calls (logout revoke, device revoke): when there is no usable
    token, try one blocking /entitlement refresh first, then return whatever
    usable token exists (possibly None: the caller proceeds without it)."""
    tok = header_token(cred_path)
    if tok is not None:
        return tok
    gate = gate_for(cred_path)
    if gate is None:
        return None
    try:
        gate.refresh(blocking=True)
        return gate.peek_token()
    except Exception:
        return None


def gate_http_line(status: int, code: str | None, *, cred_path: Path | None = None,
                   body: Any = None) -> str | None:
    """One-line copy for the api's account-gate answers on CLI calls (5.6).

    ``client_too_old`` with ``cred_path`` given and no usable token on this
    computer (the client present) means the call went out WITHOUT a token and
    was refused for that: the plugin is current, setup is not finished, so the
    line is the setup one, not "update the plugin".

    F-1: a 401 whose ``code`` is ``signed_out_on_claim`` gets its own
    line, with the ``signed_out_at`` from ``body`` (the HttpError body)."""
    if status == 401 and code == SIGNED_OUT_ON_CLAIM:
        return signed_out_on_claim_line(body.get("signed_out_at") if isinstance(body, dict) else None)
    if status == 403 and code == "entitlement_invalid":
        return ENTITLEMENT_INVALID_LINE
    if status == 403 and code == "client_too_old":
        if cred_path is not None and _E is not None and header_token(cred_path) is None:
            return SETUP_INCOMPLETE_LINE
        return CLIENT_TOO_OLD_LINE
    return None


def status_info(cred_path: Path, *, refresh: bool) -> dict[str, Any] | None:
    """The gate's view for `sibyl status` / `sibyl health`. With ``refresh``,
    one blocking refresh attempt first (the refresh IS the status check).
    Returns the gate's token-free status dict plus ``refreshed`` (the attempt's
    outcome status) or None when the client is unavailable."""
    gate = gate_for(cred_path)
    if gate is None:
        return None
    outcome = None
    if refresh:
        try:
            outcome = gate.refresh(blocking=True)
        except Exception:
            outcome = None
    info = gate.status()
    info["refreshed"] = getattr(outcome, "status", None)
    info["refresh_code"] = getattr(outcome, "code", None)
    return info


def _read_only_row(info: dict[str, Any]) -> str:
    """R1 (plan 4.2): read-only because of the computer limit
    (``acct.devices.over``) or, otherwise, the email."""
    try:
        from sibyl_memory_client.exceptions import SECURITY_URL, device_limit_info
        d = device_limit_info(info.get("acct"))
    except Exception:
        d = None
    if d is not None and d["over"]:
        page = SECURITY_URL.split("://", 1)[-1]
        return ("read only on this computer · more computers signed in than the plan allows: sign "
                f"the others out at {page} (sibyl devices) or get Pro")
    return "read only · confirm your email: sibyl email add you@example.com"


def has_valid_pass(info: dict[str, Any] | None) -> bool:
    """A verified, bound pass this computer may use (fresh or stale)."""
    return bool(info) and info.get("state") in ("fresh", "stale") and isinstance(info.get("tier"), str)


def display_tier(info: dict[str, Any] | None, saved: Any) -> Any:
    """Shown-tier fix (2026-10-08): the tier every user-facing line shows. The current
    verified pass's tier (as enforced: an unrecognised label is Free) when
    there is a valid pass, else the saved credentials tier. One source per
    screen, so two lines never show two tiers."""
    if has_valid_pass(info):
        return info.get("tier_effective") or info.get("tier")
    return saved


def peek_tier(cred_path: Path, saved: Any) -> Any:
    """display_tier with no network (init, whoami, upgrade). With no
    entitlement file there is no pass, so nothing is evaluated (an evaluation
    could leave a bookkeeping-only entitlement.json behind)."""
    try:
        p = entitlement_path(cred_path)
        if p.is_symlink() or not p.is_file():
            return saved
        info = status_info(cred_path, refresh=False)
    except Exception:
        info = None
    return display_tier(info, saved)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(value: Any) -> str | None:
    """'2027-10-08T12:00:00.000Z' -> '8 Oct 2027' in this computer's local
    time, or None when the value is not an ISO timestamp."""
    import calendar
    import re
    if not isinstance(value, str):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", value.strip())
    if not m:
        return None
    try:
        y, mo, d, hh, mi, ss = (int(x) for x in m.groups())
        epoch = calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0))
        lt = time.localtime(epoch)
    except (ValueError, OverflowError, OSError):
        return None
    return f"{lt.tm_mday} {_MONTHS[lt.tm_mon - 1]} {lt.tm_year}"


def plan_row(info: dict[str, Any] | None) -> str | None:
    """Plan-end fix (2026-10-08): the `Plan` value, 'Pro · ends 8 Oct 2027' (a
    scheduled or completed cancellation, or a fixed term) or 'Pro · renews
    8 Oct 2027' (a renewing card plan), from the pass's acct.plan. None when
    there is no valid pass or the server sent no date (older servers, free,
    staker, Lifetime). The tier word is the same one the Tier row shows."""
    if not has_valid_pass(info):
        return None
    plan = info.get("plan")
    if not isinstance(plan, dict):
        return None
    try:
        from sibyl_memory_client.tiers import display
        word = display(display_tier(info, None))
    except Exception:  # pragma: no cover - client absent
        word = str(display_tier(info, None) or "").capitalize()
    for key, verb in (("ends_at", "ends"), ("renews_at", "renews")):
        when = _short_date(plan.get(key))
        if when:
            return f"{word} \u00b7 {verb} {when}"
    return None


TIER_UNKNOWN = "tier_unknown"


def _tier_unknown_row(info: dict[str, Any]) -> str | None:
    """Tier convention (2026-10-05): the two ``tier_unknown`` rows, or None.

    * the server answered 503 ``tier_unknown`` to the renewal: the last valid
      token is kept and this computer keeps working on it;
    * the signed label is not one this version recognises: treated as Free
      until the plugin is updated."""
    if info.get("state") not in ("fresh", "stale", "bootstrap"):
        return None
    if info.get("refresh_code") == TIER_UNKNOWN:
        return f"renewal paused · access label check ({TIER_UNKNOWN}) · keep working; it retries on its own"
    if info.get("state") == "fresh" and info.get("tier_known") is False:
        raw = str(info.get("tier") or "")[:32]
        return (f'active · access label "{raw}" is not recognised by this version, treated as Free '
                f"({TIER_UNKNOWN}) · run sibyl update")
    return None


def access_row(info: dict[str, Any]) -> str:
    """The `access` eyebrow value (spec 3.4, 8.2-8.5)."""
    state = info.get("state")
    code = info.get("code")
    tier_row = _tier_unknown_row(info)
    if tier_row is not None:
        return tier_row
    if state == "fresh":
        secs = info.get("renews_in_seconds") or 0
        hours = max(0, int(round(secs / 3600)))
        if info.get("mem") == "read_only":
            # Reads work, writes are refused (policy_read_only): say why.
            return _read_only_row(info) + f" · re-checks in {hours}h"
        # Plan-end fix (2026-10-08): this is the pass re-check, not a billing renewal
        # (the billing date is the Plan row).
        return f"active · re-checks in {hours}h"
    if state in ("stale", "bootstrap"):
        left = info.get("grace_left_seconds") or 0
        days = max(0, int(-(-left // 86400)))
        return f"offline · {days} days of grace left · connect once to renew"
    if state == "no_account":
        return "signed out · run sibyl init"
    if code == "policy_read_only":
        return _read_only_row(info)
    if code == "signed_out" and info.get("cause") == SIGNED_OUT_ON_CLAIM:
        return signed_out_on_claim_line(info.get("signed_out_at"))
    return {
        "expired_offline": "paused · offline too long · connect to the internet, or run sibyl init",
        "setup_incomplete": "setup not finished · connect to the internet once to finish setup",
        "signed_out": "signed out · run sibyl init",
        "under_review": "under review · email sibyl@sibyllabs.org",
        "account_deleted": "account deleted · run sibyl init",
        "invalid_entitlement": "sign-in file damaged · run sibyl init",
        "wrong_device": "sign-in file damaged · run sibyl init",
        "client_too_old": "update needed · " + CLIENT_TOO_OLD_LINE,
        "policy_read_only": "read only · confirm your email: sibyl email add you@example.com",
        "policy_none": "paused · confirm your email: sibyl email add you@example.com",
    }.get(code or "", "signed out · run sibyl init")


def _devices_over(info: dict[str, Any]) -> bool:
    try:
        from sibyl_memory_client.exceptions import device_limit_info
        d = device_limit_info(info.get("acct"))
    except Exception:
        d = None
    return d is not None and d["over"]


def code_handoff(code: str | None) -> tuple[str, str | None]:
    """(next, human) for an entitlement code (client table rows C1 to C11)."""
    try:
        from sibyl_memory_client.exceptions import ENTITLEMENT_HUMAN_ACTION, ENTITLEMENT_NEXT
    except Exception:  # pragma: no cover - client absent or pre-0.9.0
        return ("sibyl init", None)
    code = code if code in ENTITLEMENT_NEXT else "invalid_entitlement"
    return ENTITLEMENT_NEXT[code], ENTITLEMENT_HUMAN_ACTION[code]


def _read_only_handoff(info: dict[str, Any]) -> tuple[str, str | None]:
    if _devices_over(info):
        try:
            from sibyl_memory_client.exceptions import (DEVICES_READ_ONLY_HUMAN_ACTION,
                                                        DEVICES_READ_ONLY_NEXT)
            return DEVICES_READ_ONLY_NEXT, DEVICES_READ_ONLY_HUMAN_ACTION
        except Exception:  # pragma: no cover
            return "sibyl devices", None
    return code_handoff("policy_read_only")


def access_handoff(info: dict[str, Any] | None) -> tuple[str, str | None]:
    """The agent handoff for an access state (spec 2026-10-03 section 1.7):
    (next, human). Kept beside ``access_row`` so status, refresh, health and
    the gate lines agree (rows S3 to S8, R3, R4, H3c)."""
    if not info:
        return ("sibyl init", None)
    state = info.get("state")
    code = info.get("code")
    if state in ("fresh", "stale", "bootstrap"):
        if info.get("refresh_code") == TIER_UNKNOWN:
            return ("done", None)
        if state == "fresh" and info.get("tier_known") is False:
            return ("sibyl update", None)
    if state == "fresh":
        if info.get("mem") == "read_only":
            return _read_only_handoff(info)
        return ("done", None)
    if state in ("stale", "bootstrap"):
        left = info.get("grace_left_seconds") or 0
        days = max(0, int(-(-left // 86400)))
        return ("sibyl refresh",
                f"connect this computer to the internet once; memory keeps working {days} more days")
    if state == "no_account":
        return code_handoff("no_account")
    if code == "policy_read_only":
        return _read_only_handoff(info)
    if code == "signed_out" and info.get("cause") == SIGNED_OUT_ON_CLAIM:
        return (SIGNED_OUT_ON_CLAIM_NEXT, None)
    return code_handoff(code or "signed_out")


def gate_http_handoff(status: int, code: str | None, *, cred_path: Path | None = None,
                      body: Any = None) -> tuple[str, str | None] | None:
    """The handoff for the api's 403 gate answers (rows E23, K3, V4): the same
    branch choice as ``gate_http_line``."""
    line = gate_http_line(status, code, cred_path=cred_path, body=body)
    if line is None:
        return None
    if status == 401 and code == SIGNED_OUT_ON_CLAIM:
        return (SIGNED_OUT_ON_CLAIM_NEXT, None)
    if line == ENTITLEMENT_INVALID_LINE:
        return code_handoff("invalid_entitlement")
    if line == SETUP_INCOMPLETE_LINE:
        return code_handoff("setup_incomplete")
    return code_handoff("client_too_old")


ADOPT_HINT = "run sibyl memory adopt to move them into your account"


def _creds_tenant(cred_path: Path) -> str | None:
    import json
    try:
        p = Path(cred_path).expanduser()
        if p.is_symlink() or not p.is_file():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return (raw.get("tenant_id") or "") or (raw.get("account_id") or "") or None


def adopt_pre_signin(db_path: Path, cred_path: Path, *, trigger: str,
                     confirm_other_accounts: bool = False):
    """Move the rows written before sign-in (DEFAULT_TENANT) of ``db_path``
    into the signed-in account's tenant (client ``_adopt``): one local
    transaction, never deletes, no network of its own. Requires a verified
    entitlement for the credentials at ``cred_path`` (``gate.require`` may do
    the one blocking refresh a device without a token needs). Raises
    ``EntitlementError`` when there is no verified sign-in and
    ``_adopt.AdoptionRefused`` when the move is not allowed. Returns the
    ``AdoptionResult``, or None when there is no store."""
    from sibyl_memory_client import _adopt
    from sibyl_memory_client.storage import Storage
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        return None
    gate = gate_for(cred_path)
    ent = gate.require("adopt")
    tenant = _creds_tenant(cred_path)
    if ent is None or tenant is None:
        raise _adopt.AdoptionRefused("not_verified",
                                     "No verified sign-in on this computer. Run sibyl init first.")
    storage = Storage(db_path)
    try:
        return _adopt.adopt_default_tenant(storage, tenant, ent, trigger=trigger,
                                           confirm_other_accounts=confirm_other_accounts)
    finally:
        storage.close()


def pinned_kids() -> list[str]:
    if _E is None:
        return []
    try:
        return sorted(_E.TRUSTED_KEYS)
    except Exception:
        return []
