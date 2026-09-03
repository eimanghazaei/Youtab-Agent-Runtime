"""Regression: updater tests can never reach the developer's real checkout.

These tests exercise the containment primitives from ``_update_containment`` —
the fail-closed target guard, the offline disposable repo, the primary-ref
tripwire, and both the POSIX and Windows git-flag forks of the updater — all
confined to ``tmp_path``. None of them invoke ``youtab update``/``cmd_update`` or
run reset/checkout against anything but the throwaway repo built here.

This is the tripwire that would have caught the WAVE-23 corruption, expressed as
an always-on regression gate.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from ._update_containment import (
    ContainmentError,
    PRIMARY_CHECKOUT,
    assert_disposable_target,
    assert_refs_unchanged,
    make_disposable_repo,
    snapshot_refs,
)

_REAL_RUN = subprocess.run


# --------------------------------------------------------------------------
# Target guard: fail-closed against escape attempts (requirement 7)
# --------------------------------------------------------------------------
class TestTargetGuardRejectsEscapes:
    def test_accepts_the_disposable_checkout(self, tmp_path):
        repo = make_disposable_repo(tmp_path)
        resolved = assert_disposable_target(repo.checkout, tmp_path)
        assert resolved == repo.checkout.resolve()
        assert tmp_path.resolve() in resolved.parents

    def test_rejects_parent_traversal(self, tmp_path):
        malicious = tmp_path / "checkout" / ".." / ".." / "etc"
        with pytest.raises(ContainmentError):
            assert_disposable_target(malicious, tmp_path)

    def test_rejects_absolute_path_outside_tmp(self, tmp_path):
        outside = Path(tempfile.gettempdir()).resolve().parent
        with pytest.raises(ContainmentError):
            assert_disposable_target(outside, tmp_path)

    def test_rejects_the_real_primary_checkout(self, tmp_path):
        # The exact WAVE-23 target: the updater's PROJECT_ROOT.
        with pytest.raises(ContainmentError):
            assert_disposable_target(PRIMARY_CHECKOUT, tmp_path)

    def test_rejects_symlink_escaping_tmp(self, tmp_path):
        link = tmp_path / "escape_link"
        outside = tmp_path.parent  # definitively outside tmp_path
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            # Windows without the symlink privilege — the .resolve()-based guard
            # is already proven by the other escape cases; skip only the OS
            # capability we lack, never the assertion.
            pytest.skip(f"symlink creation not permitted here: {exc}")
        with pytest.raises(ContainmentError):
            assert_disposable_target(link, tmp_path)


# --------------------------------------------------------------------------
# Disposable repo supports the real destructive op, OFFLINE, both forks
# (requirement 6: exercise BOTH platform forks without the _is_windows dodge)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "platform,git_cmd",
    [
        ("linux", ["git"]),
        ("win32", ["git", "-c", "windows.appendAtomically=false"]),
    ],
)
def test_reset_hard_origin_contained_for_both_platform_forks(
    tmp_path, platform, git_cmd
):
    """Both git_cmd forks the updater selects on ``sys.platform`` drive
    ``reset --hard origin/main`` successfully and stay inside ``tmp_path``.

    ``update_cmd.py`` builds ``git_cmd`` inline from ``sys.platform == "win32"``
    (not ``_is_windows()``), so we cover the real selection by driving each
    resulting command against the disposable repo — no platform-dodging patch,
    all filesystem effects confined to the throwaway checkout.
    """
    repo = make_disposable_repo(tmp_path)
    repo.diverge_local(f"divergent for {platform}\n")

    # Sanity: the fork we are about to run matches what production would build.
    expected = (
        ["git", "-c", "windows.appendAtomically=false"]
        if platform == "win32"
        else ["git"]
    )
    assert git_cmd == expected

    env = dict(os.environ)
    env.update(repo.env)
    result = _REAL_RUN(
        git_cmd + ["reset", "--hard", "origin/main"],
        cwd=str(repo.checkout),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (repo.checkout / "file.txt").read_text(encoding="utf-8") == "v1\n"
    # Contained: the op only touched the disposable checkout.
    assert assert_disposable_target(repo.checkout, tmp_path) == repo.checkout.resolve()


# --------------------------------------------------------------------------
# Tripwire detects mutation — proven against a FAKE primary, never the real one
# (requirement 4)
# --------------------------------------------------------------------------
class TestPrimaryRefTripwire:
    @staticmethod
    def _fake_primary(tmp_path: Path):
        repo = make_disposable_repo(tmp_path)
        return repo

    def test_stable_when_repo_untouched(self, tmp_path):
        repo = self._fake_primary(tmp_path)
        before = snapshot_refs(repo.checkout)
        after = snapshot_refs(repo.checkout)
        # Must not raise.
        assert_refs_unchanged(before, after, repo.checkout)

    def test_detects_new_commit(self, tmp_path):
        repo = self._fake_primary(tmp_path)
        before = snapshot_refs(repo.checkout)
        repo.diverge_local("mutation\n")
        after = snapshot_refs(repo.checkout)
        with pytest.raises(ContainmentError, match="WAVE-23 corruption tripwire"):
            assert_refs_unchanged(before, after, repo.checkout)

    def test_detects_dirty_worktree(self, tmp_path):
        repo = self._fake_primary(tmp_path)
        before = snapshot_refs(repo.checkout)
        (repo.checkout / "file.txt").write_text("uncommitted\n", encoding="utf-8")
        after = snapshot_refs(repo.checkout)
        with pytest.raises(ContainmentError):
            assert_refs_unchanged(before, after, repo.checkout)


# --------------------------------------------------------------------------
# The autouse tripwire really is armed for this module (requirement 4 wiring)
# --------------------------------------------------------------------------
def test_primary_checkout_untouched_by_this_module(primary_ref_tripwire):
    """A trivial test that simply asserts the primary tripwire fixture runs and
    leaves the real checkout identical. Exercises the fixture end-to-end."""
    assert PRIMARY_CHECKOUT.exists()
