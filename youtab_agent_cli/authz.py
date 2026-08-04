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

#: What each role may do. Written out per role rather than inherited, so the
#: Tenant Admin row can be read on its own and confirmed to contain no
#: provider, credential or engine scope at all.
ROLE_SCOPES: Final[Mapping[Role, frozenset[str]]] = {
    Role.NORMAL_USER: frozenset(),
    Role.TENANT_ADMIN: frozenset({TENANT_MANAGE_OWN}),
    # An operator holds nothing implicitly. Their scopes come from the roster
    # entry, one capability at a time — "explicitly scoped" is the whole point
    # of the role, so a blanket grant here would defeat it.
    Role.YOUTAB_OPERATOR: frozenset(),
    Role.YOUTAB_SUPERADMIN: frozenset({
        PROVIDER_READ, PROVIDER_WRITE, CREDENTIAL_READ, CREDENTIAL_WRITE,
        ENGINE_SELECT, TENANT_MANAGE_ANY, DEPLOYMENT_MANAGE,
    }),
    Role.YOUTAB_OWNER: frozenset({
        PROVIDER_READ, PROVIDER_WRITE, CREDENTIAL_READ, CREDENTIAL_WRITE,
        ENGINE_SELECT, TENANT_MANAGE_ANY, DEPLOYMENT_MANAGE,
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
    if not isinstance(entry, dict):
        return Principal(user_id=user_id, org_id=org_id)

    try:
        role = Role(entry.get("role", Role.NORMAL_USER))
    except ValueError:
        # An unrecognised role name is a typo or a downgrade of this binary
        # against a newer roster. Either way the safe reading is "no
        # authority", never "some authority we cannot name".
        return Principal(user_id=user_id, org_id=org_id)

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
)

#: Methods that mutate. A mutating request to a read-scoped prefix is held to
#: the matching write scope, so `PUT /api/env` cannot be reached with
#: `credential:read` just because the prefix table lists the read scope.
WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Read scope → the write scope that supersedes it on a mutating request.
_WRITE_ESCALATION: Final[Mapping[str, str]] = {
    CREDENTIAL_READ: CREDENTIAL_WRITE,
    PROVIDER_READ: PROVIDER_WRITE,
}


def required_scope(path: str, method: str = "GET") -> str | None:
    """The scope needed for ``path``, or ``None`` if it is not a guarded route.

    ``None`` means "this table does not guard this path" — it is not a grant.
    Callers apply it to the guarded surface only; everything else keeps
    whatever authentication it already had.
    """
    match = None
    for prefix, scope in ROUTE_SCOPES:
        if path.startswith(prefix) and (match is None or len(prefix) > len(match[0])):
            match = (prefix, scope)
    if match is None:
        return None
    scope = match[1]
    if method.upper() in WRITE_METHODS:
        return _WRITE_ESCALATION.get(scope, scope)
    return scope


def authorize(principal: Principal, path: str, method: str = "GET") -> bool:
    """True if ``principal`` may issue ``method path``.

    Unguarded paths return True: this function answers only the question the
    route table poses, and an unguarded path is not this module's to refuse.
    """
    scope = required_scope(path, method)
    if scope is None:
        return True
    return principal.has(scope)
