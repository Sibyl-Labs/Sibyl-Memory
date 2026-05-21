"""Bundled Hermes plugin payload. NOT for direct import.

Contains the validated MemoryProvider adapter (`adapter.py`) and its
metadata (`plugin.yaml`). The `sibyl-memory-hermes install-plugin`
console script copies these files to $HERMES_HOME/plugins/sibyl/ where
Hermes' loader discovers them.

`adapter.py` is intentionally NOT named `__init__.py` here: it imports
`agent.memory_provider` which only exists inside a Hermes-installed
environment. Naming it as a module member would cause Python to attempt
to load it on package import and fail in our test environments.
"""
