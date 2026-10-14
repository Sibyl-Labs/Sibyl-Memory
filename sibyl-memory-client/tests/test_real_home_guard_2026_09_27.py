"""The real-home guard itself (save/debloat build 2026-09-27): tests never
see the account's real home, and a change under the real home is detected.
Fails on the pre-change tree (the helpers did not exist)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sibyl_memory_client import testing as T


def test_home_during_tests_is_not_the_real_home():
    assert Path.home().resolve() != T.real_home().resolve()
    T.assert_home_is_not_real()


def test_guard_raises_when_home_is_the_real_home():
    old = os.environ["HOME"]
    os.environ["HOME"] = str(T.real_home())
    try:
        with pytest.raises(AssertionError):
            T.assert_home_is_not_real()
    finally:
        os.environ["HOME"] = old


def test_snapshot_detects_a_new_file_under_the_watched_home(tmp_path, monkeypatch):
    fake_real = tmp_path / "realhome"
    fake_real.mkdir()
    monkeypatch.setattr(T, "real_home", lambda: fake_real)
    before = T.real_home_snapshot()
    assert T.real_home_changes(before) == []
    (fake_real / ".sibyl-memory").mkdir()
    (fake_real / ".sibyl-memory" / "lifecycle.json").write_text("{}")
    (fake_real / ".claude" / "skills" / "sibyl-save").mkdir(parents=True)
    changed = T.real_home_changes(before)
    assert ".sibyl-memory/lifecycle.json" in changed and ".claude/skills/sibyl-save" in changed


def test_snapshot_flags_a_sibyl_hook_added_to_settings(tmp_path, monkeypatch):
    fake_real = tmp_path / "realhome"
    (fake_real / ".claude").mkdir(parents=True)
    (fake_real / ".claude" / "settings.json").write_text('{"hooks": {}}')
    monkeypatch.setattr(T, "real_home", lambda: fake_real)
    before = T.real_home_snapshot()
    (fake_real / ".claude" / "settings.json").write_text('{"x": "/bin/sibyl hook session-start"}')
    assert T.real_home_changes(before) == ["settings_has_sibyl_hook"]
