"""Normal user, Tenant Admin and Youtab internal admin are separated server-side.

The earlier version of this file tested a deployment-wide switch, which could
only ever answer "this whole instance offers credential management, or it does
not". That shape could not express the actual requirement: the Owner keeps
every administrative capability, and a customer on the same instance reaches
none of it. So these tests drive the real gated path — a verified session
cookie, a roster, and a scope check — rather than an environment variable.

Every request here is authenticated. A test that showed an anonymous caller
being turned away would prove nothing about authorization: the auth gate
already does that, and every assertion below would still pass with the
authorization gate deleted. What is being pinned is that a caller who logs in
perfectly well is still refused, because of who they are.

Three populations appear, and the middle one is the point. A Tenant Admin is a
real administrator with real authority over their own company, and they are
refused the provider and engine surface exactly as firmly as an ordinary user
is. If a future change models authority as one ascending ladder, the Tenant
Admin cases below fail first.

No upstream provider or model is named anywhere in this file, including in
comments — the reachability matrix reads source text, so naming one in order
to forbid it is itself a finding.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from youtab_agent_cli import web_server
from youtab_agent_cli.authz import (
    CREDENTIAL_WRITE,
    PROVIDER_READ,
    Principal,
    ROLE_SCOPES,
    Role,
    ROSTER_ENV,
    authorize,
    required_scope,
    resolve_principal,
)
from youtab_agent_cli.dashboard_auth import clear_providers, register_provider
from youtab_agent_cli.dashboard_auth.base import DashboardAuthProvider, Session

# The gated bind terminates TLS, so the cookie resolves to its __Host- variant.
SESSION_COOKIE = "__Host-youtab_session_at"

OWNER = "owner-1"
SUPERADMIN = "superadmin-1"
OPERATOR_BARE = "operator-bare"
OPERATOR_SCOPED = "operator-scoped"
TENANT_ADMIN = "tenant-admin-1"
NORMAL = "normal-1"

ACME = "org-acme"
RIVAL = "org-rival"

#: Who holds what. Everyone absent from this document is a normal user with no
#: scopes, which is the case that needs no entry: `normal-1` is not here.
ROSTER = {
    OWNER: {"role": Role.YOUTAB_OWNER.value},
    SUPERADMIN: {"role": Role.YOUTAB_SUPERADMIN.value},
    # An operator with no explicit grant. Present in the roster, and still
    # holds nothing — the role by itself is not authority.
    OPERATOR_BARE: {"role": Role.YOUTAB_OPERATOR.value},
    OPERATOR_SCOPED: {
        "role": Role.YOUTAB_OPERATOR.value,
        "scopes": [PROVIDER_READ],
    },
    TENANT_ADMIN: {"role": Role.TENANT_ADMIN.value},
}

#: user id → org, so a session can be minted for either tenant.
ORGS = {
    OWNER: "", SUPERADMIN: "", OPERATOR_BARE: "", OPERATOR_SCOPED: "",
    TENANT_ADMIN: ACME, NORMAL: ACME,
}


class _IdentityProvider(DashboardAuthProvider):
    """Turns an opaque token straight into the identity it names.

    Deliberately trivial. These tests are about what happens *after* a session
    is verified, and a real signing round trip here would only add a way for
    them to fail for reasons that have nothing to do with authorization.
    """

    name = "identity-stub"
    display_name = "Identity Stub"

    def start_login(self, *, redirect_uri: str):  # pragma: no cover - unused
        raise NotImplementedError

    def complete_login(self, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    def verify_session(self, *, access_token: str):
        if access_token not in ORGS:
            return None
        return Session(
            user_id=access_token,
            email=f"{access_token}@example.test",
            display_name=access_token,
            org_id=ORGS[access_token],
            provider=self.name,
            expires_at=int(time.time()) + 3600,
            access_token=access_token,
            refresh_token="",
        )

    def refresh_session(self, *, refresh_token: str):  # pragma: no cover
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


@pytest.fixture
def gated(monkeypatch):
    """A hosted, gated dashboard — what agent.youtab.io is — plus the roster."""
    monkeypatch.setenv(ROSTER_ENV, json.dumps(ROSTER))
    clear_providers()
    register_provider(_IdentityProvider())
    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "agent.example.test"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://agent.example.test")
    clear_providers()
    (
        web_server.app.state.bound_host,
        web_server.app.state.bound_port,
        web_server.app.state.auth_required,
    ) = prev


def _as(client, user_id):
    client.cookies.set(SESSION_COOKIE, user_id)
    return client


#: The privileged surface, grouped by the requirement each group belongs to.
CATALOGUE = ["/api/providers/oauth", "/api/providers/custom-endpoints", "/api/model/options"]
RAW_METADATA = ["/api/env", "/api/model/info", "/api/model/auxiliary"]
CREDENTIAL_SUBMIT = [
    ("put", "/api/env", {"key": "EXAMPLE_UPSTREAM_API_KEY", "value": "x"}),
    ("post", "/api/env/reveal", {"key": "EXAMPLE_UPSTREAM_API_KEY"}),
    ("post", "/api/providers/validate", {"provider": "p", "api_key": "x"}),
]
RAW_ENGINE = [
    ("post", "/api/model/set", {"scope": "main", "provider": "p", "model": "m"}),
    ("put", "/api/model/moa", {"enabled": True, "models": ["m"]}),
]


# --- Normal user ------------------------------------------------------------

@pytest.mark.parametrize("path", CATALOGUE)
def test_normal_user_cannot_discover_a_provider_catalogue(gated, path):
    assert _as(gated, NORMAL).get(path).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), CREDENTIAL_SUBMIT)
def test_normal_user_cannot_submit_provider_credentials(gated, method, path, body):
    assert getattr(_as(gated, NORMAL), method)(path, json=body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), RAW_ENGINE)
def test_normal_user_cannot_select_raw_models(gated, method, path, body):
    assert getattr(_as(gated, NORMAL), method)(path, json=body).status_code == 403


@pytest.mark.parametrize("path", RAW_METADATA)
def test_normal_user_cannot_access_models_or_keys_endpoints(gated, path):
    assert _as(gated, NORMAL).get(path).status_code == 403


# --- Tenant Admin -----------------------------------------------------------

@pytest.mark.parametrize("path", CATALOGUE + RAW_METADATA)
def test_tenant_admin_cannot_access_provider_or_binding_endpoints(gated, path):
    """A customer administrator is not a junior Youtab administrator.

    This is the case a single ascending privilege ladder gets wrong.
    """
    assert _as(gated, TENANT_ADMIN).get(path).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), CREDENTIAL_SUBMIT + RAW_ENGINE)
def test_tenant_admin_cannot_write_provider_state(gated, method, path, body):
    assert getattr(_as(gated, TENANT_ADMIN), method)(path, json=body).status_code == 403


def test_tenant_admin_is_confined_to_their_own_tenant():
    """Authority over one company grants nothing over another.

    Asserted on the principal rather than through a route because tenant-scoped
    routes are not in this service yet; pinning it here means the rule is
    already true when the first one lands.
    """
    admin = resolve_principal(user_id=TENANT_ADMIN, org_id=ACME, roster=ROSTER)
    assert admin.org_id == ACME
    assert admin.org_id != RIVAL
    assert not admin.is_internal
    # The scope that would let anyone read across tenants is Youtab-internal
    # and no tenant role holds it.
    assert "tenant:manage:any" not in admin.scopes


# --- Youtab internal --------------------------------------------------------

def test_a_youtab_operator_holds_nothing_without_an_explicit_scope(gated):
    """Being internal staff is not itself authority."""
    assert _as(gated, OPERATOR_BARE).get("/api/providers/oauth").status_code == 403


def test_a_scoped_operator_reaches_exactly_what_was_granted(gated):
    scoped = _as(gated, OPERATOR_SCOPED)
    assert scoped.get("/api/providers/oauth").status_code != 403
    # Granted the catalogue read, never credential write.
    assert scoped.put(
        "/api/env", json={"key": "EXAMPLE_UPSTREAM_API_KEY", "value": "x"}
    ).status_code == 403


@pytest.mark.parametrize("who", [OWNER, SUPERADMIN])
@pytest.mark.parametrize("path", CATALOGUE + RAW_METADATA)
def test_owner_and_superadmin_keep_the_full_surface(gated, who, path):
    """The capability is separated, not removed. This is the half that proves it."""
    assert _as(gated, who).get(path).status_code != 403


@pytest.mark.parametrize("who", [OWNER, SUPERADMIN])
def test_only_owner_and_superadmin_may_manage_credentials(gated, who):
    assert _as(gated, who).put(
        "/api/env", json={"key": "EXAMPLE_UPSTREAM_API_KEY", "value": "x"}
    ).status_code != 403


# --- Unauthenticated and reporting -----------------------------------------

def test_an_unauthenticated_caller_is_refused(gated):
    assert gated.get("/api/providers/oauth").status_code in (401, 403)


def test_the_capability_report_is_answered_per_caller(gated):
    """One instance, different answers — because for a customer it is absent."""
    normal = _as(gated, NORMAL).get("/api/dashboard/capabilities").json()
    assert normal["credential_surface"] is False
    assert normal["provider_catalogue"] is False
    assert normal["role"] == Role.NORMAL_USER.value

    owner = _as(gated, OWNER).get("/api/dashboard/capabilities").json()
    assert owner["credential_surface"] is True
    assert owner["role"] == Role.YOUTAB_OWNER.value


def test_the_capability_report_is_reachable_by_a_normal_user():
    """Asking whether a surface exists must not require the surface.

    If this route required a privileged scope, a normal user could not
    distinguish "absent" from "refused" and the dashboard would have no way to
    render correctly in either case.

    It used to be enough to assert this route had no entry at all. That is no
    longer the same statement: an unclassified route is now a refusal, so
    "unguarded" would mean "unreachable by anyone". The property held here is
    the one that always mattered -- an ordinary user can ask.
    """
    scope = required_scope("/api/dashboard/capabilities", "GET")
    assert scope is not None, "an unclassified route is now refused, not open"
    assert scope not in PRIVILEGED
    normal = resolve_principal(user_id="nobody", org_id=ACME, roster=ROSTER)
    assert authorize(normal, "/api/dashboard/capabilities", "GET")


def _audit_lines(home):
    log = home / "logs" / "dashboard-auth.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_privileged_decisions_generate_audit_events(gated, tmp_path, monkeypatch):
    """Both outcomes are recorded, and neither record carries a secret.

    A trail holding only refusals cannot answer who actually read a credential
    slot or changed a binding, which is the question an incident starts from.
    """
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))

    _as(gated, NORMAL).get("/api/providers/oauth")
    _as(gated, OWNER).get("/api/providers/oauth")

    events = _audit_lines(tmp_path)
    denied = [e for e in events if e["event"] == "privileged_access_denied"]
    granted = [e for e in events if e["event"] == "privileged_access_granted"]

    assert denied, "a refusal on the privileged surface left no audit record"
    assert granted, "an authorized privileged read left no audit record"
    assert denied[-1]["user_id"] == NORMAL
    assert denied[-1]["role"] == Role.NORMAL_USER.value
    assert denied[-1]["scope"] == PROVIDER_READ
    assert granted[-1]["user_id"] == OWNER
    assert granted[-1]["role"] == Role.YOUTAB_OWNER.value

    # The audit trail must never become the place a credential ends up.
    blob = json.dumps(events)
    for forbidden in ("access_token", "api_key", "password", "cookie"):
        assert forbidden not in blob


def test_a_credential_write_is_audited_without_its_payload(gated, tmp_path, monkeypatch):
    """The value being written must not reach the log with the event."""
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(tmp_path))
    secret = "sk-" + "z" * 24

    _as(gated, NORMAL).put(
        "/api/env", json={"key": "EXAMPLE_UPSTREAM_API_KEY", "value": secret}
    )

    events = _audit_lines(tmp_path)
    assert any(e["event"] == "privileged_access_denied" for e in events)
    assert any(e["scope"] == CREDENTIAL_WRITE for e in events), (
        "a mutating credential request must be audited against the write scope"
    )
    assert secret not in json.dumps(events), "the submitted value reached the audit log"


def test_an_unguarded_route_is_not_refused_for_a_normal_user(gated):
    """Positive control.

    Without it, a gate that refused every request would satisfy every negative
    assertion above and read as correct separation.
    """
    assert _as(gated, NORMAL).get("/api/sessions").status_code != 403


#: Everything a customer must never hold, at any privilege level of theirs.
PRIVILEGED = frozenset({
    "provider:read", "provider:write", "credential:read", "credential:write",
    "engine:select", "tenant:manage:any", "deployment:manage", "events:read",
    "ops:manage",
})


# --- The policy itself ------------------------------------------------------

def test_an_identity_absent_from_the_roster_holds_no_privileged_authority():
    """The roster grants elevation; it is not what makes someone a user.

    An unrostered identity resolves to the ordinary user baseline -- the
    capabilities they had over their own installation before authorization was
    default-deny, when every unguarded route was reachable. What the roster's
    absence must never produce is a *privileged* scope, and it does not.
    """
    stranger = resolve_principal(user_id="nobody", org_id=ACME, roster=ROSTER)
    assert stranger.role is Role.NORMAL_USER
    assert stranger.scopes == ROLE_SCOPES[Role.NORMAL_USER]
    assert not (stranger.scopes & PRIVILEGED)


def test_an_unverified_identity_holds_nothing_at_all():
    """The baseline is keyed on a verified identity, not handed out freely."""
    anonymous = resolve_principal(user_id="", org_id="", roster={})
    assert anonymous.scopes == frozenset()


def test_an_unrecognised_role_name_grants_no_elevation():
    """A newer roster against an older binary must not fail open."""
    principal = resolve_principal(
        user_id="x", org_id="", roster={"x": {"role": "future_super_role"}}
    )
    assert principal.role is Role.NORMAL_USER
    assert not (principal.scopes & PRIVILEGED)


def test_a_mutating_request_needs_the_write_scope():
    """Reading which credential slots exist is not the same act as setting one."""
    assert required_scope("/api/env", "GET") != required_scope("/api/env", "PUT")
    assert required_scope("/api/env", "PUT") == CREDENTIAL_WRITE

    reader = Principal(user_id="r", org_id="", scopes=frozenset({"credential:read"}))
    assert authorize(reader, "/api/env", "GET")
    assert not authorize(reader, "/api/env", "PUT")


def test_every_privileged_prefix_is_mapped_to_a_scope():
    """Structural: an unmapped privileged prefix would be reachable by anyone."""
    for path in CATALOGUE + RAW_METADATA + ["/api/credentials/pool/x/1"]:
        assert required_scope(path) is not None, path
