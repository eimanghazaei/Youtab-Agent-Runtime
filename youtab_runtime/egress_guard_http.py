"""WAVE-26 audited, SSRF-guarded httpx client factory (contract 3).

Composes the existing connect-time SSRF guard from ``tools/url_safety.py`` with
the shared egress audit boundary in :mod:`youtab_runtime.egress_audit`:

  * **authorize before connect** — every request is classified and an
    ``egress`` ``authorized``/``denied`` decision is journalled *before* the
    socket is opened. A denied request raises :class:`~youtab_runtime.egress_audit.EgressDenied`
    and never touches the network.
  * **SSRF pin at connect** — the transport dials the validated IP (closing the
    DNS-rebinding gap) exactly as ``create_ssrf_safe_client`` does.
  * **record the outcome** — ``attempted`` then ``succeeded`` / ``failed`` are
    journalled, carrying only the outbound byte *count* and a *digest*, never the
    body.

This is the approved way to build an outbound httpx client inside the runtime.

WAVE-27 status: the companion CI static gate (``tools/egress_policy_lint.py``,
run by the required ``python-security`` job) now BANS bare ``httpx.Client`` /
``httpx.AsyncClient`` construction AND the httpx module-level verbs
(``httpx.get`` / ``post`` / ``stream`` / ...), plus ``requests`` / ``aiohttp`` /
``urllib`` / ``websockets``, in production code outside the audited adapters,
except for sites enumerated with a justification in
``security/egress_allowlist.json`` (mirrored,
human-readable, in ``docs/security/EGRESS_EXCEPTIONS.md``). The shared SSRF
factory ``tools.url_safety.create_ssrf_safe_*`` routes through this boundary in
OBSERVE mode while a run context is active (see
:mod:`youtab_runtime.egress_context`). ``enforce=True`` (the default) additionally
blocks a denied destination pre-connect; ``enforce=False`` (observe) delegates the
allow/deny decision to the connect-time SSRF guard so wrapping an existing caller
is behaviour-preserving. "Universal coverage" is claimed ONLY for this
repository-controlled in-process boundary; out-of-process paths (vendor SDK
internal transports, subprocess/sandbox egress) are enumerated as exceptions and
their runtime verification is PENDING_OWNER_ACTION.
"""

from __future__ import annotations

from typing import Any, Optional

from youtab_runtime.egress_audit import (
    AuditDecision,
    EgressDenied,
    authorize,
    digest_bytes,
    record_attempt,
    record_observed,
    record_outcome,
)
from youtab_runtime.run_journal import Principal
from youtab_runtime.run_states import EgressDecision

__all__ = [
    "audited_client",
    "audited_async_client",
    "audited_client_ambient",
    "audited_async_client_ambient",
    "EgressDenied",
]

_DECISION_EXT_KEY = "youtab_egress_decision"


def _measure_request(request: Any) -> tuple[Optional[int], Optional[str]]:
    """Return ``(bytes_out, digest)`` for a request body, never the body itself.

    Streaming request bodies are not read (that would consume them); those are
    recorded with an unknown size rather than being materialised.
    """
    try:
        content = request.content  # raises if the body is a stream not yet read
    except Exception:
        return None, None
    if content is None:
        return None, None
    try:
        return digest_bytes(bytes(content))
    except Exception:
        return None, None


def _authorize_or_none(
    request: Any,
    *,
    run_id: str,
    principal: Principal,
    adapter: str,
    effect_ref: Optional[str],
    enforce: bool,
) -> Optional[AuditDecision]:
    """Classify + journal a decision. Returns the decision, or ``None`` when
    audit itself failed in OBSERVE mode (fail-open: the request still proceeds
    under the connect-time SSRF guard). In ENFORCE mode an audit failure fails
    closed via :func:`authorize` (which returns a DENIED decision) and a denied
    decision raises :class:`EgressDenied`.
    """
    try:
        decision = authorize(
            str(request.url), adapter, run_id, principal, effect_ref=effect_ref
        )
    except Exception:
        if enforce:
            raise
        # Observe mode: a journal hiccup must never break a request that the
        # SSRF guard is already enforcing. Drop the audit for this request.
        return None
    if not decision.allowed and enforce:
        raise EgressDenied(decision)
    return decision


def _record_authorized_attempt(request: Any, decision: AuditDecision) -> None:
    request.extensions[_DECISION_EXT_KEY] = decision
    bytes_out, dg = _measure_request(request)
    record_attempt(decision, bytes_out=bytes_out, digest=dg)


def _sync_class() -> type:
    import httpx

    from tools.url_safety import _install_ssrf_guard_on_client

    class _AuditedClient(httpx.Client):
        def __init__(
            self,
            *,
            _run_id: str,
            _principal: Principal,
            _adapter: str,
            _effect_ref: Optional[str] = None,
            _enforce: bool = True,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._run_id = _run_id
            self._principal = _principal
            self._adapter = _adapter
            self._effect_ref = _effect_ref
            self._enforce = _enforce
            _install_ssrf_guard_on_client(self)

        def send(self, request: Any, **kwargs: Any) -> Any:
            decision = _authorize_or_none(
                request,
                run_id=self._run_id,
                principal=self._principal,
                adapter=self._adapter,
                effect_ref=self._effect_ref,
                enforce=self._enforce,
            )
            if decision is None:  # observe-mode audit failure: proceed (SSRF-guarded)
                return super().send(request, **kwargs)
            if not decision.allowed:  # observe-mode denied classification
                try:
                    response = super().send(request, **kwargs)
                except BaseException as exc:
                    record_observed(decision, EgressDecision.FAILED, error=exc)
                    raise
                record_observed(
                    decision, EgressDecision.SUCCEEDED,
                    http_status=getattr(response, "status_code", None),
                )
                return response
            _record_authorized_attempt(request, decision)
            try:
                response = super().send(request, **kwargs)
            except BaseException as exc:  # connection error / cancellation
                record_outcome(decision, EgressDecision.FAILED, error=exc)
                raise
            record_outcome(
                decision,
                EgressDecision.SUCCEEDED,
                http_status=getattr(response, "status_code", None),
            )
            return response

    return _AuditedClient


def _async_class() -> type:
    import httpx

    from tools.url_safety import _install_ssrf_guard_on_async_client

    class _AuditedAsyncClient(httpx.AsyncClient):
        def __init__(
            self,
            *,
            _run_id: str,
            _principal: Principal,
            _adapter: str,
            _effect_ref: Optional[str] = None,
            _enforce: bool = True,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._run_id = _run_id
            self._principal = _principal
            self._adapter = _adapter
            self._effect_ref = _effect_ref
            self._enforce = _enforce
            _install_ssrf_guard_on_async_client(self)

        async def send(self, request: Any, **kwargs: Any) -> Any:
            decision = _authorize_or_none(
                request,
                run_id=self._run_id,
                principal=self._principal,
                adapter=self._adapter,
                effect_ref=self._effect_ref,
                enforce=self._enforce,
            )
            if decision is None:  # observe-mode audit failure: proceed (SSRF-guarded)
                return await super().send(request, **kwargs)
            if not decision.allowed:  # observe-mode denied classification
                try:
                    response = await super().send(request, **kwargs)
                except BaseException as exc:
                    record_observed(decision, EgressDecision.FAILED, error=exc)
                    raise
                record_observed(
                    decision, EgressDecision.SUCCEEDED,
                    http_status=getattr(response, "status_code", None),
                )
                return response
            _record_authorized_attempt(request, decision)
            try:
                response = await super().send(request, **kwargs)
            except BaseException as exc:
                record_outcome(decision, EgressDecision.FAILED, error=exc)
                raise
            record_outcome(
                decision,
                EgressDecision.SUCCEEDED,
                http_status=getattr(response, "status_code", None),
            )
            return response

    return _AuditedAsyncClient


def audited_client(
    *,
    run_id: str,
    principal: Principal,
    adapter: str,
    effect_ref: Optional[str] = None,
    enforce: bool = True,
    **kwargs: Any,
) -> Any:
    """Return an SSRF-guarded, audit-emitting ``httpx.Client``.

    ``enforce=True`` (default): every request is authorized (and journalled)
    before connect; a denied destination raises :class:`EgressDenied` and never
    connects. ``enforce=False`` (observe): the request is journalled but the
    allow/deny decision is delegated to the connect-time SSRF guard, so wrapping
    an existing caller is behaviour-preserving. Extra ``kwargs`` are forwarded to
    ``httpx.Client``.
    """
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal instance")
    cls = _sync_class()
    return cls(
        _run_id=run_id,
        _principal=principal,
        _adapter=adapter,
        _effect_ref=effect_ref,
        _enforce=enforce,
        **kwargs,
    )


def audited_async_client(
    *,
    run_id: str,
    principal: Principal,
    adapter: str,
    effect_ref: Optional[str] = None,
    enforce: bool = True,
    **kwargs: Any,
) -> Any:
    """Return an SSRF-guarded, audit-emitting ``httpx.AsyncClient`` (see
    :func:`audited_client` for ``enforce`` semantics)."""
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal instance")
    cls = _async_class()
    return cls(
        _run_id=run_id,
        _principal=principal,
        _adapter=adapter,
        _effect_ref=effect_ref,
        _enforce=enforce,
        **kwargs,
    )


def audited_client_ambient(
    adapter: str,
    *,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Build an audited ``httpx.Client`` bound to the AMBIENT egress context.

    Reads ``(run_id, principal)`` from :mod:`youtab_runtime.egress_context`
    (the SYSTEM fallback out of a run), so a call site with no run/principal in
    scope can still route through the boundary. This is the approved construction
    path the CI lint gate points callers to.
    """
    from youtab_runtime.egress_context import current_context

    ctx = current_context()
    return audited_client(
        run_id=ctx.run_id, principal=ctx.principal, adapter=adapter,
        effect_ref=effect_ref, enforce=enforce, **kwargs,
    )


def audited_async_client_ambient(
    adapter: str,
    *,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Ambient-context ``httpx.AsyncClient`` (see :func:`audited_client_ambient`)."""
    from youtab_runtime.egress_context import current_context

    ctx = current_context()
    return audited_async_client(
        run_id=ctx.run_id, principal=ctx.principal, adapter=adapter,
        effect_ref=effect_ref, enforce=enforce, **kwargs,
    )
