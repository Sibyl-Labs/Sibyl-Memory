import pytest
from sibyl_memory_cli import setup as _setup


@pytest.fixture(autouse=True)
def _no_real_claude_cli(monkeypatch):
    """SAFETY + determinism: by default pretend the `claude` CLI is absent, so the
    settings.json-fallback tests are deterministic and NO test ever shells out to the
    real `claude mcp` (which would mutate this machine's actual MCP config). Tests that
    exercise the CLI path re-patch _claude_cli + _run explicitly."""
    monkeypatch.setattr(_setup.ClaudeCodeWirer, "_claude_cli", staticmethod(lambda: None))
    yield
