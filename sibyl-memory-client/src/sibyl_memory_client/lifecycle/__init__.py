"""Save and debloat, the plugin's lifecycle (spec plugin-save-debloat 2026-09-27).

Modules:

* ``measure``   deterministic context-weight measurement (Python port of the
                public ``context-hygiene.mjs``), stat only;
* ``splitter``  the pure HISTORY / CURRENT classifier;
* ``apply``     plan, back up, store the history, rewrite last; undo;
* ``policy``    ``~/.sibyl-memory/debloat.json``;
* ``state``     ``~/.sibyl-memory/lifecycle.json`` and the write counter;
* ``entitled``  ``automation_allowed``: the one gate check (verified fresh or
                stale entitlement whose tier is paid);
* ``capsule``   the Pro session capsule;
* ``reminders`` the free reminder lines and the Pro report lines;
* ``runner``    the Pro process-start run; ``notices`` per-process delivery.

Nothing here opens a network connection. Content of an instruction file is
read only to split it, and goes only to the backup, the person's own memory
store (through the gated client) or the sibling ``*-history`` file.
"""
from .entitled import automation_allowed, automation_allowed_for_credentials

__all__ = ["automation_allowed", "automation_allowed_for_credentials"]
