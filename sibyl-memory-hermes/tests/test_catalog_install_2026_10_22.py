"""Hermes plugin-catalog install (hermes-catalog/, 2026-10-22).

A catalog install clones the repo at a pinned commit and loads `hermes-catalog/`
directly; it never runs `sibyl-memory-hermes install-plugin`. These tests keep
that folder loadable and in step with the bundled adapter, and check what a
user sees when the SDK package did not install.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter

PKG_ROOT = Path(__file__).resolve().parents[1]
CATALOG = PKG_ROOT / "hermes-catalog"
BUNDLED = PKG_ROOT / "src" / "sibyl_memory_hermes" / "_hermes_plugin"


def _manifest_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{key} missing from {path}")


def _pyproject_version() -> str:
    for line in (PKG_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("no version in pyproject.toml")


def test_catalog_entrypoint_is_the_bundled_adapter() -> None:
    # The catalog loads hermes-catalog/__init__.py; install-plugin copies
    # _hermes_plugin/adapter.py. One adapter, two paths: they must not drift.
    assert (CATALOG / "__init__.py").read_bytes() == (BUNDLED / "adapter.py").read_bytes()


def test_catalog_and_bundled_manifests_agree_with_the_package_version() -> None:
    version = _pyproject_version()
    for manifest in (CATALOG / "plugin.yaml", BUNDLED / "plugin.yaml"):
        assert _manifest_value(manifest, "name") == "sibyl"
        assert _manifest_value(manifest, "version").strip('"') == version


def test_manifest_descriptions_are_quoted() -> None:
    # An unquoted "Sibyl Memory: local-first..." is not valid YAML (second colon).
    for manifest in (CATALOG / "plugin.yaml", BUNDLED / "plugin.yaml"):
        value = _manifest_value(manifest, "description")
        assert value.startswith('"') and value.endswith('"'), manifest


def test_catalog_declares_both_packages_with_upper_bounds() -> None:
    text = (CATALOG / "plugin.yaml").read_text(encoding="utf-8")
    for pkg in ("sibyl-memory-hermes", "sibyl-memory-client"):
        lines = [ln for ln in text.splitlines() if ln.strip().startswith(f"- {pkg}")]
        assert lines, f"{pkg} not declared"
        assert ">=" in lines[0] and "<" in lines[0].split(">=", 1)[1], lines[0]


def test_tool_call_names_the_missing_package_when_sdk_absent(monkeypatch) -> None:
    # sys.modules[name] = None makes `import sibyl_memory_hermes` raise ImportError,
    # which is what a catalog install without its dependencies hits.
    monkeypatch.setitem(sys.modules, "sibyl_memory_hermes", None)
    out = json.loads(SibylAdapter().handle_tool_call("sibyl_list", {}))
    assert out["error"] == "Sibyl provider not initialized"
    assert "sibyl-memory-hermes" in out["human_action"]
    assert not out["human_action"].startswith("restart Hermes")
