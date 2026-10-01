"""Fixtures shared across youtab_agent_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Youtab profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from youtab_agent_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_youtab_gate(request, monkeypatch):
    """Default ``_detect_concurrent_youtab_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``youtab.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``youtab`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_youtab_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from youtab_agent_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches youtab_agent_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_youtab_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_youtab_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


# ==== BEGIN WAVE-24 updater-test containment (Agent 3) ====
# Fixtures + autouse tripwire that keep every updater test off the developer's
# real checkout. Helpers live in ``_update_containment`` (import-safe, no pytest
# dependency); the fixtures are defined here so pytest auto-discovers them for
# every test in this directory.
from ._update_containment import (  # noqa: E402
    PRIMARY_CHECKOUT,
    assert_disposable_target,
    assert_refs_unchanged,
    make_disposable_repo,
    snapshot_refs,
)

# Module leaf names whose tests may drive the updater's real git code paths.
_UPDATER_TEST_PREFIXES = ("test_cmd_update", "test_update_", "test_install_diverged_update")


@pytest.fixture
def contained_update_repo(tmp_path, monkeypatch):
    """Point the updater at a disposable OFFLINE repo and redirect all state.

    * ``youtab_agent_cli.main.PROJECT_ROOT`` -> disposable checkout (the single
      cwd every updater git op uses).
    * ``HOME`` / ``USERPROFILE`` / ``LOCALAPPDATA`` / ``XDG_*`` /
      ``YOUTAB_AGENT_HOME`` -> under ``tmp_path`` so config, cache, update-state
      and the update lock never touch the real home.
    * git config + network isolated (see ``_isolating_git_env``).
    """
    repo = make_disposable_repo(tmp_path)

    from youtab_agent_cli import main as cli_main

    monkeypatch.setattr(cli_main, "PROJECT_ROOT", repo.checkout, raising=True)
    for key, value in repo.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path / "youtab-home"))

    # Guard BEFORE any updater code runs: the cwd the updater will use must be
    # disposable, not the primary checkout.
    assert_disposable_target(cli_main.PROJECT_ROOT, tmp_path)
    return repo


@pytest.fixture
def update_target_guard(tmp_path):
    """Expose the fail-closed target guard bound to this test's ``tmp_path``."""

    def _guard(target):
        return assert_disposable_target(target, tmp_path)

    return _guard


@pytest.fixture
def primary_ref_tripwire():
    """Snapshot the PRIMARY checkout before/after; fail loudly on any change.

    Uses the captured real ``subprocess.run`` and runs OUTSIDE any in-test
    ``subprocess`` monkeypatch, so a test that swaps ``subprocess.run`` cannot
    blind it.
    """
    before = snapshot_refs(PRIMARY_CHECKOUT)
    yield PRIMARY_CHECKOUT
    after = snapshot_refs(PRIMARY_CHECKOUT)
    assert_refs_unchanged(before, after, PRIMARY_CHECKOUT)


@pytest.fixture(autouse=True)
def _updater_primary_ref_tripwire(request):
    """Autouse WAVE-23 tripwire: for every updater test module, assert the real
    checkout's HEAD / branch / porcelain status / reflog top are byte-identical
    before and after the test. Any drift fails the test loudly — this is the
    guard that would have caught the WAVE-23 corruption.

    Scoped to updater modules so the rest of the suite pays nothing.
    """
    leaf = request.module.__name__.rsplit(".", 1)[-1]
    if not leaf.startswith(_UPDATER_TEST_PREFIXES):
        yield
        return
    before = snapshot_refs(PRIMARY_CHECKOUT)
    try:
        yield
    finally:
        after = snapshot_refs(PRIMARY_CHECKOUT)
        assert_refs_unchanged(before, after, PRIMARY_CHECKOUT)
# ==== END WAVE-24 updater-test containment ====
