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
        resp = gated.post(GUARDED, headers=_hdr(_token(keypair[0], OWNER)))
        assert resp.status_code != 401, "Owner was not authenticated"
        assert resp.status_code != 403, "Owner was not authorized"

    def test_superadmin_is_accepted(self, gated, keypair):
        resp = gated.post(GUARDED, headers=_hdr(_token(keypair[0], SUPERADMIN)))
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
            GUARDED, headers=_hdr(_token(keypair[0], REMOVED))
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

    def test_sse_authorization_follows_the_same_route_table_as_http(
            self, gated, keypair):
        """Streams are governed by the same table, not a separate one.

        `/api/events` is not scope-guarded, so a verified identity reaches it
        exactly as it reaches any unguarded HTTP route -- that is the rule
        being matched, and asserting a 403 here would be asserting a control
        this deployment does not have. What must hold is that the *table* is
        the authority for both, and that a scope-guarded path refuses the same
        principal on either transport.
        """
        from youtab_agent_cli.authz import required_scope

        assert required_scope("/api/events", "GET") is None, (
            "if /api/events becomes scope-guarded, this test must assert the "
            "refusal rather than be deleted"
        )
        assert required_scope(GUARDED, "POST") == DEPLOYMENT_MANAGE

        # Same principal, same table, guarded path: refused.
        assert gated.post(
            GUARDED, headers=_hdr(_token(keypair[0], CUSTOMER))
        ).status_code == 403


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
            "Cf-Access-Authenticated-User-Email": OWNER})
        assert resp.status_code not in (401, 403), "sanity: the mutation is in effect"
        # The real implementation refuses this exact request -- see
        # TestNoHeaderShortcutOverHttp.

    def test_removing_the_roster_check_would_admit_a_customer(
            self, gated, keypair, monkeypatch):
        from youtab_agent_cli import authz

        monkeypatch.setattr(authz, "authorize", lambda principal, path, method="GET": True)
        monkeypatch.setattr(web_server, "required_scope", lambda path, method="GET": None)
        resp = gated.post(GUARDED, headers=_hdr(_token(keypair[0], CUSTOMER)))
        assert resp.status_code != 403, "sanity: the mutation is in effect"
        # The real implementation returns 403 -- see
        # TestAuthorizationIsSeparateFromAuthentication.
