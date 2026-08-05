"""Cloudflare Access as a registered dashboard-auth provider.

See `docs/architecture/ADR-0001-authentication-and-trust-boundary.md`.

The origin already verifies the assertion Cloudflare signs for every request it
forwards (:mod:`youtab_agent_cli.access_jwt`). What it did not do was produce a
*session*, so the dashboard gate had no provider and failed closed on any
non-loopback bind — which is every bind, now that the container sits on a
private bridge.

This closes that gap by reusing the verifier rather than re-implementing it.
Everything the verifier already enforces — RS256 pinned rather than read from
the token header, issuer and audience taken from configuration, JWKS with a
bounded cache, `exp` and `nbf`, and an exact address allowlist — applies here
unchanged, because this module calls it instead of copying it.

Two things it deliberately does not do
--------------------------------------
**It never trusts a header.** Cloudflare also sets
``Cf-Access-Authenticated-User-Email``, and reading that would be a one-line
provider. It would also be worthless: this origin's address is public, so
anything that can open a socket to it can send that header. Only the verified
assertion is a credential.

**It never reads authority from a claim.** Access proves *who*. The roster in
:mod:`youtab_agent_cli.authz` decides *what they may do*. An identity provider
that could also grant itself Owner scope would not be an identity provider.
"""

from __future__ import annotations

import logging
from typing import Optional

from youtab_agent_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)

_log = logging.getLogger(__name__)

#: Cloudflare mints and renews the assertion; Youtab does not control its
#: lifetime. Sessions therefore carry the token's own ``exp``.
PROVIDER_NAME = "cloudflare-access"


class CloudflareAccessProvider(DashboardAuthProvider):
    """Turns a verified Access assertion into a Youtab session.

    There is no interactive leg. Access has already authenticated the person
    before the request reaches this process, so ``start_login`` has nothing to
    redirect to that Cloudflare has not already done -- the browser cannot
    arrive here at all without passing the edge.
    """

    name = PROVIDER_NAME
    display_name = "Cloudflare Access"

    def __init__(self, verifier=None) -> None:
        # Injected for tests so the real verifier's behaviour is exercised
        # rather than stubbed; production passes nothing and gets the module.
        if verifier is None:
            from youtab_agent_cli import access_jwt as verifier  # noqa: PLC0415
        self._verifier = verifier

    # -- interactive login: not applicable -------------------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        """Access has no in-app login leg.

        Raising rather than returning a redirect is deliberate: a plausible
        redirect here would be a second, weaker way in, and the only correct
        answer to "log me in" is that the edge already did or the request
        would not exist.
        """
        raise ProviderError(
            "Cloudflare Access authenticates at the edge; there is no in-app login"
        )

    def complete_login(self, **kwargs):
        raise ProviderError("Cloudflare Access has no authorization-code exchange")

    # -- the real path ----------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        """Verify an Access assertion and map it to a Youtab identity.

        Returns ``None`` on every failure rather than raising, because the
        gate's contract is "``None`` means not authenticated" and a provider
        that raised would turn a refused credential into a 500 -- telling the
        caller they found something that breaks.

        Nothing about the token, the claims or the address is logged. A
        refusal must not record the credential it refused; logs travel further
        than the request does.
        """
        if not access_token or not access_token.strip():
            return None
        try:
            email = self._verifier.require_access_identity(access_token)
        except self._verifier.AccessDenied as exc:
            # The exception type is safe; its string can embed claim values on
            # some paths, so only the class name is recorded.
            _log.warning("access assertion refused (%s)", type(exc).__name__)
            return None
        except Exception:  # pragma: no cover - defensive
            _log.exception("access verification failed")
            return None

        claims = self._claims(access_token)
        return Session(
            # `sub` is Cloudflare's stable per-identity id; the address is the
            # fallback because the roster is keyed by something an operator can
            # actually write down.
            user_id=str(claims.get("sub") or email),
            email=email,
            display_name=email,
            # Access carries no Youtab tenant. Owner/Superadmin are
            # Youtab-internal and org-less by construction in the roster.
            org_id="",
            provider=self.name,
            expires_at=int(claims.get("exp") or 0),
            access_token=access_token,
            refresh_token="",
        )

    def _claims(self, token: str) -> dict:
        """Verified claims, or an empty mapping.

        Called only after ``require_access_identity`` has already accepted the
        token, so this is a second verified decode rather than a bare one --
        there is no path here that reads an unverified claim.
        """
        try:
            config = self._verifier.load_config()
            if config is None:
                return {}
            return self._verifier.verify_access_token(token, config)
        except Exception:
            return {}

    def refresh_session(self, *, refresh_token: str) -> Session:
        """Cloudflare renews its own assertion; Youtab cannot refresh one.

        The browser gets a fresh assertion from the edge on the next request,
        so the correct behaviour is to expire and re-verify rather than to
        mint a longer-lived Youtab session on top of a dead Access one.
        """
        raise RefreshExpiredError(
            "Cloudflare Access assertions are renewed by the edge, not refreshed here"
        )

    def revoke_session(self, *, refresh_token: str) -> None:
        """Local logout only.

        Youtab cannot end a Cloudflare Access session; that is the edge's to
        revoke. Clearing the Youtab cookie is what this can honestly do, and
        pretending otherwise would report a revocation that did not happen.
        """
        return None


def register_if_configured() -> bool:
    """Register the provider when Access is configured. Returns whether it was.

    Absent configuration is not an error and must not be: a loopback developer
    bind needs no provider, and `should_require_auth` already leaves that
    ungated. On a gated bind, a missing registration means the gate fails
    closed -- which is the correct direction, and is why this returns a boolean
    the caller can log rather than raising.
    """
    from youtab_agent_cli import access_jwt
    from youtab_agent_cli.dashboard_auth import register_provider

    if access_jwt.load_config() is None:
        _log.info("Cloudflare Access is not configured; provider not registered")
        return False
    register_provider(CloudflareAccessProvider())
    _log.info("Cloudflare Access registered as a dashboard-auth provider")
    return True
