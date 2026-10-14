"""The test network guard (conftest) refuses every non-loopback connection and
name lookup, records it, and fails the test that made it. A previous build's
suite hit the production api by accident; this file fails if the guard is
missing, loosened, or no longer records."""
from __future__ import annotations

import http.server
import socket
import threading
import urllib.error
import urllib.request

import pytest

from sibyl_memory_client.testing import (
    assert_no_external_attempts,
    clear_refused_connections,
    refused_connections,
)


@pytest.mark.parametrize("address", [
    ("203.0.113.9", 443),            # TEST-NET-3
    ("8.8.8.8", 53),
    ("api.sibyllabs.org", 443),
    ("localhost.sibyllabs.org", 443),  # a prefix check would pass this
    ("127.evil.example", 80),
    ("::ffff:8.8.8.8", 53),
])
def test_non_loopback_connect_is_refused_and_recorded(address) -> None:
    start = len(refused_connections())
    fam = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(address)
        assert s.connect_ex is not None
        with pytest.raises(ConnectionRefusedError):
            s.connect_ex(address)
    finally:
        s.close()
    assert refused_connections()[start:] == [address[0], address[0]]
    clear_refused_connections(start)


def test_lookups_and_http_are_refused() -> None:
    start = len(refused_connections())
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.sibyllabs.org", 443)
    with pytest.raises(OSError):
        socket.create_connection(("api.sibyllabs.org", 443), timeout=1)
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen("https://api.sibyllabs.org/api/plugin/health", timeout=1)
    assert "api.sibyllabs.org" in refused_connections()[start:]
    clear_refused_connections(start)


def test_a_swallowed_attempt_still_fails_the_test() -> None:
    start = len(refused_connections())
    try:
        socket.create_connection(("203.0.113.10", 80), timeout=1)
    except OSError:
        pass                                  # what a careless test would do
    with pytest.raises(AssertionError, match="non-loopback"):
        assert_no_external_attempts(start)
    assert refused_connections()[start:] == []


def test_loopback_still_works() -> None:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        for host in ("127.0.0.1", "localhost"):
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as r:
                assert r.status == 204
    finally:
        srv.shutdown()


def test_guard_is_installed() -> None:
    for fn in (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,
               socket.create_connection):
        assert "forbid_external_network" in fn.__qualname__, fn
