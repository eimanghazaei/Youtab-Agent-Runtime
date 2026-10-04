"""Provider model-cache persistence/invalidation independent of live discovery."""

import json
from pathlib import Path
from typing import Callable, Optional

from youtab_agent_cli.model_provider_identity import normalize_provider


def _provider_models_cache_path() -> Path:
    from youtab_constants import get_youtab_home
    return get_youtab_home() / "provider_models_cache.json"


def _load_provider_models_cache(*, path: Optional[Path] = None) -> dict:
    """Return the full cache dict, or {} on any error."""
    try:
        path = path if path is not None else _provider_models_cache_path()
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_provider_models_cache(data: dict, *, path: Optional[Path] = None) -> None:
    """Persist the cache dict. Best-effort — silent on any error."""
    try:
        from utils import atomic_json_write
        path = path if path is not None else _provider_models_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data, indent=None)
    except Exception:
        pass


def clear_provider_models_cache(
    provider: Optional[str] = None, *,
    cache_path: Optional[Callable[[], Path]] = None,
    load_cache: Optional[Callable[[], dict]] = None,
    save_cache: Optional[Callable[[dict], None]] = None,
    normalize: Optional[Callable[[Optional[str]], str]] = None,
) -> None:
    """Drop a single provider's cache entry, or wipe the whole cache.

    ``provider=None`` wipes everything; otherwise only that provider's
    entry is removed. Used by ``/model --refresh`` and
    ``youtab model --refresh``.
    """
    try:
        if provider is None:
            path = (cache_path or _provider_models_cache_path)()
            if path.exists():
                path.unlink()
            return
        cache = (load_cache or _load_provider_models_cache)()
        normalized = (normalize or normalize_provider)(provider) or provider or ""
        if normalized in cache:
            del cache[normalized]
            (save_cache or _save_provider_models_cache)(cache)
    except Exception:
        pass
