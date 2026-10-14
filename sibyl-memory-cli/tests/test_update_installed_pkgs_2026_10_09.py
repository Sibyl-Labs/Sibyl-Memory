"""`sibyl update` covers every INSTALLED Sibyl package (2026-10-09).

The installer installs `sibyl-memory-cli[mcp]`, so the MCP server is on every standard install;
`sibyl update --apply` used to leave it on the old version. Now cli, hermes, client, mcp and
langgraph are each checked and upgraded when installed, and a package that is not installed is
neither shown nor installed by --apply.

PyPI (`_pypi_latest`), importlib.metadata (`_installed_version`), the install-method probe and
`subprocess.call` are stubbed; nothing reaches the network.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from sibyl_memory_cli import cli

ALL = ("sibyl-memory-cli", "sibyl-memory-hermes", "sibyl-memory-client",
       "sibyl-memory-mcp", "sibyl-memory-langgraph")


def _stub(monkeypatch, installed: dict, latest: dict, method: str = "venv"):
    asked = []

    def pypi(pkg, timeout=4.0):
        asked.append(pkg)
        return latest.get(pkg)

    monkeypatch.setattr(cli, "_installed_version", lambda pkg: installed.get(pkg))
    monkeypatch.setattr(cli, "_pypi_latest", pypi)
    monkeypatch.setattr(cli, "_detect_install_method", lambda: method)
    return asked


# The production install: cli[mcp] pulls client + hermes + mcp. No langgraph.
STD = {"sibyl-memory-cli": "0.5.0", "sibyl-memory-hermes": "0.5.0",
       "sibyl-memory-client": "0.9.0", "sibyl-memory-mcp": "0.3.0"}
CURRENT = {"sibyl-memory-cli": "0.5.0", "sibyl-memory-hermes": "0.5.0",
           "sibyl-memory-client": "0.9.0", "sibyl-memory-mcp": "0.3.0",
           "sibyl-memory-langgraph": "0.3.0"}


def _pip_calls(monkeypatch, rc=0):
    calls = []

    def call(args, *a, **k):
        calls.append(list(args))
        return rc
    monkeypatch.setattr(subprocess, "call", call)
    return calls


def test_update_packages_lists_all_five():
    assert cli.UPDATE_PACKAGES == ALL


def test_mcp_outdated_is_listed_and_in_the_pip_call(monkeypatch, capsys):
    asked = _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-mcp": "0.4.0"})
    calls = _pip_calls(monkeypatch)
    rc = cli.main(["update", "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sibyl-memory-mcp" in out and "0.4.0" in out
    pip = calls[0]
    assert pip[1:5] == ["-m", "pip", "install", "-U"]
    assert pip[5:] == ["sibyl-memory-mcp"]
    # langgraph is not installed: never asked of PyPI, never shown.
    assert "sibyl-memory-langgraph" not in asked
    assert "sibyl-memory-langgraph" not in out


def test_cli_and_mcp_upgrade_in_one_pip_call(monkeypatch, capsys):
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-cli": "0.6.0", "sibyl-memory-mcp": "0.4.0"})
    calls = _pip_calls(monkeypatch)
    assert cli.main(["update", "--apply"]) == 0
    capsys.readouterr()
    pip_calls = [c for c in calls if "pip" in c]
    assert len(pip_calls) == 1
    assert pip_calls[0][5:] == ["sibyl-memory-cli", "sibyl-memory-mcp"]


def test_langgraph_not_installed_absent_from_rows_and_pip_args(monkeypatch, capsys):
    out_of_date = {p: "9.9.9" for p in ALL}
    _stub(monkeypatch, STD, out_of_date)
    calls = _pip_calls(monkeypatch)
    cli.main(["update", "--apply"])
    out = capsys.readouterr().out
    assert "langgraph" not in out
    assert "sibyl-memory-langgraph" not in calls[0]
    assert calls[0][5:] == ["sibyl-memory-cli", "sibyl-memory-hermes",
                            "sibyl-memory-client", "sibyl-memory-mcp"]


def test_langgraph_installed_is_included(monkeypatch, capsys):
    installed = {**STD, "sibyl-memory-langgraph": "0.3.0"}
    _stub(monkeypatch, installed, {**CURRENT, "sibyl-memory-langgraph": "0.4.0"})
    calls = _pip_calls(monkeypatch)
    assert cli.main(["update", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "sibyl-memory-langgraph" in out
    assert calls[0][5:] == ["sibyl-memory-langgraph"]


def test_langgraph_installed_and_current_shows_as_a_row(monkeypatch, capsys):
    installed = {**STD, "sibyl-memory-langgraph": "0.3.0"}
    _stub(monkeypatch, installed, CURRENT)
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "All packages current." in out
    for p in ALL:
        assert p in out
    assert "(not installed)" not in out


def test_json_shape_unchanged_except_rows(monkeypatch, capsys):
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-mcp": "0.4.0"})
    rc = cli.main(["update", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert doc["any_outdated"] is True
    assert doc["next"] == "sibyl update --apply"
    pkgs = [r["pkg"] for r in doc["packages"]]
    assert pkgs == ["sibyl-memory-cli", "sibyl-memory-hermes",
                    "sibyl-memory-client", "sibyl-memory-mcp"]
    for r in doc["packages"]:
        assert set(r) == {"pkg", "installed", "latest", "outdated"}
        assert r["installed"] is not None
    mcp = doc["packages"][3]
    assert mcp == {"pkg": "sibyl-memory-mcp", "installed": "0.3.0",
                   "latest": "0.4.0", "outdated": True}


def test_json_with_langgraph_installed_has_five_rows(monkeypatch, capsys):
    _stub(monkeypatch, {**STD, "sibyl-memory-langgraph": "0.3.0"}, CURRENT)
    assert cli.main(["update", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [r["pkg"] for r in doc["packages"]] == list(ALL)
    assert doc["any_outdated"] is False and doc["next"] == "done"


@pytest.mark.parametrize("argv", [["update"], ["update", "--apply"]])
def test_pipx_hint_lists_mcp_when_outdated(monkeypatch, capsys, argv):
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-cli": "0.6.0", "sibyl-memory-mcp": "0.4.0"},
          method="pipx")
    calls = _pip_calls(monkeypatch)
    rc = cli.main(argv)
    out = capsys.readouterr().out
    assert rc == 2
    assert "pipx upgrade sibyl-memory-cli sibyl-memory-mcp" in out
    assert not calls  # pipx is never driven through pip


def test_pipx_json_next_lists_mcp(monkeypatch, capsys):
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-mcp": "0.4.0"}, method="pipx")
    cli.main(["update", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["next"] == "pipx upgrade sibyl-memory-mcp"


def test_pep440_compare_kept_for_mcp(monkeypatch, capsys):
    # 0.3.0 vs 0.3 is equal, 0.3.0 vs 0.3.1rc1 is older.
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-mcp": "0.3"})
    assert cli.main(["update", "--json"]) == 0
    capsys.readouterr()
    _stub(monkeypatch, STD, {**CURRENT, "sibyl-memory-mcp": "0.3.1rc1"})
    assert cli.main(["update", "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert [r["pkg"] for r in doc["packages"] if r["outdated"]] == ["sibyl-memory-mcp"]


def test_mcp_pypi_unreachable_is_retry(monkeypatch, capsys):
    latest = dict(CURRENT)
    latest["sibyl-memory-mcp"] = None
    _stub(monkeypatch, STD, latest)
    assert cli.main(["update", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["next"] == "retry"
