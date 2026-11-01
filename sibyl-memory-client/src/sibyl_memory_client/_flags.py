"""Environment flags for the 0.10.0 retrieval options, read at call time.

`on(name)` is true only when the value is exactly "1" (default off);
`on_default(name)` is true unless the value is exactly "0" (default on).

Flags (documented in the client and MCP READMEs):
- SIBYL_MEMORY_RECALL_HUBS (default on): memory_recall adds a document's
  owning hubs and small-hub siblings. Tuning: SIBYL_MEMORY_RECALL_SMALL_HUB_MAX
  and SIBYL_MEMORY_RECALL_SIBLING_CAP (both 60).
- SIBYL_MEMORY_RANKED_FALLBACK (default off): a short multi-record search
  result is filled with ranked candidates. Tuning: SIBYL_MEMORY_RANKED_MIN_TOKENS
  (3), SIBYL_MEMORY_RANKED_EXCLUDE_CATEGORIES (comma list).
- SIBYL_MEMORY_HOP_OWNERS (default off): link samples on a document show its
  owning hubs first, with `owners_total`.
"""
from __future__ import annotations

import os


def on(name: str) -> bool:
    return os.environ.get(name) == "1"


def num(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def csv(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.environ.get(name, "").split(",") if x.strip())


def on_default(name: str) -> bool:
    """Default-ON flag: on unless the environment value is exactly "0"."""
    return os.environ.get(name, "1") != "0"
