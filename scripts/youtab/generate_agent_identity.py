#!/usr/bin/env python3
"""Generate the Runtime's Agent identity artifact from the authoritative source.

The Runtime must show a normal user "Alpha v0.6", never the engine underneath.
It therefore needs two things: the public identity of each Agent, and a way to
recognise which Agent the locally configured engine belongs to.

Neither is invented here. Both are derived from ``youtab-ai-os``'s governed
registry — ``profiles.v1.json`` for identity, ``registry.v1.json`` for the
binding — and written to a checked-in artifact. A hand-maintained roster in
this repository would be a second authority, and the first time it disagreed
with the backend a user would see an Agent that does not exist, or the wrong
name for one that does.

Run it against a checkout of the backend::

    python scripts/youtab/generate_agent_identity.py \\
        --backend /path/to/youtab-ai-os \\
        --output youtab_agent_cli/agent_identity.v1.json

The artifact records ``generated_from`` so a reader can tell which catalogue
revision it came from, and ``--check`` re-derives it and fails if the checked-in
copy has drifted — the same shape as the other governed artifacts here.

On the binding half: the generated map is the private Agent-to-engine binding.
It exists in this repository because the Runtime resolves it server-side to
choose a label, and it is never served to a normal user. That is why the
resolver is the only consumer and why the reachability matrix classifies this
module as PRIVATE_BINDING rather than something a document may quote.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1

# Public fields only. The generator refuses to copy anything else across, so a
# future field added to the backend profile cannot silently become public here.
PUBLIC_PROFILE_FIELDS = (
    "profile_id",
    "public_label",
    "display_name",
    "display_version",
    "role",
)

_ROLE_ICONS = {
    "reasoning": "agent-reasoning",
    "conversational": "agent-conversational",
    "economy": "agent-economy",
    "vision": "agent-vision",
}
_DEFAULT_ICON = "agent-generic"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"authoritative source not found: {path}") from None


def build(backend_root: Path) -> dict:
    registry_dir = backend_root / "services" / "gateway" / "app" / "engine_registry"
    profiles = _load(registry_dir / "profiles.v1.json")
    registry = _load(registry_dir / "registry.v1.json")

    engines = {e["engine_id"]: e for e in registry.get("engines", [])}

    agents: list[dict] = []
    # engine_id -> profile_id, and (provider_id, model_id) -> profile_id.
    # Both directions are needed because the Runtime's config names a provider
    # and a model, not an engine.
    by_engine: dict[str, str] = {}
    by_provider_model: dict[str, str] = {}

    for profile in profiles.get("profiles", []):
        if profile.get("state") != "enabled":
            # A disabled profile is not an Agent a user may be shown. Omitted
            # rather than emitted-and-filtered, so the artifact cannot leak a
            # roster entry that the backend would refuse.
            continue
        agent = {field: profile.get(field) for field in PUBLIC_PROFILE_FIELDS}
        agent["icon"] = _ROLE_ICONS.get(str(profile.get("role")), _DEFAULT_ICON)
        agents.append(agent)

        version = profile.get("current_binding_version")
        for binding in profile.get("bindings", []):
            if binding.get("binding_version") != version:
                continue
            engine_id = binding.get("primary_engine_id")
            if not engine_id:
                continue
            by_engine[engine_id] = profile["profile_id"]
            engine = engines.get(engine_id) or {}
            provider_id = engine.get("provider_id")
            model_id = engine.get("model_id")
            if provider_id and model_id:
                by_provider_model[f"{provider_id}/{model_id}"] = profile["profile_id"]

    agents.sort(key=lambda a: a["profile_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            "profiles_catalogue_version": profiles.get("catalogue_version"),
            "profiles_revision": profiles.get("revision"),
            "registry_version": registry.get("registry_version"),
            "registry_revision": registry.get("revision"),
        },
        "agents": agents,
        "private_binding": {
            "by_engine_id": dict(sorted(by_engine.items())),
            "by_provider_model": dict(sorted(by_provider_model.items())),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, default=Path("/root/youtab-ai-os"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("youtab_agent_cli/agent_identity.v1.json"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-derive and fail if the checked-in artifact has drifted",
    )
    args = parser.parse_args()

    built = build(args.backend.resolve())
    payload = json.dumps(built, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.exists():
            print(f"FAIL: {args.output} is missing", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        if current != payload:
            print(
                f"FAIL: {args.output} has drifted from the authoritative "
                f"registry — regenerate it",
                file=sys.stderr,
            )
            return 1
        print(f"PASS: {args.output} matches the authoritative registry", file=sys.stderr)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(f"wrote {args.output} ({len(built['agents'])} agents)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
