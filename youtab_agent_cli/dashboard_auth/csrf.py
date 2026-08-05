"""CSRF protection for the assertion-authenticated browser flow.

Cloudflare Access does not remove the need for this, and the reason is worth
stating plainly because the opposite is a tempting conclusion: the assertion is
*ambient*. The browser attaches it to every request to the protected hostname,
including a request some other site caused. An attacker's page cannot read the
assertion, but it does not need to -- it only needs the browser to send it, and
the browser will. That is exactly the property session cookies have, and
exactly why CSRF exists.

So the defence is the standard one for ambient credentials, and it is two
independent checks rather than one:

1. **Exact origin.** A state-changing request must declare an ``Origin`` (or
   failing that a ``Referer``) that matches the configured public origin
   exactly -- scheme, host and port. A missing, foreign, malformed or
   downgraded origin is refused. This alone stops the ordinary cross-site form
   post, which cannot forge the header.
2. **A token the attacker cannot obtain.** Minted for a verified principal and
   returned only to that principal, so a cross-origin page cannot read it (the
   response is not CORS-readable) and cannot guess it. Presented in a custom
   header, which a simple form post cannot set at all.

Either check alone has a gap. Origin is absent on some legitimate same-origin
navigations in older engines and can be stripped by an intermediary; a token
alone is defeated by anything that can read a response. Together they close
each other's.

Tokens are single-use with a short TTL, so a captured token cannot be replayed.
The store is in-memory and per-process, which is correct for a single-container
control plane and is a constraint to revisit if the dashboard is ever scaled
horizontally -- recorded here rather than discovered later.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from hashlib import sha256
from typing import Optional

#: How long a minted token stays usable. Long enough for a form the user is
#: filling in, short enough that a leaked one is stale before it travels.
TTL_SECONDS = 900

#: Header the SPA presents the token in. A custom header is load-bearing, not
#: cosmetic: a cross-site form post cannot set one, and a fetch that tries is
#: subject to preflight.
CSRF_HEADER = "x-youtab-csrf"

#: Methods that change state and therefore require proof.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_lock = threading.Lock()
#: nonce -> (user_id, expires_at). Single-use: consuming removes the entry.
_issued: dict[str, tuple[str, float]] = {}

_SECRET_ENV = "YOUTAB_CSRF_SECRET"
_process_secret = secrets.token_bytes(32)


def _secret() -> bytes:
    """Signing key. From the environment when supplied, else per-process.

    A per-process key is not a weakness here: tokens are single-use and
    validated against an in-memory store in the same process, so a key that
    dies with the process invalidates exactly the tokens that process issued.
    """
    configured = os.environ.get(_SECRET_ENV, "")
    return configured.encode("utf-8") if configured else _process_secret


def _sign(nonce: str, user_id: str) -> str:
    return hmac.new(
        _secret(), f"{nonce}:{user_id}".encode("utf-8"), sha256
    ).hexdigest()[:32]


def _prune(now: float) -> None:
    for nonce, (_uid, exp) in list(_issued.items()):
        if exp <= now:
            _issued.pop(nonce, None)


def mint(user_id: str) -> str:
    """Issue a single-use token bound to ``user_id``."""
    if not user_id:
        raise ValueError("a CSRF token must be bound to a principal")
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _prune(now)
        _issued[nonce] = (user_id, now + TTL_SECONDS)
    return f"{nonce}.{_sign(nonce, user_id)}"


def consume(token: str, user_id: str) -> bool:
    """Validate and burn a token. False on anything that is not exactly right.

    Checks the signature before the store so a forged token is rejected on its
    own merits, and compares with :func:`hmac.compare_digest` so the answer
    does not leak through timing.
    """
    if not token or not user_id:
        return False
    nonce, _, provided = token.partition(".")
    if not nonce or not provided:
        return False
    if not hmac.compare_digest(provided, _sign(nonce, user_id)):
        return False
    now = time.time()
    with _lock:
        _prune(now)
        entry = _issued.pop(nonce, None)   # single use: gone once consumed
    if entry is None:
        return False
    bound_user, expires_at = entry
    if expires_at <= now:
        return False
    return hmac.compare_digest(bound_user, user_id)


# --- origin -----------------------------------------------------------------


def _origin_of(url: str) -> Optional[str]:
    """``scheme://host[:port]`` of a URL, or ``None`` if it is not usable."""
    import urllib.parse

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def origin_is_trusted(
    origin_header: str, referer_header: str, expected_url: str
) -> bool:
    """Does this browser request declare the expected origin, exactly?

    ``Origin`` is preferred; ``Referer`` is the fallback for the engines that
    omit Origin on same-origin navigations. Both are compared whole -- scheme,
    host and port together -- because comparing the host alone would accept
    ``http://`` where ``https://`` is expected, and comparing a prefix would
    accept ``https://agent.youtab.io.evil.test``.

    A request declaring neither is refused. "No origin" is not evidence of
    same-origin; it is absence of evidence, and this is a state-changing
    request.
    """
    expected = _origin_of(expected_url)
    if expected is None:
        # Nothing to compare against. Refusing is the only safe answer: it
        # cannot be verified, so it is not verified.
        return False
    declared = (origin_header or "").strip()
    if declared and declared.lower() != "null":
        return _origin_of(declared) == expected
    referer = (referer_header or "").strip()
    if referer:
        return _origin_of(referer) == expected
    return False
