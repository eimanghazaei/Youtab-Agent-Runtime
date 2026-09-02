"""One authorization-registry lifecycle for the whole dashboard-auth surface.

The dashboard-auth authorization state lives in two module registries — the
provider registry (:mod:`youtab_agent_cli.dashboard_auth.registry`) and the
provider-bound token-route registry
(:mod:`youtab_agent_cli.dashboard_auth.token_auth`). WAVE-15 makes them share a
single lifecycle so they become ONE immutable authorization generation before
the app serves traffic:

    BUILDING  ──freeze_dashboard_auth()──▶  FROZEN

* **BUILDING** (startup): plugins register providers and their owned routes.
  Registration is fail-closed (duplicate provider name / conflicting route owner
  raise), so a mis-wired dual owner cannot arise.
* **FROZEN** (serving): every mutator across BOTH registries — ``register_provider``,
  ``clear_providers``, ``register_token_route[_prefix]``, ``clear_token_routes`` —
  refuses. Only a byte-identical idempotent route re-registration stays a no-op
  (so a ``discover_plugins(force=True)`` re-run of the SAME plugins is safe). No
  request is ever served while either registry can mutate, so the token seam's
  owner/provider/capability resolution and the ``_authorization_gate`` re-check
  observe exactly one frozen generation — there is no cross-generation / split
  lock TOCTOU window.

This module is a leaf (it imports neither registry) so both registries can
import it without a cycle.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_frozen = False


class FrozenRegistryError(RuntimeError):
    """A dashboard-auth registry mutation was refused because the registry is
    FROZEN (immutable-after-startup, Contract A). Carries no secret."""


def freeze_dashboard_auth() -> None:
    """Seal the complete dashboard-auth registry (providers + token routes).

    Idempotent. Called from the dashboard app's lifespan startup once plugin
    discovery has registered every provider and route and BEFORE the server
    accepts traffic.
    """
    global _frozen
    with _lock:
        _frozen = True


def is_frozen() -> bool:
    """True once :func:`freeze_dashboard_auth` has sealed the registry."""
    with _lock:
        return _frozen


def raise_if_frozen(what: str) -> None:
    """Fail closed if the registry is frozen. ``what`` is a non-secret label."""
    with _lock:
        frozen = _frozen
    if frozen:
        raise FrozenRegistryError(
            f"dashboard-auth registry is frozen; refusing {what} after startup"
        )


def _reset_for_tests() -> None:
    """PRIVATE test-only hook: return the lifecycle to BUILDING.

    NOT part of the supported runtime surface and never called by application or
    plugin code — only by the test harness (an autouse fixture) so a test that
    froze the shared module registry does not leak FROZEN state into the next
    test. It does not touch the provider or route dicts; each test manages its
    own content through the normal (now-unfrozen) mutators.
    """
    global _frozen
    with _lock:
        _frozen = False
