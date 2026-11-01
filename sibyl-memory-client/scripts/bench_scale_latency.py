"""Reproduce the multi_record_search latency table at 22k and 102k rows.

Synthetic stores from ``tests/_scale_corpus.build_large`` (direct SQL, the real
FTS triggers), built in a temp dir that is deleted afterwards. Never touches
``~/.sibyl-memory`` or any bench store: HOME is pointed at the temp dir, the
entitlement is a throwaway test signer, the network is forbidden.

    python scripts/bench_scale_latency.py                  # this tree, 22k + 102k
    python scripts/bench_scale_latency.py --baseline REF   # also a git ref (e.g. 708bd89 = 0.9.0)
    python scripts/bench_scale_latency.py --sizes 20000 --reps 7

Query classes (same as tests/test_scale_fixes_2026_10_22.py::test_large_store_latency):
  q15       15 planted target tokens (df ~2% each)
  q24       24 vocabulary tokens
  term16    "approved " + q15 (terminal word: the prep/terminal gate path)
  abstain   8 target tokens + one unsupported token (N1 abstention)
Reported: p50 and max of ``--reps`` timed runs after one warm-up run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent


def _env(home: Path):
    import pytest
    from sibyl_memory_client.testing import (
        TestSigner, forbid_external_network, install_entitled_home, patch_trusted_keys)
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(home))
    mp.setenv("USERPROFILE", str(home))
    mp.setenv("HERMES_HOME", str(home / ".hermes"))
    mp.delenv("SIBYL_MEMORY_DB", raising=False)
    signer = TestSigner()
    patch_trusted_keys(mp, signer)
    forbid_external_network(mp)
    install_entitled_home(home, signer)
    return mp


def _queries(facts) -> dict:
    sys.path.insert(0, str(PKG / "tests"))
    import _scale_corpus as sc
    vocab = sc.vocabulary()
    q15 = " ".join(facts["target_tokens"])
    return {
        "q15": q15,
        "q24": " ".join(vocab[300:324]),
        "term16": "approved " + q15,
        "abstain": " ".join(facts["target_tokens"][:8]) + " " + facts["unsupported"],
    }


def worker(db: str, facts_json: str, reps: int) -> None:
    """Time every query class against ``db`` with whichever engine is on sys.path."""
    home = Path(tempfile.mkdtemp(prefix="bench-home-"))
    mp = _env(home)
    try:
        from sibyl_memory_client import MemoryClient
        from sibyl_memory_client.multi_record import multi_record_search
        c = MemoryClient.local(db, tenant_id="big")
        out = {}
        for name, q in _queries(json.loads(facts_json)).items():
            res = multi_record_search(c, q, limit=10)   # warm
            ts = []
            for _ in range(reps):
                t0 = time.perf_counter()
                res = multi_record_search(c, q, limit=10)
                ts.append(time.perf_counter() - t0)
            out[name] = {"p50": statistics.median(ts) * 1000, "max": max(ts) * 1000,
                         "keys": [h["key"] for h in res]}
        print("RESULT " + json.dumps(out))
    finally:
        mp.undo()
        shutil.rmtree(home, ignore_errors=True)


def _export_ref(ref: str, dst: Path) -> Path:
    """git archive <ref>:sibyl-memory-client/src into dst (read-only use)."""
    tar = dst / "src.tar"
    with open(tar, "wb") as fh:
        subprocess.run(["git", "-C", str(PKG), "archive", ref, "src"],
                       stdout=fh, check=True)
    with tarfile.open(tar) as tf:
        tf.extractall(dst, filter="data")
    return dst / "src"


def _run(src: Path, db: Path, facts: dict, reps: int) -> dict:
    env = dict(os.environ, PYTHONPATH=str(src))
    p = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(db),
         json.dumps(facts), "--reps", str(reps)],
        env=env, capture_output=True, text=True, check=True)
    line = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="20000,100000",
                    help="entity counts (2,000 reference docs + 151 planted rows are added)")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--baseline", default=None, help="git ref to compare against")
    ap.add_argument("--worker", nargs=2, metavar=("DB", "FACTS"))
    a = ap.parse_args()
    if a.worker:
        worker(a.worker[0], a.worker[1], a.reps)
        return
    tmp = Path(tempfile.mkdtemp(prefix="bench-scale-"))
    try:
        sys.path.insert(0, str(PKG / "src"))
        sys.path.insert(0, str(PKG / "tests"))
        import _scale_corpus as sc
        engines = {"this": PKG / "src"}
        if a.baseline:
            engines[a.baseline] = _export_ref(a.baseline, tmp)
        mp = _env(tmp / "home")
        try:
            stores = []
            for n in (int(x) for x in a.sizes.split(",")):
                db = tmp / f"s{n}.db"
                facts = sc.build_large(db, "big", n_entities=n)
                stores.append((n + 2_000 + 151, db, facts))
        finally:
            mp.undo()
        print(f"{'rows':>8} {'class':>8} " + " ".join(
            f"{e[:10]:>10}p50 {'max':>6}" for e in engines))
        for rows, db, facts in stores:
            res = {e: _run(src, db, facts, a.reps) for e, src in engines.items()}
            for cls in res["this"]:
                cells = " ".join(f"{res[e][cls]['p50']:>13.0f} {res[e][cls]['max']:>6.0f}"
                                 for e in engines)
                same = ""
                if len(engines) > 1:
                    same = "  same-keys" if len({json.dumps(res[e][cls]["keys"])
                                                 for e in engines}) == 1 else "  keys-differ"
                print(f"{rows:>8} {cls:>8} {cells}{same}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
