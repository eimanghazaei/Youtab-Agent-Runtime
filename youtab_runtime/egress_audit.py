"""WAVE-26 outbound-network audit boundary (frozen contracts 3, 6, 8).

This is the *shared* egress audit boundary every in-process outbound adapter is
meant to pass through so that a run's network side effects are judged from
observable, ordered, durable journal state rather than from an agent's
self-report. Scope (WAVE-27): the httpx audited factory
(:mod:`youtab_runtime.egress_guard_http`) and the non-httpx audited adapters
(:mod:`youtab_runtime.egress_adapters`) route directly-constructable clients
through here, and a CI static gate (``tools/egress_policy_lint.py``) bans new raw
client construction outside those adapters. Coverage is claimed ONLY for this
repository-controlled in-process boundary; vendor SDK internal transports and
subprocess/sandbox egress cannot be wrapped in-process and are enumerated as
exceptions in ``docs/security/EGRESS_EXCEPTIONS.md`` (verification
PENDING_OWNER_ACTION). What this module provides is:

  * :func:`authorize` — classify a destination and emit an ``egress``
    ``authorized``/``denied`` decision to the run journal **before** any network
    side effect. Fails **closed** (denied) when policy state is unavailable.
  * :func:`record_attempt` / :func:`record_outcome` — emit
    ``attempted`` -> ``succeeded``/``failed``/``unknown`` carrying only dest
    metadata + byte *counts* / *digests*. The audit trail must never itself
    become an exfiltration channel, so the exfiltrated bytes are never stored.
  * A **NETWORK-DENY posture** (env / config, benchmark-only) that denies every
    non-allowlisted destination, so an injection / exfiltration oracle is
    definitive *by construction*: any egress outside the audited allowlist is
    observably blocked and recorded.

Destination classification reuses the SSRF primitives in ``tools/url_safety.py``
(the metadata floor, the private/loopback IP predicate, and the global
``allow_private`` toggle) rather than re-implementing them, so the audit
boundary and the SSRF-guarded transport agree on what "private" means.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, FrozenSet, Optional, Tuple

from youtab_runtime.redaction import digest as _digest
from youtab_runtime.redaction import redact_error, redact_url
from youtab_runtime.run_journal import Principal, append_event
from youtab_runtime.run_states import EgressDecision

__all__ = [
    "DestClass",
    "EgressPolicy",
    "AuditDecision",
    "EgressDenied",
    "EgressAuditError",
    "authorize",
    "record_attempt",
    "record_outcome",
    "record_observed",
    "digest_bytes",
    "network_deny_active",
    "resolve_policy",
    "set_policy_provider",
    "reset_policy_provider",
    "NETWORK_DENY_ENV",
    "ALLOWLIST_ENV",
]

#: Env var (truthy) that turns on the benchmark-only network-deny posture.
NETWORK_DENY_ENV = "YOUTAB_AGENT_EGRESS_NETWORK_DENY"
#: Env var: comma/space separated exact hostnames allowed to egress.
ALLOWLIST_ENV = "YOUTAB_AGENT_EGRESS_ALLOWLIST"

_WEB_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


class EgressAuditError(ValueError):
    """Raised on a fail-closed contract violation in the audit boundary."""


class DestClass(StrEnum):
    """Destination classification (contract 3 ``dest_class``)."""

    PUBLIC = "public"
    PRIVATE = "private"
    METADATA = "metadata"
    LOOPBACK = "loopback"
    ALLOWLISTED = "allowlisted"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class EgressPolicy:
    """Resolved egress policy state.

    ``network_deny`` is the benchmark-only posture; when set, only ``allowlist``
    hosts are authorized. ``allowlist`` holds normalized (lowercased, trailing
    dot stripped) exact hostnames.
    """

    network_deny: bool
    allowlist: FrozenSet[str]


@dataclass(frozen=True)
class _Target:
    scheme: Optional[str]
    host: Optional[str]
    port: Optional[int]
    path_shape: Optional[str]
    dest_class: DestClass


@dataclass(frozen=True)
class AuditDecision:
    """Outcome of :func:`authorize`, threaded into attempt/outcome recording.

    Carries no payload bytes and no query string — only destination metadata and
    the journal coordinates needed to correlate the follow-up events.
    """

    decision: EgressDecision  # AUTHORIZED or DENIED
    dest_class: DestClass
    reason: str
    adapter: str
    effect_ref: Optional[str]
    tool_call_id: Optional[str]
    run_id: str
    principal: Principal
    correlation_id: str
    scheme: Optional[str]
    host: Optional[str]
    port: Optional[int]
    path_shape: Optional[str]
    _db_path: Optional[Path] = None

    @property
    def allowed(self) -> bool:
        return self.decision == EgressDecision.AUTHORIZED


class EgressDenied(RuntimeError):
    """Raised by the audited HTTP factory when a request is denied pre-connect."""

    def __init__(self, decision: AuditDecision) -> None:
        self.decision = decision
        super().__init__(
            f"egress denied ({decision.reason}) to "
            f"{decision.scheme}://{decision.host}:{decision.port} "
            f"[dest_class={decision.dest_class}]"
        )


# ---------------------------------------------------------------------------
# Policy resolution (fail-closed on unavailability)
# ---------------------------------------------------------------------------
_policy_provider: Optional[Callable[[], EgressPolicy]] = None


def set_policy_provider(provider: Optional[Callable[[], EgressPolicy]]) -> None:
    """Install a policy provider (benchmark control / tests).

    A provider that raises models "policy state unavailable" — :func:`authorize`
    then fails **closed** (denies) rather than guessing.
    """
    global _policy_provider
    _policy_provider = provider


def reset_policy_provider() -> None:
    """Restore the default env/config policy provider."""
    global _policy_provider
    _policy_provider = None


def _truthy(value: str) -> Optional[bool]:
    low = value.strip().lower()
    if low in _TRUTHY:
        return True
    if low in _FALSY:
        return False
    return None


def _deny_from_env_or_config() -> bool:
    env = os.getenv(NETWORK_DENY_ENV)
    if env is not None and env.strip():
        parsed = _truthy(env)
        if parsed is not None:
            return parsed
    # Config is best-effort: its absence means "posture off" (normal), which is
    # NOT the same as "policy unavailable". A hard failure to consult policy is
    # modelled by an injected provider that raises (see set_policy_provider).
    try:
        from youtab_agent_cli.config import read_raw_config

        cfg = read_raw_config()
        sec = cfg.get("security", {}) if isinstance(cfg, dict) else {}
        if isinstance(sec, dict):
            parsed = _truthy(str(sec.get("egress_network_deny", "")))
            if parsed is not None:
                return parsed
    except Exception:
        return False
    return False


def _allowlist_from_env_or_config() -> FrozenSet[str]:
    hosts: set[str] = set()
    env = os.getenv(ALLOWLIST_ENV, "")
    for token in env.replace(",", " ").split():
        host = token.strip().lower().rstrip(".")
        if host:
            hosts.add(host)
    try:
        from youtab_agent_cli.config import read_raw_config

        cfg = read_raw_config()
        sec = cfg.get("security", {}) if isinstance(cfg, dict) else {}
        raw = sec.get("egress_allowlist", []) if isinstance(sec, dict) else []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                host = str(item).strip().lower().rstrip(".")
                if host:
                    hosts.add(host)
    except Exception:
        # Config absence must not widen the allowlist; keep only env entries.
        pass
    return frozenset(hosts)


def _default_policy_provider() -> EgressPolicy:
    return EgressPolicy(
        network_deny=_deny_from_env_or_config(),
        allowlist=_allowlist_from_env_or_config(),
    )


def resolve_policy() -> EgressPolicy:
    """Resolve the active egress policy. May raise (=> caller fails closed)."""
    provider = _policy_provider or _default_policy_provider
    policy = provider()
    if not isinstance(policy, EgressPolicy):
        raise EgressAuditError("policy provider returned a non-EgressPolicy value")
    return policy


def network_deny_active() -> bool:
    """Best-effort read of the network-deny posture (False if unavailable)."""
    try:
        return resolve_policy().network_deny
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Destination classification (reuses tools/url_safety SSRF primitives)
# ---------------------------------------------------------------------------
def _normalize_host(host: Optional[str]) -> str:
    return (host or "").strip().lower().rstrip(".")


def _classify_ip(ip: ipaddress._BaseAddress) -> DestClass:
    from tools import url_safety as _us

    if ip in _us._ALWAYS_BLOCKED_IPS or any(
        ip in net for net in _us._ALWAYS_BLOCKED_NETWORKS
    ):
        return DestClass.METADATA
    # Loopback (incl. IPv4-mapped) is a distinct, reportable class.
    mapped = getattr(ip, "ipv4_mapped", None)
    probe = mapped if mapped is not None else ip
    if probe.is_loopback:
        return DestClass.LOOPBACK
    if _us._is_blocked_ip(ip):
        return DestClass.PRIVATE
    return DestClass.PUBLIC


def _classify(dest: str, allowlist: FrozenSet[str], *, resolve_dns: bool = True) -> _Target:
    """Classify ``dest`` into a :class:`DestClass` without emitting anything.

    Fail-closed: an unparseable URL, unsupported scheme, or a hostname that
    cannot be resolved classifies as :attr:`DestClass.BLOCKED`.

    ``resolve_dns=False`` skips every DNS lookup (used under the network-deny
    posture, where a non-allowlisted host is denied regardless of where it
    resolves): the classification stays offline-definitive and never leaks a DNS
    query for a destination that is going to be blocked anyway.
    """
    from tools import url_safety as _us

    red = redact_url(dest)
    scheme = red.get("scheme")
    host = _normalize_host(red.get("host"))
    port = red.get("port")
    path_shape = red.get("path_shape")

    def _mk(dc: DestClass) -> _Target:
        return _Target(scheme, host or None, port, path_shape, dc)

    if not scheme or scheme.lower() not in _WEB_SCHEMES or not host:
        return _mk(DestClass.BLOCKED)

    # Cheap metadata floor FIRST — a literal metadata IP or a known metadata
    # hostname can NEVER be laundered into "allowlisted". This preserves the
    # url_safety invariant that metadata endpoints are blocked regardless of any
    # operator toggle/allowlist. (No DNS here; the DNS-based floor runs below.)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and (
        literal in _us._ALWAYS_BLOCKED_IPS
        or any(literal in net for net in _us._ALWAYS_BLOCKED_NETWORKS)
    ):
        return _mk(DestClass.METADATA)
    if host in _us._BLOCKED_HOSTNAMES:
        return _mk(DestClass.METADATA)

    # Exact-host allowlist (how the deny posture whitelists the provider/gateway
    # endpoint the benchmark may reach). Checked after the cheap metadata floor
    # so it cannot re-enable a metadata endpoint, and before DNS so an
    # allowlisted hostname stays offline-definitive under the deny posture. The
    # audited transport still pins the connect IP as defence in depth.
    if host in allowlist:
        return _mk(DestClass.ALLOWLISTED)

    # Literal IP — no DNS needed (safe under resolve_dns=False too).
    if literal is not None:
        return _mk(_classify_ip(literal))

    # Beyond this point every check needs DNS. Under the deny posture a
    # non-allowlisted host is denied regardless, so we skip the lookup entirely
    # and classify as BLOCKED (unverifiable).
    if not resolve_dns:
        return _mk(DestClass.BLOCKED)

    # DNS-based always-blocked metadata floor (resolves the hostname).
    try:
        if _us.is_always_blocked_url(dest):
            return _mk(DestClass.METADATA)
    except Exception:
        return _mk(DestClass.BLOCKED)

    # Hostname — resolve and classify by the most restrictive answer so a
    # split-horizon / rebinding answer cannot be laundered into "public".
    try:
        addr_info = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception:
        # Cannot verify where this resolves -> fail closed.
        return _mk(DestClass.BLOCKED)

    worst = DestClass.PUBLIC
    order = {
        DestClass.PUBLIC: 0,
        DestClass.PRIVATE: 1,
        DestClass.LOOPBACK: 2,
        DestClass.METADATA: 3,
    }
    seen_any = False
    for _family, _t, _p, _c, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if "%" in ip_str:
            ip_str = ip_str.split("%")[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return _mk(DestClass.BLOCKED)
        seen_any = True
        dc = _classify_ip(ip)
        if order.get(dc, 0) > order.get(worst, 0):
            worst = dc
    if not seen_any:
        return _mk(DestClass.BLOCKED)
    return _mk(worst)


def _decide(target: _Target, policy: EgressPolicy) -> Tuple[EgressDecision, str]:
    """Map a classified target + policy to an authorize/deny decision."""
    if policy.network_deny:
        if target.dest_class == DestClass.ALLOWLISTED:
            return EgressDecision.AUTHORIZED, "allowlisted_under_deny_posture"
        return EgressDecision.DENIED, "network_deny_posture"

    if target.dest_class == DestClass.ALLOWLISTED:
        return EgressDecision.AUTHORIZED, "allowlisted"
    if target.dest_class == DestClass.PUBLIC:
        return EgressDecision.AUTHORIZED, "public"
    if target.dest_class in (DestClass.METADATA, DestClass.BLOCKED):
        return EgressDecision.DENIED, f"blocked_{target.dest_class.value}"
    # LOOPBACK / PRIVATE — permitted only when the operator has explicitly opted
    # in via the same toggle url_safety honours; otherwise denied.
    try:
        from tools import url_safety as _us

        if _us._global_allow_private_urls():
            return EgressDecision.AUTHORIZED, f"private_allowed:{target.dest_class.value}"
    except Exception:
        return EgressDecision.DENIED, "policy_unavailable"
    return EgressDecision.DENIED, f"blocked_{target.dest_class.value}"


# ---------------------------------------------------------------------------
# Public boundary API
# ---------------------------------------------------------------------------
def _base_payload(decision: AuditDecision) -> dict[str, Any]:
    return {
        "effect_ref": decision.effect_ref,
        "adapter": decision.adapter,
        "scheme": decision.scheme,
        "host": decision.host,
        "port": decision.port,
        "path_shape": decision.path_shape,
        "dest_class": decision.dest_class.value,
        "policy_decision": decision.reason,
        "tool_call_id": decision.tool_call_id,
    }


def authorize(
    dest: str,
    adapter: str,
    run_id: str,
    principal: Principal,
    effect_ref: Optional[str] = None,
    *,
    tool_call_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> AuditDecision:
    """Classify ``dest`` and record an authorize/deny decision BEFORE any egress.

    Emits an ``egress`` ``requested`` event, then an ``authorized`` or ``denied``
    event, into the run journal — both before the caller performs any network
    side effect. Returns an :class:`AuditDecision` the caller threads into
    :func:`record_attempt` / :func:`record_outcome`.

    Fail-closed: if policy state cannot be resolved (provider raises) or the
    destination cannot be classified, the decision is ``DENIED`` with reason
    ``policy_unavailable`` / ``blocked_*`` and the deny event is still recorded.
    """
    if not isinstance(principal, Principal):
        raise EgressAuditError("principal must be a Principal instance")
    if not isinstance(adapter, str) or not adapter.strip():
        raise EgressAuditError("adapter must be a non-empty string")

    corr = correlation_id or f"egress:{uuid.uuid4().hex}"

    # 1) Record the request intent first so an audit trail exists even if
    #    classification below throws for an unexpected reason.
    red = redact_url(dest)
    append_event(
        run_id,
        principal,
        "egress",
        EgressDecision.REQUESTED.value,
        {
            "adapter": adapter,
            "effect_ref": effect_ref,
            "tool_call_id": tool_call_id,
            "scheme": red.get("scheme"),
            "host": _normalize_host(red.get("host")) or None,
            "port": red.get("port"),
            "path_shape": red.get("path_shape"),
        },
        correlation_id=corr,
        db_path=db_path,
    )

    # 2) Resolve policy + classify. Any failure => fail closed (denied).
    try:
        policy = resolve_policy()
        # Under the deny posture a non-allowlisted host is denied regardless of
        # where it resolves, so classify without DNS to keep the oracle
        # offline-definitive; the open posture needs full DNS classification.
        target = _classify(dest, policy.allowlist, resolve_dns=not policy.network_deny)
        egress_decision, reason = _decide(target, policy)
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY policy fault
        target = _Target(
            red.get("scheme"),
            _normalize_host(red.get("host")) or None,
            red.get("port"),
            red.get("path_shape"),
            DestClass.BLOCKED,
        )
        egress_decision = EgressDecision.DENIED
        reason = "policy_unavailable"
        # Do not surface the exception text (may echo config internals); the
        # deny event below is the durable record.
        del exc

    decision = AuditDecision(
        decision=egress_decision,
        dest_class=target.dest_class,
        reason=reason,
        adapter=adapter,
        effect_ref=effect_ref,
        tool_call_id=tool_call_id,
        run_id=run_id,
        principal=principal,
        correlation_id=corr,
        scheme=target.scheme,
        host=target.host,
        port=target.port,
        path_shape=target.path_shape,
        _db_path=db_path,
    )

    # 3) Record the pre-side-effect decision.
    append_event(
        run_id,
        principal,
        "egress",
        egress_decision.value,  # "authorized" | "denied"
        _base_payload(decision),
        correlation_id=corr,
        db_path=db_path,
    )
    return decision


def digest_bytes(data: Optional[bytes]) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(byte_count, sha256_hex)`` for ``data`` — never the bytes.

    The audited transport uses this so the journal records the *size* and a
    *digest* of an outbound body without the body itself ever entering the
    audit module.
    """
    if data is None:
        return None, None
    raw = bytes(data)
    return len(raw), _digest(raw)


def record_attempt(
    decision: AuditDecision,
    *,
    bytes_out: Optional[int] = None,
    digest: Optional[str] = None,
) -> None:
    """Emit ``egress`` ``attempted`` after authorize, before the socket write.

    Only valid for an authorized decision — recording an attempt for a denied
    decision would misrepresent the audit trail, so it is refused.
    """
    if not decision.allowed:
        raise EgressAuditError("cannot record an attempt for a denied decision")
    payload = _base_payload(decision)
    payload["bytes_out"] = bytes_out
    payload["digest"] = digest
    append_event(
        decision.run_id,
        decision.principal,
        "egress",
        EgressDecision.ATTEMPTED.value,
        payload,
        correlation_id=decision.correlation_id,
        db_path=decision._db_path,
    )


def record_outcome(
    decision: AuditDecision,
    status: EgressDecision,
    *,
    bytes_out: Optional[int] = None,
    digest: Optional[str] = None,
    http_status: Optional[int] = None,
    error: Any = None,
) -> None:
    """Emit ``egress`` ``succeeded`` | ``failed`` | ``unknown`` after the attempt.

    ``unknown`` is a first-class outcome (contract 8): use it when the boundary
    cannot prove whether bytes left the host (e.g. a crash mid-flight).
    """
    if status not in (
        EgressDecision.SUCCEEDED,
        EgressDecision.FAILED,
        EgressDecision.UNKNOWN,
    ):
        raise EgressAuditError(f"invalid outcome status: {status!r}")
    payload = _base_payload(decision)
    payload["bytes_out"] = bytes_out
    payload["digest"] = digest
    payload["http_status"] = http_status
    if error is not None:
        payload["error"] = redact_error(error)
    append_event(
        decision.run_id,
        decision.principal,
        "egress",
        status.value,
        payload,
        correlation_id=decision.correlation_id,
        db_path=decision._db_path,
    )


def record_observed(
    decision: AuditDecision,
    status: EgressDecision,
    *,
    http_status: Optional[int] = None,
    error: Any = None,
) -> None:
    """Record an OBSERVE-mode egress outcome (WAVE-27).

    Observe mode is used when the audited factory does not itself enforce the
    allow/deny decision — enforcement is delegated to the connect-time SSRF guard
    (behaviour-preserving for existing callers) — but the request still passes
    through the boundary for visibility. Unlike :func:`record_attempt` /
    :func:`record_outcome` this tolerates a ``denied``-classified decision (the
    request may still have proceeded under SSRF-guard enforcement), and emits a
    distinct ``observed_<status>`` kind so the audit trail never conflates an
    observed request with an enforced one. Carries only destination metadata.
    """
    if status not in (
        EgressDecision.SUCCEEDED,
        EgressDecision.FAILED,
        EgressDecision.UNKNOWN,
    ):
        raise EgressAuditError(f"invalid observed status: {status!r}")
    payload = _base_payload(decision)
    payload["observed"] = True
    payload["http_status"] = http_status
    if error is not None:
        payload["error"] = redact_error(error)
    append_event(
        decision.run_id,
        decision.principal,
        "egress",
        f"observed_{status.value}",
        payload,
        correlation_id=decision.correlation_id,
        db_path=decision._db_path,
    )
