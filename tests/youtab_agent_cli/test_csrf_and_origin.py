"""CSRF and exact-origin enforcement on the assertion-authenticated bind.

Access does not make these unnecessary, and that is the premise every test here
rests on: the assertion is *ambient*. The browser attaches it to any request to
the protected hostname, including one another site caused. An attacker's page
cannot read it and does not need to.

So the two checks are tested independently, because each covers the other's
gap. Origin stops the ordinary cross-site form post, which cannot forge the
header. The token stops anything that can cause a request but cannot read a
response. Removing either has a mutation control below.
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
from youtab_agent_cli.authz import ROSTER_ENV, Role  # noqa: E402
from youtab_agent_cli.dashboard_auth import clear_providers  # noqa: E402
from youtab_agent_cli.dashboard_auth import csrf  # noqa: E402
from youtab_agent_cli.dashboard_auth.cloudflare_access import (  # noqa: E402
    register_if_configured,
)

TEAM = "youtab-preprod.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a" * 64
KID = "csrf-key"
OWNER = "owner@youtab.io"
PUBLIC_URL = "https://agent.example.test"
GUARDED = "/api/gateway/restart"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(scope="module")
def jwks(keypair):
    _p, pub = keypair
    n = pub.public_numbers()

    def b64(v: int) -> str:
        raw = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                      "n": b64(n.n), "e": b64(n.e)}]}


def _token(key, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUD, "email": OWNER, "sub": "cf-owner",
              "iat": now - 10, "nbf": now - 10, "exp": now + 600}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def gated(monkeypatch, jwks):
    from youtab_agent_cli import access_jwt

    monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("YOUTAB_ACCESS_AUD", AUD)
    monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", OWNER)
    monkeypatch.setenv(ROSTER_ENV, json.dumps(
        {"cf-owner": {"role": Role.YOUTAB_OWNER.value}}))
    monkeypatch.setenv("YOUTAB_AGENT_DASHBOARD_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setattr(access_jwt, "_fetch_jwks", lambda url: jwks)
    access_jwt._jwks_cache.clear()

    clear_providers()
    assert register_if_configured() is True
    prev = (web_server.app.state.__dict__.get("bound_host"),
            web_server.app.state.__dict__.get("auth_required"))
    web_server.app.state.bound_host = "0.0.0.0"
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url=PUBLIC_URL,
                     raise_server_exceptions=False)
    clear_providers()
    web_server.app.state.bound_host, web_server.app.state.auth_required = prev


def _auth(keypair) -> dict:
    return {"Cf-Access-Jwt-Assertion": _token(keypair[0])}


def _csrf_token(gated, keypair) -> str:
    resp = gated.get("/api/auth/csrf", headers=_auth(keypair))
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


# --- positive ---------------------------------------------------------------


class TestAValidStateChangingRequestSucceeds:
    def test_same_origin_with_a_fresh_token(self, gated, keypair):
        resp = gated.post(GUARDED, headers={
            **_auth(keypair),
            "Origin": PUBLIC_URL,
            csrf.CSRF_HEADER: _csrf_token(gated, keypair),
        })
        assert resp.status_code not in (401, 403), resp.text

    def test_referer_is_accepted_when_origin_is_absent(self, gated, keypair):
        """Some engines omit Origin on same-origin navigation."""
        resp = gated.post(GUARDED, headers={
            **_auth(keypair),
            "Referer": f"{PUBLIC_URL}/system",
            csrf.CSRF_HEADER: _csrf_token(gated, keypair),
        })
        assert resp.status_code not in (401, 403), resp.text

    def test_safe_methods_need_no_token(self, gated, keypair):
        """A GET changes nothing, so requiring proof would only break reads."""
        assert gated.get("/api/auth/csrf", headers=_auth(keypair)).status_code == 200


# --- origin -----------------------------------------------------------------


class TestOriginIsEnforcedExactly:
    @pytest.mark.parametrize("origin", [
        "https://evil.test",
        "https://agent.example.test.evil.test",   # prefix, not the origin
        "https://evil.test/agent.example.test",   # path, not the origin
        "http://agent.example.test",              # downgraded scheme
        "https://agent.example.test:8443",        # different port
        "null",
        "not-a-url",
        "javascript:alert(1)",
    ])
    def test_a_foreign_malformed_or_downgraded_origin_is_refused(
            self, gated, keypair, origin):
        resp = gated.post(GUARDED, headers={
            **_auth(keypair),
            "Origin": origin,
            csrf.CSRF_HEADER: _csrf_token(gated, keypair),
        })
        assert resp.status_code == 403, f"{origin} was accepted"

    def test_a_missing_origin_and_referer_is_refused(self, gated, keypair):
        """Absence of evidence is not evidence of same-origin."""
        resp = gated.post(GUARDED, headers={
            **_auth(keypair),
            csrf.CSRF_HEADER: _csrf_token(gated, keypair),
        })
        assert resp.status_code == 403

    def test_a_foreign_referer_is_refused(self, gated, keypair):
        resp = gated.post(GUARDED, headers={
            **_auth(keypair),
            "Referer": "https://evil.test/page",
            csrf.CSRF_HEADER: _csrf_token(gated, keypair),
        })
        assert resp.status_code == 403


# --- token ------------------------------------------------------------------


class TestTokenIsRequiredAndSingleUse:
    def test_an_absent_token_is_refused(self, gated, keypair):
        resp = gated.post(GUARDED, headers={**_auth(keypair), "Origin": PUBLIC_URL})
        assert resp.status_code == 403

    def test_a_garbage_token_is_refused(self, gated, keypair):
        resp = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: "nope.nope"})
        assert resp.status_code == 403

    def test_a_replayed_token_is_refused(self, gated, keypair):
        """Single use. A captured token is stale before it can travel."""
        token = _csrf_token(gated, keypair)
        first = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: token})
        assert first.status_code not in (401, 403), "sanity: the first use works"

        replay = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: token})
        assert replay.status_code == 403, "a token was accepted twice"

    def test_a_token_minted_for_another_principal_is_refused(self, gated, keypair):
        other = csrf.mint("cf-somebody-else")
        resp = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: other})
        assert resp.status_code == 403

    def test_an_expired_token_is_refused(self, gated, keypair):
        """Aged in the store rather than by moving the clock.

        Winding `time.time` forward would also expire the Access assertion, and
        the request would be refused for the wrong reason -- a 401 that looked
        like the 403 this is testing for.
        """
        token = csrf.mint("cf-owner")
        nonce = token.split(".")[0]
        with csrf._lock:
            user_id, _expires = csrf._issued[nonce]
            csrf._issued[nonce] = (user_id, time.time() - 1)

        resp = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: token})
        assert resp.status_code == 403

    def test_a_forged_signature_is_refused(self, gated, keypair):
        """The nonce is real; the signature is not."""
        real = csrf.mint("cf-owner")
        nonce = real.split(".")[0]
        resp = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": PUBLIC_URL,
            csrf.CSRF_HEADER: f"{nonce}.{'0' * 32}"})
        assert resp.status_code == 403


class TestTokenUnit:
    def test_mint_and_consume_round_trips_once(self):
        t = csrf.mint("u1")
        assert csrf.consume(t, "u1") is True
        assert csrf.consume(t, "u1") is False

    def test_a_token_will_not_consume_for_a_different_user(self):
        t = csrf.mint("u1")
        assert csrf.consume(t, "u2") is False

    def test_an_unbound_mint_is_refused(self):
        with pytest.raises(ValueError):
            csrf.mint("")

    def test_empty_input_is_refused(self):
        assert csrf.consume("", "u1") is False
        assert csrf.consume("x.y", "") is False


class TestOriginUnit:
    def test_an_unconfigured_public_url_refuses_everything(self):
        """Nothing to compare against means nothing is verified."""
        assert csrf.origin_is_trusted("https://agent.example.test", "", "") is False


# --- non-browser contracts are governed separately --------------------------


class TestNonBrowserContractsAreNotWeakened:
    def test_loopback_is_not_subject_to_the_browser_check(self, monkeypatch, jwks):
        """A local operator on their own machine carries no ambient browser
        credential, and the rule is not relaxed for them -- it does not apply."""
        prev = web_server.app.state.__dict__.get("auth_required")
        web_server.app.state.auth_required = False
        try:
            client = TestClient(web_server.app, base_url="http://127.0.0.1:8081",
                                raise_server_exceptions=False)
            assert client.post(GUARDED).status_code != 403
        finally:
            web_server.app.state.auth_required = prev


# --- mutation controls -------------------------------------------------------


class TestMutationsTurnItRed:
    def test_removing_the_origin_check_admits_a_cross_site_post(
            self, gated, keypair, monkeypatch):
        monkeypatch.setattr(csrf, "origin_is_trusted", lambda o, r, e: True)
        resp = gated.post(GUARDED, headers={
            **_auth(keypair), "Origin": "https://evil.test",
            csrf.CSRF_HEADER: _csrf_token(gated, keypair)})
        assert resp.status_code not in (401, 403), "sanity: the mutation is in effect"
        # The real check refuses this -- see TestOriginIsEnforcedExactly.

    def test_removing_the_token_check_admits_a_tokenless_post(
            self, gated, keypair, monkeypatch):
        monkeypatch.setattr(csrf, "consume", lambda token, user_id: True)
        resp = gated.post(GUARDED, headers={**_auth(keypair), "Origin": PUBLIC_URL})
        assert resp.status_code not in (401, 403), "sanity: the mutation is in effect"
        # The real check refuses this -- see TestTokenIsRequiredAndSingleUse.

    def test_making_tokens_reusable_admits_a_replay(self, gated, keypair, monkeypatch):
        """Single-use is the property that defeats replay, so it has a control."""
        real_consume = csrf.consume
        seen: dict = {}

        def reusable(token, user_id):
            if token not in seen:
                seen[token] = real_consume(token, user_id)
            return seen[token]

        monkeypatch.setattr(csrf, "consume", reusable)
        token = _csrf_token(gated, keypair)
        hdrs = {**_auth(keypair), "Origin": PUBLIC_URL, csrf.CSRF_HEADER: token}
        assert gated.post(GUARDED, headers=hdrs).status_code not in (401, 403)
        assert gated.post(GUARDED, headers=hdrs).status_code not in (401, 403), (
            "sanity: the mutation makes the token reusable"
        )


# --- nothing sensitive leaks --------------------------------------------------


def test_no_token_or_credential_appears_in_a_refusal(gated, keypair):
    token = _csrf_token(gated, keypair)
    assertion = _token(keypair[0])
    resp = gated.post(GUARDED, headers={
        "Cf-Access-Jwt-Assertion": assertion,
        "Origin": "https://evil.test",
        csrf.CSRF_HEADER: token,
    })
    assert resp.status_code == 403
    assert token not in resp.text
    assert assertion not in resp.text
    assert OWNER not in resp.text
