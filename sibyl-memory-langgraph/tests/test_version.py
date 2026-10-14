"""``__version__`` follows the installed distribution (0.3.0 shipped with a
hand-kept "0.2.0")."""
from importlib.metadata import version

import sibyl_memory_langgraph


def test_version_matches_installed_metadata() -> None:
    assert sibyl_memory_langgraph.__version__ == version("sibyl-memory-langgraph")
