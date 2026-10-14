import pytest
from sibyl_memory_cli import setup as _setup
from sibyl_memory_client.testing import (
    TestSigner,
    forbid_external_network,
    install_entitled_home,
    patch_trusted_keys,
)


@pytest.fixture(autouse=True)
def _no_real_claude_cli(monkeypatch):
    """SAFETY + determinism: by default pretend the `claude` CLI is absent, so the
    settings.json-fallback tests are deterministic and NO test ever shells out to the
    real `claude mcp` (which would mutate this machine's actual MCP config). Tests that
    exercise the CLI path re-patch _claude_cli + _run explicitly."""
    monkeypatch.setattr(_setup.ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: None))
    yield


# ---- 0.5.0 account gate (spec 4.4) -----------------------------------------
# The gate (sibyl-memory-client) reads entitlement.json beside the credentials
# file and may refresh it over the network. In tests: a throwaway Ed25519 key is
# monkeypatched into TRUSTED_KEYS (the production pinned key is untouched),
# every refresh is routed to that test signer, non-loopback sockets are refused,
# and HOME plus the CLI's import-time default paths point at a per-test fake
# home, so no test can read or write a real ~/.sibyl-memory or reach an api.

@pytest.fixture(scope="session")
def test_signer() -> TestSigner:
    return TestSigner()


@pytest.fixture(scope="session", autouse=True)
def _session_gate_guard(test_signer):
    mp = pytest.MonkeyPatch()
    patch_trusted_keys(mp, test_signer)
    forbid_external_network(mp)
    yield
    mp.undo()


@pytest.fixture
def entitled_home(_isolated_home):
    """The activated fake home's ~/.sibyl-memory directory."""
    return _isolated_home / ".sibyl-memory"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch, test_signer):
    from sibyl_memory_cli import cli
    home = tmp_path_factory.mktemp("cli-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("SIBYL_MEMORY_DB", raising=False)
    patch_trusted_keys(monkeypatch, test_signer)
    # The CLI's own defaults point at an EMPTY directory (as the suite always
    # had: no credentials unless a test writes them), while the SDK's default
    # home (~/.sibyl-memory) is activated, because test code that SEEDS a store
    # through MemoryClient (what an agent does) needs an account.
    d = home / "cli-default"
    monkeypatch.setattr(cli, "DEFAULT_CRED_PATH", d / "credentials.json")
    monkeypatch.setattr(cli, "DEFAULT_DB_PATH", d / "memory.db")
    monkeypatch.setattr(cli, "DEFAULT_TIER_CACHE_PATH", d / "tier_cache.json")
    monkeypatch.setattr(cli, "_ENT_CRED_PATH", d / "credentials.json")
    install_entitled_home(home, test_signer)
    return home


# ---- network guard (0.9.0 gate fix) -----------------------------------------
# A previous build's suite reached the production api by accident. The guard is
# installed at conftest import (so collection-time code is covered too), stays
# installed for the whole session (the session fixture above re-applies it), and
# EVERY test fails if it made a refused non-loopback connection or name lookup,
# even one it caught and ignored. tests/test_network_guard_2026_09_27.py proves
# the guard itself.
from sibyl_memory_client.testing import (  # noqa: E402
    assert_no_external_attempts as _assert_no_external_attempts,
    forbid_external_network as _forbid_external_network,
    refused_connections as _refused_connections,
)

_IMPORT_TIME_GUARD = pytest.MonkeyPatch()
_forbid_external_network(_IMPORT_TIME_GUARD)


@pytest.fixture(autouse=True)
def _no_external_network_attempts():
    start = len(_refused_connections())
    yield
    _assert_no_external_attempts(start)


# ---- real-home guard (save/debloat build 2026-09-27) --------------------------
# Every test must see a temporary HOME (asserted per test), and at session end
# none of the paths the save/debloat build writes (skills, lifecycle, hooks,
# shims) may have changed under the account's real home.
from sibyl_memory_client.testing import (  # noqa: E402
    assert_home_is_not_real as _assert_home_is_not_real,
    real_home_changes as _real_home_changes,
    real_home_snapshot as _real_home_snapshot,
)

_REAL_HOME_BEFORE = _real_home_snapshot()


@pytest.fixture(autouse=True)
def _home_is_never_the_real_home(_isolated_home):
    _assert_home_is_not_real()
    yield
    _assert_home_is_not_real()


def pytest_sessionfinish(session, exitstatus):
    changed = _real_home_changes(_REAL_HOME_BEFORE)
    if changed:
        print(f"\nREAL HOME CHANGED during the test session: {changed}")
        session.exitstatus = 1


# ---- agent handoff (spec agent-handoff-messages 2026-10-03, 4.1 / 4.2) -------
# `handoff(out)` parses the convention block from captured stdout;
# `assert_convention(out, err)` is the lint every stop must pass.
import re as _re  # noqa: E402

HANDOFF_KEYS = ("later: ", "ask your human: ", "next: ")
_NEXT_PREFIXES = ("sibyl ", "pip ", "pipx ", "claude ", "sibyl-memory-hermes ")
_NEXT_WORDS = ("done", "retry", "sibyl-memory-mcp")
_PLACEHOLDER = _re.compile(r"<[a-z_|\-]+>")


def handoff(out: str) -> dict:
    """Parse the convention block from captured stdout. Raises if the last
    non-empty line is not `next:`; returns {next, human, later: [...], waiting,
    block: [lines]}. `waiting` lists every `waiting:` line anywhere in out."""
    lines = [ln for ln in out.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not lines[-1].startswith("next: "):
        raise AssertionError(f"last non-empty line is not `next:`: {lines[-1:]!r}")
    block = []
    for ln in reversed(lines):
        if ln.startswith(HANDOFF_KEYS):
            block.insert(0, ln)
        else:
            break
    human = [ln[len("ask your human: "):] for ln in block if ln.startswith("ask your human: ")]
    return {
        "next": block[-1][len("next: "):],
        "human": human[0] if human else None,
        "later": [ln[len("later: "):] for ln in block if ln.startswith("later: ")],
        "waiting": [ln for ln in out.splitlines() if ln.startswith("waiting: ")],
        "block": block,
    }


def json_handoff(payload: dict) -> dict:
    assert "next" in payload and "human_action" in payload, payload
    return payload


def _next_ok(value: str) -> bool:
    if value in _NEXT_WORDS:
        return True
    if _re.fullmatch(r"retry in \d+ minutes", value):
        return True
    return value.startswith(_NEXT_PREFIXES)


def assert_convention(out: str, err: str = "") -> dict:
    """The 4.2 lint: last line `next:`; at most one `ask your human:`, directly
    above `next:`; `later:` lines first; a placeholder needs an ask line; no
    ANSI, no indentation, no em dash in the block; the `next` grammar; nothing
    on stderr."""
    h = handoff(out)
    block = h["block"]
    kinds = ["later" if b.startswith("later: ") else "ask" if b.startswith("ask your human: ")
             else "next" for b in block]
    assert kinds.count("next") == 1, block
    assert kinds.count("ask") <= 1, block
    if "ask" in kinds:
        assert kinds[-2] == "ask", block
    assert kinds == sorted(kinds, key=["later", "ask", "next"].index), block
    for b in block:
        assert "\x1b" not in b and b == b.lstrip() and "\u2014" not in b, b
    for b in block:
        if b.startswith(("later: ", "next: ")) and _PLACEHOLDER.search(b):
            assert h["human"], f"placeholder without an ask line: {block}"
    assert _next_ok(h["next"]), h["next"]
    assert err == "", err
    return h
