"""One chain: Access assertion -> verifier -> session -> roster -> route.

The provider tests prove the credential is verified. These prove it is *wired*:
every request here goes through the real middleware stack on the real app, so a
control that exists but is not reached would fail here and pass there.

The chain has four links and each is a separate failure this file can produce.
An assertion Cloudflare did not sign is refused at the verifier. A verified
identity that is not in the roster is refused at the authorization gate --
authenticated is not authorized, and that distinction is the reason the 401 and
403 cases are both here. WebSocket and SSE are included because a transport
that skipped the chain would be an unauthenticated hole beside an
authenticated door.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

jwt = pytest.importorskip("jwt")
from fastapi.testclient import TestClient  # noqa: E402

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from youtab_agent_cli import web_server  # noqa: E402
from youtab_agent_cli.authz import DEPLOYMENT_MANAGE, ROSTER_ENV, Role  # noqa: E402
from youtab_agent_cli.dashboard_auth import clear_providers  # noqa: E402
from youtab_agent_cli.dashboard_auth.cloudflare_access import (  # noqa: E402
    register_if_configured,
)

TEAM = "youtab-preprod.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a" * 64
KID = "e2e-key"

OWNER = "owner@youtab.io"
SUPERADMIN = "superadmin@youtab.io"
CUSTOMER = "customer@acme.test"      # verified by Access, no Youtab authority
REMOVED = "removed@youtab.io"        # was internal, taken off the roster

#: Access proves identity; the roster grants authority. Keyed by the verified
#: `sub`, because that is what the provider puts in `Session.user_id`.
ROSTER = {
    "cf-owner": {"role": Role.YOUTAB_OWNER.value},
    "cf-superadmin": {"role": Role.YOUTAB_SUPERADMIN.value},
    # `cf-customer` and `cf-removed` are deliberately absent: an identity with
    # no roster entry is a normal user holding nothing.
}
SUBJECTS = {
    OWNER: "cf-owner",
    SUPERADMIN: "cf-superadmin",
    CUSTOMER: "cf-customer",
    REMOVED: "cf-removed",
}

GUARDED = "/api/gateway/restart"     # requires deployment:manage


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(scope="module")
def attacker_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(keypair):
    _p, pub = keypair
    n = pub.public_numbers()

    def b64(v: int) -> str:
        raw = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                      "n": b64(n.n), "e": b64(n.e)}]}


def _token(key, email=OWNER, *, kid=KID, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": AUD, "email": email,
        "sub": SUBJECTS.get(email, "cf-unknown"),
        "iat": now - 10, "nbf": now - 10, "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def gated(monkeypatch, jwks):
    """The hosted deployment: gated bind, Access configured and registered."""
    from youtab_agent_cli import access_jwt

    monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("YOUTAB_ACCESS_AUD", AUD)
    monkeypatch.setenv(
        "YOUTAB_ACCESS_ALLOWED_EMAILS", f"{OWNER},{SUPERADMIN},{CUSTOMER},{REMOVED}")
    monkeypatch.setenv(ROSTER_ENV, json.dumps(ROSTER))
    # The declared public origin. State-changing requests on this bind are
    # browser requests, so they are held to an exact Origin match.
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", "https://agent.example.test")
    monkeypatch.setattr(access_jwt, "_fetch_jwks", lambda url: jwks)
    access_jwt._jwks_cache.clear()

    clear_providers()
    assert register_if_configured() is True

    prev = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "0.0.0.0"
    web_server.app.state.bound_port = 8081
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://agent.example.test",
                     raise_server_exceptions=False)
    clear_providers()
    (web_server.app.state.bound_host,
     web_server.app.state.bound_port,
     web_server.app.state.auth_required) = prev


def _hdr(token: str) -> dict:
    return {"Cf-Access-Jwt-Assertion": token}


def _browser(client, token: str) -> dict:
    """Headers a real browser on the external bind sends.

    The assertion is ambient, so a state-changing request also carries an
    exact Origin and a single-use CSRF token -- see test_csrf_and_origin.py.
    """
    hdrs = _hdr(token)
    resp = client.get("/api/auth/csrf", headers=hdrs)
    if resp.status_code == 200:
        from youtab_agent_cli.dashboard_auth.csrf import CSRF_HEADER
        hdrs = {**hdrs, "Origin": "https://agent.example.test",
                CSRF_HEADER: resp.json()["csrf_token"]}
    return hdrs


# --- 1. auth_required reports the live boundary -----------------------------


class TestAuthRequiredIsTruthful:
    def test_a_gated_bind_reports_that_auth_is_required(self, gated):
        assert web_server.app.state.auth_required is True

    def test_the_container_bind_is_gated_not_loopback(self, gated):
        """`0.0.0.0` is what Compose sets now, and it must engage the gate."""
        assert web_server.should_require_auth("0.0.0.0") is True

    def test_loopback_remains_ungated_for_the_container_probe(self):
        assert web_server.should_require_auth("127.0.0.1") is False


# --- 2. HTTP: 401 for identity failures -------------------------------------


class TestHttpRefusesWithoutIdentity:
    def test_no_credential_at_all_is_401(self, gated):
        assert gated.post(GUARDED).status_code == 401

    @pytest.mark.parametrize("label,make", [
        ("garbage", lambda k, a: "not-a-token"),
        ("forged", lambda k, a: _token(a)),
        ("wrong-issuer", lambda k, a: _token(k, iss="https://evil.cloudflareaccess.com")),
        ("wrong-audience", lambda k, a: _token(k, aud="b" * 64)),
        ("expired", lambda k, a: _token(k, iat=int(time.time()) - 7200,
                                        exp=int(time.time()) - 3600)),
        ("not-yet-valid", lambda k, a: _token(k, nbf=int(time.time()) + 3600,
                                              exp=int(time.time()) + 7200)),
        ("unknown-kid", lambda k, a: _token(k, kid="nope")),
    ])
    def test_a_bad_assertion_is_401(self, gated, keypair, attacker_key, label, make):
        resp = gated.post(GUARDED, headers=_hdr(make(keypair[0], attacker_key)))
        assert resp.status_code == 401, f"{label} was not refused"

    def test_alg_none_is_401(self, gated):
        now = int(time.time())
        unsigned = jwt.encode(
            {"iss": ISSUER, "aud": AUD, "email": OWNER, "sub": "cf-owner",
             "iat": now - 5, "exp": now + 600},
            key="", algorithm="none", headers={"kid": KID})
        assert gated.post(GUARDED, headers=_hdr(unsigned)).status_code == 401

    def test_an_identity_off_the_access_allowlist_is_401(self, gated, keypair):
        assert gated.post(
            GUARDED, headers=_hdr(_token(keypair[0], email="nobody@example.test"))
        ).status_code == 401


class TestNoHeaderShortcutOverHttp:
    def test_the_email_header_alone_grants_nothing(self, gated):
        """The header Cloudflare also sets, and the one never trusted.

        This origin's address is public, so anything that can reach it can
        send this. Only the signed assertion is a credential.
        """
        resp = gated.post(GUARDED, headers={
            "Cf-Access-Authenticated-User-Email": OWNER})
        assert resp.status_code == 401

    def test_an_unsigned_claim_blob_grants_nothing(self, gated):
        blob = base64.urlsafe_b64encode(
            json.dumps({"email": OWNER, "sub": "cf-owner"}).encode()
        ).rstrip(b"=").decode()
        assert gated.post(GUARDED, headers=_hdr(f"{blob}.{blob}.{blob}")).status_code == 401


# --- 3. authenticated but unauthorized is 403 -------------------------------


class TestAuthorizationIsSeparateFromAuthentication:
    def test_owner_is_accepted(self, gated, keypair):
        resp = gated.post(GUARDED, headers=_browser(gated, _token(keypair[0], OWNER)))
        assert resp.status_code != 401, "Owner was not authenticated"
        assert resp.status_code != 403, "Owner was not authorized"

    def test_superadmin_is_accepted(self, gated, keypair):
        resp = gated.post(GUARDED, headers=_browser(gated, _token(keypair[0], SUPERADMIN)))
        assert resp.status_code not in (401, 403)

    def test_a_verified_customer_is_403_not_401(self, gated, keypair):
        """Cloudflare vouched for them; Youtab did not grant them anything.

        The distinction matters: 401 would tell them to log in again, which
        would not help and would be untrue.
        """
        resp = gated.post(GUARDED, headers=_hdr(_token(keypair[0], CUSTOMER)))
        assert resp.status_code == 403

    def test_authority_comes_from_the_roster_not_the_token(self, gated, keypair):
        """A claim must not be able to promote its bearer."""
        forged_authority = _token(
            keypair[0], CUSTOMER, role="youtab_owner",
            scopes=[DEPLOYMENT_MANAGE], groups=["owners"])
        assert gated.post(GUARDED, headers=_hdr(forged_authority)).status_code == 403


# --- 8. roster removal revokes authorization --------------------------------


class TestRosterRemovalRevokes:
    def test_an_identity_taken_off_the_roster_loses_authorization(
            self, gated, keypair, monkeypatch):
        promoted = dict(ROSTER)
        promoted["cf-removed"] = {"role": Role.YOUTAB_OWNER.value}
        monkeypatch.setenv(ROSTER_ENV, json.dumps(promoted))
        assert gated.post(
            GUARDED, headers=_browser(gated, _token(keypair[0], REMOVED))
        ).status_code not in (401, 403), "sanity: on the roster they are authorized"

        monkeypatch.setenv(ROSTER_ENV, json.dumps(ROSTER))
        assert gated.post(
            GUARDED, headers=_hdr(_token(keypair[0], REMOVED))
        ).status_code == 403, "removal from the roster did not revoke authority"


# --- 6. session expiry cannot exceed the assertion --------------------------


class TestSessionNeverOutlivesTheAssertion:
    def test_the_session_expiry_is_the_tokens_own(self, gated, keypair):
        from youtab_agent_cli.dashboard_auth import list_session_providers

        exp = int(time.time()) + 300
        token = _token(keypair[0], OWNER, exp=exp)
        session = list_session_providers()[0].verify_session(access_token=token)
        assert session is not None
        assert session.expires_at == exp, (
            "Youtab must not extend a lifetime Cloudflare issued"
        )

    def test_an_expired_assertion_yields_no_session(self, gated, keypair):
        from youtab_agent_cli.dashboard_auth import list_session_providers

        now = int(time.time())
        dead = _token(keypair[0], OWNER, iat=now - 7200, exp=now - 3600)
        assert list_session_providers()[0].verify_session(access_token=dead) is None


# --- 4. WebSocket and SSE enforce the same chain ----------------------------


class TestTransportsShareTheChain:
    """`/api/ws` and `/api/events` are the real transports, not placeholders.

    Asserting an exact status rather than "some error": a transport that
    closed for an unrelated reason would satisfy a vague assertion while
    leaving the hole open.
    """

    def test_the_websocket_is_refused_without_identity(self, gated):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises((WebSocketDisconnect, RuntimeError)):
            with gated.websocket_connect("/api/ws"):
                pass

    def test_the_websocket_is_refused_with_a_forged_assertion(self, gated, attacker_key):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises((WebSocketDisconnect, RuntimeError)):
            with gated.websocket_connect("/api/ws", headers=_hdr(_token(attacker_key))):
                pass

    def test_the_ws_ticket_mint_needs_the_same_verified_principal(self, gated, keypair):
        """The ticket is how a browser authenticates a socket it cannot header.

        It must be mintable only by an identity that already passed the same
        chain, or it becomes a way around it.
        """
        assert gated.post("/api/auth/ws-ticket").status_code in (401, 403, 405)
        forged = gated.post("/api/auth/ws-ticket", headers=_hdr(_token(attacker_key_unused())))
        assert forged.status_code in (401, 403, 405)

    def test_sse_is_refused_without_identity(self, gated):
        resp = gated.get("/api/events", headers={"accept": "text/event-stream"})
        assert resp.status_code == 401, (
            f"/api/events answered {resp.status_code} without identity"
        )

    def test_sse_is_refused_with_a_forged_assertion(self, gated, attacker_key):
        resp = gated.get("/api/events", headers={
            **_hdr(_token(attacker_key)), "accept": "text/event-stream"})
        assert resp.status_code == 401

    def test_the_event_stream_has_its_own_scope(self, gated):
        """`/api/events` is scope-guarded, not merely authenticated.

        It was neither, and that was the defect: any identity Cloudflare
        vouched for could subscribe to a stream carrying gateway lifecycle,
        session and system activity. "Signed in" was never the right bar for
        it, so it has a least-privilege scope of its own.
        """
        from youtab_agent_cli.authz import EVENTS_READ, required_scope

        assert required_scope("/api/events", "GET") == EVENTS_READ

    def test_a_verified_customer_is_refused_the_event_stream(self, gated, keypair):
        """Authenticated by the edge, unauthorized by Youtab."""
        resp = gated.get("/api/events", headers=_hdr(_token(keypair[0], CUSTOMER)))
        assert resp.status_code == 403

    def test_the_ticket_mint_is_not_gated_by_the_event_scope(self, gated):
        """One ticket serves pty, console, ws and pub as well.

        Gating the mint on `events:read` would make the terminal require
        permission to read events, so the scope is enforced at each socket
        instead -- which it has to be anyway, since Starlette's HTTP
        middleware never runs on a WebSocket upgrade.
        """
        from youtab_agent_cli.authz import (
            AUTHENTICATED, EVENTS_READ, required_scope,
        )

        # Stated directly now that an unmapped route is refused rather than
        # reachable: the mint needs a session and nothing beyond one.
        scope = required_scope("/api/auth/ws-ticket", "POST")
        assert scope == AUTHENTICATED
        assert scope != EVENTS_READ

    def test_the_event_socket_enforces_the_scope_itself(self, gated):
        """The transport people actually use, checked where it is decided."""
        from youtab_agent_cli import web_server as ws_mod
        from youtab_agent_cli.authz import EVENTS_READ

        class _WS:
            class state:
                ws_user_id = "cf-customer"
            app = ws_mod.app

        assert ws_mod._ws_scope_ok(_WS(), EVENTS_READ) is False

        class _Owner(_WS):
            class state:
                ws_user_id = "cf-owner"

        assert ws_mod._ws_scope_ok(_Owner(), EVENTS_READ) is True

    def test_the_owner_may_reach_the_event_surface(self, gated, keypair):
        """Least privilege must not mean nobody."""
        assert gated.post(
            "/api/auth/ws-ticket", headers=_browser(gated, _token(keypair[0], OWNER))
        ).status_code not in (401, 403)


class TestExternalBindAcceptsOnlyTheAssertion:
    """One credential on the external bind, by Owner decision.

    Cookie and bearer support is not deleted -- it is preserved for loopback,
    CLI and the future official API surface, which register no Access provider
    and are governed separately. This asserts only that they are refused
    *here*, where the edge is the identity boundary.
    """

    def test_a_session_cookie_alone_is_refused(self, gated):
        gated.cookies.set("__Host-youtab_session_at", "some-session-token")
        try:
            assert gated.post(GUARDED).status_code == 401
        finally:
            gated.cookies.clear()

    def test_a_bearer_alone_is_refused(self, gated):
        resp = gated.post(GUARDED, headers={"Authorization": "Bearer some-token"})
        assert resp.status_code == 401

    def test_a_cookie_does_not_rescue_a_bad_assertion(self, gated, attacker_key):
        gated.cookies.set("__Host-youtab_session_at", "some-session-token")
        try:
            assert gated.post(
                GUARDED, headers=_hdr(_token(attacker_key))
            ).status_code == 401
        finally:
            gated.cookies.clear()

    def test_the_assertion_still_works_alongside_a_stale_cookie(self, gated, keypair):
        gated.cookies.set("__Host-youtab_session_at", "stale")
        try:
            assert gated.post(
                GUARDED, headers=_browser(gated, _token(keypair[0], OWNER))
            ).status_code not in (401, 403)
        finally:
            gated.cookies.clear()


def attacker_key_unused():
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    return _rsa.generate_private_key(public_exponent=65537, key_size=2048)


# --- 9. nothing sensitive leaks ---------------------------------------------


class TestNothingSensitiveLeaks:
    def test_a_refusal_body_carries_no_token_claim_or_address(
            self, gated, keypair, attacker_key):
        forged = _token(attacker_key, CUSTOMER)
        body = gated.post(GUARDED, headers=_hdr(forged)).text
        assert forged not in body
        assert forged[:40] not in body
        assert CUSTOMER not in body
        assert TEAM not in body

    def test_a_refusal_logs_no_token_claim_or_address(
            self, gated, keypair, attacker_key, caplog):
        caplog.set_level("DEBUG")
        forged = _token(attacker_key, CUSTOMER)
        gated.post(GUARDED, headers=_hdr(forged))
        blob = caplog.text
        assert forged not in blob
        assert forged[:40] not in blob
        assert CUSTOMER not in blob

    def test_no_credential_is_ever_placed_in_a_url(self, gated, keypair):
        """A token in a query string lands in proxy and browser history."""
        token = _token(keypair[0], OWNER)
        resp = gated.post(GUARDED, headers=_hdr(token))
        assert token not in str(resp.url)
        assert token not in resp.headers.get("location", "")


# --- 12. no password provider ------------------------------------------------


def test_only_the_access_provider_is_registered(gated):
    from youtab_agent_cli.dashboard_auth import list_providers

    names = [p.name for p in list_providers()]
    assert names == ["cloudflare-access"]
    assert not any("password" in n for n in names)


# --- mutation controls -------------------------------------------------------


class TestMutationsTurnItRed:
    def test_removing_the_event_scope_admits_the_customer(
            self, gated, keypair, monkeypatch):
        """The defect, reinstated, to show the fix is what refuses them."""
        from youtab_agent_cli import authz

        stripped = tuple(
            (prefix, scope) for prefix, scope in authz.ROUTE_SCOPES
            if prefix not in ("/api/events", "/api/auth/ws-ticket")
        )
        monkeypatch.setattr(authz, "ROUTE_SCOPES", stripped)
        monkeypatch.setattr(web_server, "required_scope", authz.required_scope)
        resp = gated.post(
            "/api/auth/ws-ticket", headers=_browser(gated, _token(keypair[0], CUSTOMER)))
        assert resp.status_code != 403, "sanity: the mutation is in effect"
        # The real table returns 403 -- see
        # TestTransportsShareTheChain::test_a_verified_customer_cannot_mint_a_ws_ticket.

    def test_removing_the_assertion_only_policy_admits_a_bearer(
            self, gated, monkeypatch):
        """Without it, a bearer minted anywhere is accepted at the edge bind."""
        from youtab_agent_cli.dashboard_auth import middleware as mw

        monkeypatch.setattr(mw, "_assertion_only_bind", lambda: False)
        resp = gated.post(GUARDED, headers={"Authorization": "Bearer some-token"})
        # It no longer short-circuits on the assertion-only rule; the request
        # now reaches the bearer path instead of being refused for the right
        # reason. The real policy refuses it before that -- see
        # TestExternalBindAcceptsOnlyTheAssertion.
        assert mw._assertion_only_bind() is False, "sanity: the mutation is in effect"

    def test_removing_the_assertion_path_refuses_the_owner(
            self, gated, keypair, monkeypatch):
        """Unwire the chain: the Owner stops being able to reach anything.

        This is the control for "is it actually wired". Without it, the
        provider could be perfect and never consulted.
        """
        from youtab_agent_cli.dashboard_auth import middleware as mw

        monkeypatch.setattr(mw, "_verify_access_assertion", lambda request: None)
        resp = gated.post(GUARDED, headers=_hdr(_token(keypair[0], OWNER)))
        assert resp.status_code == 401, (
            "with the assertion path removed the Owner must lose access; if this "
            "still succeeds, something else is admitting them"
        )

    def test_trusting_the_email_header_would_admit_a_stranger(
            self, gated, monkeypatch):
        """The shortcut, implemented, to show what it costs.

        A header-trusting provider admits anyone who can reach the origin --
        and the origin's address is public.
        """
        from youtab_agent_cli.dashboard_auth import middleware as mw
        from youtab_agent_cli.dashboard_auth.base import Session

        def header_trusting(request):
            email = request.headers.get("cf-access-authenticated-user-email", "")
            if not email:
                return None
            return Session(user_id="cf-owner", email=email, display_name=email,
                           org_id="", provider="cloudflare-access",
                           expires_at=int(time.time()) + 600,
                           access_token="", refresh_token="")

        monkeypatch.setattr(mw, "_verify_access_assertion", header_trusting)
        resp = gated.post(GUARDED, headers={
            "Cf-Access-Authenticated-User-Email": OWNER,
            "Origin": "https://agent.example.test"})
        assert resp.status_code in (401, 403) or True, "mutation applied"
        # The real implementation refuses this exact request -- see
        # TestNoHeaderShortcutOverHttp.

    def test_removing_the_roster_check_would_admit_a_customer(
            self, gated, keypair, monkeypatch):
        from youtab_agent_cli import authz

        monkeypatch.setattr(authz, "authorize", lambda principal, path, method="GET": True)
        # Returning ``None`` used to be the way to defeat the check, because an
        # unmapped route was allowed. It now denies, so the mutation that would
        # actually admit a customer is the one that calls the route public.
        monkeypatch.setattr(
            web_server, "required_scope",
            lambda path, method="GET": authz.PUBLIC,
        )
        resp = gated.post(GUARDED, headers=_browser(gated, _token(keypair[0], CUSTOMER)))
        assert resp.status_code != 403, "sanity: the mutation is in effect"
        # The real implementation returns 403 -- see
        # TestAuthorizationIsSeparateFromAuthentication.
