"""AWS Bedrock Converse API adapter for Youtab Agent Runtime.

Provides native integration with Amazon Bedrock using the Converse API,
bypassing the OpenAI-compatible endpoint in favor of direct AWS SDK calls.
This enables full access to the Bedrock ecosystem:

  - **Native Converse API**: Unified interface for all Bedrock models
    (Claude, Nova, Llama, Mistral, etc.) with streaming support.
  - **AWS credential chain**: IAM roles, SSO profiles, environment variables,
    instance metadata — zero API key management for AWS-native environments.
  - **Dynamic model discovery**: Auto-discovers available foundation models
    and cross-region inference profiles via the Bedrock control plane.
  - **Guardrails support**: Optional Bedrock Guardrails configuration for
    content filtering and safety policies.
  - **Inference profiles**: Supports cross-region inference profiles
    (us.anthropic.claude-*, global.anthropic.claude-*) for better capacity
    and automatic failover.

Architecture follows the same pattern as ``anthropic_adapter.py``:
  - All Bedrock-specific logic is isolated in this module.
  - Messages/tools are converted between OpenAI format and Converse format.
  - Responses are normalized back to OpenAI-compatible objects for the agent loop.

Reference: OpenClaw's ``extensions/amazon-bedrock/`` plugin, which implements
the same Converse API integration in TypeScript via ``@aws-sdk/client-bedrock``.

Requires: ``boto3`` (optional dependency — only needed when using the Bedrock provider).
"""

import json
import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from agent import bedrock_catalog as _catalog
from agent.bedrock_catalog import (
    _require_boto3 as _require_boto3,
    resolve_bedrock_region as resolve_bedrock_region,
    reset_discovery_cache as reset_discovery_cache,
    _extract_provider_from_arn as _extract_provider_from_arn,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure boto3/botocore are installed before any code in this module runs.
# Upstream removed boto3 from [all] extras (PRs #24220, #24515); lazy_deps
# handles on-demand installation so the Bedrock provider still works in the
# EKS deployment without baking boto3 into the base image.
# ---------------------------------------------------------------------------
def ensure(feature: str, *, prompt: bool = True) -> None:
    """Compatibility entry point for optional SDK bootstrap."""
    from tools.lazy_deps import ensure as bootstrap

    return bootstrap(feature, prompt=prompt)


try:
    ensure("provider.bedrock", prompt=False)
except Exception:
    pass  # lazy_deps unavailable or install failed — let downstream imports surface the real error


# ---------------------------------------------------------------------------
# Lazy boto3 import — only loaded when the Bedrock provider is actually used.
# This keeps startup fast for users who don't use Bedrock.
# ---------------------------------------------------------------------------

_bedrock_runtime_client_cache: Dict[str, Any] = {}
_bedrock_control_client_cache = _catalog._bedrock_control_client_cache






def _get_bedrock_runtime_client(region: str):
    """Get or create a cached ``bedrock-runtime`` client for the given region.

    Uses the default AWS credential chain (env vars → profile → instance role).
    """
    if region not in _bedrock_runtime_client_cache:
        boto3 = _require_boto3()
        _bedrock_runtime_client_cache[region] = boto3.client(
            "bedrock-runtime", region_name=region,
        )
    return _bedrock_runtime_client_cache[region]


def _get_bedrock_control_client(region: str):
    return _catalog._get_bedrock_control_client(region, boto3_loader=_require_boto3)


def reset_client_cache():
    """Clear cached boto3 clients. Used in tests and profile switches."""
    _bedrock_runtime_client_cache.clear()
    _bedrock_control_client_cache.clear()


def invalidate_runtime_client(region: str) -> bool:
    """Evict the cached ``bedrock-runtime`` client for a single region.

    Per-region counterpart to :func:`reset_client_cache`. Used by the converse
    call wrappers to discard clients whose underlying HTTP connection has
    gone stale, so the next call allocates a fresh client (with a fresh
    connection pool) instead of reusing a dead socket.

    Returns True if a cached entry was evicted, False if the region was not
    cached.
    """
    existed = region in _bedrock_runtime_client_cache
    _bedrock_runtime_client_cache.pop(region, None)
    return existed


# ---------------------------------------------------------------------------
# Stale-connection detection
# ---------------------------------------------------------------------------
#
# boto3 caches its HTTPS connection pool inside the client object. When a
# pooled connection is killed out from under us (NAT timeout, VPN flap,
# server-side TCP RST, proxy idle cull, etc.), the next use surfaces as
# one of a handful of low-level exceptions — most commonly
# ``botocore.exceptions.ConnectionClosedError`` or
# ``urllib3.exceptions.ProtocolError``. urllib3 also trips an internal
# ``assert`` in a couple of paths (connection pool state checks, chunked
# response readers) which bubbles up as a bare ``AssertionError`` with an
# empty ``str(exc)``.
#
# In all of these cases the client is the problem, not the request: retrying
# with the same cached client reproduces the failure until the process
# restarts. The fix is to evict the region's cached client so the next
# attempt builds a new one.

_STALE_LIB_MODULE_PREFIXES = (
    "urllib3.",
    "botocore.",
    "boto3.",
)


def _traceback_frames_modules(exc: BaseException):
    """Yield ``__name__``-style module strings for each frame in exc's traceback."""
    tb = getattr(exc, "__traceback__", None)
    while tb is not None:
        frame = tb.tb_frame
        module = frame.f_globals.get("__name__", "")
        yield module or ""
        tb = tb.tb_next


def is_stale_connection_error(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates a dead/stale Bedrock HTTP connection.

    Matches:
      * ``botocore.exceptions.ConnectionError`` and subclasses
        (``ConnectionClosedError``, ``EndpointConnectionError``,
        ``ReadTimeoutError``, ``ConnectTimeoutError``).
      * ``urllib3.exceptions.ProtocolError`` / ``NewConnectionError`` /
        ``ConnectionError`` (best-effort import — urllib3 is a transitive
        dependency of botocore so it is always available in practice).
      * Bare ``AssertionError`` raised from a frame inside urllib3, botocore,
        or boto3. These are internal-invariant failures (typically triggered
        by corrupted connection-pool state after a dropped socket) and are
        recoverable by swapping the client.

    Non-library ``AssertionError``s (from application code or tests) are
    intentionally not matched — only library-internal asserts signal stale
    connection state.
    """
    # botocore: the canonical signal — HTTPClientError is the umbrella for
    # ConnectionClosedError, ReadTimeoutError, EndpointConnectionError,
    # ConnectTimeoutError, and ProxyConnectionError. ConnectionError covers
    # the same family via a different branch of the hierarchy.
    try:
        from botocore.exceptions import (
            ConnectionError as BotoConnectionError,
            HTTPClientError,
        )
        botocore_errors: tuple = (BotoConnectionError, HTTPClientError)
    except ImportError:  # pragma: no cover — botocore always present with boto3
        botocore_errors = ()
    if botocore_errors and isinstance(exc, botocore_errors):
        return True

    # urllib3: low-level transport failures
    try:
        from urllib3.exceptions import (
            ProtocolError,
            NewConnectionError,
            ConnectionError as Urllib3ConnectionError,
        )
        urllib3_errors = (ProtocolError, NewConnectionError, Urllib3ConnectionError)
    except ImportError:  # pragma: no cover
        urllib3_errors = ()
    if urllib3_errors and isinstance(exc, urllib3_errors):
        return True

    # Library-internal AssertionError (urllib3 / botocore / boto3)
    if isinstance(exc, AssertionError):
        for module in _traceback_frames_modules(exc):
            if any(module.startswith(prefix) for prefix in _STALE_LIB_MODULE_PREFIXES):
                return True

    return False


def is_streaming_access_denied_error(exc: BaseException) -> bool:
    """Return True when AWS denied the ``bedrock:InvokeModelWithResponseStream`` action.

    IAM policies scoped to ``bedrock:InvokeModel`` only (a common least-privilege
    setup) reject ``converse_stream()`` with an ``AccessDeniedException`` whose
    message names the streaming action, e.g.::

        User: arn:aws:iam::123456789012:user/x is not authorized to perform:
        bedrock:InvokeModelWithResponseStream on resource: ...

    This is permanent for the session — retrying the stream can never succeed —
    so callers should flip to the non-streaming ``converse()`` path (which maps
    to ``bedrock:InvokeModel``) instead of burning retries.

    Detection is deliberately message-based: boto3 surfaces this as a
    ``ClientError`` with ``Error.Code == "AccessDeniedException"``, and the
    AnthropicBedrock SDK wraps the same AWS response in its own exception
    types, but both preserve the action name in the message.
    """
    msg = str(exc).lower()
    if "invokemodelwithresponsestream" not in msg:
        return False
    # ClientError with an explicit access-denied code is the canonical form.
    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover — botocore always present with boto3
        ClientError = None  # type: ignore[assignment]
    if ClientError is not None and isinstance(exc, ClientError):
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
        return code in ("AccessDeniedException", "UnauthorizedException")
    # Wrapped forms (e.g. AnthropicBedrock SDK PermissionDeniedError) — match
    # on the authorization-failure phrasing AWS uses.
    return "not authorized" in msg or "accessdenied" in msg


# ---------------------------------------------------------------------------
# AWS credential detection
# ---------------------------------------------------------------------------

# Priority order matches OpenClaw's resolveAwsSdkEnvVarName():
#   1. AWS_BEARER_TOKEN_BEDROCK (Bedrock-specific bearer token)
#   2. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (explicit IAM credentials)
#   3. AWS_PROFILE (named profile → SSO, assume-role, etc.)
#   4. Implicit: instance role, ECS task role, Lambda execution role
_AWS_CREDENTIAL_ENV_VARS = [
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    # These are checked by boto3's default chain but we list them for
    # has_aws_credentials() detection:
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
]


from agent.aws_auth import (
    has_aws_credentials as has_aws_credentials,
    resolve_aws_auth_env_var as resolve_aws_auth_env_var,
)






def bedrock_model_ids_or_none() -> Optional[List[str]]:
    return _catalog.bedrock_model_ids_or_none(discover=discover_bedrock_models, resolve_region=resolve_bedrock_region)


# ---------------------------------------------------------------------------
# Tool-calling capability detection
# ---------------------------------------------------------------------------
# Some Bedrock models don't support tool/function calling. Sending toolConfig
# to these models causes ValidationException. We maintain a denylist of known
# non-tool-calling model patterns and strip tools for them.
#
# This is a conservative approach: unknown models are assumed to support tools.
# If a model fails with a tool-related ValidationException, add it here.

_NON_TOOL_CALLING_PATTERNS = [
    "deepseek.r1",          # DeepSeek R1 — reasoning only, no tool support
    "deepseek-r1",          # Alternate ID format
    "stability.",           # Image generation models
    "cohere.embed",         # Embedding models
    "amazon.titan-embed",   # Embedding models
]


def _model_supports_tool_use(model_id: str) -> bool:
    """Return True if the model is expected to support tool/function calling.

    Models in the denylist are known to reject toolConfig in the Converse API.
    Unknown models default to True (assume tool support).
    """
    model_lower = model_id.lower()
    return not any(pattern in model_lower for pattern in _NON_TOOL_CALLING_PATTERNS)


# ---------------------------------------------------------------------------
# Prompt-cache capability detection (Converse API cachePoint)
# ---------------------------------------------------------------------------
# Claude on Bedrock already gets prompt caching through the AnthropicBedrock
# SDK path (see is_anthropic_bedrock_model / runtime_provider.py's dual-path
# routing) — it never reaches build_converse_kwargs unless bearer-token auth
# forces the Converse path (#28156). This allowlist covers the Converse API
# itself: sending an unsupported model a cachePoint block raises a
# ValidationException, so — like _model_supports_tool_use but inverted —
# unknown models default to NOT receiving cache markers until confirmed.
# Ref: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
_CACHE_POINT_PATTERNS = [
    "anthropic.claude",  # bearer-token fallback path
    "amazon.nova",
]


def _model_supports_prompt_cache(model_id: str) -> bool:
    """Return True if the model accepts a Converse API cachePoint block."""
    model_lower = model_id.lower()
    return any(pattern in model_lower for pattern in _CACHE_POINT_PATTERNS)


def is_anthropic_bedrock_model(model_id: str) -> bool:
    """Return True if the model is an Anthropic Claude model on Bedrock.

    These models should use the AnthropicBedrock SDK path for full feature
    parity (prompt caching, thinking budgets, adaptive thinking).
    Non-Claude models use the Converse API path.

    Matches:
      - ``anthropic.claude-*`` (foundation model IDs)
      - ``us.anthropic.claude-*`` (US inference profiles)
      - ``global.anthropic.claude-*`` (global inference profiles)
      - ``eu.anthropic.claude-*`` (EU inference profiles)
    """
    model_lower = model_id.lower()
    # Strip regional prefix if present
    for prefix in (
        "global.", "us.", "eu.", "apac.", "ap.", "au.", "jp.",
        "ca.", "sa.", "me.", "af.",
    ):
        if model_lower.startswith(prefix):
            model_lower = model_lower[len(prefix):]
            break
    return model_lower.startswith("anthropic.claude")


# ---------------------------------------------------------------------------
# Message format conversion: OpenAI → Bedrock Converse
# ---------------------------------------------------------------------------

def convert_tools_to_converse(tools: List[Dict]) -> List[Dict]:
    """Convert OpenAI-format tool definitions to Bedrock Converse ``toolConfig``.

    OpenAI format::

        {"type": "function", "function": {"name": "...", "description": "...",
         "parameters": {"type": "object", "properties": {...}}}}

    Converse format::

        {"toolSpec": {"name": "...", "description": "...",
         "inputSchema": {"json": {"type": "object", "properties": {...}}}}}
    """
    if not tools:
        return []
    result = []
    for t in tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        description = fn.get("description", "")
        parameters = fn.get("parameters", {"type": "object", "properties": {}})
        result.append({
            "toolSpec": {
                "name": name,
                "description": description,
                "inputSchema": {"json": parameters},
            }
        })
    return result


# Bedrock's Converse API rejects any text content block whose text is empty
# OR whitespace-only (ValidationException: "text content blocks must contain
# non-whitespace text"). A lone space is whitespace and is rejected too — the
# placeholder MUST itself be non-whitespace. Ref: issue #9486.
_EMPTY_TEXT_PLACEHOLDER = "(empty)"


def _safe_text(text) -> str:
    """Return ``text`` if it's non-whitespace, else a non-whitespace placeholder.

    Handles None, empty string, and whitespace-only string (spaces, tabs,
    newlines) — all of which Bedrock's Converse API rejects as text content.
    """
    if text is None:
        return _EMPTY_TEXT_PLACEHOLDER
    if not isinstance(text, str):
        text = str(text)
    return text if text.strip() else _EMPTY_TEXT_PLACEHOLDER


def _convert_content_to_converse(content) -> List[Dict]:
    """Convert OpenAI message content (string or list) to Converse content blocks.

    Handles:
      - Plain text strings → [{"text": "..."}]
      - Content arrays with text/image_url parts → mixed text/image blocks

    Replaces empty/whitespace-only text blocks with a non-whitespace
    placeholder — Bedrock's Converse API rejects messages where a text
    content block is empty or whitespace-only (ValidationException:
    "text content blocks must contain non-whitespace text"). Ref: issue #9486.
    """
    if content is None:
        return [{"text": _safe_text(content)}]
    if isinstance(content, str):
        return [{"text": _safe_text(content)}]
    if isinstance(content, list):
        blocks = []
        for part in content:
            if isinstance(part, str):
                blocks.append({"text": _safe_text(part)})
                continue
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "text":
                text = part.get("text", "")
                blocks.append({"text": _safe_text(text)})
            elif part_type == "image_url":
                image_url = part.get("image_url", {})
                url = image_url.get("url", "") if isinstance(image_url, dict) else ""
                if url.startswith("data:"):
                    # data:image/jpeg;base64,/9j/4AAQ...
                    header, _, data = url.partition(",")
                    media_type = "image/jpeg"
                    if header.startswith("data:"):
                        mime_part = header[5:].split(";")[0]
                        if mime_part:
                            media_type = mime_part
                    # Decode base64 to raw bytes — boto3 re-encodes at the
                    # wire layer, so passing the base64 string directly
                    # results in double-encoding and Bedrock rejects it with
                    # "Failed to sanitize image".  Ref: #33317.
                    import base64
                    try:
                        raw_bytes = base64.b64decode(data)
                    except Exception:
                        raw_bytes = data.encode("utf-8")
                    blocks.append({
                        "image": {
                            "format": media_type.split("/")[-1] if "/" in media_type else "jpeg",
                            "source": {"bytes": raw_bytes},
                        }
                    })
                else:
                    # Remote URL — Converse doesn't support URLs directly,
                    # include as text reference for the model.
                    blocks.append({"text": f"[Image: {url}]"})
        return blocks if blocks else [{"text": _EMPTY_TEXT_PLACEHOLDER}]
    return [{"text": _safe_text(content)}]


def convert_messages_to_converse(
    messages: List[Dict],
) -> Tuple[Optional[List[Dict]], List[Dict]]:
    """Convert OpenAI-format messages to Bedrock Converse format.

    Returns ``(system_prompt, converse_messages)`` where:
      - ``system_prompt`` is a list of system content blocks (or None)
      - ``converse_messages`` is the conversation in Converse format

    Handles:
      - System messages → extracted as system prompt
      - User messages → ``{"role": "user", "content": [...]}``
      - Assistant messages → ``{"role": "assistant", "content": [...]}``
      - Tool calls → ``{"toolUse": {"toolUseId": ..., "name": ..., "input": ...}}``
      - Tool results → ``{"toolResult": {"toolUseId": ..., "content": [...]}}``

    Converse requires strict user/assistant alternation. Consecutive messages
    with the same role are merged into a single message.
    """
    system_blocks: List[Dict] = []
    converse_msgs: List[Dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            # System messages become the system prompt. Blank/whitespace-only
            # parts are dropped entirely (not placeholder-filled) since a
            # system prompt made up of only placeholder text is meaningless.
            if isinstance(content, str) and content.strip():
                system_blocks.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text.strip():
                            system_blocks.append({"text": text})
                    elif isinstance(part, str) and part.strip():
                        system_blocks.append({"text": part})
            continue

        if role == "tool":
            # Tool result messages → merge into the preceding user turn
            tool_call_id = msg.get("tool_call_id", "")
            result_content = content if isinstance(content, str) else json.dumps(content)
            tool_result_block = {
                "toolResult": {
                    "toolUseId": tool_call_id,
                    "content": [{"text": _safe_text(result_content)}],
                }
            }
            # In Converse, tool results go in a "user" role message
            if converse_msgs and converse_msgs[-1]["role"] == "user":
                converse_msgs[-1]["content"].append(tool_result_block)
            else:
                converse_msgs.append({
                    "role": "user",
                    "content": [tool_result_block],
                })
            continue

        if role == "assistant":
            content_blocks = []
            # Convert text content
            if isinstance(content, str) and content.strip():
                content_blocks.append({"text": content})
            elif isinstance(content, list):
                content_blocks.extend(_convert_content_to_converse(content))

            # Convert tool calls
            tool_calls = msg.get("tool_calls", [])
            for tc in (tool_calls or []):
                fn = tc.get("function", {})
                args_str = fn.get("arguments", "{}")
                try:
                    args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args_dict = {}
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args_dict,
                    }
                })

            if not content_blocks:
                content_blocks = [{"text": _EMPTY_TEXT_PLACEHOLDER}]

            # Merge with previous assistant message if needed (strict alternation)
            if converse_msgs and converse_msgs[-1]["role"] == "assistant":
                converse_msgs[-1]["content"].extend(content_blocks)
            else:
                converse_msgs.append({
                    "role": "assistant",
                    "content": content_blocks,
                })
            continue

        if role == "user":
            content_blocks = _convert_content_to_converse(content)
            # Merge with previous user message if needed (strict alternation)
            if converse_msgs and converse_msgs[-1]["role"] == "user":
                converse_msgs[-1]["content"].extend(content_blocks)
            else:
                converse_msgs.append({
                    "role": "user",
                    "content": content_blocks,
                })
            continue

    # Converse requires the first message to be from the user
    if converse_msgs and converse_msgs[0]["role"] != "user":
        converse_msgs.insert(0, {"role": "user", "content": [{"text": _EMPTY_TEXT_PLACEHOLDER}]})

    # Converse requires the last message to be from the user
    if converse_msgs and converse_msgs[-1]["role"] != "user":
        converse_msgs.append({"role": "user", "content": [{"text": _EMPTY_TEXT_PLACEHOLDER}]})

    return (system_blocks if system_blocks else None, converse_msgs)


# ---------------------------------------------------------------------------
# Response format conversion: Bedrock Converse → OpenAI
# ---------------------------------------------------------------------------

def _converse_stop_reason_to_openai(stop_reason: str) -> str:
    """Map Bedrock Converse stop reasons to OpenAI finish_reason values."""
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "content_filtered": "content_filter",
        "guardrail_intervened": "content_filter",
    }
    return mapping.get(stop_reason, "stop")


def normalize_converse_response(response: Dict) -> SimpleNamespace:
    """Convert a Bedrock Converse API response to an OpenAI-compatible object.

    The agent loop in ``run_agent.py`` expects responses shaped like
    ``openai.ChatCompletion`` — this function bridges the gap.

    Returns a SimpleNamespace with:
      - ``.choices[0].message.content`` — text response
      - ``.choices[0].message.tool_calls`` — tool call list (if any)
      - ``.choices[0].finish_reason`` — stop/tool_calls/length
      - ``.usage`` — token usage stats
    """
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])
    stop_reason = response.get("stopReason", "end_turn")

    text_parts = []
    reasoning_parts = []
    tool_calls = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "reasoningContent" in block:
            reasoning = block["reasoningContent"]
            if isinstance(reasoning, dict):
                thinking_text = reasoning.get("text", "")
                if thinking_text:
                    reasoning_parts.append(str(thinking_text))
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append(SimpleNamespace(
                id=tu.get("toolUseId", ""),
                type="function",
                function=SimpleNamespace(
                    name=tu.get("name", ""),
                    arguments=json.dumps(tu.get("input", {})),
                ),
            ))

    # Build the message object
    msg = SimpleNamespace(
        role="assistant",
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls if tool_calls else None,
        reasoning_content="\n\n".join(reasoning_parts) if reasoning_parts else None,
    )

    # Build usage stats. Converse's inputTokens excludes cache read/write
    # tokens (unlike OpenAI's prompt_tokens, which includes them) — restore
    # the OpenAI-style "total includes cache" convention here so downstream
    # normalize_usage() can subtract them back out consistently, and surface
    # the Anthropic-named fields it already falls back to for cache reads.
    usage_data = response.get("usage", {})
    input_tokens = usage_data.get("inputTokens", 0)
    cache_read_tokens = usage_data.get("cacheReadInputTokens", 0)
    cache_write_tokens = usage_data.get("cacheWriteInputTokens", 0)
    output_tokens = usage_data.get("outputTokens", 0)
    usage = SimpleNamespace(
        prompt_tokens=input_tokens + cache_read_tokens + cache_write_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + cache_read_tokens + cache_write_tokens + output_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_write_tokens,
    )

    finish_reason = _converse_stop_reason_to_openai(stop_reason)
    if tool_calls and finish_reason == "stop":
        finish_reason = "tool_calls"

    choice = SimpleNamespace(
        index=0,
        message=msg,
        finish_reason=finish_reason,
    )

    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model=response.get("modelId", ""),
    )


# ---------------------------------------------------------------------------
# Streaming response conversion
# ---------------------------------------------------------------------------

def normalize_converse_stream_events(event_stream) -> SimpleNamespace:
    """Consume a Bedrock ConverseStream event stream and build an OpenAI-compatible response.

    Processes the stream events in order:
      - ``messageStart`` — role info
      - ``contentBlockStart`` — new text or toolUse block
      - ``contentBlockDelta`` — incremental text or toolUse input
      - ``contentBlockStop`` — block complete
      - ``messageStop`` — stop reason
      - ``metadata`` — usage stats

    Returns the same shape as ``normalize_converse_response()``.
    """
    return stream_converse_with_callbacks(event_stream)


def stream_converse_with_callbacks(
    event_stream,
    on_text_delta=None,
    on_tool_start=None,
    on_reasoning_delta=None,
    on_interrupt_check=None,
    on_event=None,
) -> SimpleNamespace:
    """Process a Bedrock ConverseStream event stream with real-time callbacks.

    This is the core streaming function that powers both the CLI's live token
    display and the gateway's progressive message updates.

    Args:
        event_stream: The boto3 ``converse_stream()`` response containing a
            ``stream`` key with an iterable of events.
        on_text_delta: Called with each text chunk as it arrives. Only fires
            when no tool_use blocks have been seen (same semantics as the
            Anthropic and chat_completions streaming paths).
        on_tool_start: Called with the tool name when a toolUse block begins.
            Lets the TUI show a spinner while tool arguments are generated.
        on_reasoning_delta: Called with reasoning/thinking text chunks.
            Bedrock surfaces thinking via ``reasoning`` content block deltas
            on supported models (Claude 4.6+).
        on_interrupt_check: Called on each event. Should return True if the
            agent has been interrupted and streaming should stop.
        on_event: Called once at the top of the loop body for EVERY yielded
            Bedrock event (text/tool-input/reasoning/metadata deltas alike),
            before any branching. Provides a wire-level liveness signal so an
            external watchdog can distinguish "still receiving events" from
            "stream wedged with no data". Errors raised by the callback are
            swallowed so a liveness hook can never abort the stream.

    Returns:
        An OpenAI-compatible SimpleNamespace response, identical in shape to
        ``normalize_converse_response()``.
    """
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[SimpleNamespace] = []
    current_tool: Optional[Dict] = None
    current_text_buffer: List[str] = []
    has_tool_use = False
    stop_reason = "end_turn"
    usage_data: Dict[str, int] = {}

    for event in event_stream.get("stream", []):
        # Wire-level liveness signal: fire on EVERY yielded event (text, tool
        # input, reasoning, metadata) before branching so an external watchdog
        # can tell a still-flowing stream from a wedged one. Best-effort — a
        # liveness callback must never be able to abort the stream.
        if on_event is not None:
            try:
                on_event()
            except Exception:
                pass
        # Check for interrupt
        if on_interrupt_check and on_interrupt_check():
            break

        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                has_tool_use = True
                # Flush any accumulated text
                if current_text_buffer:
                    text_parts.append("".join(current_text_buffer))
                    current_text_buffer = []
                current_tool = {
                    "toolUseId": start["toolUse"].get("toolUseId", ""),
                    "name": start["toolUse"].get("name", ""),
                    "input_json": "",
                }
                if on_tool_start:
                    on_tool_start(current_tool["name"])

        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text = delta["text"]
                current_text_buffer.append(text)
                # Fire text delta callback only when no tool calls are present
                # (same semantics as Anthropic/chat_completions streaming)
                if on_text_delta and not has_tool_use:
                    on_text_delta(text)
            elif "toolUse" in delta:
                if current_tool is not None:
                    current_tool["input_json"] += delta["toolUse"].get("input", "")
            elif "reasoningContent" in delta:
                # Claude 4.6+ on Bedrock surfaces thinking via reasoningContent
                reasoning = delta["reasoningContent"]
                if isinstance(reasoning, dict):
                    thinking_text = reasoning.get("text", "")
                    if thinking_text:
                        reasoning_parts.append(str(thinking_text))
                        if on_reasoning_delta:
                            on_reasoning_delta(thinking_text)

        elif "contentBlockStop" in event:
            if current_tool is not None:
                try:
                    input_dict = json.loads(current_tool["input_json"]) if current_tool["input_json"] else {}
                except (json.JSONDecodeError, TypeError):
                    input_dict = {}
                tool_calls.append(SimpleNamespace(
                    id=current_tool["toolUseId"],
                    type="function",
                    function=SimpleNamespace(
                        name=current_tool["name"],
                        arguments=json.dumps(input_dict),
                    ),
                ))
                current_tool = None
            elif current_text_buffer:
                text_parts.append("".join(current_text_buffer))
                current_text_buffer = []

        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason", "end_turn")

        elif "metadata" in event:
            meta_usage = event["metadata"].get("usage", {})
            usage_data = {
                "inputTokens": meta_usage.get("inputTokens", 0),
                "outputTokens": meta_usage.get("outputTokens", 0),
                "cacheReadInputTokens": meta_usage.get("cacheReadInputTokens", 0),
                "cacheWriteInputTokens": meta_usage.get("cacheWriteInputTokens", 0),
            }

    # Flush remaining text
    if current_text_buffer:
        text_parts.append("".join(current_text_buffer))

    msg = SimpleNamespace(
        role="assistant",
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls if tool_calls else None,
        reasoning_content="\n\n".join(reasoning_parts) if reasoning_parts else None,
    )

    input_tokens = usage_data.get("inputTokens", 0)
    cache_read_tokens = usage_data.get("cacheReadInputTokens", 0)
    cache_write_tokens = usage_data.get("cacheWriteInputTokens", 0)
    output_tokens = usage_data.get("outputTokens", 0)
    usage = SimpleNamespace(
        prompt_tokens=input_tokens + cache_read_tokens + cache_write_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + cache_read_tokens + cache_write_tokens + output_tokens,
        cache_read_input_tokens=cache_read_tokens,
        cache_creation_input_tokens=cache_write_tokens,
    )

    finish_reason = _converse_stop_reason_to_openai(stop_reason)
    if tool_calls and finish_reason == "stop":
        finish_reason = "tool_calls"

    choice = SimpleNamespace(
        index=0,
        message=msg,
        finish_reason=finish_reason,
    )

    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model="",
    )


# ---------------------------------------------------------------------------
# High-level API: call Bedrock Converse
# ---------------------------------------------------------------------------

def build_converse_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Build kwargs for ``bedrock-runtime.converse()`` or ``converse_stream()``.

    Converts OpenAI-format inputs to Converse API parameters.
    """
    system_prompt, converse_messages = convert_messages_to_converse(messages)
    cache_enabled = _model_supports_prompt_cache(model)

    kwargs: Dict[str, Any] = {
        "modelId": model,
        "messages": converse_messages,
        "inferenceConfig": {
            "maxTokens": max_tokens,
        },
    }

    if system_prompt:
        if cache_enabled:
            system_prompt = system_prompt + [{"cachePoint": {"type": "default"}}]
        kwargs["system"] = system_prompt

    from agent.anthropic_adapter import _forbids_sampling_params

    if not _forbids_sampling_params(model):
        if temperature is not None:
            kwargs["inferenceConfig"]["temperature"] = temperature

        if top_p is not None:
            kwargs["inferenceConfig"]["topP"] = top_p

    if stop_sequences:
        kwargs["inferenceConfig"]["stopSequences"] = stop_sequences

    if tools:
        converse_tools = convert_tools_to_converse(tools)
        if converse_tools:
            # Some Bedrock models don't support tool/function calling (e.g.
            # DeepSeek R1, reasoning-only models).  Sending toolConfig to
            # these models causes a ValidationException → retry loop → failure.
            # Strip tools for known non-tool-calling models and warn the user.
            # Ref: PR #7920 feedback from @ptlally, pattern from PR #4346.
            if _model_supports_tool_use(model):
                if cache_enabled:
                    converse_tools = converse_tools + [{"cachePoint": {"type": "default"}}]
                kwargs["toolConfig"] = {"tools": converse_tools}
            else:
                logger.warning(
                    "Model %s does not support tool calling — tools stripped. "
                    "The agent will operate in text-only mode.", model
                )

    if cache_enabled and len(converse_messages) >= 2:
        # Checkpoint everything up to (not including) the newest turn, so the
        # marker survives unchanged across requests as only the tail grows —
        # mirroring the Anthropic system_and_3 strategy in prompt_caching.py.
        content = converse_messages[-2].get("content")
        if isinstance(content, list) and content:
            content.append({"cachePoint": {"type": "default"}})

    if guardrail_config:
        kwargs["guardrailConfig"] = guardrail_config

    return kwargs


def call_converse(
    region: str,
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
) -> SimpleNamespace:
    """Call Bedrock Converse API (non-streaming) and return an OpenAI-compatible response.

    This is the primary entry point for the agent loop when using the Bedrock provider.
    """
    client = _get_bedrock_runtime_client(region)
    kwargs = build_converse_kwargs(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        guardrail_config=guardrail_config,
    )

    try:
        response = client.converse(**kwargs)
    except Exception as exc:
        if is_stale_connection_error(exc):
            logger.warning(
                "bedrock: stale-connection error on converse(region=%s, model=%s): "
                "%s — evicting cached client so the next call reconnects.",
                region, model, type(exc).__name__,
            )
            invalidate_runtime_client(region)
        raise
    return normalize_converse_response(response)


def call_converse_stream(
    region: str,
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
) -> SimpleNamespace:
    """Call Bedrock ConverseStream API and return an OpenAI-compatible response.

    Consumes the full stream and returns the assembled response. For true
    streaming with delta callbacks, use ``iter_converse_stream()`` instead.
    """
    client = _get_bedrock_runtime_client(region)
    kwargs = build_converse_kwargs(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        guardrail_config=guardrail_config,
    )

    try:
        response = client.converse_stream(**kwargs)
    except Exception as exc:
        if is_streaming_access_denied_error(exc):
            # IAM allows bedrock:InvokeModel but not
            # InvokeModelWithResponseStream — permanent for this session.
            # Fall back to the non-streaming converse() path.
            logger.info(
                "bedrock: converse_stream denied by IAM on (region=%s, model=%s) — "
                "falling back to non-streaming converse().",
                region, model,
            )
            return normalize_converse_response(client.converse(**kwargs))
        if is_stale_connection_error(exc):
            logger.warning(
                "bedrock: stale-connection error on converse_stream(region=%s, "
                "model=%s): %s — evicting cached client so the next call reconnects.",
                region, model, type(exc).__name__,
            )
            invalidate_runtime_client(region)
        raise
    return normalize_converse_stream_events(response)


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------





def discover_bedrock_models(region: str, provider_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return _catalog.discover_bedrock_models(region, provider_filter, client_factory=_get_bedrock_control_client)


# ---------------------------------------------------------------------------
# Error classification — Bedrock-specific exceptions
# ---------------------------------------------------------------------------
# Mirrors OpenClaw's classifyFailoverReason() and matchesContextOverflowError()
# in extensions/amazon-bedrock/register.sync.runtime.ts.

# Patterns that indicate the input context exceeded the model's token limit.
# Used by run_agent.py to trigger context compression instead of retrying.
CONTEXT_OVERFLOW_PATTERNS = [
    re.compile(r"ValidationException.*(?:input is too long|max input token|input token.*exceed)", re.IGNORECASE),
    re.compile(r"ValidationException.*(?:exceeds? the (?:maximum|max) (?:number of )?(?:input )?tokens)", re.IGNORECASE),
    re.compile(r"ModelStreamErrorException.*(?:Input is too long|too many input tokens)", re.IGNORECASE),
]

# Patterns for throttling / rate limit errors — should trigger backoff + retry.
THROTTLE_PATTERNS = [
    re.compile(r"ThrottlingException", re.IGNORECASE),
    re.compile(r"Too many concurrent requests", re.IGNORECASE),
    re.compile(r"ServiceQuotaExceededException", re.IGNORECASE),
]

# Patterns for transient overload — model is temporarily unavailable.
OVERLOAD_PATTERNS = [
    re.compile(r"ModelNotReadyException", re.IGNORECASE),
    re.compile(r"ModelTimeoutException", re.IGNORECASE),
    re.compile(r"InternalServerException", re.IGNORECASE),
]


def is_context_overflow_error(error_message: str) -> bool:
    """Return True if the error indicates the input context was too large.

    When this returns True, the agent should compress context and retry
    rather than treating it as a fatal error.
    """
    return any(p.search(error_message) for p in CONTEXT_OVERFLOW_PATTERNS)


def classify_bedrock_error(error_message: str) -> str:
    """Classify a Bedrock error for retry/failover decisions.

    Returns:
      - ``"context_overflow"`` — input too long, compress and retry
      - ``"rate_limit"`` — throttled, backoff and retry
      - ``"overloaded"`` — model temporarily unavailable, retry with delay
      - ``"unknown"`` — unclassified error
    """
    if is_context_overflow_error(error_message):
        return "context_overflow"
    if any(p.search(error_message) for p in THROTTLE_PATTERNS):
        return "rate_limit"
    if any(p.search(error_message) for p in OVERLOAD_PATTERNS):
        return "overloaded"
    return "unknown"


# ---------------------------------------------------------------------------
# Bedrock model context lengths
# ---------------------------------------------------------------------------
# Static fallback table for models where the Bedrock API doesn't expose
# context window sizes.  Used by agent/model_metadata.py when dynamic
# detection is unavailable.

BEDROCK_CONTEXT_LENGTHS: Dict[str, int] = {
    # Anthropic Claude models on Bedrock.
    # Context windows per Anthropic's official models comparison
    # (https://platform.claude.com/docs/en/about-claude/models/overview).
    # Fable / Sonnet 5 / Opus 4.8 / 4.7 / 4.6 / Sonnet 4.6 have 1M generally
    # available (no beta header required as of April 2026). Sonnet 4.5 and
    # Sonnet 4 had their `context-1m-2025-08-07` beta retired on
    # April 30, 2026, so they are standard 200K; Haiku 4.5 is 200K.
    # These 1M entries must match agent/model_metadata.py
    # DEFAULT_CONTEXT_LENGTHS or the agent compresses context prematurely.
    # Keys are matched by longest-substring, so the versioned 4-6/4-7/4-8
    # entries win over the generic "anthropic.claude-opus-4" fallback.
    "anthropic.claude-fable-5":      1_000_000,
    "anthropic.claude-fable":        1_000_000,
    "anthropic.claude-sonnet-5":     1_000_000,
    "anthropic.claude-opus-4-8":     1_000_000,
    "anthropic.claude-opus-4-7":     1_000_000,
    "anthropic.claude-opus-4-6":     1_000_000,
    "anthropic.claude-sonnet-4-6":   1_000_000,
    "anthropic.claude-sonnet-4-5":   200_000,
    "anthropic.claude-haiku-4-5":    200_000,
    "anthropic.claude-opus-4":       200_000,
    "anthropic.claude-sonnet-4":     200_000,
    "anthropic.claude-3-5-sonnet":   200_000,
    "anthropic.claude-3-5-haiku":    200_000,
    "anthropic.claude-3-opus":       200_000,
    "anthropic.claude-3-sonnet":     200_000,
    "anthropic.claude-3-haiku":      200_000,
    # Amazon Nova
    "amazon.nova-pro":               300_000,
    "amazon.nova-lite":              300_000,
    "amazon.nova-micro":             128_000,
    # Meta Llama
    "meta.llama4-maverick":          128_000,
    "meta.llama4-scout":             128_000,
    "meta.llama3-3-70b-instruct":    128_000,
    # Mistral
    "mistral.mistral-large":         128_000,
    # DeepSeek
    "deepseek.v3":                   128_000,
}

# Default for unknown Bedrock models
BEDROCK_DEFAULT_CONTEXT_LENGTH = 128_000

# Probe tiers (in tokens).  We send a request padded just past each tier and
# read the real window from Bedrock's length-validation error.  Two reasons
# this is tiered rather than one giant request:
#   1. A wildly oversized payload (e.g. 5M tokens) makes Bedrock return an
#      opaque InternalServerException after retries instead of a clean
#      ValidationException — so we must stay within a sane overage.
#   2. Stepping up lets us discover larger windows (2M+) without over-padding
#      smaller ones.
# Each tier value is the *padding target*; the error reports the true maximum,
# which is what we actually return.
_BEDROCK_PROBE_TIERS = (1_300_000, 2_200_000)
_WORDS_PER_TOKEN = 0.9  # conservative: ensures the padded prompt clears the tier


def _static_bedrock_context_length(model_id: str) -> int:
    """Longest-substring-match lookup against the static fallback table.

    Uses substring matching so versioned IDs like
    ``anthropic.claude-sonnet-4-6-20250514-v1:0`` resolve correctly.
    """
    model_lower = model_id.lower()
    best_key = ""
    best_val = BEDROCK_DEFAULT_CONTEXT_LENGTH
    for key, val in BEDROCK_CONTEXT_LENGTHS.items():
        if key in model_lower and len(key) > len(best_key):
            best_key = key
            best_val = val
    return best_val


def probe_bedrock_context_length(model_id: str, region: str) -> Optional[int]:
    """Discover a Bedrock model's real context window by provoking a length error.

    Bedrock does not expose the context window via any metadata API
    (``get-foundation-model`` omits it, ``Converse`` metrics omit it,
    ``CountTokens`` is unsupported on several models).  The only authoritative
    source is the ``ValidationException`` raised when a prompt exceeds the
    window:

        "The model returned the following errors: prompt is too long:
         1300032 tokens > 1000000 maximum"

    Length validation happens *before* inference, so an oversized request is
    rejected immediately and cheaply — no tokens are generated and no input is
    actually processed.  We pad a request just past each tier in
    ``_BEDROCK_PROBE_TIERS`` and parse the reported ``maximum``.  Tiers exist
    because (a) a *wildly* oversized payload makes Bedrock fail with an opaque
    InternalServerException instead of a clean length error, and (b) stepping
    up discovers larger windows without over-padding smaller ones.

    Returns the detected window, or ``None`` if the probe could not run
    (missing credentials, network error, or no parseable limit) so the caller
    can fall back to the static table.
    """
    try:
        from agent.model_metadata import parse_context_limit_from_error
    except ImportError:  # pragma: no cover — same package
        return None

    try:
        client = _get_bedrock_runtime_client(region)
    except Exception as exc:  # boto3 missing / credential resolution failure
        logger.debug("Bedrock context probe skipped for %s: %s", model_id, exc)
        return None

    last_error = ""
    for tier_tokens in _BEDROCK_PROBE_TIERS:
        pad_words = int(tier_tokens / _WORDS_PER_TOKEN)
        oversized = "data " * pad_words
        try:
            client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": oversized}]}],
                inferenceConfig={"maxTokens": 8},
            )
            # Accepted a prompt this large → the window is at least this tier.
            # Returning the tier as a lower bound is safe and avoids inventing
            # a number we can't confirm.
            logger.debug(
                "Bedrock context probe for %s accepted ~%s-token prompt; "
                "window is at least that", model_id, f"{tier_tokens:,}",
            )
            return tier_tokens
        except Exception as exc:
            msg = str(exc)
            last_error = msg
            limit = parse_context_limit_from_error(msg)
            if limit and limit >= 1024:
                logger.info(
                    "Probed Bedrock context window for %s: %s tokens",
                    model_id, f"{limit:,}",
                )
                return limit
            # No parseable limit at this tier (opaque server error, auth,
            # throttle).  Try the next, smaller-overage strategy is N/A here —
            # tiers ascend — so just continue; if all fail we return None.
            continue

    logger.debug(
        "Bedrock context probe for %s returned no parseable limit: %s",
        model_id, last_error[:200],
    )
    return None


def get_bedrock_context_length(model_id: str, region: str = "", probe: bool = True) -> int:
    """Resolve the context window for a Bedrock model.

    Resolution order:
      1. Live probe against Bedrock (authoritative; cached by the caller).
      2. Static fallback table (longest-substring match).
      3. Conservative default.

    The static table is intentionally a *fallback*, not the primary source:
    AWS ships new model versions (opus-4-7, opus-4-8, ...) faster than the
    table can track, and a stale entry silently caps the window (e.g. a
    1M-token Opus pinned to 200K via an ``opus-4`` substring match).  The
    probe asks Bedrock directly so every model — current or future — gets its
    real window with no table maintenance.

    ``probe=False`` (or an empty ``region``) skips the network call and uses
    the static table only — used by pure-offline/display code paths.
    """
    if probe and region:
        probed = probe_bedrock_context_length(model_id, region)
        if probed:
            return probed
    return _static_bedrock_context_length(model_id)


# Public export contract: retain the historical wildcard surface and shared aliases.
__all__ = [
    'Any',
    'BEDROCK_CONTEXT_LENGTHS',
    'BEDROCK_DEFAULT_CONTEXT_LENGTH',
    'CONTEXT_OVERFLOW_PATTERNS',
    'Dict',
    'List',
    'OVERLOAD_PATTERNS',
    'Optional',
    'SimpleNamespace',
    'THROTTLE_PATTERNS',
    'Tuple',
    '_extract_provider_from_arn',
    'bedrock_model_ids_or_none',
    'build_converse_kwargs',
    'call_converse',
    'call_converse_stream',
    'classify_bedrock_error',
    'convert_messages_to_converse',
    'convert_tools_to_converse',
    'discover_bedrock_models',
    'ensure',
    'get_bedrock_context_length',
    'has_aws_credentials',
    'invalidate_runtime_client',
    'is_anthropic_bedrock_model',
    'is_context_overflow_error',
    'is_stale_connection_error',
    'is_streaming_access_denied_error',
    'json',
    'logger',
    'logging',
    'normalize_converse_response',
    'normalize_converse_stream_events',
    'probe_bedrock_context_length',
    're',
    'reset_client_cache',
    'reset_discovery_cache',
    'resolve_aws_auth_env_var',
    'resolve_bedrock_region',
    'stream_converse_with_callbacks',
]
