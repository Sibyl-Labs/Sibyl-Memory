"""Regression: a pre-existing loose `~/.sibyl-memory` must be tightened to 0700.

`mkdir(mode=0o700)` is a no-op when the directory already exists, so a dir that
was created earlier at 0755 kept loose permissions on the credentials directory.
`write_credentials_atomic` now chmods the parent to 0700 explicitly.
Source: beta security report (dor_alpha, 2026-06-01).
"""
import os
import stat
import pytest
from sibyl_memory_cli.cli import write_credentials_atomic


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not stable on Windows")
def test_preexisting_loose_dir_is_tightened(tmp_path):
    d = tmp_path / ".sibyl-memory"
    d.mkdir()
    os.chmod(d, 0o755)  # simulate a pre-existing loose directory
    assert stat.S_IMODE(d.stat().st_mode) == 0o755

    write_credentials_atomic({"tenant_id": "t"}, path=d / "credentials.json")

    assert stat.S_IMODE(d.stat().st_mode) == 0o700, "parent dir not tightened to 0700"
    assert stat.S_IMODE((d / "credentials.json").stat().st_mode) == 0o600
