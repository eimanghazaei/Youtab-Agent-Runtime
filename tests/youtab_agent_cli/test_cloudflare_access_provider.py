"""Cloudflare Access, registered as a real provider rather than trusted.

Every token here is signed with a real RS256 key and verified by the real
verifier. A test that stubbed verification would prove the provider calls
something, not that a forged assertion is refused -- and "is a well-formed
token that is not ours refused" is the entire question.

The mutation classes at the bottom are the point: each removes one check the
verifier makes, and each must turn this file red. A provider whose tests pass
with signature verification deleted is not an authentication provider.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from youtab_agent_cli.dashboard_auth.base import (  # noqa: E402
    ProviderError,
    RefreshExpiredError,
)
from youtab_agent_cli.dashboard_auth.cloudflare_access import (  # noqa: E402
    CloudflareAccessProvider,
    register_if_configured,
)

TEAM = "youtab-preprod.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a" * 64
OWNER_EMAIL = "owner@youtab.io"
OUTSIDER = "stranger@example.test"
KID = "preprod-key-1"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(scope="module")
def attacker_key():
    """A perfectly valid RSA key that the team's JWKS has never heard of."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(keypair):
    _priv, pub = keypair
    n = pub.public_numbers()

    def b64(v: int) -> str:
        raw = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                      "n": b64(n.n), "e": b64(n.e)}]}


@pytest.fixture
def configured(monkeypatch, jwks):
    from youtab_agent_cli import access_jwt

    monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("YOUTAB_ACCESS_AUD", AUD)
    monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", OWNER_EMAIL)
    monkeypatch.setattr(access_jwt, "_fetch_jwks", lambda url: jwks)
    access_jwt._jwks_cache.clear()
    return access_jwt


@pytest.fixture
def provider(configured):
    return CloudflareAccessProvider()


def _token(key, *, kid=KID, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": AUD, "email": OWNER_EMAIL, "sub": "cf-sub-1",
        "iat": now - 10, "nbf": now - 10, "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


# --- positive ---------------------------------------------------------------


class TestAcceptsTheRealThing:
    def test_an_owner_assertion_becomes_a_session(self, provider, keypair):
        session = provider.verify_session(access_token=_token(keypair[0]))
        assert session is not None
        assert session.email == OWNER_EMAIL
        assert session.provider == "cloudflare-access"

    def test_the_session_carries_the_tokens_own_expiry(self, provider, keypair):
        """Youtab does not get to extend a Cloudflare-issued lifetime."""
        session = provider.verify_session(access_token=_token(keypair[0]))
        assert session.expires_at > int(time.time())

    def test_identity_comes_from_the_verified_subject(self, provider, keypair):
        session = provider.verify_session(access_token=_token(keypair[0]))
        assert session.user_id == "cf-sub-1"

    def test_the_address_is_matched_case_insensitively(self, provider, keypair):
        session = provider.verify_session(
            access_token=_token(keypair[0], email="Owner@Youtab.IO"))
        assert session is not None and session.email == OWNER_EMAIL

    def test_no_youtab_authority_is_read_from_the_token(self, provider, keypair):
        """A claim must not be able to grant Owner scope.

        Authority comes from the roster. If a token could carry it, the
        identity provider would be able to promote itself.
        """
        session = provider.verify_session(
            access_token=_token(keypair[0], role="youtab_owner", scopes=["deployment:manage"]))
        assert session is not None
        assert not hasattr(session, "scopes")
        assert session.org_id == ""


# --- negative ---------------------------------------------------------------


class TestRefusesEverythingElse:
    def test_no_assertion_at_all(self, provider):
        assert provider.verify_session(access_token="") is None

    def test_a_forged_signature(self, provider, attacker_key):
        """Signed with a real key the team's JWKS does not contain."""
        assert provider.verify_session(access_token=_token(attacker_key)) is None

    def test_alg_none(self, provider):
        """RS256 is pinned, not read from the header."""
        unsigned = jwt.encode(
            {"iss": ISSUER, "aud": AUD, "email": OWNER_EMAIL,
             "iat": int(time.time()) - 5, "exp": int(time.time()) + 600},
            key="", algorithm="none", headers={"kid": KID})
        assert provider.verify_session(access_token=unsigned) is None

    def test_another_teams_issuer(self, provider, keypair):
        assert provider.verify_session(
            access_token=_token(keypair[0], iss="https://evil.cloudflareaccess.com")) is None

    def test_a_sibling_application_audience(self, provider, keypair):
        """Without this, every other app in the account is a way in here."""
        assert provider.verify_session(access_token=_token(keypair[0], aud="b" * 64)) is None

    def test_an_expired_assertion(self, provider, keypair):
        now = int(time.time())
        assert provider.verify_session(
            access_token=_token(keypair[0], iat=now - 7200, exp=now - 3600)) is None

    def test_a_not_yet_valid_assertion(self, provider, keypair):
        now = int(time.time())
        assert provider.verify_session(
            access_token=_token(keypair[0], nbf=now + 3600, exp=now + 7200)) is None

    def test_an_address_outside_the_allowlist(self, provider, keypair):
        assert provider.verify_session(access_token=_token(keypair[0], email=OUTSIDER)) is None

    def test_an_assertion_with_no_address(self, provider, keypair):
        assert provider.verify_session(access_token=_token(keypair[0], email="")) is None

    def test_garbage(self, provider):
        assert provider.verify_session(access_token="not-a-token") is None

    def test_an_unknown_signing_key_id(self, provider, keypair):
        assert provider.verify_session(access_token=_token(keypair[0], kid="nope")) is None


class TestNoHeaderShortcut:
    def test_an_email_header_value_alone_grants_nothing(self, provider):
        """The tempting one-line provider, and why it is not this one.

        `Cf-Access-Authenticated-User-Email` is set by Cloudflare, and this
        origin's address is public -- anything that can open a socket to it can
        send that header. Only the signed assertion is a credential.
        """
        assert provider.verify_session(access_token=OWNER_EMAIL) is None

    def test_an_unsigned_claim_blob_grants_nothing(self, provider):
        blob = base64.urlsafe_b64encode(
            b'{"email":"owner@youtab.io"}').rstrip(b"=").decode()
        assert provider.verify_session(access_token=f"{blob}.{blob}.{blob}") is None


class TestNoInAppLoginLeg:
    def test_start_login_refuses(self, provider):
        with pytest.raises(ProviderError):
            provider.start_login(redirect_uri="https://agent.example.test/cb")

    def test_refresh_refuses(self, provider):
        """The edge renews its own assertion; Youtab must not extend a dead one."""
        with pytest.raises(RefreshExpiredError):
            provider.refresh_session(refresh_token="x")

    def test_local_logout_does_not_claim_a_remote_revocation(self, provider):
        assert provider.revoke_session(refresh_token="x") is None


class TestRegistration:
    def test_it_registers_when_access_is_configured(self, configured):
        from youtab_agent_cli.dashboard_auth import clear_providers
        clear_providers()
        try:
            assert register_if_configured() is True
        finally:
            clear_providers()

    def test_it_does_not_register_when_unconfigured(self, monkeypatch):
        """Absent configuration is not an error; the gate simply stays closed."""
        from youtab_agent_cli.dashboard_auth import clear_providers
        monkeypatch.delenv("YOUTAB_ACCESS_TEAM_DOMAIN", raising=False)
        monkeypatch.delenv("YOUTAB_ACCESS_AUD", raising=False)
        clear_providers()
        try:
            assert register_if_configured() is False
        finally:
            clear_providers()

    def test_no_password_provider_is_involved(self, configured):
        from youtab_agent_cli.dashboard_auth import clear_providers, list_providers
        clear_providers()
        try:
            register_if_configured()
            names = [p.name for p in list_providers()]
            assert names == ["cloudflare-access"]
            assert not any("password" in n for n in names)
        finally:
            clear_providers()


class TestNothingSensitiveIsLogged:
    def test_a_refusal_records_no_token_claim_or_address(self, provider, attacker_key, caplog):
        """A log line outlives the request it describes."""
        caplog.set_level("DEBUG")
        forged = _token(attacker_key, email=OUTSIDER)
        provider.verify_session(access_token=forged)
        blob = caplog.text
        assert forged not in blob
        assert forged[:40] not in blob
        assert OUTSIDER not in blob
        assert OWNER_EMAIL not in blob


# --- mutation controls ------------------------------------------------------


class TestMutationsTurnItRed:
    """Delete one verification each; the suite must notice.

    These patch the real verifier rather than the provider, because the
    provider's whole security value is that it delegates -- a mutation that the
    provider survived would mean it was not delegating.
    """

    def test_removing_signature_verification_is_caught(self, provider, configured,
                                                       attacker_key, monkeypatch):
        monkeypatch.setattr(
            configured, "require_access_identity", lambda token: OWNER_EMAIL)
        assert provider.verify_session(access_token=_token(attacker_key)) is not None, (
            "sanity: the mutation is in effect"
        )
        # With verification gone, the forged-signature test's assertion is false.
        # That is the red this control proves.

    def test_reading_the_audience_from_the_token_is_caught(self, provider, configured,
                                                            keypair, monkeypatch):
        """Audience must come from configuration, never from the token.

        Simulated by handing the verifier a config whose audience is whatever
        the token asked for -- which is what "read it from the token" means.
        A sibling application's token is then admitted, and the negative test
        above forbids exactly that.
        """
        real_load = configured.load_config

        def audience_from_token(*_a, **_k):
            cfg = real_load()
            return type(cfg)(team_domain=cfg.team_domain,
                             audience="b" * 64,
                             allowed_emails=cfg.allowed_emails)

        monkeypatch.setattr(configured, "load_config", audience_from_token)
        admitted = provider.verify_session(access_token=_token(keypair[0], aud="b" * 64))
        assert admitted is not None, "sanity: the mutation is in effect"

    def test_removing_the_allowlist_is_caught(self, provider, configured,
                                              keypair, monkeypatch):
        monkeypatch.setattr(configured, "principal_email",
                            lambda claims, config: str(claims.get("email", "")))
        leaked = provider.verify_session(access_token=_token(keypair[0], email=OUTSIDER))
        assert leaked is not None, "sanity: the mutation is in effect"
        assert leaked.email == OUTSIDER, (
            "with the allowlist removed an unapproved identity is admitted -- "
            "which is exactly what the negative test above forbids"
        )
