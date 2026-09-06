"""Platform support gate (2026-09-04 operator ruling).

Sibyl Memory runs on Linux, macOS (Apple Silicon and Intel), and Windows
through WSL2. Native Windows is not supported: `sibyl init` and `sibyl setup`
refuse it, every other command keeps working so existing native-Windows
accounts never lose `status`, `upgrade`, `claim`, `devices` or `logout`.

Six cases, and for each of them a pin on `_detect_os_family()`, whose return
vocabulary telemetry consumers key on and which this change must not move.
"""
from __future__ import annotations

import platform as _platform

import pytest

from sibyl_memory_cli import cli


def _fake(monkeypatch, *, sys_platform, release="", machine="", mac_ver="", env=None):
    monkeypatch.setattr(cli.sys, "platform", sys_platform, raising=False)
    monkeypatch.setattr(_platform, "release", lambda: release)
    monkeypatch.setattr(_platform, "machine", lambda: machine)
    monkeypatch.setattr(_platform, "mac_ver", lambda: (mac_ver, ("", "", ""), ""))
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv(cli.NATIVE_WINDOWS_ESCAPE_ENV, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------- supported

def test_linux(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="linux", release="6.8.0-40-generic", machine="x86_64")
    assert cli._platform_info() == ("linux", "Linux (x86_64)", True)
    assert cli._detect_os_family() == "linux"
    assert cli._is_wsl() is False
    assert cli.print_platform_line(gate=True) == 0
    assert "platform  Linux (x86_64) · supported" in capsys.readouterr().out


def test_wsl_detected_from_kernel_release(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="linux",
          release="5.15.153.1-microsoft-standard-WSL2", machine="x86_64")
    assert cli._platform_info() == ("linux", "Linux, WSL2 (x86_64)", True)
    assert cli._detect_os_family() == "linux"
    assert cli._is_wsl() is True
    assert cli.print_platform_line(gate=True) == 0
    assert "platform  Linux, WSL2 (x86_64) · supported" in capsys.readouterr().out


def test_wsl_detected_from_env_alone(monkeypatch, capsys):
    """A WSL kernel that does not carry 'microsoft' in its release string still
    exports WSL_DISTRO_NAME. Either signal is enough."""
    _fake(monkeypatch, sys_platform="linux", release="6.6.36.6", machine="aarch64",
          env={"WSL_DISTRO_NAME": "Ubuntu"})
    assert cli._platform_info() == ("linux", "Linux, WSL2 (aarch64)", True)
    assert cli._detect_os_family() == "linux"
    assert cli._is_wsl() is True
    assert cli.print_platform_line(gate=True) == 0


def test_macos(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="darwin", release="24.6.0", machine="arm64",
          mac_ver="15.6")
    assert cli._platform_info() == ("macos", "macOS 15.6 (arm64)", True)
    assert cli._detect_os_family() == "macos"
    assert cli.print_platform_line(gate=True) == 0
    assert "platform  macOS 15.6 (arm64) · supported" in capsys.readouterr().out


# ------------------------------------------------------------ native Windows

def test_native_windows_labels_itself_unsupported(monkeypatch):
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64")
    assert cli._platform_info() == ("windows", "Windows 11 native (AMD64)", False)
    assert cli._detect_os_family() == "windows"
    assert cli._is_wsl() is False


def test_native_windows_warns_and_continues_under_warn_policy(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64")
    monkeypatch.setattr(cli, "NATIVE_WINDOWS_POLICY", "warn")
    assert cli.print_platform_line(gate=True) == 0
    out = capsys.readouterr().out
    assert "platform  Windows 11 native (AMD64) · not supported" in out
    assert "Run it inside WSL2" in out
    assert cli.WSL_DOCS_URL in out
    assert "Continuing anyway." in out


def test_native_windows_blocks_init_and_setup_under_block_policy(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64")
    assert cli.NATIVE_WINDOWS_POLICY == "block"   # shipped policy
    assert cli.print_platform_line(gate=True) == 1
    out = capsys.readouterr().out
    assert "platform  Windows 11 native (AMD64) · not supported" in out
    assert "Install it inside WSL2" in out
    assert cli.WSL_DOCS_URL in out
    assert "status, upgrade, claim, devices and logout keep working." in out
    assert "SIBYL_ALLOW_NATIVE_WINDOWS=1" in out


def test_native_windows_escape_hatch_lets_init_through(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64",
          env={"SIBYL_ALLOW_NATIVE_WINDOWS": "1"})
    assert cli.print_platform_line(gate=True) == 0
    assert "Continuing anyway." in capsys.readouterr().out


def test_native_windows_never_blocks_an_ungated_command(monkeypatch, capsys):
    """The 76 native-Windows activations keep every command except init/setup."""
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64")
    assert cli.print_platform_line(gate=False) == 0
    assert "Continuing anyway." in capsys.readouterr().out


# ------------------------------------------------------------------ unknown

def test_unknown_platform_warns_but_never_blocks(monkeypatch, capsys):
    _fake(monkeypatch, sys_platform="freebsd14", release="14.1-RELEASE", machine="amd64")
    assert cli._platform_info() == ("unknown", "freebsd14", False)
    assert cli._detect_os_family() is None
    assert cli.print_platform_line(gate=True) == 0
    out = capsys.readouterr().out
    assert "platform  freebsd14 · not supported" in out
    assert "built and tested on Linux and macOS" in out


# ------------------------------------------- session-init telemetry payload

@pytest.mark.parametrize(
    "sys_platform,release,machine,expect_family,expect_wsl",
    [
        ("linux", "6.8.0-40-generic", "x86_64", "linux", False),
        ("linux", "5.15.153.1-microsoft-standard-WSL2", "x86_64", "linux", True),
        ("darwin", "24.6.0", "arm64", "macos", False),
        ("win32", "11", "AMD64", "windows", False),
    ],
)
def test_session_init_env_keys(monkeypatch, sys_platform, release, machine,
                               expect_family, expect_wsl):
    """The three new keys the server records (section 6): os_version, arch, wsl."""
    _fake(monkeypatch, sys_platform=sys_platform, release=release, machine=machine)
    assert cli._detect_os_family() == expect_family
    assert _platform.release() == release
    assert _platform.machine() == machine
    assert cli._is_wsl() is expect_wsl


def test_init_sends_the_new_env_keys(tmp_path, monkeypatch):
    """End to end: `sibyl init` posts os_version, arch and wsl to session-init."""
    _fake(monkeypatch, sys_platform="linux",
          release="5.15.153.1-microsoft-standard-WSL2", machine="x86_64")
    seen: dict = {}

    def fake_http(method, path, *, body=None, timeout=15.0, headers=None):
        if path.startswith("/api/plugin/session-init"):
            seen.update(body["env"])
            return {"pairing_ttl_seconds": 300}
        if path.startswith("/api/plugin/check"):
            return {"bound": True, "credentials": {
                "account_id": "acct-1", "tier": "free", "bearer_token": "btok",
            }}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "http_request", fake_http)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
    rc = cli.main(["--credentials", str(tmp_path / "credentials.json"), "init"])
    assert rc == 0
    assert seen["os_family"] == "linux"
    assert seen["os_version"] == "5.15.153.1-microsoft-standard-WSL2"
    assert seen["arch"] == "x86_64"
    assert seen["wsl"] is True


def test_init_refuses_native_windows_before_it_touches_the_network(tmp_path, monkeypatch):
    _fake(monkeypatch, sys_platform="win32", release="11", machine="AMD64")

    def fake_http(method, path, **kw):
        raise AssertionError("init must refuse before calling the server")

    monkeypatch.setattr(cli, "http_request", fake_http)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: True)
    cred = tmp_path / "credentials.json"
    rc = cli.main(["--credentials", str(cred), "init"])
    assert rc == 1
    assert not cred.exists()
