"""Canonical, server-side connection resolution for a product engine.

The problem this closes: an engine's AVAILABILITY probe and its actual
INFERENCE used to derive their endpoint from *different* settings — the probe
read ``OLLAMA_BASE_URL`` (and dropped any non-loopback host), while execution
read the profile config's ``model.base_url``. Health could therefore report
"online" for one endpoint while a run targeted another, or (the live-ECO case)
report a remote Mac endpoint as "unavailable" that a run would in fact reach.

This module is the ONE resolver both paths consume. Given a product
``profile_id`` it returns a :class:`ResolvedConnection` whose ``endpoint`` and
``model`` are the single truth for both health and execution. The physical
connection is referenced indirectly (``connection_ref``) and resolved from
protected server-side settings (deployment/secret-file env), never from a
tenant-facing input; the raw endpoint / connection_ref / model_ref are internal
and are never placed in a consumer projection, a process argument, or an
ordinary log line.

Provider/model come from :func:`agent_identity.engine_binding_for_profile`
(which already honours the ``YOUTAB_ECO_MODEL`` deployment override), so there
is no second roster here — only the endpoint half is added.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from youtab_agent_cli import agent_identity

# A resolved ECO endpoint must be a private/loopback/tailnet target — the Mac
# lives on the Tailscale CGNAT range (100.64.0.0/10). A public-Internet endpoint
# is refused (fail closed) unless a deliberate future policy opts in via this
# env; a credentials-in-URL or malformed endpoint is always refused. This keeps
# the probe from being pointed at an arbitrary public host (SSRF) and keeps ECO
# on infrastructure the Owner controls.
_ALLOW_PUBLIC_ENDPOINT_ENV = "YOUTAB_ECO_ALLOW_PUBLIC_ENDPOINT"
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# 6to4 (2002::/16) and Teredo (2001::/32) IPv6 literals embed a PUBLIC IPv4
# destination yet report ``is_private`` — treat them as public (fail closed).
_V6_PUBLIC_TUNNELS = (
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)

# Providers whose availability is a reachability probe against a model server
# (as opposed to an external-credential presence check). Kept in step with
# web_routers.runtime._LOCAL_ENGINE_PROVIDERS.
LOCAL_SERVER_PROVIDERS = frozenset(
    {"ollama", "vllm", "llamacpp", "llama.cpp", "llama-cpp", "lmstudio"}
)

# The protected env(s) that carry each local provider's endpoint. These are
# delivered by the deployment (VPS .env / secret file, mode 0400) and are never
# committed. First non-empty wins; falls back to the loopback default.
_ENDPOINT_ENV_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "ollama": ("OLLAMA_BASE_URL", "OLLAMA_HOST"),
    "vllm": ("VLLM_BASE_URL",),
    "llamacpp": ("VLLM_BASE_URL",),
    "llama.cpp": ("VLLM_BASE_URL",),
    "llama-cpp": ("VLLM_BASE_URL",),
    "lmstudio": ("LMSTUDIO_BASE_URL",),
}
_ENDPOINT_DEFAULT_BY_PROVIDER: dict[str, str] = {
    "ollama": "http://127.0.0.1:11434",
    "vllm": "http://127.0.0.1:8000/v1",
    "llamacpp": "http://127.0.0.1:8000/v1",
    "llama.cpp": "http://127.0.0.1:8000/v1",
    "llama-cpp": "http://127.0.0.1:8000/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


@dataclass(frozen=True)
class ResolvedConnection:
    """The canonical, server-side resolution for one product engine.

    Every field is internal. Nothing here may be projected to a tenant surface;
    the public catalogue exposes only branded identity (see
    ``web_routers.runtime.runtime_engines`` + ``engine_catalog``).
    """

    product_profile_id: str
    connection_ref: str
    model_ref: str
    endpoint: str
    provider: str
    model: str
    capabilities: tuple[str, ...] = ()
    availability_policy: str = "probe"  # "probe" (reachability) | "credential"
    timeout_policy_seconds: float = 30.0
    fallback_policy: str = "ladder"  # governed Auto ladder decides fallback

    def is_local_server(self) -> bool:
        return self.provider in LOCAL_SERVER_PROVIDERS


def _public_endpoint_allowed() -> bool:
    return (os.environ.get(_ALLOW_PUBLIC_ENDPOINT_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def endpoint_is_authorized(url: str) -> bool:
    """Whether a resolved endpoint may be used/probed. Fails closed on anything odd.

    Rejects: empty, malformed, a URL carrying credentials (``user:pass@host``),
    and a public-Internet host (unless the explicit opt-in env is set). Accepts
    loopback, RFC1918 private, link-local and the Tailscale CGNAT range. A bare
    hostname (not an IP) is rejected — we never DNS-resolve here (that would be
    I/O and an SSRF surface); the authorised targets are addressed by IP.
    """
    raw = (url or "").strip()
    if not raw:
        return False
    if not raw.startswith("http"):
        raw = "http://" + raw
    try:
        u = urlparse(raw)
    except Exception:  # noqa: BLE001 — malformed => refused
        return False
    if not u.hostname:
        return False
    if u.username or u.password:  # credentials-in-URL => refused
        return False
    host = u.hostname.lower().rstrip(".")
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname, not an IP — not an authorised target
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _V6_PUBLIC_TUNNELS):
        return _public_endpoint_allowed()  # 6to4/Teredo embed a public dest
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip in _TAILNET_CGNAT:
        return True
    return _public_endpoint_allowed()


def _endpoint_for_provider(provider: str) -> str:
    """Resolve a local provider's endpoint from protected env, else the default.

    An endpoint that fails :func:`endpoint_is_authorized` (public host without
    opt-in, credentials-in-URL, malformed) resolves to ``""`` — the engine then
    reads as unavailable (fail closed) rather than probing/dialing it.
    """
    p = (provider or "").strip().lower()
    for name in _ENDPOINT_ENV_BY_PROVIDER.get(p, ()):  # first non-empty wins
        raw = (os.environ.get(name) or "").strip().rstrip("/")
        if raw:
            if not raw.startswith("http"):
                raw = "http://" + raw
            return raw if endpoint_is_authorized(raw) else ""
    return _ENDPOINT_DEFAULT_BY_PROVIDER.get(p, "")


def normalize_endpoint(url: str) -> str:
    """The host:port *target* identity for comparison — path is deliberately dropped.

    Health probes the endpoint root (``/api/tags``) while inference dials the
    OpenAI-compatible sub-path (``/v1``); those are the same *server* and must
    compare equal, so agreement is decided on scheme+host+port only. Loopback
    hosts collapse to one, so ``http://localhost:11434`` and
    ``http://127.0.0.1:11434/v1`` are the same target, while a different host or
    port is a genuine split-brain.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith("http"):
        raw = "http://" + raw
    try:
        u = urlparse(raw)
    except Exception:  # noqa: BLE001
        return raw.lower()
    host = (u.hostname or "").lower().rstrip(".")
    if host in _LOOPBACK_HOSTS:
        host = "loopback"
    port = u.port
    if port is None:
        port = 443 if u.scheme == "https" else 80
    return f"{u.scheme}://{host}:{port}"


def endpoints_agree(a: str, b: str) -> bool:
    """True iff two endpoints resolve to the same server target (host:port).

    An empty ``b`` (no independently-configured inference base_url) is treated
    as *agreeing* — it is a not-configured state, not a conflicting host — so the
    guard never false-fails a correct setup; it fires only when a base_url is
    explicitly set to a *different server* than the availability endpoint.
    """
    nb = normalize_endpoint(b)
    if not nb:
        return True
    return normalize_endpoint(a) == nb


def resolve_connection(profile_id: Optional[str]) -> Optional[ResolvedConnection]:
    """Resolve a product ``profile_id`` to its canonical connection, or ``None``.

    ``None`` for an unknown/unbound profile (a valid state — the caller renders
    the generic product name / reports unavailable, never guesses). Provider and
    model come from the binding half of the identity artifact; the endpoint is
    the protected server-side value for that provider.
    """
    if not profile_id:
        return None
    bound = agent_identity.engine_binding_for_profile(profile_id)
    if not bound:
        return None
    provider, model = bound
    provider = (provider or "").strip().lower()
    endpoint = _endpoint_for_provider(provider) if provider in LOCAL_SERVER_PROVIDERS else ""
    return ResolvedConnection(
        product_profile_id=profile_id,
        # connection_ref is an opaque server-side handle, not the raw endpoint.
        connection_ref=f"{provider}:{profile_id}",
        model_ref=f"{provider}/{model}",
        endpoint=endpoint,
        provider=provider,
        model=model,
        availability_policy="probe" if provider in LOCAL_SERVER_PROVIDERS else "credential",
    )


class ConnectionMismatchError(RuntimeError):
    """Health-endpoint and inference-endpoint would differ for a profile.

    Raised so a run fails CLOSED rather than executing against an endpoint the
    availability check never validated. Carries only internal, non-tenant text.
    """

    def __init__(self, profile_id: str, canonical: str, configured: str) -> None:
        self.profile_id = profile_id
        self.canonical = canonical
        self.configured = configured
        super().__init__(
            "connection resolution mismatch for "
            f"{profile_id}: availability endpoint {normalize_endpoint(canonical)!r} "
            f"!= configured inference endpoint {normalize_endpoint(configured)!r}"
        )


def assert_consistent(conn: ResolvedConnection, configured_inference_base_url: str) -> None:
    """Fail closed when execution would target a different endpoint than health.

    ``conn`` is the canonical resolution health uses. ``configured_inference_base_url``
    is whatever the run would actually dial (e.g. config ``model.base_url``).
    Only local-server engines have an endpoint to reconcile; external providers
    have no endpoint here and always pass.
    """
    if not conn.is_local_server():
        return
    if not endpoints_agree(conn.endpoint, configured_inference_base_url):
        raise ConnectionMismatchError(
            conn.product_profile_id, conn.endpoint, configured_inference_base_url
        )
