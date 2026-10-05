"""Read-only profile inference credential policy, independent of CLI orchestration.

The active profile is authoritative; no global credential or OAuth fallback.
Validation here checks resource admission and expiry; the Gateway verifies signatures.
"""
from __future__ import annotations
import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from youtab_agent_cli.auth_errors import AuthError
from youtab_agent_cli.auth_storage import _auth_file_path, _auth_lock_holder_for, _file_lock, read_auth_document

DEFAULT_YOUTAB_PORTAL_URL = "https://api.youtab.io"
DEFAULT_YOUTAB_INFERENCE_URL = "https://api.youtab.io/v1"
YOUTAB_INFERENCE_INVOKE_SCOPE = "inference:invoke"
YOUTAB_INVOKE_JWT_MIN_TTL_SECONDS = 120

def _parse_iso_timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_expiring(expires_at_iso: Any, skew_seconds: int) -> bool:
    expires_epoch = _parse_iso_timestamp(expires_at_iso)
    if expires_epoch is None:
        return True
    return expires_epoch <= (time.time() + skew_seconds)


def _coerce_ttl_seconds(expires_in: Any) -> int:
    try:
        ttl = int(expires_in)
    except Exception:
        ttl = 0
    return max(0, ttl)


def _optional_base_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().rstrip("/")
    return cleaned if cleaned else None


def _youtab_portal_env_override() -> Optional[str]:
    """Return the user/deployment-set Portal base URL override, if any.

    Mirrors ``_youtab_inference_env_override()``: ``YOUTAB_AGENT_PORTAL_BASE_URL`` /
    ``YOUTAB_PORTAL_BASE_URL`` are the documented dev/staging escape hatch for
    pointing Youtab at a non-production Youtab Portal (e.g. a hosted agent
    provisioned on youtab-account-service's `staging` environment, which stamps
    ``YOUTAB_AGENT_PORTAL_BASE_URL=https://portal.staging-youtab.io`` into
    the container env). The env source is trusted (the OS user/deployment
    set it themselves), so — like the inference override — it must NOT be
    gated by ``_YOUTAB_PORTAL_ALLOWED_HOSTS``: that allowlist exists to reject
    an untrusted NETWORK-provided value (a poisoned portal_base_url
    persisted to auth.json), not a value the operator explicitly configured.

    Returns a trailing-slash-stripped non-empty string, or ``None`` when
    neither env var is set/blank.
    """
    value = _optional_base_url(os.getenv("YOUTAB_AGENT_PORTAL_BASE_URL")) or _optional_base_url(
        os.getenv("YOUTAB_PORTAL_BASE_URL")
    )
    if value is None:
        return None
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as exc:
        raise AuthError("Invalid configured Youtab Portal URL.", provider="youtab") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AuthError("Invalid configured Youtab Portal URL.", provider="youtab")
    return value


def _decode_jwt_claims(token: Any) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _scope_values(raw_scope: Any) -> set[str]:
    # OAuth token responses normally return a space-separated string. Keep
    # collection support for JWT ``scp`` claims and older stored test fixtures.
    scopes: set[str] = set()
    if isinstance(raw_scope, str):
        for part in raw_scope.replace(",", " ").split():
            cleaned = part.strip()
            if cleaned:
                scopes.add(cleaned)
    elif isinstance(raw_scope, (list, tuple, set, frozenset)):
        for item in raw_scope:
            if isinstance(item, str):
                scopes.update(_scope_values(item))
    return scopes


def _youtab_invoke_jwt_status(
    token: Any,
    *,
    scope: Any = None,
    expires_at: Any = None,
    min_ttl_seconds: int = YOUTAB_INVOKE_JWT_MIN_TTL_SECONDS,
) -> Optional[str]:
    """Return None when the token can be used for inference, else a reason."""
    claims = _decode_jwt_claims(token)
    if not claims:
        return "access_token_not_jwt"
    scopes = (
        _scope_values(scope)
        | _scope_values(claims.get("scope"))
        | _scope_values(claims.get("scp"))
    )
    if YOUTAB_INFERENCE_INVOKE_SCOPE not in scopes:
        return "missing_inference_invoke_scope"
    exp = claims.get("exp")
    skew = max(0, int(min_ttl_seconds))
    if isinstance(exp, (int, float)):
        if float(exp) <= (time.time() + skew):
            return "invoke_jwt_expiring"
        return None
    if _is_expiring(expires_at, skew):
        return "invoke_jwt_expiry_unknown_or_expiring"
    return None


def _agent_key_is_usable(state: Dict[str, Any], min_ttl_seconds: int) -> bool:
    key = state.get("agent_key")
    if not isinstance(key, str) or not key.strip():
        return False
    return _youtab_invoke_jwt_is_usable(
        key,
        scope=state.get("scope"),
        expires_at=state.get("agent_key_expires_at"),
        min_ttl_seconds=max(0, int(min_ttl_seconds)),
    )


def inference_token_safety_seconds(state: Dict[str, Any]) -> int:
    """Use a short clock margin bounded by this credential's own lifetime."""
    lifetime = _coerce_ttl_seconds(state.get("agent_key_expires_in"))
    if not lifetime:
        obtained = _parse_iso_timestamp(state.get("agent_key_obtained_at"))
        expires = _parse_iso_timestamp(state.get("agent_key_expires_at"))
        if obtained is not None and expires is not None:
            lifetime = max(0, int(expires - obtained))
    if not lifetime:
        claims = _decode_jwt_claims(state.get("agent_key"))
        issued, expires = claims.get("iat"), claims.get("exp")
        if isinstance(issued, (int, float)) and isinstance(expires, (int, float)):
            lifetime = max(0, int(expires - issued))
    if not lifetime:
        return 0
    return min(max(1, lifetime // 10), 30, max(0, lifetime - 1))


def _is_profile_inference_token(token: str) -> bool:
    """Check the dedicated resource profile before exposing a stored bearer."""
    claims = _decode_jwt_claims(token)
    issuer = claims.get("iss")
    return bool(
        claims.get("type") == "inference_access"
        and isinstance(issuer, str)
        and issuer.startswith("https://")
        and claims.get("aud") == f"{issuer.rstrip('/')}/v1/inference"
        and _youtab_invoke_jwt_status(token, min_ttl_seconds=0) is None
    )


def profile_inference_base_url(state: Dict[str, Any]) -> str:
    """Bind a profile inference bearer to the configured native Gateway."""
    portal = _youtab_portal_env_override() or DEFAULT_YOUTAB_PORTAL_URL
    issuer = _decode_jwt_claims(state.get("agent_key")).get("iss")
    if issuer != portal:
        raise AuthError(
            "Profile inference credential does not match the configured Gateway.",
            provider="youtab",
            code="profile_inference_authority_mismatch",
        )
    return f"{portal}/v1"


def _profile_inference_store_key() -> str:
    # Both root and named profiles may already hold legacy CLI Youtab auth.
    return "youtab_inference"


def _youtab_invoke_jwt_is_usable(token: Any, *, scope: Any = None, expires_at: Any = None, min_ttl_seconds: int = YOUTAB_INVOKE_JWT_MIN_TTL_SECONDS) -> bool:
    return _youtab_invoke_jwt_status(token, scope=scope, expires_at=expires_at, min_ttl_seconds=min_ttl_seconds) is None

def profile_inference_state(store: dict) -> Optional[Dict[str, Any]]:
    providers = store.get("providers")
    state = providers.get(_profile_inference_store_key()) if isinstance(providers, dict) else None
    if isinstance(state, dict) and "agent_key" in state and not state.get("access_token") and not state.get("refresh_token"):
        local = dict(state)
        if local.get("agent_key") and not _is_profile_inference_token(local["agent_key"]):
            local["agent_key"] = ""  # Preserve logout/invalid-credential shadow.
        return local
    return None

def get_local_inference_token_state() -> Optional[Dict[str, Any]]:
    """Read only the locked active-profile inference entry."""
    path = _auth_file_path()
    with _file_lock(path.with_suffix(".lock"), _auth_lock_holder_for(path), 15.0, "Timed out waiting for auth store lock"):
        return profile_inference_state(read_auth_document(path))


def youtab_api_mode(model: str = "") -> str:
    """Reject Messages models on the Gateway profile; preserve hosted legacy wire."""
    if str(model or "").strip().lower().startswith(("anthropic/", "anthropic.")):

        if os.environ.get("YOUTAB_AGENT_DESKTOP") == "1" or get_local_inference_token_state() is not None:
            raise ValueError("Youtab Gateway does not support /v1/messages for anthropic/* models")
        return "anthropic_messages"
    return "chat_completions"
