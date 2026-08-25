"""Shared TLS trust context for every network call the plugin family makes.

Why this exists (Darwin trust-store scar, 2026-08-25): the python.org
"Framework" build of Python on macOS ships its own OpenSSL and does NOT wire
a CA bundle into ``ssl.create_default_context()`` — users are expected to run
the bundled ``Install Certificates.command``, and many never do. On those
installs every stdlib-default HTTPS request fails with ``[SSL:
CERTIFICATE_VERIFY_FAILED] ... unable to get local issuer certificate``
against perfectly normal endpoints (api.sibyllabs.org included), while the
same URL opens fine in Safari/curl, which read the system Keychain rather
than Python's OpenSSL store. Homebrew/CLT Pythons don't have the gap, which
is why the bug looks machine-specific.

The fix is ADDITIVE trust, never replacement and never weakening:

  1. Start from ``ssl.create_default_context()`` — full verification
     (``CERT_REQUIRED`` + hostname check), the platform trust store, and the
     ``SSL_CERT_FILE``/``SSL_CERT_DIR`` env overrides all stay exactly as the
     stdlib provides them. A store that already works (Linux distro CAs,
     corporate roots) keeps working.
  2. Additionally load certifi's Mozilla CA bundle into the same context.
     On the broken macOS builds this turns an empty store into a real one;
     everywhere else it is a harmless superset (OpenSSL de-duplicates).
  3. If certifi is missing or its bundle fails to load, the context from
     step 1 is returned unchanged — byte-for-byte the pre-fix behavior,
     never a crash. certifi IS a declared runtime dependency (the SDK's
     only one, as of 0.7.1), so in any real install step 2 succeeds; the
     guard exists for bare source-tree use.

``verify_mode`` / ``check_hostname`` are never touched, here or anywhere
else. Never pass ``cafile=`` to ``create_default_context`` in this module:
that would REPLACE the system store instead of extending it.
"""
from __future__ import annotations

import ssl

_CACHED: ssl.SSLContext | None = None


def https_context() -> ssl.SSLContext:
    """Return the shared verified TLS context (built once, then cached).

    Cached because the CLI's pairing flow polls every 3 seconds for up to
    30 minutes — re-reading a ~200KB PEM bundle per poll is pointless work.
    Safe to share: an ``SSLContext`` may open connections from multiple
    threads (the heartbeat's daemon threads reuse this one instance).
    """
    global _CACHED
    if _CACHED is None:
        _CACHED = _build()
    return _CACHED


def _build() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        # No certifi (bare source tree) or an unreadable bundle: the default
        # context above already carries whatever trust the platform has.
        pass
    return ctx
