"""Origin-side verification of Cloudflare Access identity.

Reaching the Access login page proves the edge is enforcing. It proves nothing
about this process, and an origin that infers "a request arrived, therefore
Cloudflare vetted it" is trusting the network instead of a credential. These
tests drive the real verifier with real RS256 signatures, so a token that is
merely well-formed is not mistaken for one that is trusted.

Every negative case here is a way a token can be valid and still not belong to
this application: signed by the wrong key, issued by another team, minted for a
sibling app, past its expiry, or carrying an identity nobody approved.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[2]
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

TEAM = "example-team.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a" * 64
OWNER = "owner@youtab.io"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(scope="module")
def jwks(keypair):
    _private, public = keypair
    numbers = public.public_numbers()

    def b64(value: int) -> str:
        import base64

        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                      "n": b64(numbers.n), "e": b64(numbers.e)}]}


@pytest.fixture
def configured(monkeypatch, jwks):
    """Configured verifier with the JWKS fetch stubbed to our test key."""
    from youtab_agent_cli import access_jwt

    monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("YOUTAB_ACCESS_AUD", AUD)
    monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", f"{OWNER}, Admin@Youtab.io")
    monkeypatch.setattr(access_jwt, "_fetch_jwks", lambda url: jwks)
    access_jwt._jwks_cache.clear()
    return access_jwt


def _token(keypair, **overrides):
    private, _public = keypair
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUD,
        "email": OWNER,
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now + 600,
        "sub": "subject-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": KID})


# --- positive ---------------------------------------------------------------


def test_a_correctly_signed_token_for_this_app_is_accepted(configured, keypair):
    assert configured.require_access_identity(_token(keypair)) == OWNER


def test_an_approved_email_is_matched_case_insensitively(configured, keypair):
    """Identity providers do not agree on case; the approval list must not care."""
    assert configured.require_access_identity(_token(keypair, email="ADMIN@youtab.io")) == "admin@youtab.io"


# --- negative: the token is wrong -------------------------------------------


def test_a_token_for_another_application_is_refused(configured, keypair):
    """Wrong `aud`: valid for a sibling app in the same Access account.

    Without an audience check this would be accepted, and every other
    application in the account would become a way in here.
    """
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, aud="b" * 64))


def test_a_token_from_another_team_is_refused(configured, keypair):
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, iss="https://other.cloudflareaccess.com"))


def test_an_expired_token_is_refused(configured, keypair):
    now = int(time.time())
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, exp=now - 5, iat=now - 100, nbf=now - 100))


def test_a_not_yet_valid_token_is_refused(configured, keypair):
    now = int(time.time())
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, nbf=now + 600, exp=now + 1200))


def test_a_forged_signature_is_refused(configured, keypair):
    """Signed by a key the team never published."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {"iss": ISSUER, "aud": AUD, "email": OWNER, "iat": now, "nbf": now, "exp": now + 600},
        other,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(forged)


def test_an_unsigned_token_is_refused(configured, keypair):
    """`alg: none`. The algorithm is pinned, not read from the header."""
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUD, "email": OWNER, "iat": now, "exp": now + 600},
        key="",
        algorithm="none",
    )
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(unsigned)


@pytest.mark.parametrize("token", [None, "", "   ", "not-a-jwt", "a.b.c"])
def test_a_missing_or_malformed_token_is_refused(configured, token):
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(token)


# --- negative: the identity is wrong ----------------------------------------


def test_an_unauthorized_email_is_refused(configured, keypair):
    """A perfectly valid Access token for somebody who is not approved.

    Access can be configured to admit a wider set than this deployment should
    have; the approval list is the origin's own decision.
    """
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, email="stranger@example.com"))


def test_a_token_without_an_email_claim_is_refused(configured, keypair):
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(_token(keypair, email=""))


# --- negative: configuration ------------------------------------------------


@pytest.mark.parametrize(
    ("team", "aud"),
    [("", AUD), (TEAM, ""), ("", ""), ("https://" + TEAM, AUD)],
)
def test_incomplete_configuration_refuses_rather_than_admits(monkeypatch, keypair, team, aud):
    """Absent or malformed configuration must be a refusal, not a bypass."""
    from youtab_agent_cli import access_jwt

    monkeypatch.setenv("YOUTAB_ACCESS_TEAM_DOMAIN", team)
    monkeypatch.setenv("YOUTAB_ACCESS_AUD", aud)
    monkeypatch.setenv("YOUTAB_ACCESS_ALLOWED_EMAILS", OWNER)
    with pytest.raises(access_jwt.AccessDenied):
        access_jwt.require_access_identity(_token(keypair))


def test_the_issuer_and_audience_come_from_configuration_not_the_token(configured, keypair):
    """A token cannot nominate its own issuer.

    If the issuer were read from the token, an attacker could point it at a
    JWKS endpoint they control and sign anything.
    """
    config = configured.load_config()
    assert config.issuer == ISSUER
    assert config.jwks_url == f"{ISSUER}/cdn-cgi/access/certs"
    assert config.audience == AUD


# --- nothing sensitive is logged --------------------------------------------


def test_no_token_or_claim_reaches_the_log(configured, keypair, caplog):
    """A refusal must not record the credential it refused.

    Logs travel further than the request does.
    """
    import logging

    token = _token(keypair, email="stranger@example.com")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(configured.AccessDenied):
        configured.require_access_identity(token)

    recorded = " ".join(record.getMessage() for record in caplog.records)
    assert token not in recorded, "the assertion itself was logged"
    for fragment in token.split("."):
        if len(fragment) > 16:
            assert fragment not in recorded, "part of the assertion was logged"
    assert "stranger@example.com" not in recorded, "a claim value was logged"


def test_the_refusal_message_carries_no_claim_values(configured, keypair):
    """The exception text is for the operator log; it must stay opaque."""
    try:
        configured.require_access_identity(_token(keypair, email="stranger@example.com"))
    except configured.AccessDenied as exc:
        assert "stranger@example.com" not in str(exc)
    else:  # pragma: no cover
        pytest.fail("an unapproved identity was accepted")
