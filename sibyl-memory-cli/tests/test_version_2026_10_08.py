"""Version row (2026-10-08): `sibyl --version` and the `Version` row of `sibyl status`.

`sibyl --version` prints `sibyl <version>` and exits 0 without touching ~/.sibyl-memory (argparse's
version action runs before the required-subcommand check). `sibyl status` prints a `plugin` eyebrow
with one `Version  cli <v> · client <v>` row first, in both branches: not activated, and activated.

`cli.http_request` is a routing fake and the gate's refresh transport is scripted; nothing reaches
the network (the conftest refuses it).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from sibyl_memory_cli import cli
from sibyl_memory_client import _entitlement as E

ACCT = "11111111-2222-4333-8444-555555555555"
BEARER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FREE_ACCESS = {"ok": True, "tier": "free", "source": "free", "cap_bytes": 5242880}
VERSION_ROW = re.compile(r"^\s+Version\s{2,}cli (\S+) · client (\S+(?: \S+)?)\s*$", re.M)


class FakeServer:
    def __init__(self):
        self.routes: dict = {}

    def on(self, method, prefix, resp):
        self.routes[(method, prefix)] = resp
        return self

    def __call__(self, method, path, *, body=None, timeout=15.0, headers=None):
        for (m, prefix), resp in self.routes.items():
            if m == method and path.startswith(prefix):
                return json.loads(json.dumps(resp))
        raise cli.HttpError(404, {"error": "not routed"}, f"https://api{path}")


@pytest.fixture()
def server(monkeypatch):
    s = FakeServer()
    monkeypatch.setattr(cli, "http_request", s)
    return s


@pytest.fixture()
def offline_refresh(monkeypatch):
    monkeypatch.setattr(E, "http_refresh", lambda account_id, session_token: E.RefreshOutcome("transient", code="offline"))


def run(tail, tmp_path):
    return cli.main(["--credentials", str(tmp_path / "credentials.json"), "--db", str(tmp_path / "memory.db"),
                     "--tier-cache", str(tmp_path / "tier_cache.json")] + tail)


# ---- sibyl --version ---------------------------------------------------------

def test_version_flag_prints_one_line_and_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    # one line, every installed Sibyl package, the CLI first.
    assert re.fullmatch(r"sibyl cli \S+( \u00b7 [a-z]+ \S+)*\n", out), out
    assert out.startswith(f"sibyl cli {cli._client_version()}")


def test_version_flag_needs_no_subcommand_and_no_credentials_file(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        run(["--version"], tmp_path)
    assert ei.value.code == 0
    assert not (tmp_path / "credentials.json").exists()


def test_version_flag_never_touches_the_sibyl_memory_dir(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "fresh-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    default_dir = Path(cli.DEFAULT_CRED_PATH).parent
    before_default = default_dir.exists()
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert not (home / ".sibyl-memory").exists()
    assert list(home.iterdir()) == []
    assert default_dir.exists() == before_default


# ---- sibyl status: Version row ----------------------------------------------

def test_status_not_activated_prints_the_version_row_first(tmp_path, server, capsys) -> None:
    rc = run(["status"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    rows = VERSION_ROW.findall(out)
    assert len(rows) == 1, out
    assert rows[0][0] == cli._client_version()
    assert out.index("Version") < out.index("Not activated.")
    assert not (tmp_path / "credentials.json").exists()


def test_status_activated_prints_the_version_row_once_above_local(tmp_path, server, offline_refresh,
                                                                   capsys) -> None:
    c = {"account_id": ACCT, "tenant_id": ACCT, "tier": "free", "email": "u@example.com",
         "wallet": None, "issued_at": "2026-09-27T00:00:00Z", "schema_version": 3,
         "session_token": BEARER, "bearer_token": BEARER}
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps(c))
    os.chmod(p, 0o600)
    server.on("POST", "/api/plugin/access", FREE_ACCESS)
    rc = run(["status"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    rows = VERSION_ROW.findall(out)
    assert len(rows) == 1, out
    cli_v, client_v = rows[0]
    assert cli_v == cli._client_version()
    assert client_v == (cli._installed_version("sibyl-memory-client") or "not installed")
    assert out.index("Version") < out.index("Credentials")


def test_status_version_row_says_not_installed_without_the_client(tmp_path, server, monkeypatch,
                                                                  capsys) -> None:
    real = cli._installed_version
    monkeypatch.setattr(cli, "_installed_version",
                        lambda pkg: None if pkg == "sibyl-memory-client" else real(pkg))
    run(["status"], tmp_path)
    out = capsys.readouterr().out
    rows = VERSION_ROW.findall(out)
    assert len(rows) == 1 and rows[0][1] == "not installed", out
