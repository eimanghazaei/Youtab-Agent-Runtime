"""RuntimeServiceProvider — service-bearer auth for the Agent Runtime product surface.

The AR-PROD-01 analog of the bundled ``dashboard_auth/drain`` plugin, and the
second consumer of the generic non-interactive token-auth capability
(``supports_token`` / ``verify_token`` on the ``DashboardAuthProvider`` ABC +
the route-agnostic ``token_auth`` middleware seam).

What it is
----------
A service-to-service auth provider. The **youtab-ai-os gateway** (the product
control plane, api.youtab.io) holds a shared secret and presents it as an
``Authorization: Bearer`` token on every call its ``AgentRuntimeConnector``
makes to the runtime engine's ``/api/runtime/v1`` surface. This provider
verifies that token against ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` with a
constant-time compare and, on a match, vouches for the caller as the
``runtime-service`` principal with the ``runtime`` scope. It is NOT an
interactive identity provider — there is no login, cookie, session, or refresh.
It implements ONLY the token capability (``supports_token = True`` +
``verify_token``); the five interactive ABC methods raise ``NotImplementedError``.

Why a plugin (not an ad-hoc header check on the runtime routes)
--------------------------------------------------------------
Same governance principle the drain credential established: a service
credential MUST be a real auth plugin in the dashboard auth framework, not a
bolt-on. The framework widening that hosts it is generic and this plugin is
merely its second consumer. The runtime router's own identity dependency then
layers the *end-user* identity (tenant/user/roles, carried in gateway-verified
headers) on top of this *service* identity — two distinct trust facts.

Security properties (mirrors drain)
-----------------------------------
* **Constant-time compare** — ``hmac.compare_digest`` on the request path, so
  the endpoint is not a timing oracle.
* **Entropy gate at registration** — a weak/short/low-entropy secret fails
  CLOSED at load (the plugin declines to register and records a skip reason);
  it is never silently accepted. Bar: >= 256 bits of entropy / >= 43
  url-safe-base64 chars, not obviously structured.
* **Fail-closed surface** — when the secret is unset or too weak, the provider
  is not registered, so the token seam finds no provider that recognises the
  runtime bearer and the entire ``/api/runtime/v1`` surface answers 401. The
  runtime is disabled rather than exposed.

Configuration
-------------
The secret is a CREDENTIAL, carried via an env var (provisioned by the
deployment, never committed):

    YOUTAB_AGENT_RUNTIME_SERVICE_SECRET   # shared gateway<->engine secret (>=43 url-safe-b64 chars)

Behavioural knobs live in config.yaml (canonical surface):

    dashboard:
      runtime_auth:
        scope: runtime          # capability label attached to the principal
        min_secret_chars: 43    # entropy bar (optional; default 43 ~= 256 bits)

When ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` is unset, the plugin is a no-op
(records a skip reason) — deployments that don't front the engine with the
product gateway simply don't set it, and the runtime surface stays closed.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from youtab_agent_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)

# Reuse the drain plugin's fail-closed entropy gate verbatim so the two
# service credentials enforce an identical strength bar (single source of
# truth for "what counts as a strong shared secret").
from plugins.dashboard_auth.drain import assess_secret_strength

logger = logging.getLogger(__name__)

# The version-pinned runtime product surface. Registered as a token-authable
# PREFIX by ``register()`` so every parametric sub-path (runs/{id}/events, …)
# is guarded by the generic seam. Kept here (not imported from web_server) to
# avoid a heavy import at plugin load.
RUNTIME_ROUTE_PREFIX = "/api/runtime/v1/"

LAST_SKIP_REASON: str = ""


class RuntimeServiceProvider(DashboardAuthProvider):
    """Non-interactive shared-bearer-secret provider for the runtime surface."""

    name = "runtime-service"
    display_name = "Agent Runtime (service credential)"
    supports_token = True
    supports_session = False

    def __init__(self, *, secret: str, scope: str = "runtime") -> None:
        # Defence in depth: construction also enforces the entropy bar, so a
        # caller that bypasses register()'s check still can't build a weak
        # provider. register() does the friendly skip-reason path; this raises.
        reason = assess_secret_strength(secret)
        if reason is not None:
            raise ValueError(f"runtime service secret rejected: {reason}")
        self._secret = secret
        self._scope = scope or "runtime"

    # ---- token capability (the only thing this provider implements) --------

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        """Constant-time compare against the shared gateway<->engine secret.

        Returns a ``runtime-service`` principal on an exact match, else
        ``None`` (the generic seam falls through / fails closed). Uses
        ``hmac.compare_digest`` so a wrong token can't be recovered by timing.
        """
        if not token:
            return None
        if hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8")):
            return TokenPrincipal(
                principal="runtime-service",
                provider=self.name,
                scopes=(self._scope,),
            )
        return None

    # ---- interactive methods: unsupported (service credential only) --------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "RuntimeServiceProvider is a non-interactive service credential; "
            "there is no login flow."
        )

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(
            "RuntimeServiceProvider is a non-interactive service credential."
        )

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        # Not a cookie-session provider — it never mints a Session, so it can
        # never recognise a session cookie. Return None (don't raise) so it
        # stacks harmlessly in the cookie-verify loop.
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(
            "RuntimeServiceProvider is a non-interactive service credential."
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _load_config_runtime_auth_section() -> dict:
    """Return ``dashboard.runtime_auth`` from config.yaml, or ``{}``."""
    try:
        from youtab_agent_cli.config import cfg_get, load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        logger.debug(
            "dashboard-auth-runtime: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg_get(cfg, "dashboard", "runtime_auth", default=None)
    return section if isinstance(section, dict) else {}


def register(ctx) -> None:
    """Plugin entry — registers RuntimeServiceProvider when a strong secret is set.

    No-op (records a skip reason) when ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET``
    is unset or fails the entropy gate. On success, also registers the runtime
    surface prefix as token-authable via the generic seam so the gateway's
    bearer call is not bounced to the interactive login by the cookie gate.
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    # Resolve the secret from the SAME source the request path uses
    # (``web_routers/runtime.py::_runtime_secret`` → ``secret_file.env_or_file``):
    # the file-backed ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET_FILE`` (12-factor /
    # Docker/K8s secret) is honoured as well as the inline env var, so the V5
    # bundle — which sets only ``*_FILE`` — enables /api/runtime/v1 instead of
    # leaving register() a silent no-op. A dual (env + file) source is an
    # ambiguous configuration and env_or_file fails CLOSED (raises); we treat
    # that, and an unreadable/empty file, as "surface stays disabled" here rather
    # than crashing plugin load. The secret value is never logged.
    from youtab_agent_cli.secret_file import SecretFileError, env_or_file

    try:
        secret = (env_or_file("YOUTAB_AGENT_RUNTIME_SERVICE_SECRET") or "").strip()
    except SecretFileError as exc:
        LAST_SKIP_REASON = (
            f"YOUTAB_AGENT_RUNTIME_SERVICE_SECRET could not be resolved — {exc}. "
            "The runtime surface stays disabled (fail-closed)."
        )
        logger.warning("dashboard-auth-runtime: %s", LAST_SKIP_REASON)
        return
    if not secret:
        LAST_SKIP_REASON = (
            "YOUTAB_AGENT_RUNTIME_SERVICE_SECRET is not set (neither the inline "
            "var nor its _FILE variant). Set a >=256-bit secret (e.g. `python -c "
            "\"import secrets; print(secrets.token_urlsafe(32))\"`) shared with "
            "the youtab-ai-os gateway to enable the Agent Runtime product "
            "surface; leave it unset to keep /api/runtime/v1 disabled."
        )
        logger.debug("dashboard-auth-runtime: %s", LAST_SKIP_REASON)
        return

    section = _load_config_runtime_auth_section()
    scope = str(section.get("scope", "runtime") or "runtime").strip() or "runtime"
    try:
        min_chars = int(section.get("min_secret_chars", 43))
    except (TypeError, ValueError):
        min_chars = 43

    reason = assess_secret_strength(secret, min_chars=min_chars)
    if reason is not None:
        LAST_SKIP_REASON = (
            f"YOUTAB_AGENT_RUNTIME_SERVICE_SECRET rejected — {reason}. "
            "The runtime surface stays disabled (fail-closed)."
        )
        logger.warning("dashboard-auth-runtime: %s", LAST_SKIP_REASON)
        return

    try:
        provider = RuntimeServiceProvider(secret=secret, scope=scope)
    except ValueError as exc:
        LAST_SKIP_REASON = f"RuntimeServiceProvider construction failed: {exc}"
        logger.warning("dashboard-auth-runtime: %s", LAST_SKIP_REASON)
        return

    # The token-auth seam is a hard dependency of the running dashboard (its
    # middleware is installed by the web server). If it cannot even be imported,
    # the surface stays DISABLED — the provider is never registered, so there is
    # no partially-configured boundary (fail-closed by absence, not a partial
    # enable). This is the ONLY tolerated catch here.
    try:
        from youtab_agent_cli.dashboard_auth.token_auth import (
            register_token_route_prefix,
            require_route_ownership,
        )
    except Exception as exc:  # noqa: BLE001 — seam missing → surface disabled, no provider
        LAST_SKIP_REASON = (
            f"dashboard-auth token seam unavailable ({exc}); the runtime surface "
            "stays disabled (fail-closed, provider not registered)."
        )
        logger.warning("dashboard-auth-runtime: %s", LAST_SKIP_REASON)
        return

    # Opt the whole /api/runtime/v1 surface into the generic token-auth seam,
    # bound to THIS provider only and requiring the ``runtime`` scope the
    # router's ``require_service_identity`` also enforces.
    #
    # Fail-closed: DECLARE the ownership requirement BEFORE registering the
    # provider or the route. If EITHER registration fails — including a failure
    # the plugin loader swallows — the requirement stays unmet and the lifespan
    # verification (verify_service_route_ownership) aborts startup before serving
    # a single request, rather than leaving /api/runtime/v1 reachable through the
    # interactive cookie gate. No broad log-and-continue: any error propagates.
    require_route_ownership(
        provider=provider.name, path=RUNTIME_ROUTE_PREFIX,
        is_prefix=True, capability=scope,
    )
    ctx.register_dashboard_auth_provider(provider)
    register_token_route_prefix(
        RUNTIME_ROUTE_PREFIX, provider=provider.name, capability=scope
    )

    logger.info(
        "dashboard-auth-runtime: registered runtime service-credential provider "
        "(scope=%s, prefix=%s)",
        scope, RUNTIME_ROUTE_PREFIX,
    )
