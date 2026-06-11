"""PKG-1 + PKG-10 regression (beta reports 2026-06-11).

PKG-1: Hermes 0.7+ scans memory providers only under
<hermes pkg>/plugins/memory/<name>/. The installer must target that scan path
(detectable, or via --memory-provider-path) in addition to the legacy
$HERMES_HOME/plugins/sibyl user-plugin path, and degrade with a clear message
when the package isn't detected or the path isn't writable.

PKG-10: the Hermes system_prompt block coaches keyword/proper-noun search over
natural-language questions.
"""
import tempfile
from pathlib import Path

from sibyl_memory_hermes import install_plugin as ip


def test_memory_provider_dest_override():
    d = Path(tempfile.mkdtemp())
    mem_dir = d / "opt" / "hermes" / "plugins" / "memory"
    dest = ip._memory_provider_dest(str(mem_dir))
    assert dest == (mem_dir.resolve() / "sibyl")


def test_memory_provider_dest_none_when_hermes_absent():
    # hermes is not installed in the test env; with no override, returns None.
    assert ip._memory_provider_dest(None) is None


def test_install_writes_both_paths_with_override():
    d = Path(tempfile.mkdtemp())
    hermes_home = d / ".hermes"
    mem_dir = d / "pkg" / "plugins" / "memory"
    mem_dir.mkdir(parents=True)

    rc = ip.install(hermes_home, force=False, dry_run=False,
                    memory_provider_path=str(mem_dir))
    assert rc == 0

    user_path = hermes_home / "plugins" / "sibyl"
    provider_path = mem_dir.resolve() / "sibyl"
    # Both the legacy user-plugin path AND the 0.7+ scan path got the adapter.
    assert (user_path / "__init__.py").exists()
    assert (user_path / "plugin.yaml").exists()
    assert (provider_path / "__init__.py").exists()
    assert (provider_path / "plugin.yaml").exists()


def test_install_dry_run_writes_nothing():
    d = Path(tempfile.mkdtemp())
    hermes_home = d / ".hermes"
    mem_dir = d / "pkg" / "plugins" / "memory"
    mem_dir.mkdir(parents=True)
    rc = ip.install(hermes_home, force=False, dry_run=True,
                    memory_provider_path=str(mem_dir))
    assert rc == 0
    assert not (hermes_home / "plugins" / "sibyl").exists()
    assert not (mem_dir / "sibyl").exists()


def test_install_degrades_when_provider_undetected(capsys):
    # No override + no hermes pkg → user path still written, clear warning shown.
    d = Path(tempfile.mkdtemp())
    hermes_home = d / ".hermes"
    rc = ip.install(hermes_home, force=False, dry_run=False, memory_provider_path=None)
    assert rc == 0
    assert (hermes_home / "plugins" / "sibyl" / "__init__.py").exists()
    out = capsys.readouterr().out
    assert "--memory-provider-path" in out


def test_system_prompt_block_coaches_keyword_search():
    import inspect

    from sibyl_memory_hermes._hermes_plugin.adapter import SibylAdapter

    src = inspect.getsource(SibylAdapter.system_prompt_block)
    assert "search each key term separately" in src
    assert "matches stored TEXT, not meaning" in src
