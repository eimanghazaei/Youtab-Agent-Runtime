"""Who may do what, decided server-side and denied by default.

This module replaces a deployment-wide switch that was the wrong shape. The
earlier gate asked "does this deployment offer credential management" and
answered for everybody at once, which meant the only way to give the Owner the
provider catalogue was to give it to every user of the same deployment. The
requirement was never to remove the capability — it was to separate who
reaches it. So the question asked here is "may *this principal* do *this*",
and the answer comes from a role and an explicit scope grant.

Three populations, deliberately not a ladder
--------------------------------------------
A customer Tenant Admin is not a junior Youtab Superadmin. They administer
their own company — its users, its usage, its billing, its audit trail — and
they must never see Youtab's private provider bindings, raw credentials or
internal engine identifiers, because those are not their company's business at
any privilege level. Modelling authority as a single ascending scale would
make "more senior customer" shade into "sees Youtab's infrastructure", which
is exactly the boundary that has to hold. Tenant scope and Youtab-internal
scope are therefore different axes, and a role carries an explicit scope set
rather than "everything below it".

Default-deny, and what that costs
---------------------------------
:data:`ROLE_SCOPES` gives ``NORMAL_USER`` an empty frozenset. A principal that
resolves to no known role is a normal user, and a route with no entry in
:data:`ROUTE_SCOPES` is *not* thereby public — :func:`required_scope` returns
``None`` only for paths deliberately listed as unguarded, and the caller
treats an unmapped guarded prefix as a refusal. The cost is real: a new admin
route is refused until someone maps it. That is the intended failure
direction, because the opposite one ships a surface nobody authorised.

Identity comes from the session, authority comes from the roster
----------------------------------------------------------------
A ``Session`` proves who someone is. It does not say what they may do, and it
must not: the provider that authenticates a user is not the authority on
whether that user is a Youtab Superadmin. Authority is a separate, explicit
grant — the roster — resolved here, server-side, and never taken from a
client-supplied header, query parameter or token claim that the browser could
influence. Until SSO group claims are wired, the roster is the only source.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Mapping


class Role(StrEnum):
    """The populations this system distinguishes.

    Values are stable strings: they appear in the roster document and in audit
    records, so renaming one is a data migration, not a refactor.
    """

    NORMAL_USER = "normal_user"
    TENANT_ADMIN = "tenant_admin"
    YOUTAB_OPERATOR = "youtab_operator"
    YOUTAB_SUPERADMIN = "youtab_superadmin"
    YOUTAB_OWNER = "youtab_owner"


# --- Scopes -----------------------------------------------------------------
# Named for the capability, not the route, so a route can move without the
# grant that protects it having to change.

#: Read the private provider/engine catalogue, provider health, and the raw
#: upstream identifiers a normal user must never see.
PROVIDER_READ: Final = "provider:read"
#: Change engine bindings, routing and fallback configuration.
PROVIDER_WRITE: Final = "provider:write"
#: List credential *metadata* — which slots are set, when they were rotated.
#: Never the secret itself; no scope in this module grants that, because
#: nothing may return an already-stored secret.
CREDENTIAL_READ: Final = "credential:read"
#: Create, validate, rotate or revoke an upstream credential.
CREDENTIAL_WRITE: Final = "credential:write"
#: Select a raw engine identifier rather than a public Youtab profile.
ENGINE_SELECT: Final = "engine:select"
#: Administer one's own tenant: its users, roles, usage, billing, audit.
TENANT_MANAGE_OWN: Final = "tenant:manage:own"
#: Administer any tenant. Youtab-internal only.
TENANT_MANAGE_ANY: Final = "tenant:manage:any"
#: Deployment, rollback and infrastructure controls.
DEPLOYMENT_MANAGE: Final = "deployment:manage"
#: Subscribe to the realtime event stream. Its own scope rather than a
#: byproduct of being signed in: the stream carries gateway lifecycle, session
#: and system activity, so "authenticated" was never the right bar for it.
EVENTS_READ: Final = "events:read"

# --- Sentinels --------------------------------------------------------------
# Not capabilities. They are how the table says "this route is reachable
# without a grant" and "this route needs a session but no particular grant",
# so that every route resolves to an explicit decision and ``None`` can mean
# one thing only: nobody has classified this, therefore refuse.

#: Reachable with no credential. Reserved for routes that must answer before a
#: session can exist -- the login flow, and the SPA shell that renders it.
PUBLIC: Final = "public"
#: Any verified session, no particular grant. Written per route, like every
#: other decision; it is not a default and not a fallback.
AUTHENTICATED: Final = "authenticated"

# --- Capability scopes ------------------------------------------------------
# The user-facing surface: what a signed-in person may do with their own
# installation. ``NORMAL_USER`` holds these, because withholding them would
# not be a security boundary -- it would be removing the product.

SESSION_READ: Final = "session:read"
SESSION_WRITE: Final = "session:write"
PROFILE_READ: Final = "profile:read"
PROFILE_WRITE: Final = "profile:write"
REPO_READ: Final = "repo:read"
REPO_WRITE: Final = "repo:write"
SKILL_READ: Final = "skill:read"
SKILL_WRITE: Final = "skill:write"
CONFIG_READ: Final = "config:read"
CONFIG_WRITE: Final = "config:write"
FS_READ: Final = "fs:read"
FS_WRITE: Final = "fs:write"
UI_READ: Final = "ui:read"
UI_WRITE: Final = "ui:write"
MEMORY_READ: Final = "memory:read"
MEMORY_WRITE: Final = "memory:write"
TOOL_MANAGE: Final = "tool:manage"
AUTOMATION_MANAGE: Final = "automation:manage"
#: Administer pairings: list who is paired or pending, approve a pending
#: request by its server-side id, revoke another identity's access, clear the
#: pending queue. Every one of those acts on *somebody else's* access to this
#: agent, which is why it is not in the ordinary user baseline.
DEVICE_MANAGE: Final = "device:manage"
#: Redeem a pairing code that was DM'd to you. Deliberately separate from
#: :data:`DEVICE_MANAGE`: the code is never returned by any API, so possession
#: of it *is* the proof that you are the person who asked to pair. That makes
#: redeeming one self-service, while approving by request id is not.
DEVICE_PAIR_SELF: Final = "device:pair:self"
MESSAGING_MANAGE: Final = "messaging:manage"
PLUGIN_USE: Final = "plugin:use"
#: Operations, drain, self-update, and the generated API documentation. Not a
#: user capability: it describes or moves the deployment.
OPS_MANAGE: Final = "ops:manage"

# --- WebSocket scopes -------------------------------------------------------
# Starlette's HTTP middleware never runs on a WebSocket upgrade, so the refusal
# in :func:`authorize` does not reach these. Each socket enforces its own scope
# at the upgrade; naming them here makes the socket surface a table rather than
# something to hunt for across handlers.

#: Interactive shell. The highest-authority socket on the surface.
PTY_SCOPE: Final = "session:write"
#: The chat/console stream.
CONSOLE_SCOPE: Final = "session:write"
#: General dashboard socket.
WS_SCOPE: Final = "session:write"
#: Text-to-speech stream. Read-shaped: it emits audio, it does not drive a
#: session.
AUDIO_STREAM_SCOPE: Final = "session:read"

#: The capability set a signed-in person holds over their own installation.
#: Named separately so the grant can be read on its own and confirmed to carry
#: no provider, credential, engine, tenant, deployment or ops scope.
USER_CAPABILITIES: Final[frozenset[str]] = frozenset({
    SESSION_READ, SESSION_WRITE, PROFILE_READ, PROFILE_WRITE,
    REPO_READ, REPO_WRITE, SKILL_READ, SKILL_WRITE,
    CONFIG_READ, CONFIG_WRITE, FS_READ, FS_WRITE,
    UI_READ, UI_WRITE, MEMORY_READ, MEMORY_WRITE,
    TOOL_MANAGE, AUTOMATION_MANAGE, DEVICE_PAIR_SELF, MESSAGING_MANAGE,
    PLUGIN_USE,
})

#: What each role may do. Written out per role rather than inherited, so the
#: Tenant Admin row can be read on its own and confirmed to contain no
#: provider, credential or engine scope at all.
ROLE_SCOPES: Final[Mapping[Role, frozenset[str]]] = {
    # Not empty any more, and the change is deliberate. Once an unmapped route
    # is a refusal, a role holding nothing can reach nothing -- so the
    # capabilities a signed-in person already had over their own installation
    # have to be named rather than assumed. This restores exactly what was
    # reachable before the flip and widens nothing: the provider, credential,
    # engine, tenant, deployment and ops scopes are all absent from it.
    Role.NORMAL_USER: USER_CAPABILITIES,
    Role.TENANT_ADMIN: USER_CAPABILITIES | frozenset({
        TENANT_MANAGE_OWN, DEVICE_MANAGE,
    }),
    # An operator holds nothing implicitly. Their scopes come from the roster
    # entry, one capability at a time — "explicitly scoped" is the whole point
    # of the role, so a blanket grant here would defeat it.
    Role.YOUTAB_OPERATOR: frozenset(),
    Role.YOUTAB_SUPERADMIN: USER_CAPABILITIES | frozenset({
        PROVIDER_READ, PROVIDER_WRITE, CREDENTIAL_READ, CREDENTIAL_WRITE,
        ENGINE_SELECT, TENANT_MANAGE_ANY, DEPLOYMENT_MANAGE, EVENTS_READ,
        OPS_MANAGE, DEVICE_MANAGE,
    }),
    Role.YOUTAB_OWNER: USER_CAPABILITIES | frozenset({
        PROVIDER_READ, PROVIDER_WRITE, CREDENTIAL_READ, CREDENTIAL_WRITE,
        ENGINE_SELECT, TENANT_MANAGE_ANY, DEPLOYMENT_MANAGE, EVENTS_READ,
        OPS_MANAGE, DEVICE_MANAGE,
    }),
}

#: What a refused caller is told. It states the decision and nothing else: no
#: upstream name, no list of what would otherwise have been available, no hint
#: about which role or scope would change the answer. A refusal that explains
#: how to defeat itself is a worse refusal.
REFUSAL_DETAIL: Final[str] = (
    "This account is not authorized for this administrative operation."
)

#: Roles that are Youtab-internal. Used to refuse cross-tenant reads without
#: having to enumerate every tenant-scoped route.
INTERNAL_ROLES: Final[frozenset[Role]] = frozenset({
    Role.YOUTAB_OPERATOR, Role.YOUTAB_SUPERADMIN, Role.YOUTAB_OWNER,
})


@dataclass(frozen=True)
class Principal:
    """A resolved caller: who they are, and what they may do.

    ``scopes`` is the effective set — the role's baseline unioned with any
    extra capabilities the roster granted this individual. It is computed at
    resolution time so no caller has to remember to combine the two.
    """

    user_id: str
    org_id: str
    role: Role = Role.NORMAL_USER
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_internal(self) -> bool:
        return self.role in INTERNAL_ROLES


# --- Roster -----------------------------------------------------------------

#: Inline JSON roster. Read from the environment so production supplies it from
#: the Secret Manager rather than from a file in the image.
ROSTER_ENV: Final[str] = "YOUTAB_AUTHZ_ROSTER"
#: Path to a JSON roster, for deployments that mount a file instead.
ROSTER_FILE_ENV: Final[str] = "YOUTAB_AUTHZ_ROSTER_FILE"


class RosterError(ValueError):
    """The roster document is unusable. Carries no user data."""


def _parse_roster(raw: str) -> dict[str, dict]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RosterError("roster is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RosterError("roster must be a JSON object keyed by user id")
    return parsed


def load_roster(environ: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Load the internal-authority roster, or return an empty one.

    An absent roster is not an error and must not be: a deployment with no
    internal administrators is a valid deployment, and it resolves every caller
    to a normal user. A *malformed* roster is a different matter and raises,
    because silently treating an unparseable grant document as "nobody has any
    authority" would look identical to a working lockdown right up until the
    Owner could not log in to fix it.
    """
    source = os.environ if environ is None else environ
    inline = source.get(ROSTER_ENV, "").strip()
    if inline:
        return _parse_roster(inline)
    path = source.get(ROSTER_FILE_ENV, "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return _parse_roster(handle.read())
        except OSError as exc:
            raise RosterError("roster file could not be read") from exc
    return {}


def resolve_principal(
    *,
    user_id: str,
    org_id: str,
    roster: Mapping[str, dict] | None = None,
) -> Principal:
    """Resolve a verified identity to a principal with effective scopes.

    ``user_id`` must come from a verified session, never from a request the
    client controls. An identity absent from the roster is a normal user with
    no scopes — which is also what an empty, missing or unrecognised entry
    produces, so there is no arrangement of roster data that accidentally
    grants authority.
    """
    entries = load_roster() if roster is None else roster
    entry = entries.get(user_id)
    # The baseline a *verified* identity carries without a roster entry. Not a
    # weakening: before the flip an unrostered user reached every unguarded
    # route, which was the entire user surface. Resolving them to an empty
    # scope set would not restrict authority -- it would lock every ordinary
    # customer out of their own installation while leaving the privileged
    # scopes exactly as unreachable as they already were.
    #
    # Keyed on ``user_id`` being non-empty. An unauthenticated caller never
    # reaches here (``_principal_for_request`` returns a scopeless principal
    # for a missing session), so this cannot grant anything to someone who has
    # not proved who they are.
    baseline = ROLE_SCOPES[Role.NORMAL_USER] if user_id else frozenset()
    if not isinstance(entry, dict):
        return Principal(user_id=user_id, org_id=org_id, scopes=baseline)

    try:
        role = Role(entry.get("role", Role.NORMAL_USER))
    except ValueError:
        # An unrecognised role name is a typo or a downgrade of this binary
        # against a newer roster. Either way the safe reading is "no elevated
        # authority", never "some authority we cannot name" -- the ordinary
        # user baseline, and nothing above it.
        return Principal(user_id=user_id, org_id=org_id, scopes=baseline)

    granted = entry.get("scopes", ())
    extra = frozenset(s for s in granted if isinstance(s, str))
    return Principal(
        user_id=user_id,
        org_id=org_id,
        role=role,
        scopes=ROLE_SCOPES.get(role, frozenset()) | extra,
    )


# --- Route policy -----------------------------------------------------------

#: (method-agnostic prefix) → scope required to reach it. Longest prefix wins,
#: so a narrower rule can be added without reordering the table.
#:
#: Reads and writes are separated where the distinction is real: listing which
#: credential slots are set is not the same act as setting one.
ROUTE_SCOPES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/env/reveal", CREDENTIAL_WRITE),
    ("/api/env", CREDENTIAL_READ),
    ("/api/model/set", ENGINE_SELECT),
    ("/api/model/moa", ENGINE_SELECT),
    ("/api/model/", PROVIDER_READ),
    ("/api/providers/", PROVIDER_READ),
    ("/api/credentials/", CREDENTIAL_WRITE),
    # Gateway lifecycle. Bringing the gateway down is the most consequential
    # control the dashboard offers -- it is a denial of service to every user
    # of the deployment -- so it is held to the deployment scope rather than
    # left reachable by anyone who is merely signed in.
    #
    # Enumerated per verb rather than guarding the whole `/api/gateway/`
    # prefix: `/api/gateway/drain` authenticates through the non-interactive
    # token seam, which attaches a token principal and no session, so a prefix
    # rule would resolve that caller to a scopeless normal user and refuse the
    # NAS-driven drain that works today.
    ("/api/gateway/start", DEPLOYMENT_MANAGE),
    ("/api/gateway/stop", DEPLOYMENT_MANAGE),
    ("/api/gateway/restart", DEPLOYMENT_MANAGE),
    ("/api/gateway/jobs", DEPLOYMENT_MANAGE),
    # The realtime stream carries gateway lifecycle, session and system
    # activity, so "signed in" was never the right bar for it.
    #
    # Only the stream, not the ticket mint. One ticket serves `/api/pty`,
    # `/api/console`, `/api/ws` and `/api/pub` as well, so gating the mint on
    # this scope would make the terminal require permission to read events.
    # Authorization belongs at each socket, which is where `events_ws`
    # enforces it -- and it has to be there anyway, because Starlette's HTTP
    # middleware never runs on a WebSocket upgrade.
    ("/api/events", EVENTS_READ),

    # --- Cluster grants -----------------------------------------------------
    # Assigned per cluster rather than per endpoint, deliberately. Reading 250
    # function bodies before closing a surface that includes an interactive
    # shell, arbitrary file write and the endpoint that rewrites the agent's
    # system prompt gets the ordering backwards: a wrong scope here is a 403
    # that shows up in seconds and costs one line to correct, while an
    # unmapped route is an open door for as long as it takes to read
    # everything. Longest prefix still wins, so any of these can be narrowed
    # later without reordering the table.
    ("/api/sessions", SESSION_READ),
    ("/api/profiles", PROFILE_READ),
    ("/api/git", REPO_READ),
    ("/api/skills", SKILL_READ),
    ("/api/config", CONFIG_READ),
    ("/api/fs", FS_READ),
    ("/api/files", FS_READ),
    ("/api/dashboard", UI_READ),
    ("/api/memory", MEMORY_READ),
    ("/api/learning", MEMORY_READ),
    ("/api/mcp", TOOL_MANAGE),
    ("/api/tools", TOOL_MANAGE),
    ("/api/cron", AUTOMATION_MANAGE),
    ("/api/webhooks", AUTOMATION_MANAGE),
    ("/api/curator", AUTOMATION_MANAGE),
    ("/api/ops", OPS_MANAGE),
    ("/api/gateway/drain", OPS_MANAGE),
    ("/api/youtab/update", OPS_MANAGE),
    ("/api/pairing", DEVICE_MANAGE),
    ("/api/messaging", MESSAGING_MANAGE),
    ("/api/plugins", PLUGIN_USE),

    # AR-PROD-01 Agent Runtime product surface. Like /api/gateway/drain and the
    # MCP OAuth callback, this is a service-to-service contract, not an
    # interactive dashboard surface: it authenticates through the non-interactive
    # token seam (the `runtime-service` provider's shared bearer secret) and the
    # caller is a machine (the youtab-ai-os gateway), which carries a token
    # principal and no session — so the RBAC gate here would resolve it to a
    # scopeless user and wrongly refuse it. The real, stronger boundary lives at
    # the endpoint: the token seam 401s any request without the service bearer,
    # and the router's `require_service_identity` dependency additionally enforces
    # the `runtime` scope + the gateway-verified end-user identity headers, with
    # signed+replay-protected commands on every mutation. The RBAC gate therefore
    # defers to that guard (PUBLIC), exactly as it does for the other service /
    # edge routes below. See youtab_agent_cli/web_routers/runtime.py.
    ("/api/runtime/v1", PUBLIC),

    # Nearest owning domain, by the same rule.
    ("/api/analytics", UI_READ),
    ("/api/logs", OPS_MANAGE),
    ("/api/media", FS_READ),
    ("/api/portal", UI_READ),
    ("/api/system", OPS_MANAGE),
    ("/api/ssh", REPO_READ),
    ("/api/egress", OPS_MANAGE),
    ("/api/actions", SESSION_WRITE),
    ("/api/chat", SESSION_WRITE),
    ("/api/audio", SESSION_READ),
    ("/api/youtab", UI_READ),
    ("/api/auth", AUTHENTICATED),

    # The generated API documentation describes every route on the deployment,
    # including the privileged ones. That is an operations surface, not a user
    # one, and the cheapest way for an attacker to learn the shape of what
    # they are attacking.
    # Dashboard plugin assets. Declared `/dashboard-plugins/{plugin_name}/
    # {file_path:path}`, so a real asset is `/dashboard-plugins/x/dist/index.js`
    # -- three segments, which a two-segment pattern refused. A prefix is what
    # a `:path` route is. The endpoint remains the guard for *what* it serves:
    # it resolves the target and rejects anything outside the plugin base with
    # 403, and restricts to a browser-fetchable suffix allowlist, so widening
    # the authorization rule does not widen what can be read.
    ("/dashboard-plugins", PUBLIC),
    # Hosted MCP OAuth callback. The upstream provider redirects a browser here
    # with no cookie for this origin. Verified fail-closed at the endpoint: it
    # requires an in-flight flow in `authorization_required` whose
    # `expected_state` matches by `secrets.compare_digest`, 404s when none
    # matches, and rejects a replayed callback with 409. The OAuth state is the
    # boundary, and it is single-use.
    #
    # A prefix, not a pattern: the route is `{server_name:path}`, so a server
    # name that decodes to more than one segment is still this route.
    ("/api/mcp/oauth/callback", PUBLIC),
    ("/docs", OPS_MANAGE),
    ("/redoc", OPS_MANAGE),
    ("/openapi.json", OPS_MANAGE),
)

#: Routes matched on the whole path rather than as a prefix.
#:
#: A prefix rule cannot express these. ``/login`` as a prefix would also claim
#: a future ``/login-something``; ``/`` as a prefix would claim the entire
#: application. Exact matching says what is meant and nothing more.
EXACT_ROUTE_SCOPES: Final[Mapping[str, str]] = {
    # The login flow. Every one of these has to answer before a session can
    # exist, so requiring one would make the deployment unreachable -- the
    # bootstrap paradox, not a judgement that the data is harmless.
    # NOT public, despite being part of the login story. Read the handler:
    # `api_auth_csrf` raises 401 when `request.state.session` is None, and its
    # docstring is explicit -- "a single-use CSRF token for the authenticated
    # principal". Calling it public would not make it reachable; it would only
    # misdescribe the surface and dilute what "public" means. It falls to the
    # `/api/auth` prefix rule, which is AUTHENTICATED, matching `/api/auth/me`
    # and `/api/auth/ws-ticket` beside it.
    "/api/auth/providers": PUBLIC,
    "/auth/login": PUBLIC,
    "/auth/callback": PUBLIC,
    "/auth/password-login": PUBLIC,
    "/auth/logout": PUBLIC,
    "/login": PUBLIC,
    "/auth/native/authorize": PUBLIC,
    "/auth/native/token": PUBLIC,
    "/auth/native/refresh": PUBLIC,
    # Process liveness for a local supervisor. Returns no configuration.
    "/api/health": PUBLIC,
    # Liveness for the portal. NAS `fly-provider.ts getInstanceRuntimeStatus`
    # fetches this without a cookie as its sole signal that a
    # wildcard-subdomain agent is alive; holding it to a scope surfaced every
    # healthy agent as STARTING/down. The body is deliberately shaped for an
    # anonymous reader -- version, gateway state, active session count and the
    # auth-gate shape -- and `test_status_withholds_host_detail_in_gated_mode`
    # holds the line that absolute host paths, the gateway PID and the
    # internal health URL never appear in it.
    "/api/status": PUBLIC,
    # The SPA's pre-login bootstrap. All four are on the middlewares'
    # ``PUBLIC_API_PATHS`` allowlist and were reachable without a cookie
    # before authorization was default-deny; guarding them by cluster was a
    # new restriction that broke the login screen's own rendering. Bodies
    # read, not assumed:
    #
    #   /api/config/defaults   returns the shipped ``DEFAULT_CONFIG`` constant
    #                          -- defaults, never this deployment's values.
    #   /api/config/schema     returns field definitions and category order:
    #                          the shape of the form, not its contents.
    #   /api/dashboard/themes  theme manifests plus the active theme name.
    #                          The skin engine renders the login screen from
    #                          these, so they must answer before a session.
    #   /api/dashboard/plugins plugin names and mount points, already gated to
    #                          the enabled set. Exact, not prefix: the deeper
    #                          ``/api/dashboard/plugins/hub`` stays ``ui:read``.
    "/api/config/defaults": PUBLIC,
    "/api/config/schema": PUBLIC,
    "/api/dashboard/themes": PUBLIC,
    "/api/dashboard/plugins": PUBLIC,
    # The SPA shell: the HTML that renders the login screen, so it cannot
    # require the session that screen exists to obtain.
    #
    # One key, two endpoint bodies -- ``serve_spa`` when the frontend is built,
    # ``no_frontend`` (a 404 JSON) when it is not. Public is correct for both,
    # so the classification holds in either artifact state.
    "/": PUBLIC,
    # Identity of the current session, and the single-use ticket a browser
    # needs because it cannot set Authorization on a WebSocket upgrade.
    "/api/auth/me": AUTHENTICATED,
    "/api/auth/ws-ticket": AUTHENTICATED,
    # Two routes inside the plugin cluster whose bodies were read before the
    # cluster rule was applied, and which do not carry plugin authority.
    # ``model-options`` returns authenticated provider slugs and their model
    # lists; ``profiles`` returns the provider and model bound to every
    # installed profile. Both are the private engine catalogue that
    # ``/api/model/options`` is already held at ``provider:read`` for.
    #
    # Exact rather than prefix on purpose: as a prefix, ``.../profiles`` would
    # also claim ``.../profiles/{name}`` and ``.../profiles/{name}/
    # describe-auto``, which are user-authored descriptions and must stay
    # ordinary plugin capability. Restricting a disclosure must not cost the
    # editing feature beside it.
    # Chronos managed-cron fire webhook, NAS to agent. Not unauthenticated: it
    # carries a short-lived NAS-minted JWT (purpose=cron_fire) that the handler
    # verifies, and that JWT -- not this entry -- is the security boundary.
    # Held to `automation:manage` it returned 403 before reaching the verifier,
    # which broke every managed cron fire and turned a bad-token 401 into a
    # 403. Public here means "the cookie gate does not apply", not "open".
    "/api/cron/fire": PUBLIC,
    # Found by reading bodies during the route-by-route validation of what the
    # normal-user baseline actually reaches. All three were granted to every
    # signed-in user by their cluster and should not have been.
    #
    #   /api/analytics/models  selects `model, billing_provider` per session --
    #                          raw engine identifiers and the billing provider,
    #                          the same catalogue /api/model/options is held at
    #                          provider:read for. `/api/analytics/usage` stays
    #                          ui:read: it is the caller's own cost totals.
    #   /api/portal            reports each subscription feature's
    #                          `current_provider`, which is the provider
    #                          binding, alongside Youtab account state.
    #   /api/ssh/ownership     returns `sshOwnerNonce`, a live secret. Its own
    #                          docstring calls it a sensitive endpoint; repo:read
    #                          handed it to every ordinary user.
    # The /api/tools/toolsets/* group, found by sweeping every endpoint the
    # normal-user baseline reaches for bodies that touch provider bindings,
    # credentials or engine identifiers. All four sat at `tool:manage`, which
    # is ordinary user capability, and none of them is.
    #
    #   .../env    PUT persists API keys into ~/.youtab-agent-runtime/.env via
    #              save_env_value -- the same credential store `/api/env` PUT is
    #              held at credential:write for. An allowlist limits *which*
    #              env vars, not the fact that it writes credentials.
    #   .../config GET returns the provider matrix with each provider's env_vars
    #              annotated `is_set` -- the provider catalogue plus exactly the
    #              credential-slot metadata credential:read exists to gate.
    #   .../models GET returns a backend's model catalogue, priced per model.
    #   .../model  PUT persists the engine selection, the same act
    #              `/api/model/set` is held at engine:select for.
    # Redeeming a pairing code. The rest of `/api/pairing` stays
    # `device:manage`, because listing, revoking and clearing all act on other
    # identities. The endpoint itself additionally requires `device:manage`
    # when the caller approves by request id rather than by code -- the
    # authorization layer matches on path and method and cannot see which
    # branch a body selects, so that half of the decision has to live in the
    # handler.
    # The raw configuration file, read verbatim and replaced wholesale.
    #
    # `config.yaml` is where the engine bindings live -- `model.default`,
    # `model.provider`, `mcp_servers` and the custom endpoint base URLs -- so
    # `GET .../raw` hands a normal user the same private catalogue
    # `/api/model/options` is held at `provider:read` for, plus the file's
    # absolute host path.
    #
    # `PUT .../raw` is worse and is the widest bypass found on this surface:
    # it is a full-document replacement (`merge_existing=False`), so a caller
    # who can write it can set `model.provider` directly and defeat
    # `engine:select`, `provider:write` and the custom-endpoint controls in one
    # request, without ever touching the routes those scopes guard.
    #
    # `/api/config/defaults` and `/api/config/schema` stay public: they are the
    # shipped defaults and the form's shape, not this deployment's values.
    "/api/config/raw": PROVIDER_READ,
    "/api/pairing/approve": DEVICE_PAIR_SELF,
    "/api/analytics/models": PROVIDER_READ,
    "/api/portal": PROVIDER_READ,
    "/api/ssh/ownership": OPS_MANAGE,
    "/api/plugins/kanban/model-options": PROVIDER_READ,
    "/api/plugins/kanban/profiles": PROVIDER_READ,
}

#: Routes whose path carries parameters, matched against the whole path.
#:
#: Written as the inventory's normalised templates (``{}`` for a dynamic
#: segment) and compiled to anchored patterns, so a parameter cannot be renamed
#: into a different decision and a segment cannot swallow a ``/``.
PATTERN_ROUTE_SCOPES: Final[tuple[tuple[str, str], ...]] = (
    # Built CSS, served to the login screen. Single segment: the route is
    # `/assets/{filename}.css`, not a `:path`.
    ("/assets/{}.css", PUBLIC),
    # Writes ``model.default`` and ``model.provider`` into a named profile. Its
    # own docstring records that it mirrors ``POST /api/model/set``, which is
    # held at ``engine:select`` -- so letting the cluster rule resolve this to
    # ``profile:write`` would leave that scope bypassable by addressing the
    # same write through a different path. Read from the endpoint body, not
    # inferred, before the cluster rule was applied.
    ("/api/profiles/{}/model", ENGINE_SELECT),
    ("/api/tools/toolsets/{}/env", CREDENTIAL_WRITE),
    ("/api/tools/toolsets/{}/config", CREDENTIAL_READ),
    ("/api/tools/toolsets/{}/models", PROVIDER_READ),
    ("/api/tools/toolsets/{}/model", ENGINE_SELECT),
)

#: Roots that belong to the application rather than to the browser router. A
#: path under one of these is an endpoint, so falling through to the SPA
#: catch-all must never make it reachable.
_APPLICATION_ROOTS: Final[tuple[str, ...]] = (
    "/api", "/auth", "/docs", "/redoc", "/openapi.json",
    "/assets", "/dashboard-plugins",
)


def _spa_fallback(path: str) -> str | None:
    """What the SPA catch-all (``GET /{full_path:path}``) resolves to.

    The catch-all matches every path no other route claimed, which is how a
    single-page application serves its own client-side routes -- ``/settings``,
    ``/chat/abc`` -- and it has to answer before a session exists, because the
    login screen is one of them.

    That makes it the one rule that could quietly undo the flip: written as a
    pattern it would match everything, including a new ``/api`` endpoint nobody
    had classified, and the whole surface would be public again. So it is
    evaluated last, and only for paths outside the application's own roots. An
    unclassified ``/api`` route still resolves to ``None`` and is still
    refused -- which is what the endpoint itself does anyway, since
    ``serve_spa`` returns a real 404 for an unmatched ``/api`` path rather than
    the shell.
    """
    if any(path == root or path.startswith(root + "/") for root in _APPLICATION_ROOTS):
        return None
    return PUBLIC


def _compile(template: str) -> "re.Pattern[str]":
    """Anchor a normalised template so ``{}`` matches exactly one segment."""
    parts = [re.escape(p) for p in template.split("{}")]
    return re.compile("^" + "[^/]+".join(parts) + "$")


_PATTERNS: Final[tuple[tuple["re.Pattern[str]", str], ...]] = tuple(
    (_compile(template), scope) for template, scope in PATTERN_ROUTE_SCOPES
)

#: Methods that mutate. A mutating request to a read-scoped prefix is held to
#: the matching write scope, so `PUT /api/env` cannot be reached with
#: `credential:read` just because the prefix table lists the read scope.
WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Read scope → the write scope that supersedes it on a mutating request.
_WRITE_ESCALATION: Final[Mapping[str, str]] = {
    CREDENTIAL_READ: CREDENTIAL_WRITE,
    PROVIDER_READ: PROVIDER_WRITE,
    SESSION_READ: SESSION_WRITE,
    PROFILE_READ: PROFILE_WRITE,
    REPO_READ: REPO_WRITE,
    SKILL_READ: SKILL_WRITE,
    CONFIG_READ: CONFIG_WRITE,
    FS_READ: FS_WRITE,
    UI_READ: UI_WRITE,
    MEMORY_READ: MEMORY_WRITE,
}


def required_scope(path: str, method: str = "GET") -> str | None:
    """The scope needed for ``path``, or ``None`` if nothing classifies it.

    ``None`` no longer means "unguarded, carry on". It means no rule in this
    module describes this route, and :func:`authorize` treats that as a
    refusal. The tables are consulted most-specific first: an exact path, then
    a parameterised pattern, then the longest matching prefix, then the SPA
    catch-all for paths outside the application's own roots.

    A write method is held to the write half of a read/write pair, so a
    mutating request cannot be reached with the read scope just because the
    prefix table lists the read one.
    """
    scope = EXACT_ROUTE_SCOPES.get(path)
    if scope is None:
        for pattern, candidate in _PATTERNS:
            if pattern.match(path):
                scope = candidate
                break
    if scope is None:
        match = None
        for prefix, candidate in ROUTE_SCOPES:
            if path.startswith(prefix) and (
                match is None or len(prefix) > len(match[0])
            ):
                match = (prefix, candidate)
        if match is None:
            return _spa_fallback(path)
        scope = match[1]
    if scope in (PUBLIC, AUTHENTICATED):
        return scope
    if method.upper() in WRITE_METHODS:
        return _WRITE_ESCALATION.get(scope, scope)
    return scope


def authorize(principal: Principal, path: str, method: str = "GET") -> bool:
    """True if ``principal`` may issue ``method path``.

    Fail-closed. A route no table describes is refused, because the behaviour
    this replaces meant that adding an endpoint published it, and that
    forgetting to classify one was indistinguishable from deciding it was
    open. The cost is real and is the point: a new route is refused until
    somebody grants it a scope.
    """
    scope = required_scope(path, method)
    if scope is None:
        return False
    if scope == PUBLIC:
        return True
    if scope == AUTHENTICATED:
        return bool(principal.user_id)
    return principal.has(scope)
