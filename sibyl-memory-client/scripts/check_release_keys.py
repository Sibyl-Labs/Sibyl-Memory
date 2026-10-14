#!/usr/bin/env python3
"""Release gate for the pinned entitlement keys (spec account-gated 2.4, 7.1 step 10).

Fails (exit 1) while any kid in sibyl_memory_client._entitlement_keys.TRUSTED_KEYS
is a PLACEHOLDER, while a pinned key is malformed (not 32 bytes, kid does not
match sha256(raw)[:16]), or, when --expect-kids is given, while the pinned kid
set differs from the kids the operator read from production
/api/plugin/health `entitlement_public_keys`. No network: the expected kids
are an argument.

    python scripts/check_release_keys.py --expect-kids 3f9a1c2b7e4d6a80
    python scripts/check_release_keys.py --src path/to/src   # check a tree, not the install
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from pathlib import Path


def _load_keys_module(src: str | None):
    if src:
        path = Path(src) / "sibyl_memory_client" / "_entitlement_keys.py"
        spec = importlib.util.spec_from_file_location("_pinned_keys_under_check", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return mod
    from sibyl_memory_client import _entitlement_keys
    return _entitlement_keys


def check(src: str | None, expect: set[str] | None) -> list[str]:
    mod = _load_keys_module(src)
    keys = dict(mod.TRUSTED_KEYS)
    placeholders = set(getattr(mod, "PLACEHOLDER_KIDS", frozenset()))
    problems: list[str] = []
    if not keys:
        problems.append("TRUSTED_KEYS is empty")
    if not 1 <= len(keys) <= 2:
        problems.append(f"TRUSTED_KEYS holds {len(keys)} keys (expected 1, or 2 during a rotation)")
    for kid, raw in keys.items():
        if kid in placeholders:
            problems.append(f"kid {kid} is a PLACEHOLDER key; replace it with the production public key")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
            problems.append(f"kid {kid}: public key is not 32 raw bytes")
        elif hashlib.sha256(bytes(raw)).hexdigest()[:16] != kid:
            problems.append(f"kid {kid}: does not equal sha256(raw public key)[:16]")
    if expect is not None and set(keys) != expect:
        problems.append(f"pinned kids {sorted(keys)} != production kids {sorted(expect)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect-kids", default=None,
                    help="comma-separated kids from production /api/plugin/health")
    ap.add_argument("--src", default=None, help="check this source tree instead of the installed package")
    args = ap.parse_args(argv)
    expect = None
    if args.expect_kids is not None:
        expect = {k.strip() for k in args.expect_kids.split(",") if k.strip()}
    problems = check(args.src, expect)
    if problems:
        for p in problems:
            print(f"RELEASE BLOCKED: {p}")
        return 1
    print("pinned entitlement keys OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
