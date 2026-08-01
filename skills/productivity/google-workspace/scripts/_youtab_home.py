"""Resolve YOUTAB_AGENT_HOME for standalone skill scripts.

Skill scripts may run outside the Youtab process (e.g. system Python,
nix env, CI) where ``youtab_constants`` is not importable.  This module
provides the same ``get_youtab_home()`` and ``display_youtab_home()``
contracts as ``youtab_constants`` without requiring it on ``sys.path``.

When ``youtab_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``youtab_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``YOUTAB_AGENT_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from youtab_constants import display_youtab_home as display_youtab_home
    from youtab_constants import get_youtab_home as get_youtab_home
except (ModuleNotFoundError, ImportError):

    def get_youtab_home() -> Path:
        """Return the Youtab home directory (default: ~/.youtab-agent-runtime).

        Mirrors ``youtab_constants.get_youtab_home()``."""
        val = os.environ.get("YOUTAB_AGENT_HOME", "").strip()
        return Path(val) if val else Path.home() / ".youtab-agent-runtime"

    def display_youtab_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``youtab_constants.display_youtab_home()``."""
        home = get_youtab_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
