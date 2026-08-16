#!/usr/bin/env python3
"""Build the Youtab Model Catalog — a centralized JSON manifest of curated models.

This script reads the in-repo hardcoded curated lists (``OPENROUTER_MODELS``,
``_PROVIDER_MODELS["youtab"]``) and writes them to a JSON manifest that the
Youtab CLI fetches at runtime. Publishing the catalog through the docs site
lets maintainers update model lists without shipping a Youtab release.

The runtime fetcher falls back to the same in-repo hardcoded lists if the
manifest is unreachable, so this script is a convenience for keeping the
manifest in sync — not a source of truth.

Usage::

    python scripts/build_model_catalog.py

Output: ``youtab_agent_cli/data/engine_catalog.json`` (private, packaged)

Live URL (after ``deploy-site.yml`` runs on merge to main):
the packaged private catalogue (no longer published to any URL)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Ensure YOUTAB_AGENT_HOME is set for imports that touch it at module level.
os.environ.setdefault("YOUTAB_AGENT_HOME", os.path.join(os.path.expanduser("~"), ".youtab-agent-runtime"))

from youtab_agent_cli.models import (  # noqa: E402
    OPENROUTER_MODELS,
    PREFERRED_SILENT_DEFAULT_MODEL,
    _PROVIDER_MODELS,
)

OUTPUT_PATH = os.path.join(REPO_ROOT, "youtab_agent_cli", "data", "engine_catalog.json")
CATALOG_VERSION = 1


def _openrouter_entry(mid: str, desc: str) -> dict:
    entry: dict = {"id": mid, "description": desc}
    if mid == PREFERRED_SILENT_DEFAULT_MODEL:
        entry["description"] = desc or "default"
        entry["default"] = True
    return entry


def _youtab_entry(mid: str) -> dict:
    entry: dict = {"id": mid}
    if mid == PREFERRED_SILENT_DEFAULT_MODEL:
        entry["default"] = True
    return entry


def build_catalog() -> dict:
    return {
        "version": CATALOG_VERSION,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": {
            "source": "youtab-agent-runtime repo",
            "docs": "",  # the public catalogue reference page was retired with the catalogue
        },
        "providers": {
            "openrouter": {
                "metadata": {
                    "display_name": "OpenRouter",
                    "note": (
                        "Descriptions drive picker badges. Live /api/v1/models "
                        "filters curated ids by tool-calling support and free pricing. "
                        'The entry labeled "default": true is the model Youtab '
                        "silently lands on when the user never picked one."
                    ),
                },
                "models": [
                    _openrouter_entry(mid, desc)
                    for mid, desc in OPENROUTER_MODELS
                ],
            },
            "youtab": {
                "metadata": {
                    "display_name": "Youtab Portal",
                    "note": (
                        "Free-tier gating is determined live via Portal pricing "
                        "(partition_youtab_models_by_tier), not this manifest. "
                        'The entry labeled "default": true is the model Youtab '
                        "silently lands on when the user never picked one."
                    ),
                },
                "models": [
                    _youtab_entry(mid)
                    for mid in _PROVIDER_MODELS.get("youtab", [])
                ],
            },
        },
    }


def main() -> int:
    catalog = build_catalog()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {OUTPUT_PATH}")
    for provider, block in catalog["providers"].items():
        print(f"  {provider}: {len(block['models'])} models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
