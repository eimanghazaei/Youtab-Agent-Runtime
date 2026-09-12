"""WAVE-30F — authoritative native-timing mechanism for the local Ollama path.

Why this exists
---------------
The Agent Runtime's local ECO/Qwen traffic goes through Ollama's
*OpenAI-compatible* endpoint (``/v1/chat/completions``). That endpoint returns a
standard OpenAI ``ChatCompletion`` object and **does not carry** Ollama's native
timing block (``total_duration`` / ``load_duration`` / ``prompt_eval_count`` /
``prompt_eval_duration`` / ``eval_count`` / ``eval_duration``). Those fields are
emitted only by Ollama's **native** endpoints, ``/api/chat`` and ``/api/generate``
(see https://github.com/ollama/ollama/blob/main/docs/api.md). Consequently
:func:`youtab_runtime.model_timings.extract_native_ollama_timings` returns ``{}``
for every live compat response — the runtime can record its own wall-clock, but
not the model's authoritative load / prompt-eval / generation split.

This module is the capability-preserving mechanism that closes that gap **without
changing the production transport's model semantics or breaking tool calling**:
it talks to Ollama's native ``/api/chat`` endpoint directly, using the SAME
``messages``, ``tools`` and ``options`` the runtime assembled, and parses the
native timing block that ``/api/chat`` returns. It is used by the controlled
bottleneck-attribution matrix (paths A and B) to measure the model's real
prompt-eval / generation cost of the exact runtime prompt, side by side with the
runtime's wall-clock, so the dominant delay can be *proven* rather than assumed.

It is a diagnostic client. It is **not** wired into the live conversation loop —
switching the production transport from ``/v1/chat/completions`` to ``/api/chat``
would be a semantic change and is deliberately left as an ADR-gated follow-up.

Design
------
* The network wrapper (:func:`native_chat`) is a thin ``httpx`` POST. All response
  interpretation lives in **pure** functions (:func:`parse_native_nonstream`,
  :func:`parse_native_stream`) that take already-decoded JSON / NDJSON lines, so
  the parsing is unit-tested deterministically against recorded Ollama response
  shapes with no live network in CI.
* Durations stay in native nanoseconds in the returned ``raw_timings`` mapping and
  are converted to milliseconds only by the shared
  :func:`youtab_runtime.model_timings.extract_native_ollama_timings`, so there is
  exactly one ns→ms authority in the codebase.
* No secret is read, logged, or persisted here; ``api_key`` (if any) is passed as a
  bearer header and never stored or echoed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# The native Ollama timing fields that ride on the FINAL (``done: true``) message
# of an ``/api/chat`` response. Copied verbatim (nanoseconds / integer counts);
# ns→ms conversion is the shared model_timings authority's job, not ours.
_NATIVE_TIMING_KEYS = (
    "total_duration",
    "load_duration",
    "prompt_eval_count",
    "prompt_eval_cached_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
)

DEFAULT_NATIVE_BASE_URL = "http://localhost:11434"


def _assert_local_host(url: str) -> None:
    """Fail closed unless ``url``'s host resolves entirely to loopback/private IPs.

    Enforces the module's "local Ollama only" promise in code (not just by
    convention): if a misconfigured or cloud/attacker ``base_url`` is ever passed,
    the runtime prompt + any bearer key must NOT egress. Every resolved address for
    the host must be loopback, link-local, or RFC-1918 private; otherwise raise.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip()
    if not host:
        raise ValueError("native Ollama base_url has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:  # unresolvable host → refuse rather than leak
        raise ValueError(f"native Ollama host {host!r} did not resolve: {exc}") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise ValueError(f"native Ollama host {host!r} did not resolve")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise ValueError(
                f"native Ollama base_url must be local (loopback/private); host "
                f"{host!r} resolved to non-local {a} — refusing to send prompt/key"
            )


def derive_native_base_url(openai_base_url: Optional[str]) -> str:
    """Map an OpenAI-compat base URL to the Ollama native API root.

    The runtime configures the local provider with an OpenAI-compat base URL that
    ends in ``/v1`` (e.g. ``http://host:11434/v1``). Ollama's native endpoints live
    at the server root (``http://host:11434/api/chat``). This strips a trailing
    ``/v1`` (with or without a trailing slash) so the native client hits the right
    path. An empty/None base URL falls back to the documented local default.
    """
    base = (openai_base_url or "").strip()
    if not base:
        return DEFAULT_NATIVE_BASE_URL
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base or DEFAULT_NATIVE_BASE_URL


@dataclass
class NativeChatResult:
    """Outcome of one native ``/api/chat`` call.

    ``raw_timings`` holds the native nanosecond/count fields verbatim (feed it to
    :func:`youtab_runtime.model_timings.extract_native_ollama_timings` for ms). The
    wall-clock and TTFT are the *client's* own measurements, kept distinct from the
    model's native ``total_duration``.
    """

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    raw_timings: Dict[str, Any] = field(default_factory=dict)
    wall_s: Optional[float] = None
    ttft_s: Optional[float] = None
    model: str = ""
    done_reason: Optional[str] = None
    chunk_count: int = 0


def _pick_timings(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy the native timing fields present on a decoded ``/api/chat`` object."""
    out: Dict[str, Any] = {}
    for k in _NATIVE_TIMING_KEYS:
        v = obj.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def _extract_tool_calls(message: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the tool_calls list from a native ``message`` block, if any.

    Ollama returns ``message.tool_calls`` as ``[{"function": {"name", "arguments"}}]``
    where ``arguments`` is already a JSON OBJECT (not the OpenAI string form). We
    preserve the native shape verbatim; the diagnostic harness only needs to know a
    tool was requested and which one, not to replay it through the OpenAI codec.
    """
    tc = message.get("tool_calls")
    if isinstance(tc, list):
        return [c for c in tc if isinstance(c, Mapping)]
    return []


def parse_native_nonstream(obj: Mapping[str, Any]) -> NativeChatResult:
    """Parse a single non-streaming ``/api/chat`` JSON object (``stream:false``)."""
    message = obj.get("message")
    message = message if isinstance(message, Mapping) else {}
    content = message.get("content")
    return NativeChatResult(
        content=content if isinstance(content, str) else "",
        tool_calls=_extract_tool_calls(message),
        raw_timings=_pick_timings(obj),
        model=str(obj.get("model") or ""),
        done_reason=(str(obj["done_reason"]) if obj.get("done_reason") else None),
        chunk_count=1,
    )


def parse_native_stream(
    lines: Iterable[str],
    *,
    opened_at: Optional[float] = None,
    first_token_clock: Optional[Any] = None,
    line_recv_times: Optional[Sequence[float]] = None,
) -> NativeChatResult:
    """Parse an NDJSON ``/api/chat`` stream into one aggregated result.

    Each non-empty line is one JSON object. Intermediate objects carry
    ``message.content`` fragments (and possibly ``tool_calls``); the final object
    has ``done: true`` and the native timing block.

    ``ttft_s`` is the delay from ``opened_at`` to when the FIRST content/tool-call
    line was RECEIVED. The authoritative source is ``line_recv_times`` — the
    monotonic receive timestamp captured per line by :func:`native_chat` as bytes
    arrive (so TTFT reflects real time-to-first-token, not parse time). The
    ``first_token_clock`` callable is a fallback for deterministic unit tests that
    simulate receive-time reads. When neither is available, TTFT is left unmeasured
    (never zero-filled). ``line_recv_times`` is indexed by RAW line position
    (including blanks) so it stays aligned with the input iterable.
    """
    result = NativeChatResult()
    parts: List[str] = []
    recv = list(line_recv_times) if line_recv_times is not None else None
    for idx, line in enumerate(lines):
        s = line.strip() if isinstance(line, str) else ""
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, Mapping):
            continue
        result.chunk_count += 1
        if obj.get("model") and not result.model:
            result.model = str(obj["model"])
        message = obj.get("message")
        message = message if isinstance(message, Mapping) else {}
        frag = message.get("content")
        tcs = _extract_tool_calls(message)
        produced = bool((isinstance(frag, str) and frag) or tcs)
        if produced and result.ttft_s is None and opened_at is not None:
            token_t: Optional[float] = None
            if recv is not None and idx < len(recv):
                token_t = recv[idx]              # real receive time of this line
            elif first_token_clock is not None:
                try:
                    token_t = float(first_token_clock())
                except (TypeError, ValueError):
                    token_t = None
            if token_t is not None:
                result.ttft_s = max(0.0, token_t - float(opened_at))
        if isinstance(frag, str) and frag:
            parts.append(frag)
        if tcs:
            result.tool_calls.extend(tcs)
        if obj.get("done"):
            timings = _pick_timings(obj)
            if timings:
                result.raw_timings = timings
            if obj.get("done_reason"):
                result.done_reason = str(obj["done_reason"])
    result.content = "".join(parts)
    return result


def native_chat(
    *,
    base_url: Optional[str],
    model: str,
    messages: List[Mapping[str, Any]],
    tools: Optional[List[Mapping[str, Any]]] = None,
    options: Optional[Mapping[str, Any]] = None,
    keep_alive: Optional[Any] = None,
    think: Optional[bool] = None,
    stream: bool = True,
    api_key: Optional[str] = None,
    timeout: float = 600.0,
) -> NativeChatResult:
    """Call Ollama's native ``/api/chat`` and return the parsed result + timings.

    Thin ``httpx`` wrapper around the pure parsers above. Sends the runtime's exact
    ``messages`` / ``tools`` / ``options`` so the measured prompt-eval and
    generation cost corresponds to the real runtime prompt. Local-only by
    construction (native Ollama is a localhost/LAN service); it never contacts a
    cloud provider. Raises on transport/HTTP error so a diagnostic run fails loudly
    rather than reporting fabricated timings.
    """
    import httpx  # lazy: keep module import-safe and network-free

    root = derive_native_base_url(base_url)
    _assert_local_host(root)  # fail closed: never egress the prompt/key to a non-local host
    url = f"{root}/api/chat"
    payload: Dict[str, Any] = {"model": model, "messages": list(messages), "stream": bool(stream)}
    if tools:
        payload["tools"] = list(tools)
    if options:
        payload["options"] = dict(options)
    if keep_alive is not None and str(keep_alive).strip() != "":
        payload["keep_alive"] = keep_alive
    if think is not None:
        payload["think"] = bool(think)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started = time.monotonic()
    if stream:
        collected: List[str] = []
        recv_times: List[float] = []
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    # Timestamp each line AS IT ARRIVES so TTFT reflects real
                    # time-to-first-token, not post-drain parse time (HIGH-1).
                    recv_times.append(time.monotonic())
                    collected.append(line)
        result = parse_native_stream(
            collected, opened_at=started, line_recv_times=recv_times
        )
    else:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            result = parse_native_nonstream(resp.json())
    result.wall_s = max(0.0, time.monotonic() - started)
    return result
