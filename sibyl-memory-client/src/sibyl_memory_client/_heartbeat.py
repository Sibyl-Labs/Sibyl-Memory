"""Lightweight, privacy-preserving usage heartbeat.

The plugin is local-first: memory reads and writes never touch the network, so
the server has no signal for how much a user actually uses memory (it only ever
sees auth + cap pings). A heavy local user and a tire-kicker look identical in
the account's request count. This reporter closes that blind spot by sending a
periodic POST to /api/plugin/heartbeat carrying ONLY an aggregate operation
COUNT since the last beat.

Privacy + safety contract (deliberate, matches the server endpoint's design):
  - No memory content. No query text. No entity names. No PII beyond the
    account_id the client already holds. Just an integer op count.
  - Fire-and-forget: never blocks a memory operation, never raises into caller
    code, short timeout, runs on a daemon thread during the session.
  - Offline-safe: any network error is swallowed. Local memory keeps working.
  - No-op without an account_id (un-activated installs report nothing).
  - Opt out entirely with the env var SIBYL_MEMORY_TELEMETRY=0.

Cadence: debounced. A beat fires after FLUSH_EVERY ops, or after
FLUSH_INTERVAL_S of activity, and a final beat flushes the remainder at process
exit (atexit) so short sessions are still counted. The number of beats scales
with real usage, so the account's request count finally reflects engagement.
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
import urllib.request

_DEFAULT_URL = "https://api.sibyllabs.org/api/plugin/heartbeat"
_FLUSH_EVERY_OPS = 15
_FLUSH_INTERVAL_S = 600.0
_TIMEOUT_S = 4.0


def _telemetry_enabled() -> bool:
    val = os.environ.get("SIBYL_MEMORY_TELEMETRY", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


class HeartbeatReporter:
    """Accumulates memory-op counts and flushes them to /heartbeat, debounced."""

    def __init__(
        self,
        account_id: str | None,
        session_token: str | None = None,
        *,
        url: str | None = None,
        flush_every: int = _FLUSH_EVERY_OPS,
        flush_interval_s: float = _FLUSH_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self._account_id = account_id
        self._session_token = session_token
        self._url = url or os.environ.get("SIBYL_MEMORY_HEARTBEAT_URL", _DEFAULT_URL)
        self._flush_every = max(1, int(flush_every))
        self._flush_interval_s = float(flush_interval_s)
        self._enabled = bool(account_id) and enabled and _telemetry_enabled()
        self._lock = threading.Lock()
        self._ops = 0
        self._last = time.monotonic()
        if self._enabled:
            try:
                atexit.register(self._flush_final)
            except Exception:
                pass

    def record(self, kind: str = "op") -> None:
        """Count one memory operation. May trigger a debounced flush. Never raises."""
        if not self._enabled:
            return
        try:
            send_count = 0
            with self._lock:
                self._ops += 1
                now = time.monotonic()
                if self._ops >= self._flush_every or (now - self._last) >= self._flush_interval_s:
                    send_count = self._ops
                    self._ops = 0
                    self._last = now
            if send_count:
                self._fire(send_count, sync=False)
        except Exception:
            pass

    def _flush_final(self) -> None:
        try:
            with self._lock:
                send_count = self._ops
                self._ops = 0
            if send_count:
                self._fire(send_count, sync=True)
        except Exception:
            pass

    def _fire(self, ops: int, *, sync: bool) -> None:
        if sync:
            self._send(ops)
            return
        try:
            threading.Thread(target=self._send, args=(ops,), daemon=True).start()
        except Exception:
            # Thread spawn failed (rare). Best-effort inline, still swallowed.
            self._send(ops)

    def _send(self, ops: int) -> None:
        try:
            body = json.dumps(
                {"account_id": self._account_id, "event_type": "heartbeat", "heartbeat_count": int(ops)}
            ).encode("utf-8")
            headers = {"Content-Type": "application/json", "User-Agent": "sibyl-memory-client-heartbeat"}
            if self._session_token:
                headers["Authorization"] = f"Bearer {self._session_token}"
            req = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
            # Context-managed so the underlying HTTP socket closes deterministically
            # rather than waiting on GC (hygiene #15, 2026-06-30).
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                resp.read()
        except Exception:
            pass  # fire-and-forget: telemetry must never disturb local memory


class _NullHeartbeat:
    """No-op reporter used when construction fails, so callers never branch."""

    def record(self, kind: str = "op") -> None:
        return
