"""The whole Codex stale-path file again with tomllib hidden, as on Python
3.10 (CHECK-FABLE B-3): the text checks must stand alone there."""
from __future__ import annotations

import pytest

from sibyl_memory_cli import setup as S

from test_codex_stale_path_2026_11_13 import *  # noqa: F401,F403  (re-collect every test)
from test_codex_stale_path_2026_11_13 import _isolated_path  # noqa: F401

# Needs a TOML parser by design (a broken file elsewhere is found only by one).
del test_invalid_toml_never_modified  # noqa: F821


@pytest.fixture(autouse=True)
def _no_tomllib(monkeypatch):
    monkeypatch.setattr(S, "_tomllib", None)
