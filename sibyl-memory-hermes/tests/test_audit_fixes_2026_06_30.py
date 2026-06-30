"""Post-launch audit regressions (2026-06-30).

#17 (B001/B005): load_credentials must resolve account_id / tenant_id
     independently — never KeyError when one ID is present-but-empty and the
     other key is absent, and never silently let one ID inherit the other key's
     value (identity corruption).
#20 (B005): write_credentials must enforce 0o700 on the credentials parent dir
     regardless of the process umask (mkdir's mode is umask-masked).
#18 (B001): uninstall must handle a PermissionError on the USER-plugin path the
     same way it already handles the provider path — guidance + hard-refusal
     code, not an unhandled traceback.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from sibyl_memory_hermes import install_plugin as ip
from sibyl_memory_hermes.credentials import (
    Credentials,
    load_credentials,
    write_credentials,
)


def _write_raw(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# #17: independent ID resolution (no KeyError, no identity corruption)
# ----------------------------------------------------------------------
def test_account_id_empty_no_tenant_key_does_not_raise(tmp_path):
    """{"account_id": ""} with no tenant_id: the old `or raw["tenant_id"]` form
    KeyError'd here. Must load cleanly now (present-but-empty policy exercised)."""
    cred = _write_raw(tmp_path / "credentials.json", {"account_id": ""})
    creds = load_credentials(cred)  # must NOT raise
    # Present-but-empty account_id is never mirrored from anything; it stays
    # empty rather than corrupting identity.
    assert creds.account_id == ""
    assert creds.tenant_id == ""


def test_tenant_id_empty_no_account_key_does_not_raise(tmp_path):
    """Mirror case: empty tenant_id, no account_id key."""
    cred = _write_raw(tmp_path / "credentials.json", {"tenant_id": ""})
    creds = load_credentials(cred)  # must NOT raise
    assert creds.tenant_id == ""
    assert creds.account_id == ""


def test_present_but_empty_id_never_inherits_other_value(tmp_path):
    """Identity-corruption guard: an empty account_id must NOT silently become
    the tenant_id's value (the v0.3.11 bug)."""
    cred = _write_raw(
        tmp_path / "credentials.json",
        {"account_id": "", "tenant_id": "alice@example.com"},
    )
    creds = load_credentials(cred)
    assert creds.tenant_id == "alice@example.com"
    assert creds.account_id == "", "empty account_id must not inherit tenant_id"


def test_both_ids_present_stays_correct(tmp_path):
    """Both IDs present and distinct must round-trip unchanged."""
    cred = _write_raw(
        tmp_path / "credentials.json",
        {"account_id": "acct-123", "tenant_id": "alice@example.com"},
    )
    creds = load_credentials(cred)
    assert creds.account_id == "acct-123"
    assert creds.tenant_id == "alice@example.com"


def test_missing_key_falls_back_to_sibling(tmp_path):
    """Legacy single-key files: a genuinely MISSING id falls back to its
    sibling (backward compat), distinct from the present-but-empty case."""
    cred = _write_raw(tmp_path / "credentials.json", {"tenant_id": "alice"})
    creds = load_credentials(cred)
    assert creds.tenant_id == "alice"
    assert creds.account_id == "alice"  # missing account_id -> sibling fallback


def test_missing_both_ids_still_raises(tmp_path):
    """The pre-existing "missing both" guard is unchanged."""
    cred = _write_raw(tmp_path / "credentials.json", {"tier": "free"})
    with pytest.raises(ValueError):
        load_credentials(cred)


# ----------------------------------------------------------------------
# #20: credentials parent dir is 0o700 regardless of umask
# ----------------------------------------------------------------------
def test_write_credentials_parent_dir_is_0700(tmp_path):
    """mkdir(mode=0o700) is masked by umask; an explicit chmod must enforce
    owner-only on the credentials directory."""
    cred_dir = tmp_path / "nested" / ".sibyl-memory"
    cred_path = cred_dir / "credentials.json"
    creds = Credentials(account_id="acct-1", tenant_id="alice", tier="free")

    # Force a loose umask so an un-chmod'd mkdir would land world-traversable.
    old_umask = os.umask(0o022)
    try:
        write_credentials(creds, cred_path)
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(cred_path.parent.stat().st_mode)
    assert mode == 0o700, f"credentials dir mode {oct(mode)} != 0o700"


# ----------------------------------------------------------------------
# #18: uninstall handles PermissionError on the USER-plugin path
# ----------------------------------------------------------------------
def _sibyl_dir(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "plugin.yaml").write_text("name: sibyl\nversion: test\n")
    (parent / "__init__.py").write_text("# sibyl adapter\n")
    return parent


def test_uninstall_user_path_permission_error_handled(tmp_path, monkeypatch, capsys):
    """A PermissionError removing the user-plugin dir must be caught and turned
    into a clean refusal (guidance + return code 5), not an unhandled raise."""
    hermes_home = tmp_path / ".hermes"
    user_path = hermes_home / "plugins" / "sibyl"
    _sibyl_dir(user_path)

    def boom(dest, dry_run):
        raise PermissionError("simulated read-only user-plugin dir")

    monkeypatch.setattr(ip, "_remove_plugin_dir", boom)

    # Must not propagate the PermissionError.
    rc = ip.uninstall(hermes_home, dry_run=False, memory_provider_path=None)
    assert rc == 5, "user-path PermissionError should surface the hard-refusal code"
    out = capsys.readouterr().out
    assert "No write permission" in out
    assert "sudo rm -rf" in out
