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

This is the single approved way to build an outbound httpx client inside the
runtime; the accompanying lint/gate (see the WAVE-26 report) bans bare
``httpx.Client`` / ``httpx.AsyncClient`` construction outside this module so new
adapters cannot silently bypass the audit boundary.
"""

from __future__ import annotations

from typing import Any, Optional

from youtab_runtime.egress_audit import (
    AuditDecision,
    EgressDenied,
    authorize,
    digest_bytes,
    record_attempt,
    record_outcome,
)
from youtab_runtime.run_journal import Principal
from youtab_runtime.run_states import EgressDecision

__all__ = ["audited_client", "audited_async_client", "EgressDenied"]

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


def _authorize_request(
    request: Any,
    *,
    run_id: str,
    principal: Principal,
    adapter: str,
    effect_ref: Optional[str],
) -> AuditDecision:
    decision = authorize(
        str(request.url),
        adapter,
        run_id,
        principal,
        effect_ref=effect_ref,
    )
    if not decision.allowed:
        raise EgressDenied(decision)
    request.extensions[_DECISION_EXT_KEY] = decision
    bytes_out, dg = _measure_request(request)
    record_attempt(decision, bytes_out=bytes_out, digest=dg)
    return decision


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
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._run_id = _run_id
            self._principal = _principal
            self._adapter = _adapter
            self._effect_ref = _effect_ref
            _install_ssrf_guard_on_client(self)

        def send(self, request: Any, **kwargs: Any) -> Any:
            decision = _authorize_request(
                request,
                run_id=self._run_id,
                principal=self._principal,
                adapter=self._adapter,
                effect_ref=self._effect_ref,
            )
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
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._run_id = _run_id
            self._principal = _principal
            self._adapter = _adapter
            self._effect_ref = _effect_ref
            _install_ssrf_guard_on_async_client(self)

        async def send(self, request: Any, **kwargs: Any) -> Any:
            decision = _authorize_request(
                request,
                run_id=self._run_id,
                principal=self._principal,
                adapter=self._adapter,
                effect_ref=self._effect_ref,
            )
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
    **kwargs: Any,
) -> Any:
    """Return an SSRF-guarded, audit-emitting ``httpx.Client``.

    Every request is authorized (and journalled) before connect; denied requests
    raise :class:`EgressDenied`. Extra ``kwargs`` are forwarded to ``httpx.Client``.
    """
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal instance")
    cls = _sync_class()
    return cls(
        _run_id=run_id,
        _principal=principal,
        _adapter=adapter,
        _effect_ref=effect_ref,
        **kwargs,
    )


def audited_async_client(
    *,
    run_id: str,
    principal: Principal,
    adapter: str,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Return an SSRF-guarded, audit-emitting ``httpx.AsyncClient``."""
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal instance")
    cls = _async_class()
    return cls(
        _run_id=run_id,
        _principal=principal,
        _adapter=adapter,
        _effect_ref=effect_ref,
        **kwargs,
    )
