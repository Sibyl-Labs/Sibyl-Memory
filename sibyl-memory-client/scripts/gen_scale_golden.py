"""Freeze the 0.9.0 engine's outputs on the 3,000-row scale fixture.

Run ONCE against the unmodified 0.9.0 tree (2026-10-22, before the 2.D scale
fixes) to produce ``tests/fixtures/scale_golden_0_9_0.json``. The golden test
(``tests/test_scale_fixes_2026_10_22.py``) rebuilds the same store and asserts
the new engine reproduces every digest wherever no per-token cap binds.

    python scripts/gen_scale_golden.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SCALE_GOLDEN_SRC: point at an export of the 0.9.0 src/ tree (the golden must
# come from the unmodified engine).
import os  # noqa: E402
sys.path.insert(0, os.environ.get("SCALE_GOLDEN_SRC") or str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tests"))

import pytest  # noqa: E402

from sibyl_memory_client import MemoryClient  # noqa: E402
from sibyl_memory_client.multi_record import multi_record_search  # noqa: E402
from sibyl_memory_client.testing import (  # noqa: E402
    TestSigner, forbid_external_network, install_entitled_home, patch_trusted_keys)

import _scale_corpus as sc  # noqa: E402


def main() -> None:
    mp = pytest.MonkeyPatch()
    tmp = Path(tempfile.mkdtemp(prefix="scale-golden-"))
    home = tmp / "home"
    home.mkdir()
    mp.setenv("HOME", str(home))
    mp.setenv("HERMES_HOME", str(home / ".hermes"))
    mp.delenv("SIBYL_MEMORY_DB", raising=False)
    signer = TestSigner()
    patch_trusted_keys(mp, signer)
    forbid_external_network(mp)
    install_entitled_home(home, signer)
    try:
        c = MemoryClient.local(tmp / "small.db", tenant_id="golden")
        sc.lift_cap(c)
        sc.build_small(c)
        out = {"queries": {}}
        for q in sc.golden_queries():
            mr = multi_record_search(c, q, limit=10)
            mr_rows = sc.normalize(mr)
            for h in mr_rows:
                h.pop("snippet", None)
            raw = sc.normalize(c.search(q, limit=10))
            for h in raw:
                h.pop("snippet", None)
            out["queries"][q] = {
                "mrs_keys": [(h["tier"], h["key"]) for h in mr_rows],
                "mrs_digest": sc.digest(mr_rows),
                "verdict_digest": sc.digest(mr.verdict.as_dict()),
                "raw_digest": sc.digest(raw),
            }
        dst = HERE.parent / "tests" / "fixtures" / "scale_golden_0_9_0.json"
        dst.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        import sibyl_memory_client
        print(f"engine: {sibyl_memory_client.__file__}")
        print(f"wrote {dst} ({len(out['queries'])} queries)")
    finally:
        mp.undo()


if __name__ == "__main__":
    main()
