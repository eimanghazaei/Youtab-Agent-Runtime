"""Youtab-managed Camofox state helpers.

Provides profile-scoped identity and state directory paths for Camofox
persistent browser profiles.  When managed persistence is enabled, Youtab
sends a deterministic userId derived from the active profile so that
Camofox can map it to the same persistent browser profile directory
across restarts.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Dict, Optional

from youtab_constants import get_youtab_home

CAMOFOX_STATE_DIR_NAME = "browser_auth"
CAMOFOX_STATE_SUBDIR = "camofox"

# A managed run carries the gateway-verified principal in the worker's
# environment: ``kanban_db.build_worker_invocation`` exports the tenant and the
# owning user alongside the task and workspace. Browser state must follow that
# principal.
#
# Why: the Youtab home is scoped by AGENT PROFILE (``profile_arg =
# normalize_profile_name(task.assignee)``), not by tenant. Without the scoping
# below, every tenant running the same agent resolves the SAME
# ``browser_auth/camofox`` directory, so the moment
# ``browser.camofox.managed_persistence`` is enabled one user's logged-in cookies
# live in the profile the next user's run adopts — a cross-tenant authentication
# leak. That flag defaults to False, so this is a latent defect rather than a
# live one; it is fixed here so enabling persistence is safe by construction
# rather than a footgun.
#
# This deliberately does NOT tenant-scope ``YOUTAB_AGENT_HOME`` itself: that home
# also anchors the kanban board and the profile's config.yaml (see the warning in
# ``kanban_db``), so forking it per tenant would fork board and configuration
# too. Only the thing that actually leaks is scoped.
_TENANT_ENV = "YOUTAB_AGENT_TENANT"
_USER_ENV = "YOUTAB_AGENT_KANBAN_CREATED_BY"


def _principal_scope() -> Optional[str]:
    """A stable, filesystem-safe directory segment for the run's principal.

    Returns ``None`` when neither identifier is present — a desktop / single-user
    installation, where there is no second principal to isolate from. That path
    keeps the historical directory exactly as it was, so existing profiles and
    logged-in sessions survive this change untouched.

    The segment is a UUIDv5 digest rather than the raw ids: tenant and user ids
    may contain characters that are unsafe or length-limited in a path, and a
    digest also keeps customer identifiers out of on-disk paths and any log or
    crash dump that quotes them.
    """
    tenant = (os.environ.get(_TENANT_ENV) or "").strip()
    user = (os.environ.get(_USER_ENV) or "").strip()
    if not tenant and not user:
        return None
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-principal:{tenant}:{user}").hex[:16]
    return f"p_{digest}"


def get_camofox_state_dir() -> Path:
    """Return the state root for Camofox persistence, scoped to the principal.

    Profile-scoped as before, and additionally scoped to the (tenant, user) of a
    managed run so two principals never share one persistent browser profile.
    """
    root = get_youtab_home() / CAMOFOX_STATE_DIR_NAME / CAMOFOX_STATE_SUBDIR
    scope = _principal_scope()
    return root / scope if scope else root


def get_camofox_identity(task_id: Optional[str] = None) -> Dict[str, str]:
    """Return the stable Youtab-managed Camofox identity for this profile.

    The user identity is scoped to the state directory, which is the Youtab
    profile AND — inside a managed run — the (tenant, user) principal. So the
    identity is stable for one principal across restarts, and two principals
    never resolve to the same userId even when they run the same agent.
    The session key is scoped to the logical browser task so newly created
    tabs within the same profile reuse the same identity contract.
    """
    scope_root = str(get_camofox_state_dir())
    logical_scope = task_id or "default"
    user_digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"camofox-user:{scope_root}",
    ).hex[:10]
    session_digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"camofox-session:{scope_root}:{logical_scope}",
    ).hex[:16]
    return {
        "user_id": f"youtab_{user_digest}",
        "session_key": f"task_{session_digest}",
    }
