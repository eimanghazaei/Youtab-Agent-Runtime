"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

Provider-BOUND: each token route/prefix is owned by exactly ONE provider and the
seam authenticates a request only with that owner, so a credential minted for one
surface can never authenticate an unrelated one. The registration/resolution
state and the coordinator lock live in the single linearizable
:class:`youtab_agent_cli.dashboard_auth.lifecycle.AuthRegistry`; the functions
here are thin delegators plus the ASGI middleware.

  * A route opts in by registering its exact path / prefix via
    :func:`register_token_route` / :func:`register_token_route_prefix` — with a
    required owning ``provider`` and an optional required ``capability``.
  * :func:`token_auth_middleware` runs OUTERMOST. For a registered path it fully
    owns the auth decision: authenticate via the route OWNER only, attach the
    verified :class:`TokenPrincipal` + ``token_authenticated`` flag, pass through;
    otherwise reject (401, or 503 when the owner's backing store is unreachable).
  * Fails closed: an unowned/ambiguous route, a missing owner provider, a missing
    capability, or a token the owner rejects → 401 (never an open pass-through).

Lifecycle (Contract A, ENFORCED): every route/provider is registered during
``discover_plugins()`` — before the ASGI server accepts traffic — and
:func:`freeze_token_routes` (from the app lifespan startup) seals the COMPLETE
registry. After the freeze every mutator refuses; only a byte-identical idempotent
route re-registration is a no-op. See :mod:`...lifecycle` for the linearizability
and lock-order guarantees. ``verify_token`` is never called while the coordinator
lock is held.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from youtab_agent_cli.dashboard_auth import lifecycle
from youtab_agent_cli.dashboard_auth.audit import AuditEvent, audit_log
from youtab_agent_cli.dashboard_auth.base import TokenPrincipal

# Re-export the registry types so existing callers keep importing them from here.
from youtab_agent_cli.dashboard_auth.lifecycle import (  # noqa: F401
    FrozenRegistryError,
    LifecycleState,
    RegistryNotVerifiedError,
    ServiceRouteRegistrationError,
    TokenRouteOwner,
    TokenRouteOwnershipError,
    TokenRouteRegistrationError,
)

_log = logging.getLogger(__name__)


# --- registration + resolution (delegate to the single AuthRegistry) --------

def register_token_route(
    path: str, *, provider: str, capability: Optional[str] = None
) -> None:
    """Mark ``path`` (exact) as token-authable, owned by ``provider`` (+ optional
    ``capability``). Idempotent for the SAME owner; a conflicting owner raises
    :class:`TokenRouteOwnershipError`; a NEW route after freeze raises
    :class:`TokenRouteRegistrationError`."""
    lifecycle._default.register_token_route(path, provider=provider, capability=capability)


def register_token_route_prefix(
    prefix: str, *, provider: str, capability: Optional[str] = None
) -> None:
    """Mark every path under ``prefix`` (segment-anchored) as token-authable, owned
    by ``provider``. Same idempotency / conflict / freeze semantics as
    :func:`register_token_route`."""
    lifecycle._default.register_token_route_prefix(
        prefix, provider=provider, capability=capability)


def route_owner(path: str) -> Optional[TokenRouteOwner]:
    """The single provider/capability owning ``path``, or ``None`` (unowned or
    ambiguous — callers fail closed on ``None``)."""
    return lifecycle._default.route_owner(path)


def is_token_route(path: str) -> bool:
    """True if ``path`` is token-authable (a definite owner OR ambiguous — the
    seam owns and rejects the ambiguous case rather than letting it fall to the
    cookie gate)."""
    return lifecycle._default.is_token_route(path)


def clear_token_routes() -> None:
    """Drop all registered token routes. Refused once frozen (raises). Nothing on
    the serving path calls this; test isolation is a fresh injected registry."""
    lifecycle._default.clear_token_routes()


def require_route_ownership(
    *, provider: str, path: str, is_prefix: bool, capability: Optional[str] = None
) -> None:
    """Declare that a security-critical route MUST be owned by ``provider`` (with
    ``capability``) or startup aborts. A built-in service plugin calls this
    BEFORE registering its provider/route so a swallowed registration failure
    still fails closed at :func:`verify_service_route_ownership`. Delegates to the
    single :class:`~...lifecycle.AuthRegistry`."""
    lifecycle._default.require_route_ownership(
        provider=provider, path=path, is_prefix=is_prefix, capability=capability
    )


def verify_service_route_ownership() -> None:
    """Abort startup (fail-closed) if any declared service-route ownership is
    unmet. Called from the dashboard app's lifespan startup after
    :func:`freeze_token_routes`, before serving. Delegates to
    :func:`lifecycle.verify_service_route_ownership`."""
    lifecycle.verify_service_route_ownership()


def freeze_token_routes() -> None:
    """Seal the COMPLETE dashboard-auth registry (providers + token routes).

    Backwards-compatible name; delegates to
    :func:`lifecycle.freeze_dashboard_auth`. Idempotent. Called from the dashboard
    app's lifespan startup once discovery has registered everything and BEFORE the
    server accepts traffic."""
    lifecycle.freeze_dashboard_auth()


def is_frozen() -> bool:
    """True once the shared dashboard-auth registry has been frozen."""
    return lifecycle.is_frozen()


def is_verified() -> bool:
    """True only when the shared registry reached VERIFIED — the sole state in
    which token authentication may serve."""
    return lifecycle.is_verified()


# --- bearer extraction ------------------------------------------------------

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the ``Authorization`` header, or "".

    Accepts ``<scheme> <token>`` where scheme is "bearer" (case-insensitive).
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].strip().lower() == "bearer":
        return parts[1].strip()
    return ""


def authenticate_token(
    request: Request,
) -> Tuple[Optional[TokenPrincipal], Optional[str]]:
    """Authenticate the request's bearer token with the ROUTE OWNER only.

    Resolves the (owner, provider) snapshot for ``request.url.path`` under the ONE
    coordinator lock, releases it, and only then calls the owner provider's
    ``verify_token`` (no lock held). Returns ``(principal, unreachable_name)``:

      * ``(TokenPrincipal, None)`` — the owner recognised + accepted the token and
        the principal carries the route's required capability.
      * ``(None, None)`` — no token, unowned/ambiguous route, owner's provider
        absent, capability missing, or the owner rejected the token (reject 401).
      * ``(None, name)`` — the owner's backing store was unreachable (503 upstream).
    """
    token = extract_bearer_token(request)
    if not token:
        return None, None
    return lifecycle.verify_token_against_snapshot(request.url.path, token)


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Outermost auth seam for token-authable routes.

    No-op pass-through for a path with no registered owner. For a registered path,
    token auth is the only accepted scheme: valid token → attach principal +
    ``token_authenticated`` and pass through; owner unreachable → 503; otherwise
    401. The downstream cookie/session gates honour ``token_authenticated`` and
    skip enforcement.
    """
    path = request.url.path
    if not is_token_route(path):
        return await call_next(request)

    # Fail closed on an unverified generation: a registry that FROZE but never
    # reached VERIFIED (verification skipped, raced, or aborted) must not
    # authenticate a token-owned route — even though `_frozen` is true. In
    # production the lifespan freezes AND verifies before `yield`, so serving is
    # always VERIFIED; this refuses the dangerous FROZEN_UNVERIFIED window.
    if lifecycle.is_frozen() and not lifecycle.is_verified():
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE,
            reason="registry_not_verified",
            path=path,
            ip=_client_ip(request),
        )
        return JSONResponse(
            {"error": "service_unverified", "detail": "Service Unavailable"},
            status_code=503,
        )

    principal, unreachable = authenticate_token(request)
    if principal is not None:
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)

    if unreachable:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE,
            provider=unreachable,
            reason="provider_unreachable",
            path=path,
            ip=_client_ip(request),
        )
        return JSONResponse(
            {"detail": f"Auth provider {unreachable!r} unreachable"},
            status_code=503,
        )

    audit_log(
        AuditEvent.TOKEN_AUTH_FAILURE,
        reason="no_provider_recognises_token",
        path=path,
        ip=_client_ip(request),
    )
    return JSONResponse(
        {"error": "unauthenticated", "detail": "Unauthorized"},
        status_code=401,
    )
