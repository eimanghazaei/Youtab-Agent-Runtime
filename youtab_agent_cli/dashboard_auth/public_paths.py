"""Shared allowlist of ``/api/*`` paths that bypass dashboard auth.

Two middlewares enforce dashboard auth and previously kept independent
copies of this list:

* ``youtab_agent_cli.web_server.auth_middleware`` — loopback / ``--insecure``
  mode, gates on the ephemeral ``_SESSION_TOKEN``.
* ``youtab_agent_cli.dashboard_auth.middleware.gated_auth_middleware`` —
  non-loopback mode, gates on the OAuth session cookie.

When the lists drifted, ``/api/status`` ended up public under the legacy
gate but 401'd under the OAuth gate. That broke the portal's wildcard
liveness probe (``youtab-account-service`` ``fly-provider.ts``
``getInstanceRuntimeStatus``), which fetches ``/api/status`` without a
cookie as its sole signal of "agent dashboard is alive": every healthy
wildcard-subdomain agent surfaced as STARTING/down in the portal UI even
though the dashboard was serving correctly.

Centralising the allowlist here so both middlewares import the same
frozenset prevents the next drift. Keep this list minimal — only truly
non-sensitive, read-only endpoints belong here. As a sanity check, every
entry should be safe to expose to:

  * external uptime probes (Pingdom, Better Stack, NAS),
  * the dashboard SPA before the user has logged in,
  * anyone who happens to ``curl`` the hostname.

If a new endpoint doesn't pass all three tests, it should be gated and
the SPA should bootstrap it after login instead.
"""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Minimal process liveness probe for desktop/backend boot handshakes. It
    # intentionally avoids gateway config, platform discovery, MCP setup, and
    # host-local detail so readiness checks cannot spend their budget inside
    # cold plugin imports.
    "/api/health",
    # Liveness probe target. Returns version, gateway state, active
    # session count, and the dashboard auth-gate shape. No bodies, no
    # session content, no secrets. Documented as the portal's wildcard
    # liveness probe in
    # ``docs/agent-dashboard-public-url-contract.md`` (NAS side).
    "/api/status",
    # Read-only config-defaults / schema feeds for the SPA's Config page.
    "/api/config/defaults",
    "/api/config/schema",
    # ``/api/model/info`` was here, on the reasoning that its payload was "the
    # same shape as provider catalogs already exposed on the public internet".
    # That premise no longer holds and the entry has been removed: the response
    # carries the configured upstream and the raw engine identifier, which is
    # exactly what a normal user must never be shown, and being on this list
    # meant it answered 200 to a caller with no credential at all.
    #
    # It is now an ordinary authenticated route, so the authorization gate can
    # resolve a principal for it and refuse everyone below Owner/Superadmin.
    # Nothing in the boot handshake needs it — desktop readiness probes
    # ``/api/health`` and ``/api/status``, both still public above.
    # Read-only theme + plugin manifests for the dashboard skin engine.
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    # Chronos managed-cron fire webhook (NAS -> agent). NOT cookie-gated: it
    # carries its own short-lived NAS-minted JWT (purpose=cron_fire), which the
    # handler verifies as the real auth. Must bypass the dashboard auth gate so
    # the NAS relay's bearer-only callback reaches the verifier instead of a
    # 401 no_cookie. The JWT — not this allowlist — is the security boundary.
    "/api/cron/fire",
})
