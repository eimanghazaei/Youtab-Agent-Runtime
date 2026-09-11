"""Canonical runtime identity-binding contract (WAVE-30H).

ONE source of truth for a managed run's effective execution identity: which
provider + model + endpoint-class + cost-policy (and a DEFERRED model digest) a run
is bound to. The binding is persisted as a durable, immutable, append-only event
(:data:`BINDING_EVENT`) on the run's own event stream at create time, inherited
UNCHANGED by retries/respawns, and inherited-or-explicitly-rebound by delegated
children (:data:`CHILD_BINDING_EVENT`). A rebind appends a NEW ``binding_version``;
nothing ever mutates a prior binding event.

:func:`build_effective_binding` is PURE — config/string/``ipaddress`` classification
only — so it runs on the ``create_run`` hot path with NO network/Ollama probe. The
model-manifest digest is the only networked attestation input and is therefore
never resolved here (``digest_status`` starts ``"not_probed"``/``"not_applicable"``,
``model_digest`` is ``None``); it is filled only by the separate preflight
attestation step.

No secret ever enters a binding: provider/model are identifiers, the endpoint is
reduced to a CLASS (never the raw URL), and credentials never appear.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlparse

# Durable, append-only event kinds on a run's event stream.
BINDING_EVENT = "runtime_effective_binding"
CHILD_BINDING_EVENT = "runtime_child_effective_binding"

# The canonical payload field set (validation / test comparability).
BINDING_FIELDS = (
    "binding_version", "provider", "model", "model_ref",
    "model_identifier_status", "execution", "endpoint_class",
    "provider_cost_policy", "digest_status", "model_digest", "bound_at",
)

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# 6to4 (2002::/16) and Teredo (2001::/32) embed a PUBLIC IPv4 destination yet report
# ``is_private`` in the stdlib — treat them as public so they can never classify as
# verified-local (mirrors the runtime attestation classifier).
_V6_PUBLIC_TUNNELS = (
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)


def classify_endpoint(url: Optional[str]) -> str:
    """Classify an endpoint host WITHOUT exposing the raw endpoint.

    Returns loopback|private|link_local|cgnat|public|hostname|unavailable|invalid.
    Pure; mirrors ``web_routers.runtime._endpoint_class`` so a binding built here
    agrees field-for-field with the runtime's own attestation.
    """
    raw = (url or "").strip()
    if not raw:
        return "unavailable"
    if not raw.startswith("http"):
        raw = "http://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001
        return "invalid"
    if not host:
        return "invalid"
    if host in _LOOPBACK_NAMES:
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "hostname"
    if ip.is_loopback:
        return "loopback"
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _V6_PUBLIC_TUNNELS):
        return "public"
    if ip.is_link_local:
        return "link_local"
    if ip in _CGNAT_NET:
        return "cgnat"
    if ip.is_private:
        return "private"
    return "public"


def utc_iso_now() -> str:
    """UTC ``bound_at`` stamp (seconds granularity, matching the event stream)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_effective_binding(
    *,
    provider: Optional[str],
    model: Optional[str],
    model_ref: Optional[str] = None,
    endpoint: Optional[str] = None,
    binding_version: int = 1,
    bound_at: Optional[str] = None,
    model_identifier_status: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical binding from already-resolved identity pieces.

    PURE: config + ``ipaddress`` classification only, no network. ``execution``,
    ``endpoint_class`` and ``provider_cost_policy`` are derived from provider +
    endpoint exactly as the runtime attestation derives them. For a verified-local
    Ollama model ``digest_status`` is ``"not_probed"`` (the probe is deferred to
    preflight attestation); otherwise ``"not_applicable"``. ``model_digest`` is
    always ``None`` here. A falsy ``model`` yields an unresolved status (never a
    placeholder presented as the effective model). ``extra`` lets a caller attach
    link fields (e.g. a child binding's ``parent_run_id``) without widening the
    core contract.
    """
    from agent import usage_pricing as _up
    from youtab_agent_cli import engine_connection as _ec

    prov = (provider or "").strip().lower() or None
    mdl = (model or "").strip() or None
    is_local = bool(prov) and prov in _ec.LOCAL_SERVER_PROVIDERS
    local_zero = bool(prov) and _up.classify_local_zero(prov, endpoint)
    execution = "local" if is_local else "cloud"
    endpoint_class = classify_endpoint(endpoint) if is_local else "cloud"
    if local_zero:
        cost_policy = "local_zero_verified"
    elif is_local:
        cost_policy = "unpriced"
    else:
        cost_policy = "campaign_budget_eur"
    if mdl:
        status = model_identifier_status or "resolved"
        ref = model_ref or (f"{prov}/{mdl}" if prov else mdl)
    else:
        status = model_identifier_status or "OWNER_SELECTION_REQUIRED"
        ref = None
    digest_status = "not_probed" if (prov == "ollama" and local_zero and mdl) else "not_applicable"
    binding: Dict[str, Any] = {
        "binding_version": int(binding_version),
        "provider": prov,
        "model": mdl,
        "model_ref": ref,
        "model_identifier_status": status,
        "execution": execution,
        "endpoint_class": endpoint_class,
        "provider_cost_policy": cost_policy,
        "digest_status": digest_status,
        "model_digest": None,
        "bound_at": bound_at or utc_iso_now(),
    }
    if extra:
        binding.update(extra)
    return binding


def effective_binding_from_events(events: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """Return the CURRENT effective binding for a run, or ``None``.

    The current binding is the highest-``binding_version`` :data:`BINDING_EVENT`
    payload (append-only: a rebind appends version+1, so selecting by max version is
    robust to event ordering). Returns ``None`` for a legacy/standalone run with no
    binding event, and ``{"__corrupt__": True}`` when a binding event lacks a valid
    integer ``binding_version`` (so the caller can fail closed rather than guess).
    """
    current: Optional[Dict[str, Any]] = None
    best = -1
    for e in events:
        if getattr(e, "kind", None) != BINDING_EVENT:
            continue
        payload = getattr(e, "payload", None)
        if not isinstance(payload, dict):
            continue
        try:
            version = int(payload.get("binding_version"))
        except (TypeError, ValueError):
            return {"__corrupt__": True}
        if version > best:
            best, current = version, dict(payload)
    return current
