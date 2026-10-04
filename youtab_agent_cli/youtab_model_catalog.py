"""Youtab model catalog HTTP transport, independent of login orchestration."""
from __future__ import annotations
import logging
from typing import List
import httpx
from youtab_agent_cli.auth_errors import AuthError
logger = logging.getLogger(__name__)

def fetch_youtab_models(
    *,
    inference_base_url: str,
    api_key: str,
    timeout_seconds: float = 15.0,
    verify: bool | str = True,
    exact: bool = False,
) -> List[str]:
    """Fetch available model IDs from the Youtab inference API."""
    timeout = httpx.Timeout(timeout_seconds)
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}, verify=verify) as client:
        response = client.get(
            f"{inference_base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )

    if response.status_code != 200:
        description = f"/models request failed with status {response.status_code}"
        try:
            err = response.json()
            description = str(err.get("error_description") or err.get("error") or description)
        except Exception as e:
            logger.debug("Could not parse error response JSON: %s", e)
        # A rejected request is authoritative: callers must not substitute a
        # stale admitted-model catalog for revoked or unauthorized access.
        rejected = 400 <= response.status_code < 500 and response.status_code not in (408, 429)
        code = "models_fetch_rejected" if rejected else "models_fetch_failed"
        raise AuthError(description, provider="youtab", code=code)

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    model_ids: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            mid = model_id.strip()
            # Skip Youtab models — they're not reliable for agentic tool-calling
            if not exact and "youtab" in mid.lower():
                continue
            model_ids.append(mid)

    # Sort: prefer opus > pro > haiku/flash > sonnet (sonnet is cheap/fast,
    # users who want the best model should see opus first).
    def _model_priority(mid: str) -> tuple:
        low = mid.lower()
        if "opus" in low:
            return (0, mid)
        if "pro" in low and "sonnet" not in low:
            return (1, mid)
        if "sonnet" in low:
            return (3, mid)
        return (2, mid)

    model_ids.sort(key=_model_priority)
    return list(dict.fromkeys(model_ids))
