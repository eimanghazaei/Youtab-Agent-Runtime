"""Youtab deployment policy helpers.

The downstream production image is immutable. Dependencies, browser engines,
plugins, and updates are admitted during build/release; the running service
must not retrieve executable artifacts from the public internet.

The policy is opt-in so an unconfigured upstream developer checkout keeps its
original behaviour. Youtab production images set
``YOUTAB_RUNTIME_DOWNLOAD_POLICY=deny`` in their sealed environment.
"""

from __future__ import annotations

import os


RUNTIME_DOWNLOAD_POLICY_ENV = "YOUTAB_RUNTIME_DOWNLOAD_POLICY"


def runtime_artifact_downloads_denied() -> bool:
    """Return ``True`` when executable artifact downloads are prohibited."""

    return os.environ.get(RUNTIME_DOWNLOAD_POLICY_ENV, "").strip().lower() in {
        "deny",
        "denied",
        "disabled",
        "off",
    }


def runtime_download_denial_reason() -> str:
    """Stable user-facing explanation for managed immutable deployments."""

    return (
        "runtime artifact downloads are disabled by Youtab policy; "
        "the dependency must be pre-baked from the approved internal mirrors"
    )
