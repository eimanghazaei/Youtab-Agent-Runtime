"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

This is the generic API-token capability (decisions.md Q-C): a reusable seam
that ANY service-to-service / machine-credential provider plugs into, NOT a
drain-specific hook. The drain bearer-secret plugin is merely the first
consumer.

How it fits the existing auth framework:

  * The interactive gate (``gated_auth_middleware``) authenticates a human
    via a session cookie on every non-public route. A service caller has no
    cookie — it presents a bearer token in the ``Authorization`` header on a
    single request. That is what this seam verifies.

  * A route opts in by registering its exact path via
    :func:`register_token_route`. Only registered paths are token-authable;
    everything else is untouched, so this can never accidentally widen the
    auth surface of an existing route.

  * :func:`token_auth_middleware` runs OUTERMOST (installed last in
    ``web_server.py``). For a token route it fully owns the auth decision:
    authenticate via the stacked token providers, attach the verified
    :class:`~youtab_agent_cli.dashboard_auth.base.TokenPrincipal` to
    ``request.state.token_principal`` + set ``request.state.token_authenticated``,
    and pass through; otherwise reject (401 unauthenticated, or 503 when a
    provider's backing store was unreachable). The downstream cookie/session
    gates honour ``token_authenticated`` and skip enforcement, so a
    token-authed service request is never bounced to ``/login``.

  * Fails closed: a token route with no registered token provider, no token,
    or an unrecognised token gets 401 — never an open pass-through.

Provider stacking mirrors ``verify_session``: each ``supports_token`` provider
is consulted in registration order until one returns a principal. A provider
that doesn't recognise the token returns ``None`` and the seam moves on; a
provider whose backing store is unreachable raises ``ProviderError``, which the
seam remembers and surfaces as 503 only if NO provider accepts the token.
"""
from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, NamedTuple, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from youtab_agent_cli.dashboard_auth import list_token_providers
from youtab_agent_cli.dashboard_auth.audit import AuditEvent, audit_log
from youtab_agent_cli.dashboard_auth.base import ProviderError, TokenPrincipal

_log = logging.getLogger(__name__)


class TokenRouteOwner(NamedTuple):
    """Who owns a token route and the capability its principal must carry.

    ``provider`` is the ``name`` of the single ``DashboardAuthProvider`` allowed
    to authenticate this route; ``capability`` (may be ``None``) is the scope the
    authenticated :class:`TokenPrincipal` must hold for the route.
    """

    provider: str
    capability: Optional[str]


class TokenRouteOwnershipError(RuntimeError):
    """A token route/prefix was registered for two different providers.

    Raised fail-closed at registration so a mis-wired deployment cannot end up
    with an ambiguously-owned surface. Carries no secret.
    """


# Provider-BOUND token routes. A token route is owned by exactly ONE provider,
# and only that provider may authenticate a request on it (the seam never tries
# every provider against every route — that would let a credential minted for
# one surface authenticate an unrelated one). Exact paths and segment-anchored
# PREFIXES (for versioned surfaces with parametric sub-paths, e.g.
# ``/api/runtime/v1/runs/{id}/events``) are both supported; the prefix ends in
# ``/`` so ``/api/runtime/v1/`` never matches a sibling ``/api/runtime/v1x``.
_token_routes: dict[str, TokenRouteOwner] = {}
_token_route_prefixes: dict[str, TokenRouteOwner] = {}
_lock = threading.Lock()

# Sentinel: a path matched by two owners of equal specificity — fail closed.
_AMBIGUOUS = object()


def register_token_route(
    path: str, *, provider: str, capability: Optional[str] = None
) -> None:
    """Mark ``path`` (exact match) as token-authable, owned by ``provider``.

    Call at plugin registration / app setup. Registering a route does NOT make
    it public — it makes it authenticate by ``provider``'s bearer token instead
    of by session cookie. Idempotent for the SAME (provider, capability);
    re-registering with a DIFFERENT owner raises
    :class:`TokenRouteOwnershipError` (fail-closed, registration closed).
    """
    owner = TokenRouteOwner(provider, capability)
    with _lock:
        existing = _token_routes.get(path)
        if existing is not None and existing != owner:
            raise TokenRouteOwnershipError(
                f"token route {path!r} already owned by {existing.provider!r}; "
                f"refusing to reassign to {provider!r}"
            )
        _token_routes[path] = owner


def register_token_route_prefix(
    prefix: str, *, provider: str, capability: Optional[str] = None
) -> None:
    """Mark every path under ``prefix`` (segment-anchored) as token-authable,
    owned by ``provider``.

    The prefix is normalised to end in ``/`` so the match is anchored at a
    path-segment boundary — registering ``/api/runtime/v1`` guards
    ``/api/runtime/v1/...`` but never a sibling like ``/api/runtime/v1x``.
    Idempotent for the SAME owner; a conflicting owner raises
    :class:`TokenRouteOwnershipError`.
    """
    normalised = prefix if prefix.endswith("/") else prefix + "/"
    owner = TokenRouteOwner(provider, capability)
    with _lock:
        existing = _token_route_prefixes.get(normalised)
        if existing is not None and existing != owner:
            raise TokenRouteOwnershipError(
                f"token route prefix {normalised!r} already owned by "
                f"{existing.provider!r}; refusing to reassign to {provider!r}"
            )
        _token_route_prefixes[normalised] = owner


def _resolve_owner(path: str):
    """Return the single :class:`TokenRouteOwner` for ``path``.

    Returns ``None`` when ``path`` is not a token route, or ``_AMBIGUOUS`` when
    two owners of equal specificity claim it. An exact route is strictly more
    specific than any prefix; among prefixes the longest wins; a tie between
    DIFFERENT providers is ambiguous and resolves fail-closed.
    """
    with _lock:
        exact = _token_routes.get(path)
        prefix_hits = [
            (p, own)
            for p, own in _token_route_prefixes.items()
            if path == p or path.startswith(p)
        ]
    candidates = []
    if exact is not None:
        # Exact beats every prefix (a prefix length can never exceed len(path)).
        candidates.append((len(path) + 1, exact))
    for p, own in prefix_hits:
        candidates.append((len(p), own))
    if not candidates:
        return None
    best = max(c[0] for c in candidates)
    top = [own for (length, own) in candidates if length == best]
    if len({own.provider for own in top}) != 1:
        return _AMBIGUOUS
    return top[0]


def route_owner(path: str) -> Optional[TokenRouteOwner]:
    """The single provider/capability owning ``path``, or ``None``.

    ``None`` covers both "not a token route" and "ambiguously owned"; callers
    that need a definite owner therefore fail closed on ``None``.
    """
    owner = _resolve_owner(path)
    if owner is None or owner is _AMBIGUOUS:
        return None
    return owner  # type: ignore[return-value]


def is_token_route(path: str) -> bool:
    """True if ``path`` is token-authable (a definite owner OR ambiguous).

    Ambiguous routes are still token routes so the seam OWNS the decision and
    rejects them (401), rather than letting them fall to the interactive cookie
    gate.
    """
    return _resolve_owner(path) is not None


def clear_token_routes() -> None:
    """Test-only: drop all registered token routes (exact + prefix)."""
    with _lock:
        _token_routes.clear()
        _token_route_prefixes.clear()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the ``Authorization`` header, or "".

    Accepts ``<scheme> <token>`` where scheme is "bearer" (case-insensitive).
    Returns an empty string for a missing/malformed header or a non-bearer
    scheme — the caller treats "" as "no token presented".
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

    Provider-bound: the seam resolves the single provider that owns the
    request's path and consults ONLY that provider — a token minted for one
    surface can never authenticate an unrelated one, even though both are token
    routes. A path with no definite owner (unowned or ambiguously owned) fails
    closed here.

    Returns ``(principal, unreachable_provider_name)``:
      * ``(TokenPrincipal, None)`` — the owner recognised and accepted the token.
      * ``(None, None)`` — no token, no/ambiguous owner, or the owner rejected it
        (reject 401).
      * ``(None, name)`` — the owner's backing store was unreachable (the caller
        surfaces 503, not 401, so a transient outage doesn't read as "bad
        credentials").

    Never raises: a provider ``ProviderError`` is caught and remembered.
    """
    token = extract_bearer_token(request)
    if not token:
        return None, None
    owner = route_owner(request.url.path)
    if owner is None:
        # Not a definite single-owner token route: fail closed. (An ambiguously
        # owned path resolves to None and is rejected here rather than tried.)
        return None, None
    unreachable: Optional[str] = None
    for provider in list_token_providers():
        if provider.name != owner.provider:
            continue  # only the route owner may authenticate this path
        try:
            principal = provider.verify_token(token=token)
        except ProviderError as e:
            _log.warning(
                "dashboard-auth: token provider %r unreachable during verify: %s",
                provider.name, e,
            )
            if unreachable is None:
                unreachable = provider.name
            continue
        except Exception as e:  # noqa: BLE001 — a buggy provider must not 500 the gate
            _log.warning(
                "dashboard-auth: token provider %r raised during verify: %s",
                provider.name, e,
            )
            continue
        if principal is None:
            continue
        # Belt to the owner-only suspenders: the principal must have come from
        # the owner and must carry the route's required capability.
        if principal.provider != owner.provider:
            continue
        if owner.capability is not None and owner.capability not in tuple(
            getattr(principal, "scopes", ()) or ()
        ):
            continue
        return principal, None
    return None, unreachable


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Outermost auth seam for token-authable routes.

    No-op pass-through for any path not registered via
    :func:`register_token_route`. For a registered path, token auth is the
    only accepted scheme:

      * valid token  → attach principal + ``token_authenticated`` flag, pass through.
      * unreachable  → 503 (provider backing store down; not "bad credentials").
      * otherwise    → 401 unauthenticated.

    Runs before the cookie/session gates (installed last in ``web_server.py``).
    The cookie gates honour ``request.state.token_authenticated`` and skip
    enforcement, so a token-authed request is never redirected to ``/login``.
    """
    path = request.url.path
    if not is_token_route(path):
        return await call_next(request)

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
