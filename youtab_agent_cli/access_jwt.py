"""Origin-side validation of Cloudflare Access identity.

Reaching the Access login page proves the *edge* is enforcing. It proves
nothing about this process: anything that can talk to the origin socket still
reaches the app, and an origin that trusts "a request arrived, therefore
Cloudflare let it through" is trusting the network rather than a credential.

Cloudflare signs an assertion for every request it forwards and puts it in
``Cf-Access-Jwt-Assertion``. Verifying that signature here is what turns edge
enforcement into an origin-side authorization decision. The AOP client
certificate proves the *connection* came from Cloudflare; this proves *which
identity* Cloudflare authenticated.

This is the protected pre-production boundary, not the product's identity
system. Youtab SSO remains the eventual contract; this gates access to a
deployment that is not open yet.

Configuration is environment-only:

``YOUTAB_ACCESS_TEAM_DOMAIN``
    e.g. ``example.cloudflareaccess.com``. Determines the issuer and the JWKS
    URL; nothing is discovered from the token itself, because a token that
    chose its own issuer could name a key server it controls.
``YOUTAB_ACCESS_AUD``
    The Application Audience tag. Scopes the token to *this* application --
    without it, a valid token for any other app in the same Access account
    would be accepted here.
``YOUTAB_ACCESS_ALLOWED_EMAILS``
    Comma-separated. The identities mapped to the internal Owner principal.

Every failure path returns the same opaque refusal and logs no token, no
claim values and no cookie.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

ACCESS_JWT_HEADER = "cf-access-jwt-assertion"

#: Cloudflare Access signs with RS256. Pinned rather than read from the token:
#: honouring the header's `alg` is how `none` and HMAC-confusion attacks work.
_ALLOWED_ALGORITHMS = ["RS256"]

_JWKS_TTL_SECONDS = 900
_JWKS_TIMEOUT_SECONDS = 5

_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class AccessDenied(Exception):
    """Refusal. The message is for the log, never for the response body."""


@dataclass(frozen=True)
class AccessConfig:
    team_domain: str
    audience: str
    allowed_emails: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def jwks_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"


def load_config() -> Optional[AccessConfig]:
    """Configuration from the environment, or ``None`` when incomplete.

    ``None`` is not "allow" -- :func:`require_access_identity` treats missing
    configuration on an external request as a refusal. Returning ``None``
    rather than raising keeps loopback operation working on a box where Access
    is irrelevant.
    """
    team = (os.environ.get("YOUTAB_ACCESS_TEAM_DOMAIN") or "").strip().lower()
    aud = (os.environ.get("YOUTAB_ACCESS_AUD") or "").strip()
    emails_raw = os.environ.get("YOUTAB_ACCESS_ALLOWED_EMAILS") or ""
    emails = frozenset(e.strip().lower() for e in emails_raw.split(",") if e.strip())
    if not team or not aud:
        return None
    if team.startswith("http"):  # a URL where a hostname belongs
        return None
    return AccessConfig(team_domain=team, audience=aud, allowed_emails=emails)


def _fetch_jwks(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=_JWKS_TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def get_jwks(config: AccessConfig, *, force: bool = False) -> dict[str, Any]:
    """The team's signing keys, cached briefly.

    Cached because this runs per request and Cloudflare rotates keys on the
    order of weeks; short enough that a rotation heals without a restart.
    """
    now = time.time()
    with _jwks_lock:
        entry = _jwks_cache.get(config.jwks_url)
        if entry and not force and (now - entry[0]) < _JWKS_TTL_SECONDS:
            return entry[1]
    jwks = _fetch_jwks(config.jwks_url)
    with _jwks_lock:
        _jwks_cache[config.jwks_url] = (now, jwks)
    return jwks


def _signing_key(token: str, config: AccessConfig, *, force_refresh: bool = False):
    import jwt
    from jwt import PyJWKClient  # noqa: F401  (import guarded for availability)

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        # Malformed input must fail closed as a refusal. Letting the library
        # exception escape would surface as a 500 from the middleware instead
        # of a 403, which turns a rejected credential into an error page and
        # tells the caller they found something that broke.
        raise AccessDenied(f"assertion is not a readable token ({type(exc).__name__})") from None
    kid = header.get("kid")
    if not kid:
        raise AccessDenied("token header carries no kid")
    if header.get("alg") not in _ALLOWED_ALGORITHMS:
        raise AccessDenied("unexpected token algorithm")

    jwks = get_jwks(config, force=force_refresh)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return jwt.PyJWK(key).key
    if not force_refresh:
        # A key we have not seen may simply be a rotation; refetch once.
        return _signing_key(token, config, force_refresh=True)
    raise AccessDenied("no signing key matches the token kid")


def verify_access_token(token: str, config: AccessConfig) -> dict[str, Any]:
    """Return the verified claims, or raise :class:`AccessDenied`.

    Signature, issuer, audience, ``exp`` and ``nbf`` are all checked. The
    issuer and audience come from configuration, never from the token.
    """
    import jwt

    if not token or not token.strip():
        raise AccessDenied("no assertion header")

    key = _signing_key(token, config)
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=config.audience,
            issuer=config.issuer,
            options={"require": ["exp", "iat", "iss", "aud"], "verify_nbf": True},
        )
    except jwt.PyJWTError as exc:
        # The exception type is safe to record; its string can embed claim
        # values on some paths, so only the class name is logged.
        raise AccessDenied(f"assertion rejected ({type(exc).__name__})") from None
    return claims


def principal_email(claims: dict[str, Any], config: AccessConfig) -> str:
    """The approved identity this token maps to, or raise.

    Only an exact, configured address becomes the internal Owner principal. The
    email is taken from the *verified* claims -- never from a header a client
    could set, which is the whole reason this function takes claims rather than
    a request.
    """
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise AccessDenied("assertion carries no email claim")
    if email not in config.allowed_emails:
        raise AccessDenied("identity is not on the approved list")
    return email


def require_access_identity(token: Optional[str]) -> str:
    """Full check for an external request. Returns the approved email.

    Fails closed on every branch: absent configuration, absent token, bad
    signature, wrong issuer, wrong audience, expired, or an identity that is
    not approved.
    """
    config = load_config()
    if config is None:
        raise AccessDenied("Access validation is not configured")
    claims = verify_access_token(token or "", config)
    return principal_email(claims, config)
