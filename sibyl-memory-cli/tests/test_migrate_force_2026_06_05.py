"""Regression (bugflow 2026-06-05): `sibyl migrate --force` must reach the wirers.

Onboarding dead-end: when a detected harness already had a non-sibyl memory
provider, the wirer refused with "Use --force to overwrite." but
`run_guided_setup` called `wire()` with no `force`, and `sibyl migrate` had no
`--force` flag to pass. The flag now threads cli -> run_guided_setup(force=) ->
wire(force=force).
"""
from __future__ import annotations

from sibyl_memory_cli import migrate as M
from sibyl_memory_cli.setup import WireOutcome
from sibyl_memory_client import MemoryClient


class _RecordingWirer:
    """A fake harness wirer that records the `force` kwarg it was called with."""

    name = "rec"

    def __init__(self):
        self.seen_force = None

    def is_present(self):
        return True

    def current_state(self):
        return {"wired_with_sibyl": False}

    def wire(self, *, force: bool = False, dry_run: bool = False, prompt_fn=None):
        self.seen_force = force
        return WireOutcome(self.name, "wired", "ok")


def _home_with_memory(tmp_path):
    h = tmp_path / "home"
    (h / "proj").mkdir(parents=True)
    (h / "proj" / "CLAUDE.md").write_text("# memory\n- a fact worth keeping\n")
    db = h / ".sibyl-memory" / "memory.db"
    db.parent.mkdir(parents=True)
    return h, db


def _fake_extract(_backup_dir, db_path):
    MemoryClient.local(str(db_path), tenant_id="qa").set_entity("f", "a", {"v": 1})


def test_force_true_threads_to_wirer(tmp_path):
    h, db = _home_with_memory(tmp_path)
    rec = _RecordingWirer()
    M.run_guided_setup(
        home=h, cwd=h / "proj", db_path=db, backup_parent=tmp_path / "bk",
        io=M.GuidedIO(scripted=["n"]), wirers={"rec": rec},
        extract_fn=_fake_extract, force=True,
    )
    assert rec.seen_force is True


def test_force_defaults_false(tmp_path):
    h, db = _home_with_memory(tmp_path)
    rec = _RecordingWirer()
    M.run_guided_setup(
        home=h, cwd=h / "proj", db_path=db, backup_parent=tmp_path / "bk",
        io=M.GuidedIO(scripted=["n"]), wirers={"rec": rec},
        extract_fn=_fake_extract,
    )
    assert rec.seen_force is False
