"""Fixtures shared across youtab_agent_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def credential_entitlement(monkeypatch):
    """Grant this deployment the credential/catalogue/raw-engine entitlement.

    Those routes are refused by default, which is what production does and what
    ``test_credential_surface_refused.py`` pins. A module whose subject is the
    capability *behind* the gate — what an env row looks like, how a stored
    value rotates, which tab a provider lands on — has to get past the gate
    before it can test any of that.

    Requesting this fixture is therefore a statement about what a module is
    for. It is deliberately opt-in rather than autouse: if it were applied to
    the whole package, the refusal would be tested nowhere and a future route
    would be covered only in its entitled state.
    """
    from youtab_agent_cli.credential_entitlement import (
        ENTITLEMENT_ENV,
        ENTITLEMENT_GRANTED,
    )

    monkeypatch.setenv(ENTITLEMENT_ENV, ENTITLEMENT_GRANTED)


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
