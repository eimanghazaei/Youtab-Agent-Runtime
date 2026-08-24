"""Public Agent identity for user-facing Runtime surfaces.

A normal user sees ``Alpha v0.6``. The provider and model that serve it are
implementation detail, and this module is the boundary between the two: it
takes the locally configured engine and returns the Agent's public identity,
or nothing.

It is the *only* place that reads the binding half of
``agent_identity.v1.json``. Everything above it — the dashboard, the CLI, any
export — asks for an identity and gets public fields, so a caller cannot leak
the binding by forgetting to strip a field it never received.

``None`` is a meaningful answer. A configured engine that no Agent claims is
not an error and must not be papered over with a guess: a caller renders the
generic product name instead. Inventing an Agent for an unrecognised engine
would put a name on something Youtab never admitted.

The artifact is generated from ``youtab-ai-os``'s governed registry by
``scripts/youtab/generate_agent_identity.py``; it is not maintained here. See
that script for why a second roster in this repository would be a bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ARTIFACT = Path(__file__).with_name("agent_identity.v1.json")

# What a Runtime surface shows when no Agent claims the configured engine.
# Deliberately the product name, not a provider and not a guess.
GENERIC_AGENT_LABEL = "Youtab Agent"


@dataclass(frozen=True)
class PublicAgentIdentity:
    """Exactly what a user-facing surface may render."""

    profile_id: str
    public_label: str
    display_name: str
    display_version: str | None
    role: str
    icon: str

    def as_public_dict(self) -> dict[str, str | None]:
        return {
            "profile_id": self.profile_id,
            "public_label": self.public_label,
            "display_name": self.display_name,
            "display_version": self.display_version,
            "role": self.role,
            "icon": self.icon,
        }


@lru_cache(maxsize=1)
def _artifact() -> dict:
    try:
        return json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or unreadable artifact must not take the Runtime down, and
        # must not fall back to showing the engine. Every lookup then returns
        # None and callers render the generic label.
        return {"agents": [], "private_binding": {}}


@lru_cache(maxsize=1)
def _agents_by_id() -> dict[str, PublicAgentIdentity]:
    out: dict[str, PublicAgentIdentity] = {}
    for entry in _artifact().get("agents", []):
        try:
            identity = PublicAgentIdentity(
                profile_id=entry["profile_id"],
                public_label=entry["public_label"],
                display_name=entry["display_name"],
                display_version=entry.get("display_version"),
                role=entry.get("role", ""),
                icon=entry.get("icon", "agent-generic"),
            )
        except KeyError:
            continue
        out[identity.profile_id] = identity
    return out


def public_agents() -> list[PublicAgentIdentity]:
    """Every Agent this Runtime knows how to name, ordered by identifier."""
    return [_agents_by_id()[k] for k in sorted(_agents_by_id())]


def identity_for_profile(profile_id: str | None) -> PublicAgentIdentity | None:
    if not profile_id:
        return None
    return _agents_by_id().get(profile_id)


@lru_cache(maxsize=1)
def _binding_by_profile() -> dict[str, tuple[str, str]]:
    """Invert ``private_binding.by_provider_model`` to ``profile_id -> (provider, model)``.

    The forward map is keyed by the substrate name ``"provider/model"``; the
    Runtime needs the reverse direction to answer "what does this Agent run on?"
    without any caller ever touching the two halves of a substrate name. Kept
    here because this module owns the binding half of the artifact.
    """
    out: dict[str, tuple[str, str]] = {}
    binding = _artifact().get("private_binding", {})
    for key, profile_id in (binding.get("by_provider_model", {}) or {}).items():
        if not isinstance(key, str) or not isinstance(profile_id, str) or "/" not in key:
            continue
        provider, _, model = key.partition("/")
        provider, model = provider.strip(), model.strip()
        if provider and model:
            # First binding wins if a profile were ever multiply-bound (it isn't).
            out.setdefault(profile_id, (provider, model))
    return out


def engine_binding_for_profile(profile_id: str | None) -> tuple[str, str] | None:
    """Resolve an Agent ``profile_id`` to its bound ``(provider, model)``, or ``None``.

    The inverse of :func:`identity_for_engine`: given the Agent, return the
    substrate it runs on. ``None`` for an unknown *or* unbound profile — a known
    Agent with no engine binding is a valid state (it runs on its profile
    default), not an error. This is the only sanctioned reverse reader of the
    binding half of the artifact.
    """
    if not profile_id:
        return None
    return _binding_by_profile().get(profile_id)


def identity_for_engine(provider: str | None, model: str | None) -> PublicAgentIdentity | None:
    """Resolve a configured provider/model pair to its Agent, or ``None``.

    This is the private-binding lookup, and the reason this module is the only
    consumer of that half of the artifact.
    """
    if not provider or not model:
        return None
    binding = _artifact().get("private_binding", {})
    profile_id = binding.get("by_provider_model", {}).get(f"{provider}/{model}")
    return identity_for_profile(profile_id)


def label_for_engine(provider: str | None, model: str | None) -> str:
    """The label a user-facing surface shows for a configured engine.

    Never returns the provider or the model. An unrecognised engine yields the
    generic product name, because the alternative — showing what is actually
    configured — is the leak this function exists to prevent, and guessing an
    Agent would name something Youtab never admitted.
    """
    identity = identity_for_engine(provider, model)
    return identity.public_label if identity else GENERIC_AGENT_LABEL


def label_for_qualified_model(qualified: str | None) -> str:
    """The label for a stored ``provider/model`` string, as sessions record it.

    Session rows persist the engine as one qualified string. Splitting it here
    rather than at each call site keeps the parsing in the module that owns the
    binding, so no caller has to touch the two halves of a substrate name to
    ask what to display.

    Anything unrecognised -- an empty value, a bare model with no provider, a
    pair that is not bound -- yields the generic product name. That is the
    point: a surface that cannot resolve an Agent must not fall back to
    printing what is configured.
    """
    if not qualified or "/" not in qualified:
        return GENERIC_AGENT_LABEL
    provider, _, model = qualified.partition("/")
    return label_for_engine(provider, model)


__all__ = [
    "label_for_qualified_model",
    "GENERIC_AGENT_LABEL",
    "PublicAgentIdentity",
    "engine_binding_for_profile",
    "identity_for_engine",
    "identity_for_profile",
    "label_for_engine",
    "public_agents",
]
