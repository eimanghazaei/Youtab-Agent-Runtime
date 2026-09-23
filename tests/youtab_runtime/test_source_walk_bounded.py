"""Regression guard: the repo-hygiene source walk stays bounded and cycle-safe.

WAVE-30H. ``test_no_typo_pragmas`` once used ``Path.rglob("*.py")`` which
descended into ``.claude`` (agent worktrees, ~125k files), ``.venv-qual`` and
``node_modules`` before filtering, and followed directory symlinks — a ~53-minute
"hang" that denied the suite a clean verdict. These tests pin the fixed walker's
two guarantees on a synthetic tree so the foot-gun cannot return: skip
directories are pruned DURING traversal, and symlink/junction cycles are never
followed (so the walk terminates).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.youtab_runtime._source_walk import (
    _is_reparse_or_symlink as _is_reparse_dir,
    iter_source_py_files,
)


def _make_dir_cycle(link, target) -> bool:
    """Create a directory reparse cycle ``link -> target``. Returns success.

    Tries a POSIX/Windows symlink first; on Windows falls back to a junction
    (``mklink /J``), which needs no elevation, so the no-follow guarantee is
    proven on this host and not merely skipped.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True, timeout=15,
            )
            return r.returncode == 0 and link.exists()
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# x\n", encoding="utf-8")


@pytest.fixture()
def tree(tmp_path):
    # real source that MUST be found
    _touch(tmp_path / "pkg" / "real.py")
    _touch(tmp_path / "pkg" / "sub" / "deep.py")
    # skip dirs that MUST be pruned (never scanned)
    _touch(tmp_path / ".claude" / "worktrees" / "agent-x" / "poison.py")
    _touch(tmp_path / ".venv-qual" / "lib" / "site-packages" / "vendored.py")
    _touch(tmp_path / "node_modules" / "dep" / "index.py")
    _touch(tmp_path / "pkg" / "__pycache__" / "real.cpython-312.py")
    _touch(tmp_path / "thing.egg-info" / "meta.py")
    return tmp_path


def _names(root):
    return {p.relative_to(root).as_posix() for p in iter_source_py_files(root)}


def test_prunes_skip_dirs_during_traversal(tree):
    found = _names(tree)
    assert found == {"pkg/real.py", "pkg/sub/deep.py"}
    # none of the pruned trees leaked in
    assert not any(
        seg in p
        for p in found
        for seg in (".claude", ".venv-qual", "node_modules", "__pycache__", ".egg-info")
    )


def test_walk_terminates_and_does_not_follow_symlink_cycle(tree):
    # A directory symlink pointing back at the root would make an rglob-style walk
    # that follows links loop forever. The walk must not follow it.
    cycle = tree / "pkg" / "loop"
    if not _make_dir_cycle(cycle, tree):
        pytest.skip("directory symlinks/junctions not permitted on this host")
    assert _is_reparse_dir(cycle)  # sanity: we really created a reparse cycle

    # If the walk followed the symlink it would either loop forever (the test
    # would time out) or yield 'real.py' again via pkg/loop/... — assert neither:
    found = _names(tree)
    assert found == {"pkg/real.py", "pkg/sub/deep.py"}
    assert not any("loop" in p for p in found)


def test_skip_predicate_prunes_additional_dirs(tree):
    _touch(tree / "generated" / "g.py")
    found = _names_with_pred(tree, lambda name: name == "generated")
    assert "generated/g.py" not in found
    assert "pkg/real.py" in found


def _names_with_pred(root, pred):
    return {
        p.relative_to(root).as_posix()
        for p in iter_source_py_files(root, skip_predicate=pred)
    }
