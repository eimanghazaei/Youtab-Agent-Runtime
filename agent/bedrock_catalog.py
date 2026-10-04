"""AWS model catalog and SDK guards, independent of agent/terminal orchestration.

Discovery uses installed boto3. Runtime provider setup retains optional dependency
installation; catalog reads never launch package installation as an import side effect.
"""
from __future__ import annotations
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_MIN_BOTO3_VERSION = (1, 34, 59)
_bedrock_control_client_cache: Dict[str, Any] = {}
_discovery_cache: Dict[str, Any] = {}
_DISCOVERY_CACHE_TTL_SECONDS = 3600

def _require_boto3():
    """Import boto3, raising a clear error if not installed or too old."""
    try:
        import boto3
    except ImportError:
        raise ImportError(
            "The 'boto3' package is required for the AWS Bedrock provider. "
            "Install it with: pip install boto3\n"
            "Or install Youtab with Bedrock support: pip install -e '.[bedrock]'"
        )
    # converse() / converse_stream() were added in boto3 1.34.59.
    # When Youtab is installed editable into system Python, the system boto3
    # (e.g. Ubuntu 24.04 ships 1.34.46) may take precedence over the venv
    # version pinned in pyproject.toml.
    try:
        version = tuple(int(x) for x in boto3.__version__.split(".")[:3])
    except (AttributeError, ValueError):
        return boto3  # can't parse — don't block on version check
    if version < _MIN_BOTO3_VERSION:
        raise RuntimeError(
            f"boto3 {boto3.__version__} does not support converse_stream "
            f"(minimum 1.34.59 required). Upgrade with: "
            f"pip install --upgrade boto3"
        )
    return boto3


def _get_bedrock_control_client(region: str, *, boto3_loader=None):
    """Get or create a cached ``bedrock`` control-plane client for model discovery."""
    if region not in _bedrock_control_client_cache:
        boto3 = (boto3_loader or _require_boto3)()
        _bedrock_control_client_cache[region] = boto3.client(
            "bedrock", region_name=region,
        )
    return _bedrock_control_client_cache[region]


def resolve_bedrock_region(env: Optional[Dict[str, str]] = None) -> str:
    """Resolve the AWS region for Bedrock API calls.

    Priority:
      1. AWS_REGION env var
      2. AWS_DEFAULT_REGION env var
      3. boto3/botocore configured region (from ~/.aws/config or SSO profile)
      4. us-east-1 (hard fallback)

    The boto3 fallback is critical for EU/AP users who configure their region
    in ~/.aws/config via a named profile rather than env vars — without it,
    live model discovery would always return us.* profile IDs regardless of
    the user's actual region.
    """
    env = env if env is not None else os.environ
    explicit = (
        env.get("AWS_REGION", "").strip()
        or env.get("AWS_DEFAULT_REGION", "").strip()
    )
    if explicit:
        return explicit
    try:
        import botocore.session
        region = botocore.session.get_session().get_config_variable("region")
        if region:
            return region
    except Exception:
        logger.debug("Bedrock catalog lookup unavailable", exc_info=True)
    return "us-east-1"


def bedrock_model_ids_or_none(*, discover=None, resolve_region=None) -> Optional[List[str]]:
    """Live-discover Bedrock model IDs for the active region.

    Returns a list of model ID strings if discovery succeeds and yields
    at least one model, or ``None`` on failure / empty result.  Callers
    should fall back to the static curated list when ``None`` is returned.

    This helper consolidates the discover → extract-ids → fallback
    pattern that was previously duplicated across ``provider_model_ids``,
    ``list_authenticated_providers`` section 2, and section 3.
    """
    try:
        discovered = (discover or discover_bedrock_models)((resolve_region or resolve_bedrock_region)())
        if discovered:
            return [m["id"] for m in discovered]
    except Exception:
        logger.debug("Bedrock catalog lookup unavailable", exc_info=True)
    return None


def reset_discovery_cache():
    """Clear the model discovery cache. Used in tests."""
    _discovery_cache.clear()


def discover_bedrock_models(
    region: str,
    provider_filter: Optional[List[str]] = None,
    *, client_factory=None,
) -> List[Dict[str, Any]]:
    """Discover available Bedrock foundation models and inference profiles.

    Returns a list of model info dicts with keys:
      - ``id``: Model ID (e.g. "anthropic.claude-sonnet-4-6-20250514-v1:0")
      - ``name``: Human-readable name
      - ``provider``: Model provider (e.g. "Anthropic", "Amazon", "Meta")
      - ``input_modalities``: List of input types (e.g. ["TEXT", "IMAGE"])
      - ``output_modalities``: List of output types
      - ``streaming``: Whether streaming is supported

    Caches results for 1 hour per region to avoid repeated API calls.

    Mirrors OpenClaw's ``discoverBedrockModels()`` in
    ``extensions/amazon-bedrock/discovery.ts``.
    """
    import time

    cache_key = f"{region}:{','.join(sorted(provider_filter or []))}"
    cached = _discovery_cache.get(cache_key)
    if cached and (time.time() - cached["timestamp"]) < _DISCOVERY_CACHE_TTL_SECONDS:
        return cached["models"]

    try:
        client = (client_factory or _get_bedrock_control_client)(region)
    except Exception as e:
        logger.warning("Failed to create Bedrock client for model discovery: %s", e)
        return []

    models = []
    seen_ids = set()
    filter_set = {f.lower() for f in (provider_filter or [])}

    # 1. Discover foundation models
    try:
        response = client.list_foundation_models()
        for summary in response.get("modelSummaries", []):
            model_id = (summary.get("modelId") or "").strip()
            if not model_id:
                continue

            # Apply provider filter
            if filter_set:
                provider_name = (summary.get("providerName") or "").lower()
                model_prefix = model_id.split(".")[0].lower() if "." in model_id else ""
                if provider_name not in filter_set and model_prefix not in filter_set:
                    continue

            # Only include active, streaming-capable, text-output models
            lifecycle = summary.get("modelLifecycle", {})
            if lifecycle.get("status", "").upper() != "ACTIVE":
                continue
            if not summary.get("responseStreamingSupported", False):
                continue
            output_mods = summary.get("outputModalities", [])
            if "TEXT" not in output_mods:
                continue

            models.append({
                "id": model_id,
                "name": (summary.get("modelName") or model_id).strip(),
                "provider": (summary.get("providerName") or "").strip(),
                "input_modalities": summary.get("inputModalities", []),
                "output_modalities": output_mods,
                "streaming": True,
            })
            seen_ids.add(model_id.lower())
    except Exception as e:
        logger.warning("Failed to list Bedrock foundation models: %s", e)

    # 2. Discover inference profiles (cross-region, better capacity)
    try:
        profiles = []
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_inference_profiles(**kwargs)
            for profile in response.get("inferenceProfileSummaries", []):
                profiles.append(profile)
            next_token = response.get("nextToken")
            if not next_token:
                break

        for profile in profiles:
            profile_id = (profile.get("inferenceProfileId") or "").strip()
            if not profile_id:
                continue
            if profile.get("status") != "ACTIVE":
                continue
            if profile_id.lower() in seen_ids:
                continue

            # Apply provider filter to underlying models
            if filter_set:
                profile_models = profile.get("models", [])
                matches = any(
                    _extract_provider_from_arn(m.get("modelArn", "")).lower() in filter_set
                    for m in profile_models
                )
                if not matches:
                    continue

            models.append({
                "id": profile_id,
                "name": (profile.get("inferenceProfileName") or profile_id).strip(),
                "provider": "inference-profile",
                "input_modalities": ["TEXT"],
                "output_modalities": ["TEXT"],
                "streaming": True,
            })
            seen_ids.add(profile_id.lower())
    except Exception as e:
        logger.debug("Skipping inference profile discovery: %s", e)

    # Sort: global cross-region profiles first (recommended), then alphabetical
    models.sort(key=lambda m: (
        0 if m["id"].startswith("global.") else 1,
        m["name"].lower(),
    ))

    _discovery_cache[cache_key] = {
        "timestamp": time.time(),
        "models": models,
    }
    return models


def _extract_provider_from_arn(arn: str) -> str:
    """Extract the model provider from a Bedrock model ARN.

    Example: "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2"
    → "anthropic"
    """
    match = re.search(r"foundation-model/([^.]+)", arn)
    return match.group(1) if match else ""
