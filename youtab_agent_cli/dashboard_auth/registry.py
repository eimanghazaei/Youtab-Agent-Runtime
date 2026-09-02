"""Provider registry — thin delegators over the single :class:`AuthRegistry`.

The provider registrations live in the one linearizable authorization registry
(:mod:`youtab_agent_cli.dashboard_auth.lifecycle`) so that provider and route
mutations share ONE coordinator lock. These module functions preserve the
historic public API and resolve the module-level default registry at call time
(so a test that injects a fresh registry is honoured). There is no reset/clear
that can reopen a frozen registry: ``clear_providers`` raises once frozen.
"""
from __future__ import annotations

from typing import List, Optional

from youtab_agent_cli.dashboard_auth import lifecycle
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider


def register_provider(provider: DashboardAuthProvider) -> None:
    """Register a provider.

    Startup-only (Contract A): refused once the registry is frozen. There is no
    provider-removal or provider-replacement API — a name already registered
    raises, so a provider can never be swapped under an owner's name.

    Raises:
        TypeError: on protocol violation.
        ValueError: if a provider with the same name is already registered.
        lifecycle.FrozenRegistryError: if the registry is frozen (post-startup).
    """
    lifecycle._default.register_provider(provider)


def get_provider(name: str) -> Optional[DashboardAuthProvider]:
    """Return the registered provider for ``name``, or None if unknown."""
    return lifecycle._default.get_provider(name)


def list_providers() -> List[DashboardAuthProvider]:
    """All registered providers, in registration order."""
    return lifecycle._default.list_providers()


def list_token_providers() -> List[DashboardAuthProvider]:
    """Registered providers that support non-interactive token auth."""
    return lifecycle._default.list_token_providers()


def list_session_providers() -> List[DashboardAuthProvider]:
    """Registered providers with supports_session True (interactive cookie sessions)."""
    return lifecycle._default.list_session_providers()


def clear_providers() -> None:
    """Drop all registrations. Refused once the registry is frozen.

    Nothing on the production serving path calls this; after freeze it raises so a
    live authorization surface cannot be reopened. Test isolation comes from a
    fresh injected registry, not from clearing.
    """
    lifecycle._default.clear_providers()
