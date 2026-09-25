"""Canonical runtime identity-binding contract (WAVE-30H).

ONE source of truth for a managed run's effective execution identity: which
provider + model + normalized endpoint fingerprint + endpoint-class + cost-policy
(and a DEFERRED model digest) a run is bound to, plus the run_id / tenant /
workspace it belongs to. The binding is persisted as a durable, immutable,
append-only event (:data:`BINDING_EVENT`) on the run's own event stream at create
time, validated + dispatched-from by the worker BEFORE any provider client or
budget consumption, RE-SCOPED to the child run on retry (same substrate, the
child's own ``run_id`` — see :func:`rescope_binding`), inherited UNCHANGED on
respawn (same run), and inherited EXACTLY (same substrate) by delegated children.
Nothing ever mutates a prior binding event.

Integrity: every binding carries a ``binding_hash`` — sha256 over its canonical
identity fields (including run_id/tenant/workspace AND digest_status/model_digest,
excluding only the volatile ``bound_at``). :func:`verify_binding` recomputes and
constant-time compares it, so a tampered/malformed binding fails closed.
:func:`binding_digest` is the SUBSTRATE-only digest (no run/tenant, no model_digest)
used to make benchmark preflight→create atomic: the same substrate yields the same
digest at preflight (which may have probed the manifest) and at the pure create.

:func:`build_effective_binding` is PURE — config/string/``ipaddress`` classification
only — so it runs on the ``create_run`` hot path with NO network/Ollama probe. The
model-manifest digest is never PROBED here; it is only PINNED when an already-
attested digest is passed in (from an authenticated preflight), which sets
``digest_status="attested"`` and is covered by ``binding_hash`` (tamper-evident at
rest). Absent that, ``digest_status`` starts ``"not_probed"``/``"not_applicable"``
and ``model_digest`` is ``None``. The worker re-probes and compares an ``attested``
digest before executing, closing the tag→manifest TOCTOU.

No secret ever enters a binding: provider/model are identifiers, the endpoint is
reduced to a non-reversible FINGERPRINT + a CLASS (never the raw URL, never
credentials).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
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
    "endpoint_fingerprint", "provider_cost_policy", "digest_status",
    "model_digest", "run_id", "root_run_id", "tenant", "workspace",
    "binding_hash", "bound_at",
)

# VERSIONED canonical hash field sets (WAVE-30H #F7). The binding_hash algorithm is
# pinned PER binding_version so a binding is NEVER verified with a different version's
# field set (which would either reject an unchanged legacy binding or let one claim
# protection it never possessed). Both sets EXCLUDE only the volatile bound_at.
#
# v1 = the original pre-contract set (no model-digest coverage). v2 ADDS
# digest_status/model_digest: the model-manifest digest is attested ONCE at create
# from an authenticated preflight (never re-filled on a persisted binding), so
# covering it makes a tag->manifest re-point tamper-evident at rest. A v1 binding
# therefore CANNOT cryptographically possess attested model-digest protection — the
# worker refuses a v1 binding that claims ``digest_status=="attested"``.
#
# The SUBSTRATE digest (_DIGEST_FIELDS) excludes model_digest AND binding_version, so
# preflight (which may probe) and the pure create binding of the SAME substrate still
# yield an identical binding_digest across versions (the atomic preflight->create
# match, WAVE-30H #1).
_HASH_FIELDS_V1 = (
    "binding_version", "provider", "model", "model_ref",
    "model_identifier_status", "execution", "endpoint_class",
    "endpoint_fingerprint", "provider_cost_policy",
    "run_id", "root_run_id", "tenant", "workspace",
)
_HASH_FIELDS_V2 = _HASH_FIELDS_V1 + ("digest_status", "model_digest")
_HASH_FIELDS_BY_VERSION = {1: _HASH_FIELDS_V1, 2: _HASH_FIELDS_V2}

# The version new bindings are created at, and the full set the contract supports.
CURRENT_BINDING_VERSION = 2
SUPPORTED_BINDING_VERSIONS = frozenset(_HASH_FIELDS_BY_VERSION)


class UnknownBindingVersion(ValueError):
    """A binding carries a binding_version with no defined canonical hash set."""


def _hash_fields_for(binding: Mapping[str, Any]) -> Sequence[str]:
    version = binding.get("binding_version") if isinstance(binding, Mapping) else None
    fields = _HASH_FIELDS_BY_VERSION.get(version) if isinstance(version, int) else None
    if fields is None:
        raise UnknownBindingVersion(f"unsupported binding_version {version!r}")
    return fields

# Substrate-only identity (NO run_id/tenant/workspace/binding_version) used for the
# benchmark expected-binding-digest, so preflight (which does not know run_id) and
# create compute the same digest for the same substrate.
_DIGEST_FIELDS = (
    "provider", "model", "model_ref", "execution",
    "endpoint_class", "endpoint_fingerprint", "provider_cost_policy",
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
    # Scheme detection is CASE-INSENSITIVE and mirrors ``normalize_endpoint`` (only
    # prepend when there is no scheme at all). ``raw.startswith("http")`` missed an
    # uppercase ``HTTPS://…`` and prepended ``http://`` to it, misclassifying the host.
    if "://" not in raw:
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


class EndpointCredentialError(ValueError):
    """The endpoint embedded credentials (userinfo) — refused fail-closed."""


def normalize_endpoint(url: Optional[str]) -> str:
    """Canonicalize an endpoint to ``scheme://host[:port][/path]`` (WAVE-30H #F3).

    ONE canonical identity used by preflight, create, the binding digest, and worker
    comparison, so the fingerprint agrees across all of them. Pure; no secret is ever
    retained. Rules (documented, deterministic):

    * scheme detection + normalization is CASE-INSENSITIVE (``HTTPS://h/p`` and
      ``https://h/p`` canonicalize identically); a schemeless input defaults to http;
    * host is lower-cased, a trailing ``.`` stripped;
    * the DEFAULT port for the scheme (80/http, 443/https) is dropped; any other
      explicit port is preserved;
    * the PATH is retained credential-free and normalized (a single trailing ``/`` is
      collapsed, ``/`` alone becomes empty) so a path that selects a tenant/deployment
      (``/tenant-a`` vs ``/tenant-b``) yields DIFFERENT fingerprints;
    * query and fragment are DROPPED (they cannot smuggle a credential or silently
      alter identity);
    * embedded userinfo is REFUSED (raises :class:`EndpointCredentialError`) — never
      silently stripped, so a credential-bearing endpoint fails closed rather than
      collapsing to the credential-free form.

    Returns ``""`` when no endpoint is configured (distinct from any real endpoint).
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    # Case-insensitive scheme detection: only prepend when there is no scheme at all.
    if "://" not in raw:
        raw = "http://" + raw
    try:
        p = urlparse(raw)
    except Exception:  # noqa: BLE001
        return ""
    # Fail closed on credentials — NEVER silently strip them.
    if p.username or p.password:
        raise EndpointCredentialError(
            "endpoint must not embed credentials (userinfo); refusing"
        )
    scheme = (p.scheme or "http").lower()
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    default_port = {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme)
    port_str = f":{p.port}" if (p.port and p.port != default_port) else ""
    path = p.path or ""
    path = "" if path == "/" else path.rstrip("/")
    return f"{scheme}://{host}{port_str}{path}"


def compute_endpoint_fingerprint(url: Optional[str]) -> str:
    """Non-reversible sha256 fingerprint of the normalized endpoint authority.

    Distinguishes two endpoints that share an ``endpoint_class`` (e.g. two loopback
    ports) without ever persisting the raw endpoint or any credential.
    """
    return hashlib.sha256(normalize_endpoint(url).encode("utf-8")).hexdigest()


def _canonical(binding: Mapping[str, Any], fields: Sequence[str]) -> str:
    return json.dumps(
        {k: binding.get(k) for k in fields}, sort_keys=True, separators=(",", ":")
    )


def compute_binding_hash(binding: Mapping[str, Any]) -> str:
    """sha256 over the canonical identity fields for the binding's OWN version.

    Raises :class:`UnknownBindingVersion` for a version with no defined field set, so
    a binding is never hashed with the wrong algorithm (callers fail closed)."""
    return hashlib.sha256(
        _canonical(binding, _hash_fields_for(binding)).encode("utf-8")
    ).hexdigest()


def binding_digest(binding: Mapping[str, Any]) -> str:
    """sha256 over the SUBSTRATE-only identity (``_DIGEST_FIELDS``).

    Used to make benchmark preflight→create atomic: the same substrate digests
    identically at preflight (no run_id) and at create (run_id present).
    """
    return hashlib.sha256(_canonical(binding, _DIGEST_FIELDS).encode("utf-8")).hexdigest()


def verify_binding(binding: Mapping[str, Any]) -> bool:
    """True iff ``binding`` carries a ``binding_hash`` that matches its identity.

    Constant-time compare; False on a missing/empty/mismatched hash so a caller can
    fail closed on a tampered or malformed binding. An UNKNOWN binding_version (no
    defined hash field set) also returns False — fail closed, never guess an
    algorithm (WAVE-30H #F7).
    """
    stored = binding.get("binding_hash") if isinstance(binding, Mapping) else None
    if not isinstance(stored, str) or not stored:
        return False
    try:
        expected = compute_binding_hash(binding)
    except UnknownBindingVersion:
        return False
    return hmac.compare_digest(stored, expected)


def utc_iso_now() -> str:
    """UTC ``bound_at`` stamp (seconds granularity, matching the event stream)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_effective_binding(
    *,
    provider: Optional[str],
    model: Optional[str],
    model_ref: Optional[str] = None,
    endpoint: Optional[str] = None,
    binding_version: int = CURRENT_BINDING_VERSION,
    bound_at: Optional[str] = None,
    model_identifier_status: Optional[str] = None,
    model_digest: Optional[str] = None,
    digest_status: Optional[str] = None,
    run_id: Optional[str] = None,
    root_run_id: Optional[str] = None,
    tenant: Optional[str] = None,
    workspace: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a canonical, self-hashed binding from already-resolved identity pieces.

    PURE: config + ``ipaddress`` classification only, no network. ``execution``,
    ``endpoint_class`` and ``provider_cost_policy`` are derived from provider +
    endpoint exactly as the runtime attestation derives them; ``endpoint_fingerprint``
    is the non-secret normalized-authority hash. A falsy ``model`` yields an
    unresolved status (never a placeholder presented as the effective model).
    ``run_id``/``tenant``/``workspace`` scope the binding to its run.

    Model-manifest digest (WAVE-30H #7): when ``model_digest`` is supplied (an
    already-attested digest from an authenticated preflight — this stays PURE, no
    probe here) the binding pins it and marks ``digest_status="attested"``; the
    worker re-probes and constant-time compares before executing, closing the
    tag->manifest TOCTOU. When absent, ``digest_status`` is the caller-provided
    value or defaults to ``"not_probed"`` for a verified-local Ollama model else
    ``"not_applicable"``, and ``model_digest`` is ``None``. Both fields are covered
    by ``binding_hash`` (tamper-evident at rest) but NOT by ``binding_digest`` (so
    the substrate digest stays probe-independent).

    ``binding_hash`` is computed LAST over the identity fields. ``extra`` may attach
    non-identity link fields without affecting the hash.
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
    mdigest = (model_digest or "").strip().lower() or None
    if mdigest:
        # An authenticated preflight attested a concrete manifest digest — pin it.
        resolved_digest_status = "attested"
    elif digest_status:
        resolved_digest_status = digest_status
    else:
        resolved_digest_status = (
            "not_probed" if (prov == "ollama" and local_zero and mdl) else "not_applicable"
        )
    binding: Dict[str, Any] = {
        "binding_version": int(binding_version),
        "provider": prov,
        "model": mdl,
        "model_ref": ref,
        "model_identifier_status": status,
        "execution": execution,
        "endpoint_class": endpoint_class,
        "endpoint_fingerprint": compute_endpoint_fingerprint(endpoint),
        "provider_cost_policy": cost_policy,
        "digest_status": resolved_digest_status,
        "model_digest": mdigest,
        "run_id": (run_id or None),
        "root_run_id": (root_run_id or run_id or None),
        "tenant": (tenant or None),
        "workspace": (workspace or None),
        "bound_at": bound_at or utc_iso_now(),
    }
    if extra:
        binding.update(extra)
    # Hash LAST over identity fields only (independent of bound_at / extra links).
    binding["binding_hash"] = compute_binding_hash(binding)
    return binding


def rescope_binding(
    binding: Mapping[str, Any],
    *,
    run_id: str,
    root_run_id: Optional[str] = None,
    tenant: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-scope an inherited binding to a NEW run, preserving its exact substrate.

    A retry spawns a fresh run (a new ``run_id``) that must re-execute on the SAME
    provider/model/endpoint substrate as the original but is a DIFFERENT run, so it
    must carry its OWN run scope (else the worker's run-scope gate would reject it
    as a cross-run/stale binding). This copies every substrate/identity field
    UNCHANGED and overrides ONLY the run-scope fields — ``run_id`` always;
    ``root_run_id`` / ``tenant`` / ``workspace`` when supplied — then recomputes
    ``binding_hash`` so the child binding self-verifies.

    The SUBSTRATE :func:`binding_digest` is INVARIANT across the re-scope (it
    excludes run/tenant/workspace), so ``binding_digest(child) ==
    binding_digest(parent)`` PROVES the child runs on the parent's exact substrate,
    while ``child["run_id"]`` equals the child's own run so the worker run-scope
    gate is satisfied. ``root_run_id`` defaults to the parent's (lineage
    preserved); ``binding_version`` and the digest fields carry over unchanged.
    Never mutates the input.

    Fail-closed integrity (WAVE-30H #4): the input's ORIGINAL stored hash MUST
    self-verify first. Otherwise a tampered-at-rest parent (fields changed, stale
    hash) would be laundered into a fresh VALID child hash over the tampered
    substrate. A legitimate parent always self-verifies (it came from
    :func:`build_effective_binding`), so the legitimate re-scope still succeeds.
    """
    if not isinstance(binding, Mapping):
        raise TypeError("rescope_binding requires a binding mapping")
    if not verify_binding(binding):
        raise ValueError(
            "rescope_binding refuses a binding that does not self-verify "
            "(tampered/malformed parent); refusing to launder it into a child"
        )
    child: Dict[str, Any] = dict(binding)
    child.pop("binding_hash", None)
    child["run_id"] = run_id or None
    child["root_run_id"] = root_run_id or binding.get("root_run_id") or run_id or None
    if tenant is not None:
        child["tenant"] = tenant or None
    if workspace is not None:
        child["workspace"] = workspace or None
    # Hash LAST over the re-scoped identity so the child binding self-verifies.
    child["binding_hash"] = compute_binding_hash(child)
    return child


def effective_binding_from_events(events: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """Return the CURRENT effective binding for a run, or ``None``.

    The current binding is the highest-``binding_version`` :data:`BINDING_EVENT`
    payload (append-only: a legitimate rebind appends a STRICTLY INCREASING version
    under explicit authorization). The binding log is an immutable append-only
    contract, so TWO binding events sharing the same ``binding_version`` are
    CORRUPTION — never "the last one wins". A self-consistent, correctly re-hashed
    same-version replacement would otherwise silently displace the original
    provider/model/endpoint binding (the unkeyed self-hash can be recomputed for the
    replacement and the downstream integrity/scope/version gates cannot detect it when
    the replacement keeps the same run/tenant/workspace). So a duplicate version fails
    closed here: we return ``{"__corrupt__": True}`` and the caller refuses. Selection
    is therefore strictly increasing (``>``), and there is NO unsigned rebind-acceptance
    path — a rebind must arrive as a higher version through the authorized protocol.
    Returns ``None`` for a legacy/standalone run with no binding event, and
    ``{"__corrupt__": True}`` when a binding event lacks a valid integer
    ``binding_version`` OR when any version appears more than once.
    """
    current: Optional[Dict[str, Any]] = None
    best = -1
    seen_versions: set = set()
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
        if version in seen_versions:
            # Duplicate version on an append-only contract = corruption / injected
            # replacement. Refuse fail-closed rather than pick a winner.
            return {"__corrupt__": True}
        seen_versions.add(version)
        if version > best:
            best, current = version, dict(payload)
    return current
