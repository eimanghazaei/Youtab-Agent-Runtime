"""Guard against rebrand-typo'd SQLite PRAGMA statements (WAVE-24 / Agent-4 F1).

A repo-wide ``nous -> youtab`` rebrand find-replace once corrupted the SQLite
keyword ``synchronous`` into ``synchroyoutab`` inside live
``conn.execute("PRAGMA ...")`` calls (kanban.db, state.db, cron/executions.db).
SQLite silently IGNORES an unknown PRAGMA name — it returns no rows and raises
nothing — so ``PRAGMA synchroyoutab=FULL`` was a durability **no-op**: the
``synchronous=FULL`` fsync-on-commit hardening those sites intend (to narrow the
crash window that can leave a b-tree page header torn) never took effect, and a
prior ``synchronous=NORMAL`` could never be re-tightened.

Because the failure is silent, only a source-level invariant catches a
regression. This test fails if ANY executable ``PRAGMA`` statement in the
codebase carries a ``...youtab...`` corruption of a real pragma keyword. It is
intentionally scoped to the *executable* form (``execute("PRAGMA ...")``) so
that prose/comments mentioning the historical bug do not trip it.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.youtab_runtime._source_walk import iter_source_py_files

# Matches an executable PRAGMA whose keyword contains the rebrand token
# "youtab" (real pragma keywords never do; its presence is always the
# corruption). Kept ASCII so a cp1252 CI console cannot mangle it.
_TYPO_PRAGMA = re.compile(r"""execute\(\s*["']\s*PRAGMA\s+\w*youtab\w*""", re.IGNORECASE)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()


def _iter_py_files():
    # Prune vendored/gitignored copies, virtualenvs and agent worktrees DURING
    # traversal (never descend them) — an rglob-then-filter scan enumerated ~140k
    # files under .claude/.venv-qual/node_modules and effectively hung on Windows.
    for path in iter_source_py_files(_REPO_ROOT):
        # Skip this guard itself — it documents the forbidden pattern by example.
        if path.resolve() == _THIS_FILE:
            continue
        yield path


def test_no_executable_pragma_carries_the_rebrand_typo():
    offenders: list[str] = []
    for path in _iter_py_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _TYPO_PRAGMA.search(line):
                rel = path.relative_to(_REPO_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Executable PRAGMA statements with a rebrand-typo keyword (SQLite "
        "silently ignores these, so the durability setting is a no-op):\n"
        + "\n".join(offenders)
    )
