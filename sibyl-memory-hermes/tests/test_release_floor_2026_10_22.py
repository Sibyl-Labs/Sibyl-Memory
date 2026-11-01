"""Release train (V1 panel B, YARROW 1): the provider imports against the
client floor it declares, and the floor is this train's client release."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ver(text: str, pat: str) -> tuple[int, ...]:
    m = re.search(pat, text, re.M)
    assert m, pat
    return tuple(int(x) for x in m.group(1).split("."))


def test_client_floor_is_the_train_release():
    mine = (ROOT / "sibyl-memory-hermes" / "pyproject.toml").read_text()
    client = (ROOT / "sibyl-memory-client" / "pyproject.toml").read_text()
    assert _ver(mine, r'^version = "([0-9.]+)"') == (0, 6, 0)
    assert (_ver(mine, r'"sibyl-memory-client>=([0-9.]+)"')
            == _ver(client, r'^version = "([0-9.]+)"') == (0, 10, 0))


def test_provider_imports_against_the_floor_client_tree():
    src = ROOT / "sibyl-memory-client" / "src"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import sibyl_memory_client as c; assert c.__file__.startswith(sys.argv[1]); "
            "import sibyl_memory_hermes.provider; print('ok')")
    out = subprocess.run([sys.executable, "-c", code, str(src)],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]
