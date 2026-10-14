"""The `sibyl` console script (0.5.0).

`sibyl hook <event>` is what Claude Code runs at every session start, prompt,
turn end, compaction and session end, under a 5 s timeout. It goes straight to
``hooks.main_hook`` (standard library only until a hook actually needs the
client) instead of importing the whole CLI first. Every other command is the
unchanged ``cli.main``.
"""
from __future__ import annotations

import sys

_HOOK_EVENTS = ("session-start", "prompt", "stop", "pre-compact", "session-end")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) == 2 and args[0] == "hook" and args[1] in _HOOK_EVENTS:
        from .hooks import main_hook
        return main_hook(args[1])
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
