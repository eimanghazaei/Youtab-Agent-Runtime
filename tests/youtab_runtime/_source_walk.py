"""Bounded, cycle-safe source-tree walker for repo-hygiene tests.

WAVE-30H regression-test reliability fix. ``Path.rglob("*.py")`` is a foot-gun
for a repo-wide hygiene scan: it enumerates and stats EVERY entry before any
skip filter can run, and on Python < 3.13 it FOLLOWS directory symlinks/junctions
(so a reparse cycle loops forever). In this repo ``.claude/worktrees`` alone held
~34k directories / ~125k ``*.py`` files (dozens of full worktree checkouts, each
with its own venv), so an rglob-then-filter scan took ~53 minutes on Windows —
long enough to read as a hang and to deny the suite a clean verdict.

``iter_source_py_files`` fixes both problems structurally: it prunes skip
directories DURING traversal (``os.walk`` topdown, editing ``dirnames`` in place,
so pruned subtrees are never descended) and never follows symlinks/junctions
(``followlinks=False`` plus an explicit reparse-point guard). The walk is
therefore bounded to the project's own source and cannot loop on a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator, Optional

# Windows FILE_ATTRIBUTE_REPARSE_POINT — a junction or symlink dir carries it.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Names never scanned for project source: VCS, vendored deps, virtualenvs, agent
# worktree checkouts and caches. ``.venv-qual`` is why a name-set alone is not
# enough — see ``_is_skippable_dirname`` for the pattern rules.
DEFAULT_SKIP_NAMES = frozenset(
    {
        ".claude",  # agent worktrees (.claude/worktrees) — tens of thousands of files
        ".git",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
    }
)


def _is_reparse_or_symlink(path: str) -> bool:
    """True for a symlink or a Windows junction/reparse-point directory."""
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_skippable_dirname(name: str) -> bool:
    """Skip any virtualenv (``.venv``/``.venv-qual``/``venv``) or egg-info dir.

    A plain name set missed ``.venv-qual`` (only ``.venv``/``venv`` were listed),
    so 8k site-package files were being read and regex-scanned. Pattern rules
    close that class of gap for good.
    """
    return (
        name in DEFAULT_SKIP_NAMES
        or name == "venv"
        or name.startswith(".venv")
        or name.endswith(".egg-info")
    )


def iter_source_py_files(
    root: os.PathLike | str,
    *,
    skip_predicate: Optional[Callable[[str], bool]] = None,
) -> Iterator[Path]:
    """Yield project-source ``*.py`` files under ``root``, bounded and cycle-safe.

    Skip directories are pruned DURING traversal (never descended), and
    symlink/junction directories are never followed, so the walk is bounded to
    real source and terminates on any reparse cycle. ``skip_predicate`` (given a
    directory *name*) prunes additional directories.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        kept = []
        for d in dirnames:
            if _is_skippable_dirname(d):
                continue
            if skip_predicate is not None and skip_predicate(d):
                continue
            if _is_reparse_or_symlink(os.path.join(dirpath, d)):
                continue  # never follow a junction/symlink dir (cycle-safe)
            kept.append(d)
        dirnames[:] = kept  # prune in place -> pruned subtrees are not walked
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name
