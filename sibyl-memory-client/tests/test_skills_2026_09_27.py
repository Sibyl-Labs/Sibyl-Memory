"""The save and debloat procedures as installed files (spec plugin-save-debloat
3.2-3.5, tests 8.1 items 1-6 and the planted-file adversarial case).

Every test in this file fails on the pre-change tree (``sibyl_memory_client.
skills`` and the ``_skills`` package data did not exist)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

import pytest

from sibyl_memory_client import skills

DENYLIST = ("SIBYL-SOUL", "SIBYL-VOICE", "SIBYL-SPEC", "SIBYL-DIARY", "treasury", "priorities.mjs",
            "INDEX.json", "roster.json", "tradingtulips", "sibylcap", "Doppler", "0x4069",
            "startup-procedure")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_1_install_writes_files_0600_in_0700_dir_and_manifest_matches(tmp_path):
    rep = skills.install(tmp_path)
    d = tmp_path / ".sibyl-memory" / "skills"
    assert rep.results == {"SAVE.md": "installed", "DEBLOAT.md": "installed"}
    assert _mode(d) == 0o700
    man = json.loads((d / "manifest.json").read_text())
    for name in ("SAVE.md", "DEBLOAT.md"):
        p = d / name
        assert _mode(p) == 0o600
        disk = hashlib.sha256(p.read_bytes()).hexdigest()
        assert man["files"][name]["sha256"] == disk == skills.sha256(name)
        assert man["files"][name]["source"].startswith("sibyl-memory-client ")
    assert man["version"] == skills.version() == "1.1.0"
    assert _mode(d / "manifest.json") == 0o600


def test_shipped_package_manifest_matches_the_shipped_bytes():
    from importlib import resources
    man = json.loads(resources.files("sibyl_memory_client").joinpath("_skills", "manifest.json")
                     .read_text(encoding="utf-8"))
    for name in skills.SKILL_NAMES:
        assert man["files"][name]["sha256"] == skills.sha256(name)


def _bump(monkeypatch, name: str, extra: bytes, ver: str = "1.2.0"):
    real = skills.shipped_bytes

    def fake(n):
        b = real(n)
        return b + extra if n == name else b
    monkeypatch.setattr(skills, "shipped_bytes", fake)
    monkeypatch.setattr(skills, "version", lambda: ver)


def test_2_never_overwrite_a_user_edit_then_force(tmp_path, monkeypatch):
    skills.install(tmp_path)
    p = tmp_path / ".sibyl-memory" / "skills" / "SAVE.md"
    edited = p.read_bytes() + b"\nmy own rule\n"
    p.write_bytes(edited)
    _bump(monkeypatch, "SAVE.md", b"\nnew upstream line\n")
    rep = skills.install(tmp_path)
    assert rep.results["SAVE.md"] == "user_modified"
    assert p.read_bytes() == edited
    side = p.with_name("SAVE.md.upstream-1.2.0")
    assert side.exists() and side.read_bytes().endswith(b"new upstream line\n")
    assert any("SAVE.md was edited by you; the new version is beside it as SAVE.md.upstream-1.2.0 "
               "(sibyl skills reset SAVE.md to take it)." == m for m in rep.messages)
    man = json.loads((p.parent / "manifest.json").read_text())
    assert man["files"]["SAVE.md"]["user_modified"] is True
    rep2 = skills.install(tmp_path, force=True)
    assert rep2.results["SAVE.md"] == "replaced"
    assert p.read_bytes().endswith(b"new upstream line\n")
    assert not side.exists()
    assert json.loads((p.parent / "manifest.json").read_text())["files"]["SAVE.md"]["user_modified"] is False


def test_3_symlinked_file_and_symlinked_dir_are_refused(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    d = tmp_path / ".sibyl-memory" / "skills"
    d.mkdir(parents=True)
    (d / "SAVE.md").symlink_to(victim)
    rep = skills.install(tmp_path)
    assert rep.results["SAVE.md"] == "refused_symlink"
    assert rep.results["DEBLOAT.md"] == "installed"
    assert victim.read_text() == "do not touch"
    # A symlinked skills/ directory.
    home2 = tmp_path / "h2"
    (home2 / ".sibyl-memory").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home2 / ".sibyl-memory" / "skills").symlink_to(target)
    rep2 = skills.install(home2)
    assert set(rep2.results.values()) == {"refused_dir"}
    assert list(target.iterdir()) == []


def test_4_unedited_file_is_replaced_by_a_new_version_without_sidecar(tmp_path, monkeypatch):
    skills.install(tmp_path)
    _bump(monkeypatch, "DEBLOAT.md", b"\nv1.2 addition\n")
    rep = skills.install(tmp_path)
    d = tmp_path / ".sibyl-memory" / "skills"
    assert rep.results["DEBLOAT.md"] == "updated"
    assert (d / "DEBLOAT.md").read_bytes().endswith(b"v1.2 addition\n")
    assert not list(d.glob("DEBLOAT.md.upstream-*"))
    man = json.loads((d / "manifest.json").read_text())
    assert man["files"]["DEBLOAT.md"]["sha256"] == hashlib.sha256((d / "DEBLOAT.md").read_bytes()).hexdigest()
    assert man["version"] == "1.2.0"


def test_second_install_is_a_noop(tmp_path):
    skills.install(tmp_path)
    rep = skills.install(tmp_path)
    assert set(rep.results.values()) == {"unchanged"}


@pytest.mark.parametrize("name", ["SAVE.md", "DEBLOAT.md"])
def test_5_watermark_blocks_intact_and_self_consistent(name):
    doc = skills.text(name)
    head, body = skills.split_frontmatter(doc)
    assert head.startswith("---\nname: sibyl-")
    assert body.startswith("<!-- SIBYL-SKILL-WATERMARK-BEGIN\n")
    assert doc.count("SIBYL-SKILL-WATERMARK-BEGIN") == 1 and doc.count("SIBYL-SKILL-WATERMARK-END -->") == 1
    wm = skills.watermark_fields(doc)
    assert wm["origin"] == "sibyl-labs"
    assert wm["file"] == name
    assert wm["notice"].startswith("Part of the Sibyl Labs open skill collection. MIT licensed")
    assert "Free to use, modify, and redistribute with this notice preserved." in wm["notice"]
    assert wm["hash"] == skills.watermark_hash(doc)
    assert "MIT" in body and "Sibyl Labs, LLC" in body


def _lifecycle_sources() -> list[Path]:
    import sibyl_memory_client
    root = Path(sibyl_memory_client.__file__).parent
    return sorted((root / "lifecycle").glob("*.py")) + [root / "skills.py", root / "_fsio.py"]


def test_6_denylist_no_internal_vocabulary_in_shipped_text_or_lifecycle_code():
    import sibyl_memory_client
    root = Path(sibyl_memory_client.__file__).parent
    texts = {n: skills.text(n) for n in skills.SKILL_NAMES}
    texts["CHANGES.md"] = (root / "_skills" / "CHANGES.md").read_text(encoding="utf-8")
    for p in _lifecycle_sources():
        texts[p.name] = p.read_text(encoding="utf-8")
    for name, t in texts.items():
        for bad in DENYLIST:
            assert bad.lower() not in t.lower(), (name, bad)
    # The internal skill is never named, and no em dash in the shipped copy.
    for n in skills.SKILL_NAMES:
        assert "—" not in skills.text(n)


def test_planted_5mb_save_md_is_refused_without_reading_past_the_bound(tmp_path, monkeypatch):
    skills.install(tmp_path)
    p = tmp_path / ".sibyl-memory" / "skills" / "SAVE.md"
    with open(p, "wb") as fh:
        fh.write(b"A" * (5 * 1024 * 1024))
    reads: list[int] = []
    real_open = open

    class _Spy:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            reads.append(n)
            return self._fh.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    def spy_open(path, mode="r", *a, **kw):
        fh = real_open(path, mode, *a, **kw)
        if str(path) == str(p) and "b" in mode:
            return _Spy(fh)
        return fh
    import sibyl_memory_client._fsio as fsio
    monkeypatch.setattr(fsio, "open", spy_open, raising=False)
    t0 = time.monotonic()
    rep = skills.install(tmp_path)
    assert time.monotonic() - t0 < 5
    assert rep.results["SAVE.md"] == "refused_oversize"
    assert p.stat().st_size == 5 * 1024 * 1024
    assert reads and all(0 < n <= skills.MAX_SKILL_BYTES + 1 for n in reads)
    assert skills.status(tmp_path)["files"]["SAVE.md"]["state"] == "oversize"
    # The MCP/agent read falls back to the shipped text.
    assert skills.canonical_text("SAVE.md", tmp_path) == skills.text("SAVE.md")


def test_canonical_text_honours_the_installed_edit(tmp_path):
    skills.install(tmp_path)
    p = tmp_path / ".sibyl-memory" / "skills" / "SAVE.md"
    p.write_text(skills.text("SAVE.md") + "\nlocal addition\n", encoding="utf-8")
    assert skills.canonical_text("SAVE.md", tmp_path).endswith("local addition\n")
    assert skills.canonical_text("DEBLOAT.md", tmp_path) == skills.text("DEBLOAT.md")
    assert skills.canonical_text("SAVE.md", tmp_path / "nohome") == skills.text("SAVE.md")


def test_status_reports_states_and_sidecars(tmp_path, monkeypatch):
    st0 = skills.status(tmp_path)
    assert st0["exists"] is False and st0["files"]["SAVE.md"]["state"] == "missing"
    skills.install(tmp_path)
    p = tmp_path / ".sibyl-memory" / "skills" / "DEBLOAT.md"
    p.write_text("mine", encoding="utf-8")
    _bump(monkeypatch, "DEBLOAT.md", b"x")
    skills.install(tmp_path)
    st = skills.status(tmp_path)
    assert st["files"]["SAVE.md"]["state"] == "ok"
    assert st["files"]["DEBLOAT.md"]["state"] == "edited"
    assert st["files"]["DEBLOAT.md"]["upstream_waiting"] == ["DEBLOAT.md.upstream-1.2.0"]


def test_shims_are_checksum_managed(tmp_path):
    shim = tmp_path / ".claude" / "skills" / "sibyl-save" / "SKILL.md"
    txt = skills.shim_text("SAVE.md", "claude-code")
    assert skills.install_shim(tmp_path, shim, txt, harness="claude-code", skill="SAVE.md") == "installed"
    assert skills.install_shim(tmp_path, shim, txt, harness="claude-code", skill="SAVE.md") == "unchanged"
    new = txt + "\nv2\n"
    assert skills.install_shim(tmp_path, shim, new, harness="claude-code", skill="SAVE.md") == "updated"
    shim.write_text("my edit", encoding="utf-8")
    assert skills.install_shim(tmp_path, shim, txt, harness="claude-code", skill="SAVE.md") == "user_modified"
    assert shim.read_text() == "my edit"
    assert skills.remove_shim(tmp_path, shim) == "user_modified"
    assert shim.exists()
    assert skills.install_shim(tmp_path, shim, txt, harness="claude-code", skill="SAVE.md",
                               force=True) == "replaced"
    assert skills.remove_shim(tmp_path, shim) == "removed"
    assert not shim.exists()


def test_shim_text_frontmatter_and_pointer():
    for name, shim in skills.SHIM_NAMES.items():
        # SD-9: Codex gets a skill too (Codex CLI 0.135.0 has skills, not prompts).
        for harness in ("claude-code", "hermes", "codex"):
            t = skills.shim_text(name, harness)
            fm = skills.frontmatter_fields(t)
            assert fm["name"] == shim and fm["description"]
            assert f"~/.sibyl-memory/skills/{name}" in t


def test_uninstall_removes_only_unedited_files(tmp_path):
    skills.install(tmp_path)
    d = tmp_path / ".sibyl-memory" / "skills"
    (d / "DEBLOAT.md").write_text("mine", encoding="utf-8")
    out = skills.uninstall(tmp_path)
    assert out["SAVE.md"] == "removed" and not (d / "SAVE.md").exists()
    assert out["DEBLOAT.md"] == "user_modified" and (d / "DEBLOAT.md").read_text() == "mine"


def test_skill_text_modes_and_markers_the_plugin_relies_on():
    save = skills.text("SAVE.md")
    for needle in ("headless", "sibyl_save:last_save", "sibyl_save:pending", "/sibyl-save curate",
                   "memory_set_state", "Never include secrets"):
        assert needle in save
    deb = skills.text("DEBLOAT.md")
    for needle in ("sibyl debloat --json", "*-history", "context-history", "sibyl debloat auto",
                   "propose", "sibyl:immutable"):
        assert needle in deb
    assert re.search(r"never trim without an explicit choice", deb, re.I)
