"""Youtab Portal provider profile."""

from typing import Any

from agent.portal_tags import get_conversation_context, youtab_portal_tags
from providers import register_provider
from providers.base import ProviderProfile


class YoutabProfile(ProviderProfile):
    """Youtab Portal — product tags, reasoning with Youtab-specific omission."""

    def _uses_gateway_chat_wire(self, base_url: str | None) -> bool:
        from youtab_agent_cli.auth import DEFAULT_YOUTAB_PORTAL_URL, _youtab_portal_env_override

        gateway_base = (_youtab_portal_env_override() or DEFAULT_YOUTAB_PORTAL_URL) + "/v1"
        return (base_url or gateway_base).rstrip("/").lower() == gateway_base.lower()

    def build_extra_body(
        self, *, session_id: str | None = None, **context
    ) -> dict[str, Any]:
        if self._uses_gateway_chat_wire(context.get("base_url")):
            # The governed Gateway chat schema rejects the former Portal
            # tags, session_id and provider fields.
            return {}
        body: dict[str, Any] = {"tags": youtab_portal_tags(session_id=session_id)}
        # Top-level session_id → provider sticky routing key. Pins every
        # turn of a session to the same upstream endpoint so explicit
        # Anthropic cache_control breakpoints stay warm instead of
        # cold-writing a fresh cache on each reroute (Anthropic/Vertex/
        # Bedrock caches are instance-local). Mirrors the OpenRouter
        # profile; without it the portal falls back to hashing the opening
        # messages, which breaks pinning whenever those shift.
        #
        # Resolve it exactly like ``youtab_portal_tags`` resolves the
        # ``conversation=`` tag: ambient context first (the lineage ROOT id
        # published by the agent loop), explicit argument as fallback.
        #
        # The gap this closes is the auxiliary call sites — compression,
        # title generation, vision, web_extract, session_search, MoA slots.
        # They funnel through ``agent.auxiliary_client`` which has no session
        # handle, so they never pass ``session_id``: they carried the
        # ``conversation=`` tag but NO sticky key at all, and each one routed
        # independently of the conversation it belongs to. Reading the same
        # ambient contextvar the tag already uses fixes that with zero
        # per-call-site plumbing.
        #
        # For the main loop the two agree anyway under the default
        # ``compression.in_place: true`` (#38763), where compaction keeps the
        # session id; the ambient root additionally keeps the key stable for
        # installs that opt back into rotating compaction, and across
        # delegate-subagent trees.
        sticky_key = get_conversation_context() or session_id
        if sticky_key:
            body["session_id"] = sticky_key
        provider_preferences = context.get("provider_preferences")
        if provider_preferences:
            body["provider"] = provider_preferences
        return body

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **context,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Youtab: passes full reasoning_config, but OMITS when disabled."""
        if self._uses_gateway_chat_wire(context.get("base_url")):
            return {}, {}
        extra_body = {}
        if supports_reasoning:
            if reasoning_config is not None:
                rc = dict(reasoning_config)
                if rc.get("enabled") is False:
                    pass  # Youtab omits reasoning when disabled
                else:
                    extra_body["reasoning"] = rc
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}
        return extra_body, {}


youtab = YoutabProfile(
    name="youtab",
    aliases=("youtab-portal", "youtab"),
    env_vars=("YOUTAB_API_KEY",),
    display_name="Youtab B.V.",
    description="Youtab B.V. — Youtab model family",
    signup_url="https://youtab.io/",
    fallback_models=(),
    base_url="https://api.youtab.io/v1",
    auth_type="oauth_external",
)

register_provider(youtab)
