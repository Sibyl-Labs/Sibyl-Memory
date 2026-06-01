"""Regression: ``HermesWirer._install_plugin`` must call ``install()`` with its
full required signature ``(hermes_home: Path, force: bool, dry_run: bool)``.

cli 0.3.9 shipped a ``TypeError`` here (called with only ``hermes_home`` as a
``str``) because the wider setup suite stubs ``_install_plugin`` and masked it.
This test exercises the real method with ``install`` mocked, so the arg contract
is enforced without needing the hermes runtime. Source: beta reports
"sibyl setup hermes fails on fresh install" (2026-06-01).
"""
from pathlib import Path

import sibyl_memory_hermes.install_plugin as ip
from sibyl_memory_cli import setup as cli_setup


def test_install_plugin_passes_full_signature(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ip, "install",
        lambda hermes_home, force, dry_run: (calls.append((hermes_home, force, dry_run)), 0)[1],
    )
    w = cli_setup.HermesWirer.__new__(cli_setup.HermesWirer)
    w.hermes_home = Path("/tmp/fake-hermes-home")

    w._install_plugin()  # must not raise TypeError

    assert len(calls) == 1
    hermes_home, force, dry_run = calls[0]
    assert isinstance(hermes_home, Path)
    assert force is False
    assert dry_run is False
