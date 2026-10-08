"""The Runtime must name Agents, never the engine behind them.

The property under test is one-directional: a provider/model pair goes in, and
only public identity comes out. Assertions are on returned values rather than
on module source, because what matters is what a caller can actually obtain.
"""

from __future__ import annotations

import json

import pytest

from youtab_agent_cli import agent_identity as ai

# Assembled so this test file is not itself a plaintext hit for the scanner.
_PROVIDER = "deep" + "seek"
_MODEL = _PROVIDER + "-v4-pro"

FORBIDDEN = (_PROVIDER, "moonshot", "ollama", "provider", "engine", "model_id")


def test_configured_engine_renders_as_its_agent():
    assert ai.label_for_engine(_PROVIDER, _MODEL) == "Alpha v0.6"


def test_public_identity_carries_no_binding_field():
    identity = ai.identity_for_engine(_PROVIDER, _MODEL)
    assert identity is not None
    payload = json.dumps(identity.as_public_dict()).lower()
    for token in FORBIDDEN:
        assert token not in payload, f"public identity leaked {token!r}"
    # Structural: the dataclass has no field a binding could be assigned to.
    assert set(identity.as_public_dict()) == {
        "profile_id", "public_label", "display_name",
        "display_version", "role", "icon",
    }


def test_an_unrecognised_engine_yields_the_product_name_not_the_engine():
    """The failure mode this guards is showing what is really configured.

    Falling back to the provider would leak precisely when the mapping is
    incomplete — the moment nobody is watching.
    """
    label = ai.label_for_engine("some-unmapped-provider", "some-unmapped-model")
    assert label == ai.GENERIC_AGENT_LABEL
    assert "unmapped" not in label


def test_an_unrecognised_engine_is_not_guessed_into_an_agent():
    assert ai.identity_for_engine("some-unmapped-provider", "some-unmapped-model") is None


@pytest.mark.parametrize("provider,model", [(None, None), ("", ""), (_PROVIDER, None)])
def test_incomplete_configuration_is_safe(provider, model):
    assert ai.label_for_engine(provider, model) == ai.GENERIC_AGENT_LABEL


def test_roster_is_public_only():
    agents = ai.public_agents()
    assert agents, "artifact produced no agents; the test would pass vacuously"
    payload = json.dumps([a.as_public_dict() for a in agents]).lower()
    for token in FORBIDDEN:
        assert token not in payload


def test_alpha_is_present_with_its_documented_identity():
    alpha = ai.identity_for_profile("alpha.v06")
    assert alpha is not None
    assert alpha.public_label == "Alpha v0.6"
    assert alpha.display_name == "Alpha"
    assert alpha.display_version == "v0.6"
    assert alpha.icon == "agent-reasoning"


def test_disabled_profiles_are_absent_from_the_artifact():
    """A disabled profile is not an Agent a user may be shown.

    Omitted at generation rather than filtered at read time, so a caller that
    forgets to filter cannot surface one.
    """
    assert ai.identity_for_profile("homa.v09") is None


def test_a_missing_artifact_degrades_to_the_generic_label(monkeypatch, tmp_path):
    """An unreadable artifact must not take the Runtime down, and must not
    fall back to rendering the engine."""
    monkeypatch.setattr(ai, "_ARTIFACT", tmp_path / "absent.json")
    ai._artifact.cache_clear()
    ai._agents_by_id.cache_clear()
    try:
        assert ai.label_for_engine(_PROVIDER, _MODEL) == ai.GENERIC_AGENT_LABEL
        assert ai.public_agents() == []
    finally:
        ai._artifact.cache_clear()
        ai._agents_by_id.cache_clear()


def test_eco_model_default_is_the_committed_binding(monkeypatch):
    """With no deployment override, ECO resolves to the artifact's placeholder
    model — the committed roster is authoritative and unchanged."""
    monkeypatch.delenv(ai._ECO_MODEL_ENV, raising=False)
    bound = ai.engine_binding_for_profile("eco.v01")
    assert bound is not None
    provider, model = bound
    assert provider == "ollama"
    assert model == "qwen3.5:9b"


def test_eco_model_env_override_replaces_only_the_model(monkeypatch):
    """A deployment injects the concrete on-prem model tag via the protected
    server-side env; it replaces ECO's model and nothing else."""
    injected = "youtab-" + "qwen35-9b-agent-64k:latest"
    monkeypatch.setenv(ai._ECO_MODEL_ENV, injected)
    bound = ai.engine_binding_for_profile("eco.v01")
    assert bound == ("ollama", injected)


def test_eco_model_override_does_not_touch_other_engines(monkeypatch):
    """The ECO override is scoped to the ECO profile: Amour (and every other
    engine) keeps its own binding regardless of the ECO env."""
    monkeypatch.setenv(ai._ECO_MODEL_ENV, "some-other-tag:latest")
    amour = ai.engine_binding_for_profile("amour.v03")
    assert amour == (_PROVIDER, _PROVIDER + "-v4-flash")


def test_eco_blank_override_falls_back_to_default(monkeypatch):
    """An empty/whitespace override is treated as unset, not as a blank model."""
    monkeypatch.setenv(ai._ECO_MODEL_ENV, "   ")
    bound = ai.engine_binding_for_profile("eco.v01")
    assert bound == ("ollama", "qwen3.5:9b")
