"""The complete dashboard-auth authorization registry, as ONE linearizable object.

WAVE-16 unifies what used to be three module registries (the provider registry,
the token-route registry, and a separate lifecycle flag) into a single
:class:`AuthRegistry` guarded by ONE coordinator lock, so the whole
authorization state is one linearizable generation:

    BUILDING  ──registry.freeze()──▶  FROZEN

Linearizability
---------------
Every mutation — ``register_provider`` / ``clear_providers`` /
``register_token_route[_prefix]`` / ``clear_token_routes`` — and ``freeze`` hold
the SAME ``_coord`` lock CONTINUOUSLY across state-validation → conflict/idempotency
checks → commit. So a mutation that observed BUILDING cannot commit after
``freeze()`` returns: either it committed before ``freeze`` took ``_coord``, or it
takes ``_coord`` after ``freeze`` released it, re-reads ``_frozen`` under the same
hold, sees FROZEN, and is refused. There is no check-then-act window.

Reads that inform authentication (``route_owner`` / ``resolve_auth_snapshot``)
also take ``_coord``, so the (owner, provider, capability) they return is one
consistent generation. ``verify_token`` is NEVER called while ``_coord`` is held:
:meth:`resolve_auth_snapshot` captures the snapshot under the lock and returns it;
the caller releases and only then authenticates.

Lock order
----------
``_coord`` is the single authorization lock. It is a leaf: no other lock is
acquired while it is held, and provider ``verify_token`` (which may take its own
locks or do I/O) runs only after ``_coord`` is released. So there is no lock-order
cycle and no deadlock.

Reset / test isolation
----------------------
There is NO reset or unfreeze callable in this or any shipped module — a frozen
registry cannot be reopened at runtime. Tests obtain isolation by INJECTING a
fresh instance: an autouse fixture rebinds the module-level default
(``dashboard_auth.lifecycle._default``) to a new :class:`AuthRegistry` per test.
The public module functions resolve ``_default`` at call time, so the injection
is total and needs no production reset path.
"""
from __future__ import annotations

import logging
import threading
from typing import List, NamedTuple, Optional

from youtab_agent_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    ProviderError,
    TokenPrincipal,
    assert_protocol_compliance,
)

_log = logging.getLogger(__name__)


class FrozenRegistryError(RuntimeError):
    """A dashboard-auth registry mutation was refused because the registry is
    FROZEN (immutable-after-startup, Contract A). Carries no secret."""


class TokenRouteOwnershipError(RuntimeError):
    """A token route/prefix was registered for two different providers.

    Raised fail-closed at registration so a mis-wired deployment cannot end up
    with an ambiguously-owned surface. Carries no secret."""


class TokenRouteRegistrationError(FrozenRegistryError):
    """A NEW token route registration was refused after the registry FROZE.

    A specialisation of :class:`FrozenRegistryError` for the route registry so a
    caller may catch either. A byte-identical re-registration of an already-owned
    route stays a no-op (so a ``discover_plugins(force=True)`` re-run of the SAME
    plugins is safe); any new route/prefix/owner/capability is rejected. Carries
    no secret."""


class ServiceRouteRegistrationError(RuntimeError):
    """A security-critical service route was NOT correctly registered before the
    registry froze — startup must abort (fail-closed) rather than serve a
    partially-configured authorization boundary.

    Raised at lifespan startup (before the server accepts traffic) when a
    declared :class:`RequiredOwnership` is unmet: the owning provider is absent,
    or the route/prefix is not owned by it with the required capability. This is
    the backstop for a plugin whose route-ownership registration failed and was
    swallowed by the plugin loader — the requirement it declared stays unmet, so
    the server never starts with the route reachable through the cookie gate.
    Carries provider/route/reason for diagnosis but NO secret."""


class TokenRouteOwner(NamedTuple):
    """Who owns a token route and the capability its principal must carry."""

    provider: str
    capability: Optional[str]


class RequiredOwnership(NamedTuple):
    """A fail-closed assertion that a security-critical route MUST be owned.

    Declared by a built-in service plugin BEFORE it registers its provider and
    route, so that a failure of EITHER registration (even one the plugin loader
    swallows) leaves this requirement unmet and the lifespan verification aborts
    startup. ``path`` is the exact route (``is_prefix=False``) or the prefix
    (``is_prefix=True``); ``capability`` is the scope the owner's principal must
    carry."""

    provider: str
    path: str
    is_prefix: bool
    capability: Optional[str]


# Sentinel: a path matched by two owners of equal specificity — fail closed.
_AMBIGUOUS = object()


def _prefix_matches(path: str, prefix: str) -> bool:
    """Segment-anchored membership: is ``path`` strictly under ``prefix``?

    ``prefix`` is stored normalised to end in ``/``. Membership is decided on
    SEGMENT lists (not a raw ``str.startswith``): ``path`` is under ``prefix`` iff
    its leading segments are exactly the prefix's segments AND it has at least one
    further segment. So ``/api/runtime/v1/`` owns ``/api/runtime/v1/health`` but
    never the sibling ``/api/runtime/v1evil`` nor the bare ``/api/runtime/v1``.
    Decided on the same ``request.url.path`` the ASGI framework routes on — no
    separate normalisation layer.
    """
    prefix_segments = prefix.rstrip("/").split("/")
    path_segments = path.split("/")
    return (
        len(path_segments) > len(prefix_segments)
        and path_segments[: len(prefix_segments)] == prefix_segments
    )


class AuthRegistry:
    """The complete dashboard-auth authorization state behind ONE coordinator lock."""

    def __init__(self) -> None:
        # One coordinator lock for the lifecycle flag AND every provider/route
        # mutation and read. Reentrant so an internal helper called under the lock
        # does not self-deadlock.
        self._coord = threading.RLock()
        self._frozen = False
        self._providers: dict[str, DashboardAuthProvider] = {}
        self._token_routes: dict[str, TokenRouteOwner] = {}
        self._token_route_prefixes: dict[str, TokenRouteOwner] = {}
        # Fail-closed requirements declared by built-in service plugins. Checked
        # at freeze/lifespan; an unmet requirement aborts startup.
        self._required_ownerships: list[RequiredOwnership] = []

    # ---- lifecycle ---------------------------------------------------------

    def freeze(self) -> None:
        """Transition BUILDING → FROZEN under the coordinator lock. Idempotent."""
        with self._coord:
            self._frozen = True

    def is_frozen(self) -> bool:
        with self._coord:
            return self._frozen

    # ---- provider registry -------------------------------------------------

    def register_provider(self, provider: DashboardAuthProvider) -> None:
        assert_protocol_compliance(type(provider))
        with self._coord:
            if self._frozen:
                raise FrozenRegistryError(
                    f"dashboard-auth registry is frozen; refusing provider "
                    f"registration ({provider.name!r}) after startup"
                )
            if provider.name in self._providers:
                raise ValueError(
                    f"dashboard-auth provider already registered: {provider.name!r}"
                )
            self._providers[provider.name] = provider
        _log.info(
            "dashboard-auth: registered provider %r (%s)",
            provider.name, provider.display_name,
        )

    def get_provider(self, name: str) -> Optional[DashboardAuthProvider]:
        with self._coord:
            return self._providers.get(name)

    def list_providers(self) -> List[DashboardAuthProvider]:
        with self._coord:
            return list(self._providers.values())

    def list_token_providers(self) -> List[DashboardAuthProvider]:
        with self._coord:
            return [p for p in self._providers.values()
                    if getattr(p, "supports_token", False)]

    def list_session_providers(self) -> List[DashboardAuthProvider]:
        with self._coord:
            return [p for p in self._providers.values()
                    if getattr(p, "supports_session", True)]

    def clear_providers(self) -> None:
        with self._coord:
            if self._frozen:
                raise FrozenRegistryError(
                    "dashboard-auth registry is frozen; refusing provider clearing"
                )
            self._providers.clear()

    # ---- token-route registry ---------------------------------------------

    def register_token_route(
        self, path: str, *, provider: str, capability: Optional[str] = None
    ) -> None:
        owner = TokenRouteOwner(provider, capability)
        with self._coord:
            existing = self._token_routes.get(path)
            if existing is not None and existing != owner:
                raise TokenRouteOwnershipError(
                    f"token route {path!r} already owned by {existing.provider!r}; "
                    f"refusing to reassign to {provider!r}"
                )
            if self._frozen and existing is None:
                raise TokenRouteRegistrationError(
                    f"token route registry is frozen; refusing to register new "
                    f"route {path!r} after startup"
                )
            self._token_routes[path] = owner

    def register_token_route_prefix(
        self, prefix: str, *, provider: str, capability: Optional[str] = None
    ) -> None:
        normalised = prefix if prefix.endswith("/") else prefix + "/"
        owner = TokenRouteOwner(provider, capability)
        with self._coord:
            existing = self._token_route_prefixes.get(normalised)
            if existing is not None and existing != owner:
                raise TokenRouteOwnershipError(
                    f"token route prefix {normalised!r} already owned by "
                    f"{existing.provider!r}; refusing to reassign to {provider!r}"
                )
            if self._frozen and existing is None:
                raise TokenRouteRegistrationError(
                    f"token route registry is frozen; refusing to register new "
                    f"prefix {normalised!r} after startup"
                )
            self._token_route_prefixes[normalised] = owner

    def clear_token_routes(self) -> None:
        with self._coord:
            if self._frozen:
                raise FrozenRegistryError(
                    "dashboard-auth registry is frozen; refusing token-route clearing"
                )
            self._token_routes.clear()
            self._token_route_prefixes.clear()

    # ---- fail-closed ownership requirements --------------------------------

    def require_route_ownership(
        self, *, provider: str, path: str, is_prefix: bool,
        capability: Optional[str] = None,
    ) -> None:
        """Declare that ``path`` MUST end up owned by ``provider`` (with
        ``capability``) or startup aborts. Idempotent for an identical
        requirement; refused after freeze (a requirement is a startup-time
        declaration). Declaring this BEFORE registering the provider/route means
        a later registration failure — even one the plugin loader swallows —
        leaves the requirement unmet, so :meth:`verify_required_ownerships`
        turns it into a fail-closed abort.

        Recording a requirement is a verification aid, not part of the
        immutable auth-decision state, so it is intentionally allowed even after
        freeze: a freeze-before-registration ordering bug then records a
        requirement whose provider/route registration is refused, which the
        lifespan verification still catches as an unmet requirement (a loud
        abort) rather than a silently-disabled surface. Idempotent."""
        req = RequiredOwnership(provider, path, is_prefix, capability)
        with self._coord:
            if req not in self._required_ownerships:
                self._required_ownerships.append(req)

    def verify_required_ownerships(self) -> None:
        """Fail closed if any declared :class:`RequiredOwnership` is unmet.

        For each requirement, the owning provider must be registered AND the
        exact route / prefix must be owned by exactly that provider with the
        required capability. Any shortfall raises
        :class:`ServiceRouteRegistrationError` naming provider/route/reason (no
        secret). Called at lifespan startup, after freeze and BEFORE the server
        accepts traffic."""
        failures: list[str] = []
        with self._coord:
            for req in self._required_ownerships:
                expected = TokenRouteOwner(req.provider, req.capability)
                if req.provider not in self._providers:
                    failures.append(
                        f"{req.path!r}: owning provider {req.provider!r} is not "
                        "registered (provider registration failed or was skipped)"
                    )
                    continue
                if req.is_prefix:
                    normalised = req.path if req.path.endswith("/") else req.path + "/"
                    actual = self._token_route_prefixes.get(normalised)
                else:
                    actual = self._token_routes.get(req.path)
                if actual != expected:
                    failures.append(
                        f"{req.path!r}: expected owner "
                        f"({req.provider!r}, capability={req.capability!r}) but "
                        f"found {actual!r} — route-ownership registration failed"
                    )
        if failures:
            raise ServiceRouteRegistrationError(
                "dashboard-auth: security-critical service route(s) not correctly "
                "registered before freeze; aborting startup (fail-closed): "
                + "; ".join(failures)
            )

    def _resolve_owner_locked(self, path: str):
        """Resolve the single owner of ``path``. MUST be called holding ``_coord``.

        Returns ``TokenRouteOwner``, ``None`` (unowned), or ``_AMBIGUOUS``.
        """
        exact = self._token_routes.get(path)
        prefix_hits = [
            (p, own) for p, own in self._token_route_prefixes.items()
            if _prefix_matches(path, p)
        ]
        candidates = []
        if exact is not None:
            candidates.append((len(path.split("/")) + 1, exact))
        for p, own in prefix_hits:
            candidates.append((len(p.rstrip("/").split("/")), own))
        if not candidates:
            return None
        best = max(c[0] for c in candidates)
        top = [own for (rank, own) in candidates if rank == best]
        if len({own.provider for own in top}) != 1:
            return _AMBIGUOUS
        return top[0]

    def route_owner(self, path: str) -> Optional[TokenRouteOwner]:
        with self._coord:
            owner = self._resolve_owner_locked(path)
        if owner is None or owner is _AMBIGUOUS:
            return None
        return owner  # type: ignore[return-value]

    def is_token_route(self, path: str) -> bool:
        with self._coord:
            return self._resolve_owner_locked(path) is not None

    # ---- authentication snapshot ------------------------------------------

    def resolve_auth_snapshot(self, path: str):
        """Return ``(owner, provider)`` for ``path`` from ONE consistent generation,
        or ``None`` to fail closed (unowned/ambiguous route, or owner's provider
        absent/incapable). Captured entirely under ``_coord``; the caller then
        releases and calls ``verify_token`` OUTSIDE any lock.
        """
        with self._coord:
            owner = self._resolve_owner_locked(path)
            if owner is None or owner is _AMBIGUOUS:
                return None
            provider = self._providers.get(owner.provider)
            if provider is None or not getattr(provider, "supports_token", False):
                return None
            return owner, provider


# The module-level default registry. Public functions in this package resolve it
# at call time. Tests inject isolation by rebinding this attribute to a fresh
# AuthRegistry() (autouse fixture) — there is NO runtime reset/unfreeze callable.
_default = AuthRegistry()


def freeze_dashboard_auth() -> None:
    """Seal the complete dashboard-auth registry (providers + token routes).

    Idempotent. Called from the dashboard app's lifespan startup once plugin
    discovery has registered every provider and route and BEFORE the server
    accepts traffic. After this, every mutator on both registries refuses.
    """
    _default.freeze()


def is_frozen() -> bool:
    """True once the shared dashboard-auth registry has been frozen."""
    return _default.is_frozen()


def require_route_ownership(
    *, provider: str, path: str, is_prefix: bool, capability: Optional[str] = None
) -> None:
    """Declare a fail-closed route-ownership requirement on the shared registry.
    See :meth:`AuthRegistry.require_route_ownership`."""
    _default.require_route_ownership(
        provider=provider, path=path, is_prefix=is_prefix, capability=capability
    )


def verify_service_route_ownership() -> None:
    """Abort startup (fail-closed) if any declared service-route ownership is
    unmet. See :meth:`AuthRegistry.verify_required_ownerships`. Called from the
    dashboard app's lifespan startup, after freeze and BEFORE serving."""
    _default.verify_required_ownerships()


def verify_token_against_snapshot(path: str, token: str):
    """Resolve the owner/provider snapshot for ``path`` under the coordinator
    lock, RELEASE the lock, then call the owner provider's ``verify_token`` with
    NO lock held. Returns ``(principal, unreachable_name)`` mirroring the legacy
    ``authenticate_token`` contract; the seam layers the capability/provider belt
    on top.
    """
    snap = _default.resolve_auth_snapshot(path)
    if snap is None:
        return None, None
    owner, provider = snap
    try:
        principal = provider.verify_token(token=token)
    except ProviderError as exc:
        _log.warning(
            "dashboard-auth: token provider %r unreachable during verify: %s",
            provider.name, exc,
        )
        return None, provider.name
    except Exception as exc:  # noqa: BLE001 — a buggy provider must not 500 the gate
        _log.warning(
            "dashboard-auth: token provider %r raised during verify: %s",
            provider.name, exc,
        )
        return None, None
    if principal is None:
        return None, None
    if principal.provider != owner.provider:
        return None, None
    if owner.capability is not None and owner.capability not in tuple(
        getattr(principal, "scopes", ()) or ()
    ):
        return None, None
    return principal, None
