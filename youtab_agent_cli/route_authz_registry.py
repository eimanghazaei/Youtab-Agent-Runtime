"""What authority each route actually carries, decided by reading its body.

This is the data half of the route authorization control. :mod:`youtab_agent_cli.authz`
answers "does this principal hold that scope"; this module answers "what does
*this route* require in the first place", for every route the application
really builds.

Why a registry rather than more prefix rules
--------------------------------------------
``authz.ROUTE_SCOPES`` guards by path prefix, which is the right shape for a
handful of deliberately-privileged clusters and the wrong shape for a surface
of 294 route+method pairs. A prefix rule cannot say that ``GET`` on a path is a
user's own data while ``DELETE`` on the same path is an administrative act, and
it silently covers routes nobody has looked at — the failure this registry
exists to make impossible. Every entry here names one path and one method, and
a route with no entry is not thereby permitted.

Classification comes from the endpoint body, never the prefix
-------------------------------------------------------------
The authority of a route is what it reads and what it mutates, which is a
property of the function, not of the URL it happens to be mounted at. Two
findings from the first cluster classified make the point: within
``/api/plugins/kanban`` — otherwise entirely a user's own board data —
``GET /model-options`` returns provider slugs and raw model identifiers, and
``GET /profiles`` returns the ``provider`` and ``model`` bound to every
installed profile. Both are the private engine catalogue a normal user must
never see, both sit under a path prefix that reads as harmless plugin data, and
both were reachable by anyone signed in. A prefix-derived or name-derived
classification would have marked them alongside their neighbours and shipped
the leak with a test asserting it was correct.

Capability is preserved, authority is restricted
------------------------------------------------
Restricting identity, tenant boundaries, secrets and infrastructure is the
point. Restricting what the Agent or its user may do inside their own
authorized workspace is not, and would be a regression dressed as a fix. Task
creation, decomposition, dispatch, worker termination, log reads, attachments
and achievement state are all classified :data:`RouteClass.USER_OWNED_RESOURCE`
and stay reachable: persistent user data is a product capability, not an
administrative feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Iterable, Mapping

from youtab_agent_cli.authz import (
    CREDENTIAL_READ,
    PROFILE_READ,
    CREDENTIAL_WRITE,
    DEPLOYMENT_MANAGE,
    ENGINE_SELECT,
    EVENTS_READ,
    PROVIDER_READ,
    PROVIDER_WRITE,
)


class RouteClass(StrEnum):
    """The authority a route carries.

    Values are stable strings: they are written into the registry, compared by
    the Router-vs-policy gate and quoted in reports, so renaming one is a data
    migration rather than a refactor.
    """

    #: Reachable with no credential at all. Requires written justification.
    PUBLIC = "public"
    #: Liveness/readiness for a local supervisor or probe. Requires written
    #: justification, because "it is only a health check" is exactly how a
    #: surface that leaks configuration gets waved through.
    LOOPBACK_HEALTH = "loopback-health"
    #: Any authenticated principal. Carries no per-user or per-tenant data.
    AUTHENTICATED_USER = "authenticated-user"
    #: The caller's own data — their board, tasks, runs, files, memory.
    USER_OWNED_RESOURCE = "user-owned-resource"
    #: Data belonging to the caller's tenant, readable by its members.
    TENANT_SCOPED_USER = "tenant-scoped-user"
    #: Administers one tenant: its users, roles, usage, billing, audit.
    TENANT_ADMIN = "tenant-admin"
    #: A Youtab-internal operator holding one named scope, not a blanket grant.
    SCOPED_INTERNAL_OPERATOR = "scoped-internal-operator"
    #: Youtab's own provider bindings, credentials, engines, infrastructure.
    OWNER_SUPERADMIN = "owner-superadmin"
    #: A machine caller proving itself with its own credential, not a session.
    INTERNAL_SERVICE = "internal-service"
    #: A static mount or file-serving contract rather than an API decision.
    STATIC_MOUNT = "static-mount"


#: Classes whose entries must carry an individual written justification. A
#: blanket "these are all fine" is not reviewable, and these two classes are
#: the ones where being wrong means an unauthenticated reader.
_JUSTIFIED_CLASSES: Final[frozenset[RouteClass]] = frozenset({
    RouteClass.PUBLIC, RouteClass.LOOPBACK_HEALTH,
})

#: Methods a registry entry may name. ``WEBSOCKET`` and ``MOUNT`` are route
#: kinds rather than HTTP verbs, and are spelled here so a typo cannot quietly
#: create an entry that matches nothing. ``HEAD`` and ``OPTIONS`` are absent on
#: purpose: Starlette synthesises them and they carry no separate authority, so
#: the inventory excludes them and an entry naming one would be permanently
#: stale.
KNOWN_METHODS: Final[frozenset[str]] = frozenset({
    "GET", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET", "MOUNT",
})


class RegistryError(ValueError):
    """A registry entry is malformed, duplicated or unjustified."""


@dataclass(frozen=True)
class RouteEntry:
    """One route+method and the authority it carries.

    ``path`` is the *normalised* form the inventory collector produces —
    dynamic segments collapsed to ``{}`` — so renaming a path parameter is not
    mistaken for adding one route and removing another.
    """

    path: str
    method: str
    route_class: RouteClass
    #: The scope :mod:`authz` must require, where the class maps to one.
    #: ``None`` means the class is satisfied by authentication alone.
    scope: str | None = None
    #: Why this route carries this authority. Mandatory for the classes in
    #: :data:`_JUSTIFIED_CLASSES`; recommended everywhere it is not obvious.
    justification: str = ""
    #: Set when the route only exists in some build states, with the reason.
    #: The gate treats a conditional entry as satisfied whether or not the
    #: router currently exposes it.
    conditional: str = ""

    def __post_init__(self) -> None:
        if self.method not in KNOWN_METHODS:
            raise RegistryError(
                f"{self.path}: unknown method {self.method!r}; "
                f"expected one of {sorted(KNOWN_METHODS)}"
            )
        if not self.path.startswith("/"):
            raise RegistryError(f"{self.path!r}: path must be absolute")
        if self.route_class in _JUSTIFIED_CLASSES and not self.justification.strip():
            raise RegistryError(
                f"{self.method} {self.path}: {self.route_class} requires an "
                "individual written justification"
            )


def _entries(
    route_class: RouteClass,
    spec: Iterable[tuple[str, str]],
    *,
    scope: str | None = None,
    justification: str = "",
) -> tuple[RouteEntry, ...]:
    """Expand ``(path, method)`` pairs that genuinely share one authority.

    Only used where the shared class was established by reading each endpoint,
    never as a way to cover a cluster without opening it.
    """
    return tuple(
        RouteEntry(path, method, route_class, scope=scope,
                   justification=justification)
        for path, method in spec
    )


# ---------------------------------------------------------------------------
# Public surface. Seven paths, each justified individually.
#
# These are the entries of ``dashboard_auth.public_paths.PUBLIC_API_PATHS``,
# which is the list both auth middlewares actually consult. Their justification
# is reproduced here rather than referenced, because a reviewer reading the
# registry must be able to judge the decision without opening another file.
# ---------------------------------------------------------------------------
_PUBLIC: Final[tuple[RouteEntry, ...]] = (
    RouteEntry(
        "/api/health", "GET", RouteClass.LOOPBACK_HEALTH,
        justification=(
            "Minimal process-liveness probe for the desktop/backend boot "
            "handshake. Deliberately avoids gateway config, platform "
            "discovery, MCP setup and host-local detail, so a readiness check "
            "cannot spend its budget inside cold plugin imports and cannot "
            "return anything host-identifying."
        ),
    ),
    RouteEntry(
        "/api/status", "GET", RouteClass.PUBLIC,
        justification=(
            "The portal's wildcard liveness probe, fetched without a cookie "
            "as its sole signal that an agent dashboard is alive; gating it "
            "surfaced every healthy agent as down. Returns version, gateway "
            "state, active session count and the auth-gate shape — no bodies, "
            "no session content, no secrets."
        ),
    ),
    RouteEntry(
        "/api/config/defaults", "GET", RouteClass.PUBLIC,
        justification=(
            "Read-only default values the SPA's Config page renders before "
            "the user has logged in. Ships the shipped defaults, not the "
            "deployment's configured values, so it discloses nothing about "
            "this installation."
        ),
    ),
    RouteEntry(
        "/api/config/schema", "GET", RouteClass.PUBLIC,
        justification=(
            "Read-only form schema for the same pre-login Config page. Field "
            "names and types only — the shape of the form, never its contents."
        ),
    ),
    RouteEntry(
        "/api/dashboard/themes", "GET", RouteClass.PUBLIC,
        justification=(
            "Theme manifests for the dashboard skin engine, needed to render "
            "the login screen itself. Presentation assets only."
        ),
    ),
    RouteEntry(
        "/api/dashboard/plugins", "GET", RouteClass.PUBLIC,
        justification=(
            "Plugin manifests the skin engine needs before login to know "
            "which dashboard tabs exist. Names and mount points only, no "
            "plugin data."
        ),
    ),
    RouteEntry(
        "/api/cron/fire", "POST", RouteClass.INTERNAL_SERVICE,
        justification=(
            "Chronos managed-cron fire webhook, NAS to agent. On the public "
            "allowlist so the bearer-only callback reaches its verifier "
            "instead of a 401 no_cookie, but it is not unauthenticated: it "
            "carries a short-lived NAS-minted JWT (purpose=cron_fire) that "
            "the handler verifies. The JWT, not the allowlist, is the "
            "security boundary."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Owner/Superadmin surface already carrying scopes in ``authz.ROUTE_SCOPES``.
# Restated per route+method so the registry is complete on its own terms and
# the gate can prove the two agree rather than assuming it.
# ---------------------------------------------------------------------------
_PRIVILEGED: Final[tuple[RouteEntry, ...]] = (
    *_entries(RouteClass.OWNER_SUPERADMIN, (
        ("/api/credentials/pool", "GET"),
        ("/api/credentials/pool", "POST"),
        ("/api/credentials/pool/{}/{}", "DELETE"),
        ("/api/env", "DELETE"),
        ("/api/env", "PUT"),
        ("/api/env/reveal", "POST"),
    ), scope=CREDENTIAL_WRITE),
    RouteEntry("/api/env", "GET", RouteClass.OWNER_SUPERADMIN,
               scope=CREDENTIAL_READ),
    *_entries(RouteClass.OWNER_SUPERADMIN, (
        ("/api/gateway/jobs/{}", "GET"),
        ("/api/gateway/restart", "POST"),
        ("/api/gateway/start", "POST"),
        ("/api/gateway/stop", "POST"),
    ), scope=DEPLOYMENT_MANAGE),
    *_entries(RouteClass.OWNER_SUPERADMIN, (
        ("/api/model/auxiliary", "GET"),
        ("/api/model/info", "GET"),
        ("/api/model/options", "GET"),
        ("/api/model/recommended-default", "GET"),
        ("/api/providers/custom-endpoints", "GET"),
        ("/api/providers/oauth", "GET"),
        ("/api/providers/oauth/{}/poll/{}", "GET"),
    ), scope=PROVIDER_READ),
    *_entries(RouteClass.OWNER_SUPERADMIN, (
        ("/api/providers/custom-endpoints", "POST"),
        ("/api/providers/custom-endpoints/validate", "POST"),
        ("/api/providers/custom-endpoints/{}", "DELETE"),
        ("/api/providers/custom-endpoints/{}/activate", "POST"),
        ("/api/providers/oauth/sessions/{}", "DELETE"),
        ("/api/providers/oauth/{}", "DELETE"),
        ("/api/providers/oauth/{}/start", "POST"),
        ("/api/providers/oauth/{}/submit", "POST"),
        ("/api/providers/validate", "POST"),
    ), scope=PROVIDER_WRITE),
    *_entries(RouteClass.OWNER_SUPERADMIN, (
        ("/api/model/moa", "GET"),
        ("/api/model/moa", "PUT"),
        ("/api/model/set", "POST"),
    ), scope=ENGINE_SELECT),
    RouteEntry("/api/events", "WEBSOCKET", RouteClass.AUTHENTICATED_USER,
               scope=EVENTS_READ,
               justification=(
                   "Enforced at the socket by ``events_ws``: Starlette's HTTP "
                   "middleware never runs on a WebSocket upgrade, so an "
                   "HTTP-path rule alone would not cover it."
               )),
)


# ---------------------------------------------------------------------------
# /api/plugins — 47 HTTP route+method pairs and one WebSocket, every one read.
#
# The cluster is a user's own Kanban board and achievement state, held in
# per-installation SQLite and files under the Youtab home. It carries real
# capability — creating and decomposing tasks, dispatching and terminating
# workers, reading run logs, uploading attachments — and that capability is
# kept: it is the product, and it operates inside the caller's own workspace.
#
# Two routes in it are not that, and are pulled out below.
# ---------------------------------------------------------------------------
_PLUGIN_PRIVILEGED: Final[tuple[RouteEntry, ...]] = (
    RouteEntry(
        "/api/plugins/kanban/model-options", "GET", RouteClass.OWNER_SUPERADMIN,
        scope=PROVIDER_READ,
        justification=(
            "Returns authenticated provider slugs and their model lists via "
            "``inventory.build_models_payload`` — the private engine "
            "catalogue, the same data ``/api/model/options`` is already held "
            "at ``provider:read`` for. Its path prefix reads as ordinary "
            "plugin data; its body does not."
        ),
    ),
    RouteEntry(
        "/api/plugins/kanban/profiles", "GET", RouteClass.OWNER_SUPERADMIN,
        scope=PROVIDER_READ,
        justification=(
            "Returns ``provider`` and ``model`` for every installed profile, "
            "which is the raw engine binding a normal user must never see. "
            "The description-editing routes beside it stay user-owned; only "
            "this roster read discloses the bindings."
        ),
    ),
)

_PLUGIN_USER_OWNED: Final[tuple[RouteEntry, ...]] = _entries(
    RouteClass.USER_OWNED_RESOURCE,
    (
        # Board and board-collection management.
        ("/api/plugins/kanban/board", "GET"),
        ("/api/plugins/kanban/boards", "GET"),
        ("/api/plugins/kanban/boards", "POST"),
        ("/api/plugins/kanban/boards/{}", "DELETE"),
        ("/api/plugins/kanban/boards/{}", "PATCH"),
        ("/api/plugins/kanban/boards/{}/switch", "POST"),
        # Tasks: the core product capability.
        ("/api/plugins/kanban/tasks", "POST"),
        ("/api/plugins/kanban/tasks/bulk", "POST"),
        ("/api/plugins/kanban/tasks/{}", "DELETE"),
        ("/api/plugins/kanban/tasks/{}", "GET"),
        ("/api/plugins/kanban/tasks/{}", "PATCH"),
        ("/api/plugins/kanban/tasks/{}/comments", "POST"),
        ("/api/plugins/kanban/tasks/{}/log", "GET"),
        # Agent capability: decompose, specify, reassign, reclaim.
        ("/api/plugins/kanban/tasks/{}/decompose", "POST"),
        ("/api/plugins/kanban/tasks/{}/specify", "POST"),
        ("/api/plugins/kanban/tasks/{}/reassign", "POST"),
        ("/api/plugins/kanban/tasks/{}/reclaim", "POST"),
        # Attachments: the caller's own files, stored under the board root.
        ("/api/plugins/kanban/attachments/{}", "DELETE"),
        ("/api/plugins/kanban/attachments/{}", "GET"),
        ("/api/plugins/kanban/tasks/{}/attachments", "GET"),
        ("/api/plugins/kanban/tasks/{}/attachments", "POST"),
        # Task graph.
        ("/api/plugins/kanban/links", "DELETE"),
        ("/api/plugins/kanban/links", "POST"),
        # Notification routing for the caller's own tasks.
        ("/api/plugins/kanban/home-channels", "GET"),
        ("/api/plugins/kanban/tasks/{}/home-subscribe/{}", "DELETE"),
        ("/api/plugins/kanban/tasks/{}/home-subscribe/{}", "POST"),
        # Runs and workers the caller's own tasks spawned.
        ("/api/plugins/kanban/dispatch", "POST"),
        ("/api/plugins/kanban/runs/{}", "GET"),
        ("/api/plugins/kanban/runs/{}/inspect", "GET"),
        ("/api/plugins/kanban/runs/{}/terminate", "POST"),
        ("/api/plugins/kanban/workers/active", "GET"),
        # Board-derived views.
        ("/api/plugins/kanban/assignees", "GET"),
        ("/api/plugins/kanban/diagnostics", "GET"),
        ("/api/plugins/kanban/stats", "GET"),
        # The caller's own kanban preferences and orchestration knobs. Both
        # read and write only the ``dashboard.kanban`` / ``kanban`` sections of
        # the user's own config, and validate profile names rather than
        # returning their bindings.
        ("/api/plugins/kanban/config", "GET"),
        ("/api/plugins/kanban/orchestration", "GET"),
        ("/api/plugins/kanban/orchestration", "PUT"),
        # Profile descriptions are user-authored prose, not engine bindings.
        ("/api/plugins/kanban/profiles/{}", "PATCH"),
        ("/api/plugins/kanban/profiles/{}/describe-auto", "POST"),
        # Achievements: the caller's own gamification state.
        ("/api/plugins/youtab-achievements/achievements", "GET"),
        ("/api/plugins/youtab-achievements/recent-unlocks", "GET"),
        ("/api/plugins/youtab-achievements/rescan", "POST"),
        ("/api/plugins/youtab-achievements/reset-state", "POST"),
        ("/api/plugins/youtab-achievements/scan-status", "GET"),
        ("/api/plugins/youtab-achievements/sessions/{}/badges", "GET"),
    ),
)

_PLUGIN_SOCKETS: Final[tuple[RouteEntry, ...]] = (
    RouteEntry(
        "/api/plugins/kanban/events", "WEBSOCKET",
        RouteClass.USER_OWNED_RESOURCE,
        justification=(
            "Streams the caller's own board changes. Already gated at the "
            "socket by ``_ws_upgrade_authorized``, which is where it has to "
            "be — HTTP middleware does not run on a WebSocket upgrade."
        ),
    ),
)


# ---------------------------------------------------------------------------
# /api/profiles — 15 route+method pairs, every one read.
#
# Profiles are the user's own agents: their persona (SOUL.md), description,
# skills, sessions and lifecycle. That is product capability and stays
# user-owned. Two routes in the cluster are not that.
# ---------------------------------------------------------------------------
_PROFILE_PRIVILEGED: Final[tuple[RouteEntry, ...]] = (
    RouteEntry(
        "/api/profiles", "GET", RouteClass.USER_OWNED_RESOURCE,
        scope=PROFILE_READ,
        justification=(
            "KNOWN OPEN DISCLOSURE, deliberately not closed with a scope. "
            "``_profile_to_dict`` returns ``model`` and ``provider`` — the raw "
            "engine binding — for every profile, alongside the profile's "
            "absolute path on disk and ``has_env``, which reveals whether "
            "credentials are configured. But this is the route the profile "
            "picker lists from, so holding it at ``provider:read`` would take "
            "the picker away from every ordinary user. The disclosure is in "
            "the payload, so the fix belongs in the payload: mask ``model``, "
            "``provider``, ``path`` and ``has_env`` for a caller without "
            "``provider:read``, and leave the route at ``profile:read``. "
            "Tracked, not forgotten."
        ),
    ),
    RouteEntry(
        "/api/profiles/{}/model", "PUT", RouteClass.OWNER_SUPERADMIN,
        scope=ENGINE_SELECT,
        justification=(
            "Writes ``model.default`` and ``model.provider`` into a named "
            "profile's config. Its own docstring records that it mirrors "
            "``POST /api/model/set``, which is held at ``engine:select`` — so "
            "leaving it unmapped is not merely an unclassified route, it is a "
            "working bypass of that scope by way of a different path."
        ),
    ),
)

_PROFILE_USER_OWNED: Final[tuple[RouteEntry, ...]] = _entries(
    RouteClass.USER_OWNED_RESOURCE,
    (
        # Profile lifecycle, all within the caller's own installation.
        ("/api/profiles", "POST"),
        ("/api/profiles/{}", "DELETE"),
        ("/api/profiles/{}", "PATCH"),
        ("/api/profiles/active", "GET"),
        ("/api/profiles/active", "POST"),
        # The agent's persona and memory. User-scoped product capability, not
        # an administrative feature, and deliberately not restricted.
        ("/api/profiles/{}/soul", "GET"),
        ("/api/profiles/{}/soul", "PUT"),
        # User-authored prose and its auxiliary-LLM generator.
        ("/api/profiles/{}/description", "PUT"),
        ("/api/profiles/{}/describe-auto", "POST"),
        # The caller's own sessions, aggregated across their own profiles.
        ("/api/profiles/sessions", "GET"),
        ("/api/profiles/sessions/sidebar", "GET"),
        # Local desktop affordance. ``_profile_setup_command`` returns a
        # fixed-form string ("youtab setup" / "<name> setup") for a profile
        # name that ``_resolve_profile_dir`` has already resolved to an
        # existing directory, so neither route carries arbitrary-command
        # authority.
        ("/api/profiles/{}/setup-command", "GET"),
        ("/api/profiles/{}/open-terminal", "POST"),
    ),
)


#: Every classified route. Assembled from the clusters above rather than
#: written as one flat literal, so a cluster can be reviewed on its own and a
#: partially-classified surface is visible as such.
REGISTRY: Final[tuple[RouteEntry, ...]] = (
    *_PUBLIC,
    *_PRIVILEGED,
    *_PLUGIN_PRIVILEGED,
    *_PLUGIN_USER_OWNED,
    *_PLUGIN_SOCKETS,
    *_PROFILE_PRIVILEGED,
    *_PROFILE_USER_OWNED,
)


def _build_index(entries: Iterable[RouteEntry]) -> dict[tuple[str, str], RouteEntry]:
    """Index by ``(path, method)``, refusing duplicates.

    A duplicate is not merged and not last-one-wins: two entries for one
    decision is how a registry grows a contradiction that reads as coverage.
    """
    index: dict[tuple[str, str], RouteEntry] = {}
    for entry in entries:
        key = (entry.path, entry.method)
        if key in index:
            first, second = index[key], entry
            raise RegistryError(
                f"duplicate entry for {entry.method} {entry.path}: "
                f"{first.route_class} and {second.route_class}"
            )
        index[key] = entry
    return index


#: Built at import so a malformed registry fails the process that loads it
#: rather than the first request that happens to hit the bad entry.
INDEX: Final[Mapping[tuple[str, str], RouteEntry]] = _build_index(REGISTRY)


def classify(path: str, method: str) -> RouteEntry | None:
    """The entry governing ``method path``, or ``None`` if it has none.

    ``None`` is not a grant and must never be read as one — it means this
    route has not been classified, which under default-deny is a refusal.
    """
    return INDEX.get((path, method.upper()))


def registry_scope(path: str, method: str) -> str | None:
    """The scope the registry requires, or ``None`` for classes without one."""
    entry = classify(path, method)
    return entry.scope if entry is not None else None


def coverage(inventory_http: Iterable[Iterable[str]],
             inventory_ws: Iterable[str],
             inventory_mounts: Iterable[str]) -> dict[str, list[str]]:
    """Compare the registry against a collected inventory.

    Returns the four disagreements the gate fails on, as sorted lists so a
    failure message names the routes rather than a count.
    """
    router: set[tuple[str, str]] = {
        (path, method) for path, method in (tuple(p) for p in inventory_http)
    }
    router |= {(p, "WEBSOCKET") for p in inventory_ws}
    router |= {(p, "MOUNT") for p in inventory_mounts}
    registered = set(INDEX)
    conditional = {k for k, e in INDEX.items() if e.conditional}
    return {
        "unclassified": sorted(f"{m} {p}" for p, m in router - registered),
        "stale": sorted(f"{m} {p}" for p, m in (registered - router) - conditional),
        "classified": sorted(f"{m} {p}" for p, m in router & registered),
    }
