"""Youtab picker catalog operations, independent of credential orchestration."""
from __future__ import annotations
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
from youtab_agent_cli import __version__ as _YOUTAB_AGENT_VERSION
from youtab_agent_cli.urllib_security import open_credentialed_url

_YOUTAB_AGENT_USER_AGENT = f"youtab-cli/{_YOUTAB_AGENT_VERSION}"
YOUTAB_RECOMMENDED_MODELS_PATH = "/api/youtab/recommended-models"
_YOUTAB_RECOMMENDED_CACHE_TTL = 600
_youtab_recommended_cache = {}
_pricing_cache = {}
YOUTAB_MODELS = [
        # Anthropic
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-haiku-4.5",
        # OpenAI
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-sol-pro",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-terra-pro",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-luna-pro",
        "openai/gpt-5.5",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.4-mini",
        # Google
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.6-flash",
        # xAI
        "x-ai/grok-4.5",
        # DeepSeek
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        # Qwen
        "qwen/qwen3.7-max",
        # MoonshotAI
        "moonshotai/kimi-k3",
        # MiniMax
        "minimax/minimax-m3",
        # Z-AI
        "z-ai/glm-5.2",
        "z-ai/glm-5.1",
        # Xiaomi
        "xiaomi/mimo-v2.5-pro",
        # Tencent
        "tencent/hy3",
        # StepFun
        "stepfun/step-3.7-flash",
        # NVIDIA
        "nvidia/nemotron-3-super-120b-a12b",
        # Sakana
        "sakana/fugu-ultra",
    ]

def _is_model_free(model_id: str, pricing: dict[str, dict[str, str]]) -> bool:
    """Return True if *model_id* has zero-cost prompt AND completion pricing."""
    p = pricing.get(model_id)
    if not p:
        return False
    try:
        return float(p.get("prompt", "1")) == 0 and float(p.get("completion", "1")) == 0
    except (TypeError, ValueError):
        return False


def _extract_model_name(entry: Any) -> Optional[str]:
    """Pull the ``modelName`` field from a recommended-model entry, else None."""
    if not isinstance(entry, dict):
        return None
    model_name = entry.get("modelName")
    if isinstance(model_name, str) and model_name.strip():
        return model_name.strip()
    return None


def partition_youtab_models_by_tier(
    model_ids: list[str],
    pricing: dict[str, dict[str, str]],
    free_tier: bool,
    *, is_model_free=None,
) -> tuple[list[str], list[str]]:
    """Split Youtab models into (selectable, unavailable) based on user tier.

    For paid-tier users: all models are selectable, none unavailable.

    For free-tier users: only free models are selectable; paid models
    are returned as unavailable (shown grayed out in the menu).
    """
    is_model_free = is_model_free or _is_model_free
    if not free_tier:
        return (model_ids, [])

    if not pricing:
        return (model_ids, [])  # can't determine, show everything

    selectable: list[str] = []
    unavailable: list[str] = []
    for mid in model_ids:
        if is_model_free(mid, pricing):
            selectable.append(mid)
        else:
            unavailable.append(mid)
    return (selectable, unavailable)


def _youtab_recommended_disk_path() -> "Path":
    """Disk path for the persisted recommended-models cache."""
    from youtab_constants import get_youtab_home
    return get_youtab_home() / "cache" / "youtab_recommended_cache.json"


def _read_youtab_recommended_disk(base: str, *, disk_path=None) -> dict[str, Any] | None:
    """Return the last-known-good payload for ``base`` from disk, or None.

    The disk file is a JSON object keyed by portal base URL so staging and
    prod don't collide:
    ``{"<base>": {"data": {...}, "ts": <epoch_seconds>}}``.
    """
    disk_path = disk_path or _youtab_recommended_disk_path
    try:
        with open(disk_path(), encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    entry = blob.get(base)
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) and data else None


def _write_youtab_recommended_disk(base: str, data: dict[str, Any], *, disk_path=None) -> None:
    """Persist ``data`` as the last-known-good payload for ``base``.

    Merges into any existing per-base map, then writes atomically. Failures
    are non-fatal (logged at debug) — the in-process cache still works.
    """
    if not data:
        return
    path = (disk_path or _youtab_recommended_disk_path)()
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            if not isinstance(blob, dict):
                blob = {}
        except (OSError, json.JSONDecodeError):
            blob = {}
        blob[base] = {"data": data, "ts": time.time()}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        import logging
        logging.getLogger(__name__).debug(
            "youtab recommended-models disk cache write failed: %s", exc
        )


def fetch_youtab_recommended_models(
    portal_base_url: str = "",
    timeout: float = 5.0,
    *,
    force_refresh: bool = False,
    cache=None, open_url=None, read_disk=None, write_disk=None, ttl=None,
) -> dict[str, Any]:
    """Fetch the Youtab Portal's curated recommended-models payload.

    Hits ``<portal>/api/youtab/recommended-models``. The endpoint is public —
    no auth is required. Results are cached per portal URL for
    ``_YOUTAB_RECOMMENDED_CACHE_TTL`` seconds in process; pass
    ``force_refresh=True`` to bypass the in-process cache.

    A successful live fetch is also persisted to a per-base disk cache
    (``$YOUTAB_AGENT_HOME/cache/youtab_recommended_cache.json``) as last-known-good.
    When the live fetch fails (network, parse, non-2xx) and the in-process
    cache is empty, the disk copy is returned instead of ``{}`` — so a
    transient Portal hiccup no longer silently drops the free/paid model
    recommendations from the picker. Self-heals on the next successful fetch.

    Returns the parsed JSON dict, or ``{}`` only when neither the network nor
    any cache layer can supply data. Callers must treat missing/null fields
    as "no recommendation" and fall back to their own default.
    """
    cache = _youtab_recommended_cache if cache is None else cache
    open_url = open_url or open_credentialed_url
    read_disk = read_disk or _read_youtab_recommended_disk
    write_disk = write_disk or _write_youtab_recommended_disk
    ttl = _YOUTAB_RECOMMENDED_CACHE_TTL if ttl is None else ttl
    base = (portal_base_url or "https://api.youtab.io").rstrip("/")
    now = time.monotonic()
    cached = cache.get(base)
    if not force_refresh and cached is not None:
        payload, cached_at = cached
        if now - cached_at < ttl:
            return payload

    url = f"{base}{YOUTAB_RECOMMENDED_MODELS_PATH}"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with open_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    if data:
        # Live fetch succeeded — refresh both cache layers.
        cache[base] = (data, now)
        write_disk(base, data)
        return data

    # Live fetch failed. Fall back to the last-known-good disk copy so a
    # transient Portal hiccup doesn't drop the recommendations entirely.
    disk = read_disk(base)
    if disk:
        cache[base] = (disk, now)
        return disk

    cache[base] = (data, now)
    return data


def union_with_portal_free_recommendations(
    curated_ids: list[str],
    pricing: dict[str, dict[str, str]],
    portal_base_url: str = "",
    *,
    force_refresh: bool = False,
    fetch_recommendations=None,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Augment curated list + pricing with the Portal's ``freeRecommendedModels``.

    The Portal's ``/api/youtab/recommended-models`` endpoint advertises which
    models are free *right now* — independent of what the in-repo
    ``_PROVIDER_MODELS["youtab"]`` list happens to contain or whether the
    docs-hosted catalog manifest has been rebuilt since the last release.

    For free-tier users this is the source of truth: any model the Portal
    flags as free should be selectable, even if the user is running an
    older Youtab that doesn't ship that model in its hardcoded curated
    list.  This function returns an augmented ``(model_ids, pricing)``
    pair where:

    * Portal free recommendations missing from ``curated_ids`` are
      appended after the curated list (so the in-repo curated models
      show first and Portal-only picks follow).
    * ``pricing`` gets a synthetic ``{"prompt": "0", "completion": "0"}``
      entry for any free recommendation missing from the live pricing
      map, so :func:`partition_youtab_models_by_tier` keeps it.

    Failures (network, parse, missing field) are silent and degrade to
    returning the inputs unchanged.
    """
    fetch_recommendations = fetch_recommendations or fetch_youtab_recommended_models
    try:
        payload = fetch_recommendations(
            portal_base_url, force_refresh=force_refresh
        )
    except Exception:
        return (list(curated_ids), dict(pricing))

    free_block = payload.get("freeRecommendedModels") if isinstance(payload, dict) else None
    if not isinstance(free_block, list) or not free_block:
        return (list(curated_ids), dict(pricing))

    portal_free_ids: list[str] = []
    for entry in free_block:
        name = _extract_model_name(entry)
        if name:
            portal_free_ids.append(name)
    if not portal_free_ids:
        return (list(curated_ids), dict(pricing))

    augmented_pricing = dict(pricing)
    free_synthetic = {"prompt": "0", "completion": "0"}
    for mid in portal_free_ids:
        if mid not in augmented_pricing:
            augmented_pricing[mid] = dict(free_synthetic)

    augmented_ids = list(curated_ids)
    seen = set(augmented_ids)
    # Append Portal free recommendations that aren't already curated, so the
    # in-repo curated ("HA") models show first and Portal-only picks follow.
    new_ones = [mid for mid in portal_free_ids if mid not in seen]
    if new_ones:
        augmented_ids = augmented_ids + new_ones

    return (augmented_ids, augmented_pricing)


def union_with_portal_paid_recommendations(
    curated_ids: list[str],
    pricing: dict[str, dict[str, str]],
    portal_base_url: str = "",
    *,
    force_refresh: bool = False,
    fetch_recommendations=None,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Augment curated list with the Portal's ``paidRecommendedModels``.

    Mirror of :func:`union_with_portal_free_recommendations` for paid-tier
    users. The Portal's ``/api/youtab/recommended-models`` endpoint advertises
    which paid models are blessed *right now* — independent of what the
    in-repo ``_PROVIDER_MODELS["youtab"]`` list happens to contain or whether
    the docs-hosted catalog manifest has been rebuilt since the last release.

    For paid-tier users this lets newly-launched paid models surface in the
    picker even if the user is running an older Youtab that doesn't ship
    them in its hardcoded curated list. This function returns an augmented
    ``(model_ids, pricing)`` pair where:

    * Portal paid recommendations missing from ``curated_ids`` are
      appended after the curated list (so the in-repo curated models
      show first and Portal-only picks follow).
    * ``pricing`` is left untouched — we deliberately do NOT synthesize
      pricing entries for paid models. Live pricing is fetched separately
      via :func:`get_pricing_for_provider`; if the live endpoint hasn't
      published pricing yet, the picker shows a blank price column rather
      than fabricating numbers. (The free helper synthesizes ``$0`` so
      :func:`partition_youtab_models_by_tier` keeps free models selectable;
      no equivalent gating applies on the paid side, so synthesis would
      only mislead the user.)

    Failures (network, parse, missing field) are silent and degrade to
    returning the inputs unchanged — never block the picker on a
    Portal-side hiccup.
    """
    fetch_recommendations = fetch_recommendations or fetch_youtab_recommended_models
    try:
        payload = fetch_recommendations(
            portal_base_url, force_refresh=force_refresh
        )
    except Exception:
        return (list(curated_ids), dict(pricing))

    paid_block = payload.get("paidRecommendedModels") if isinstance(payload, dict) else None
    if not isinstance(paid_block, list) or not paid_block:
        return (list(curated_ids), dict(pricing))

    portal_paid_ids: list[str] = []
    for entry in paid_block:
        name = _extract_model_name(entry)
        if name:
            portal_paid_ids.append(name)
    if not portal_paid_ids:
        return (list(curated_ids), dict(pricing))

    augmented_ids = list(curated_ids)
    seen = set(augmented_ids)
    # Append Portal paid recommendations that aren't already curated, so the
    # in-repo curated ("HA") models show first and Portal-only picks follow.
    new_ones = [mid for mid in portal_paid_ids if mid not in seen]
    if new_ones:
        augmented_ids = augmented_ids + new_ones

    return (augmented_ids, dict(pricing))


def get_curated_youtab_model_ids(*, fallback=None) -> list[str]:
    """Return the curated Youtab Portal model-id list.

    Prefers the remotely-hosted catalog manifest (published under
    the packaged engine catalogue); falls back to the in-repo
    snapshot in ``_PROVIDER_MODELS["youtab"]`` when the manifest is
    unreachable. Always returns a list (never None).
    """
    try:
        from youtab_agent_cli.model_catalog import get_curated_youtab_models
        remote = get_curated_youtab_models()
    except Exception:
        remote = None
    if remote:
        return list(remote)
    return list(YOUTAB_MODELS if fallback is None else fallback)


def fetch_models_with_pricing(
    api_key: str | None = None,
    base_url: str = "https://openrouter.ai/api",
    timeout: float = 8.0,
    *,
    force_refresh: bool = False,
    include_sale_original: bool = False,
    cache=None, open_url=None,
) -> dict[str, dict[str, Any]]:
    """Fetch ``/v1/models`` and return ``{model_id: {prompt, completion, ...}}``.

    Results are cached per *base_url* so repeated calls are free.
    Works with any OpenRouter-compatible endpoint (OpenRouter, Youtab Portal).

    When *include_sale_original* is true (Youtab Portal only) and the gateway
    advertises a global discount under ``pricing.original``, those
    pre-discount rates are copied through as a nested ``original`` dict so
    pickers can show sale chrome. Other providers never opt in — OpenRouter
    (and anything else sharing this helper) keeps the legacy
    ``{prompt, completion}`` shape even if a response happens to nest
    ``original``.
    """
    cache = _pricing_cache if cache is None else cache
    open_url = open_url or open_credentialed_url
    cache_key = (base_url or "").rstrip("/")
    if not force_refresh and cache_key in cache:
        return cache[cache_key]

    url = cache_key + "/v1/models"
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": _YOUTAB_AGENT_USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with open_url(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        cache[cache_key] = {}
        return {}

    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("data", []):
        mid = item.get("id")
        pricing = item.get("pricing")
        if mid and isinstance(pricing, dict):
            entry: dict[str, Any] = {
                "prompt": str(pricing.get("prompt", "")),
                "completion": str(pricing.get("completion", "")),
            }
            if pricing.get("input_cache_read"):
                entry["input_cache_read"] = str(pricing["input_cache_read"])
            if pricing.get("input_cache_write"):
                entry["input_cache_write"] = str(pricing["input_cache_write"])
            # Sale chrome is Youtab Portal-only. Never copy pricing.original for
            # OpenRouter / other OpenAI-compatible catalogs.
            if include_sale_original:
                original = pricing.get("original")
                if isinstance(original, dict):
                    orig_entry: dict[str, str] = {}
                    for key in (
                        "prompt",
                        "completion",
                        "input_cache_read",
                        "input_cache_write",
                    ):
                        if original.get(key) not in (None, ""):
                            orig_entry[key] = str(original[key])
                    if orig_entry.get("prompt") or orig_entry.get("completion"):
                        entry["original"] = orig_entry
            result[mid] = entry

    cache[cache_key] = result
    return result


def resolve_youtab_pricing_credentials(resolve_credentials, inference_override) -> tuple[str, str]:
    """Return ``(api_key, base_url)`` for Youtab Portal pricing.

    The Youtab inference ``/v1/models`` endpoint exposes pricing without
    authentication, so the api_key is best-effort: when runtime credential
    resolution fails (expired refresh token, missing auth.json, etc.) we
    still return a usable inference base URL so the picker keeps working
    with anonymous pricing data.  Free-tier users in particular need this
    — pricing drives the free/paid partition, and silently returning empty
    pricing because of an auth blip makes the picker look broken ("No free
    models currently available").

    Base URL precedence (mirrors runtime credential resolution):
    1. ``YOUTAB_INFERENCE_BASE_URL`` env override (staging / preview)
    2. Resolved runtime credential ``base_url``
    3. Production default

    Without (1), a staging profile's sale ``pricing.original`` never
    reaches the pickers — the anonymous fallback would hit prod, which
    has no ``original`` field.
    """
    env_base = None
    try:
        env_base = inference_override()
    except Exception:
        env_base = None

    api_key = ""
    creds_base = ""
    from youtab_agent_cli.profile_inference import (get_local_inference_token_state, profile_inference_base_url)

    local = get_local_inference_token_state()
    if local is not None:
        if not local.get("agent_key"):
            return ("", "")
        return (str(local.get("agent_key") or ""), profile_inference_base_url(local))
    try:
        creds = resolve_credentials()
        if creds:
            api_key = creds.get("api_key", "") or ""
            creds_base = (creds.get("base_url", "") or "").strip()
    except Exception:
        pass

    base_url = (env_base or creds_base or "https://api.youtab.io").rstrip("/")
    return (api_key, base_url)


def get_youtab_pricing(resolve_credentials, inference_override, *, force_refresh=False):
    api_key, base_url = resolve_youtab_pricing_credentials(resolve_credentials, inference_override)
    if not base_url:
        return {}
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[:-3]
    return fetch_models_with_pricing(api_key=api_key, base_url=stripped,
                                    force_refresh=force_refresh, include_sale_original=True)


# Mutable shared state lets login refresh every picker surface without importing models.
_free_tier_cache = {}


def check_youtab_free_tier(account_reader, *, force_fresh=False, cache=None, ttl=180):
    """Cache a caller-supplied entitlement read; unknown states remain unblocked."""
    cache = _free_tier_cache if cache is None else cache
    now = time.monotonic()
    cached = cache.get("value")
    if not force_fresh and cached is not None and now - cached[1] < ttl:
        return cached[0]
    try:
        result = account_reader(force_fresh=force_fresh).is_free_tier
    except Exception:
        result = False
    cache["value"] = (result, now)
    return result
