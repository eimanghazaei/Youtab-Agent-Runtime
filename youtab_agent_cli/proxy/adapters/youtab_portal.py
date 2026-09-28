"""Youtab upstream adapter using the selected profile's Gateway bearer.

Existing non-Desktop Portal sessions retain their legacy resolver. Desktop
profile inference tokens stay local and are refreshed only by Electron main.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, FrozenSet, Optional

from youtab_agent_cli.auth import (
    AuthError,
    DEFAULT_YOUTAB_INFERENCE_URL,
    _load_auth_store,
    _auth_store_lock,
    _agent_key_is_usable,
    _is_terminal_youtab_refresh_error,
    _youtab_inference_env_override,
    _quarantine_youtab_oauth_state,
    _quarantine_youtab_pool_entries,
    _save_auth_store,
    _validate_youtab_inference_url_from_network,
    _write_shared_youtab_state,
    get_local_inference_token_state,
    inference_token_safety_seconds,
    resolve_youtab_runtime_credentials,
)
from youtab_agent_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# Legacy non-Desktop upstream routes. The Gateway Desktop contract below is
# limited to the two routes accepted by its inference-token resource.
_ALLOWED_PATHS: FrozenSet[str] = frozenset(
    {
        "/chat/completions",
        "/completions",
        "/embeddings",
        "/models",
    }
)
_GATEWAY_PATHS: FrozenSet[str] = frozenset({"/chat/completions", "/models"})


class YoutabPortalAdapter(UpstreamAdapter):
    """Proxy upstream for the Youtab Portal inference API."""

    def __init__(self) -> None:
        # Serialize proxy requests in this process; cross-process token refresh
        # and persistence are handled by resolve_youtab_runtime_credentials().
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "youtab"

    @property
    def display_name(self) -> str:
        return "Youtab Portal"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        if get_local_inference_token_state() is not None or os.environ.get("YOUTAB_AGENT_DESKTOP") == "1":
            return _GATEWAY_PATHS
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        local = get_local_inference_token_state()
        if local is not None:
            return _agent_key_is_usable(local, inference_token_safety_seconds(local))
        if os.environ.get("YOUTAB_AGENT_DESKTOP") == "1":
            return False
        state = self._read_state()
        if state is None:
            return False
        # We need either a usable inference JWT OR (refresh_token + access_token)
        # to recover. The refresh helper validates and refreshes as needed.
        return bool(
            state.get("agent_key")
            or (state.get("refresh_token") and state.get("access_token"))
        )

    def get_credential(self) -> UpstreamCredential:
        return self._get_credential()

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        _ = failed_credential
        if status_code != 401:
            return None
        if get_local_inference_token_state() is not None or os.environ.get("YOUTAB_AGENT_DESKTOP") == "1":
            # Desktop alone rotates this short-lived token. Never refresh it
            # through the legacy Portal OAuth path on a proxy retry.
            return None
        logger.info("proxy: Youtab upstream rejected bearer; force-refreshing invoke JWT")
        return self._get_credential(
            force_refresh=True,
        )

    def _get_credential(
        self,
        *,
        force_refresh: bool = False,
    ) -> UpstreamCredential:
        with self._lock:
            local = get_local_inference_token_state()
            if local is not None:
                if force_refresh or not _agent_key_is_usable(
                    local, inference_token_safety_seconds(local)
                ):
                    raise RuntimeError("Profile inference credential is unavailable; Desktop sign-in is required")
                return UpstreamCredential(
                    bearer=local["agent_key"],
                    base_url=DEFAULT_YOUTAB_INFERENCE_URL,
                    expires_at=local.get("agent_key_expires_at"),
                )
            if os.environ.get("YOUTAB_AGENT_DESKTOP") == "1":
                raise RuntimeError("Desktop Gateway sign-in is required")
            state = self._read_state()
            if state is None:
                raise RuntimeError(
                    "Not logged into Youtab Portal. Run `youtab auth add youtab` first."
                )

            try:
                refreshed = resolve_youtab_runtime_credentials(
                    force_refresh=force_refresh,
                )
            except AuthError as exc:
                if _is_terminal_youtab_refresh_error(exc):
                    _quarantine_youtab_oauth_state(
                        state,
                        exc,
                        reason="proxy_refresh_failure",
                    )
                    self._save_state(
                        state,
                        quarantine_error=exc,
                        quarantine_reason="proxy_refresh_failure",
                    )
                raise RuntimeError(
                    f"Failed to refresh Youtab Portal credentials: {exc}"
                ) from exc
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to refresh Youtab Portal credentials: {exc}"
                ) from exc

            runtime_key = refreshed.get("api_key")
            if not runtime_key:
                raise RuntimeError(
                    "Youtab Portal refresh did not return a usable inference JWT. "
                    "Try `youtab auth add youtab` to re-authenticate."
                )

            # base_url returned by resolve_youtab_runtime_credentials() already
            # honors the YOUTAB_INFERENCE_BASE_URL env override (the documented
            # dev/staging escape hatch). Re-validating it here against the prod
            # host allowlist would wrongly reject a legitimate staging override,
            # so layer the same env-first overlay on top of the network-validated
            # value: env override wins, else validate the returned URL, else
            # fall back to the production default (defense-in-depth for a future
            # source-layer bypass).
            base_url = (
                _youtab_inference_env_override()
                or _validate_youtab_inference_url_from_network(refreshed.get("base_url"))
                or DEFAULT_YOUTAB_INFERENCE_URL
            )
            base_url = base_url.rstrip("/")

            return UpstreamCredential(
                bearer=runtime_key,
                base_url=base_url,
                expires_at=refreshed.get("expires_at"),
            )

    # ------------------------------------------------------------------
    # Internal helpers — auth.json access. Kept local rather than added
    # to youtab_agent_cli.auth to avoid expanding that module's public surface.
    # ------------------------------------------------------------------

    def _read_state(self) -> Optional[Dict[str, Any]]:
        try:
            with _auth_store_lock():
                store = _load_auth_store()
        except Exception as exc:
            logger.warning("proxy: failed to load auth store: %s", exc)
            return None
        providers = store.get("providers") or {}
        state = providers.get("youtab")
        if not isinstance(state, dict):
            return None
        return dict(state)  # copy so the refresh helper can mutate freely

    def _save_state(
        self,
        state: Dict[str, Any],
        *,
        quarantine_error: Optional[AuthError] = None,
        quarantine_reason: Optional[str] = None,
    ) -> None:
        try:
            with _auth_store_lock():
                store = _load_auth_store()
                if quarantine_error is not None and quarantine_reason:
                    _quarantine_youtab_pool_entries(
                        store,
                        quarantine_error,
                        reason=quarantine_reason,
                    )
                providers = store.setdefault("providers", {})
                providers["youtab"] = state
                _save_auth_store(store)
            _write_shared_youtab_state(state)
        except Exception as exc:
            logger.warning("proxy: failed to persist Youtab quarantine state: %s", exc)


__all__ = ["YoutabPortalAdapter"]
