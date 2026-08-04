"""Who may reach the bring-your-own-credential surface, decided server-side.

The product policy is that a normal user has no credential entry, no upstream
catalogue, no raw engine identifier and no setup path for any of them. The
part that matters is *where* that is decided. Hiding a nav entry states an
intention; it does not survive curl, a stale bundle, or a fork of the web app,
and every one of those reaches the same handler the nav entry would have. So
the decision lives here, in front of the routes, and the interface is only
allowed to agree with it.

Fail-closed, and deliberately so
--------------------------------
``byok_entitled()`` returns True for exactly one spelling of one variable and
False for everything else — unset, empty, ``"0"``, ``"false"``, a typo, or a
value that arrived from somewhere unexpected. The asymmetry is the point: a
deployment that *meant* to enable this surface can say so precisely, and every
other state, including a misconfigured one, keeps it shut.

The integration code behind these routes is kept rather than deleted. It is
private backend infrastructure and a disabled internal capability for a future
entitlement-controlled enterprise or on-premise offering. Kept is not the same
as reachable, and this module is the difference.

Why whole path prefixes rather than a per-handler decorator
-----------------------------------------------------------
A decorator protects the handlers someone remembered to decorate. The next
route added under one of these prefixes inherits the refusal here without
anyone remembering anything, which is the property that matters for a surface
whose whole risk is a forgotten entry point. The prefixes are listed
explicitly, with the sibling paths that deliberately stay open named alongside
them, so widening or narrowing this set is a reviewable edit rather than a
side effect.
"""

from __future__ import annotations

import os
from typing import Final

#: The one variable that opens the surface, and the one value that counts.
#: Named for the entitlement rather than for a feature flag, because that is
#: what it is: an operator asserting this deployment is permitted the
#: capability, not a preference someone toggles.
ENTITLEMENT_ENV: Final[str] = "YOUTAB_CREDENTIAL_ENTITLEMENT"

#: Compared exactly, after stripping surrounding whitespace only. No truthiness
#: coercion: ``"0"`` and ``"false"`` must not accidentally enable anything, and
#: a bare ``"1"`` should not either — enabling this is a deliberate sentence.
ENTITLEMENT_GRANTED: Final[str] = "granted"

#: Every request path under one of these is refused without an entitlement.
#: A prefix, not a pattern: a route added here later is covered on arrival.
#:
#: Sibling prefixes that are NOT here, and why:
#:   ``/api/analytics/models``  aggregate usage counts, no upstream identity
#:   ``/api/memory/providers/`` storage backends for recall, a different axis
#: Both are named so a future reader can see they were considered rather than
#: missed.
REFUSED_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/api/env",
    "/api/model/",
    "/api/providers/",
    "/api/credentials/",
)

#: What the caller is told. It states the decision and its scope and nothing
#: else: no upstream name, no list of what would otherwise have been available,
#: no hint about which spelling of which variable would change the answer.
#: A refusal that explains how to defeat itself is a worse refusal.
REFUSAL_DETAIL: Final[str] = (
    "This deployment does not offer user-supplied model credentials, "
    "upstream catalogues or raw engine selection."
)


def byok_entitled(environ: dict[str, str] | None = None) -> bool:
    """True only when this deployment is explicitly entitled to the surface.

    ``environ`` is injectable so the decision can be tested as a pure function
    against a constructed mapping, rather than by mutating the real process
    environment inside a test and hoping it is restored.
    """
    source = os.environ if environ is None else environ
    return source.get(ENTITLEMENT_ENV, "").strip() == ENTITLEMENT_GRANTED


def is_refused_path(path: str) -> bool:
    """True when ``path`` belongs to the entitlement-gated surface.

    Matching is on the raw request path. ``/api/env`` is matched as a prefix
    rather than an exact string on purpose, so ``/api/env/reveal`` and any
    future sibling are covered by the same line.
    """
    return path.startswith(REFUSED_PATH_PREFIXES)
