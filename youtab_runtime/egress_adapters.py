"""WAVE-27 audited egress adapters for non-httpx clients.

``egress_guard_http`` gives httpx a transport that pins the validated IP at
connect (closing DNS rebinding). ``requests`` / ``urllib`` / ``aiohttp`` /
``websockets`` have no equivalent single hook, so this module provides
request-level brackets that route each outbound call through the shared audit
boundary:

    authorize(url) -> (denied? raise EgressDenied and NEVER call) -> perform ->
    record attempt/outcome (byte counts + digests only).

These are the **approved construction path** the CI egress lint gate points
callers to when a direct ``requests.*`` / ``urllib.request.urlopen`` /
``aiohttp`` / ``websockets.connect`` cannot be used. They bind to the ambient
egress context (:mod:`youtab_runtime.egress_context`) so a call site without a
run/principal in scope still audits under the SYSTEM fallback.

Residual (documented, not hidden): for the non-httpx libraries the SSRF check is
at *authorize* time (the classifier resolves DNS and takes the most-restrictive
answer, so a metadata/private address is denied), but the socket is dialled by
the library afterwards — a hostile resolver that returns a public answer to the
classifier and a private one to the library (sub-second DNS rebinding) is not
pinned the way httpx is. Callers that need rebinding-proof egress should use the
httpx audited client. This limitation is enumerated in
``docs/security/EGRESS_EXCEPTIONS.md``.
"""

from __future__ import annotations

from typing import Any, Optional

from youtab_runtime.egress_audit import (
    EgressDenied,
    authorize,
    digest_bytes,
    record_attempt,
    record_observed,
    record_outcome,
)
from youtab_runtime.run_states import EgressDecision

__all__ = [
    "EgressDenied",
    "audited_requests_request",
    "audited_urlopen",
    "audited_aiohttp_request",
    "audited_websocket_connect",
]


def _ctx():
    from youtab_runtime.egress_context import current_context

    return current_context()


def _authorize(url: str, adapter: str, effect_ref: Optional[str], enforce: bool):
    """Classify + journal; return a decision, or ``None`` on observe-mode audit
    failure (fail-open). Raises :class:`EgressDenied` in enforce mode on a denied
    destination (before any network call)."""
    ctx = _ctx()
    try:
        decision = authorize(url, adapter, ctx.run_id, ctx.principal, effect_ref=effect_ref)
    except Exception:
        if enforce:
            raise
        return None
    if not decision.allowed and enforce:
        raise EgressDenied(decision)
    return decision


def _finish(decision, *, ok: bool, http_status=None, error=None,
            bytes_out=None, digest=None) -> None:
    if decision is None:
        return
    status = EgressDecision.SUCCEEDED if ok else EgressDecision.FAILED
    if decision.allowed:
        if ok:
            record_outcome(decision, status, http_status=http_status,
                           bytes_out=bytes_out, digest=digest)
        else:
            record_outcome(decision, status, error=error)
    else:  # observe-mode denied classification that still proceeded
        record_observed(decision, status, http_status=http_status, error=error)


def audited_requests_request(
    method: str,
    url: str,
    *,
    adapter: str,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Audited ``requests.request`` — authorize before connect, record outcome."""
    import requests

    decision = _authorize(url, adapter, effect_ref, enforce)
    if decision is not None and decision.allowed:
        body = kwargs.get("data") or kwargs.get("json")
        if isinstance(body, (bytes, bytearray)):
            n, dg = digest_bytes(bytes(body))
            record_attempt(decision, bytes_out=n, digest=dg)
        else:
            record_attempt(decision)
    try:
        resp = requests.request(method, url, **kwargs)
    except BaseException as exc:
        _finish(decision, ok=False, error=exc)
        raise
    _finish(decision, ok=True, http_status=getattr(resp, "status_code", None))
    return resp


def audited_urlopen(
    url: str,
    *,
    adapter: str,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Audited ``urllib.request.urlopen``. ``url`` may be a str or a Request;
    the effective URL is authorized before the socket is opened."""
    import urllib.request as _u

    target = url.full_url if hasattr(url, "full_url") else str(url)
    decision = _authorize(target, adapter, effect_ref, enforce)
    if decision is not None and decision.allowed:
        record_attempt(decision)
    try:
        resp = _u.urlopen(url, **kwargs)
    except BaseException as exc:
        _finish(decision, ok=False, error=exc)
        raise
    _finish(decision, ok=True, http_status=getattr(resp, "status", None))
    return resp


async def audited_aiohttp_request(
    session: Any,
    method: str,
    url: str,
    *,
    adapter: str,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Audited ``aiohttp.ClientSession.request`` for one request."""
    decision = _authorize(url, adapter, effect_ref, enforce)
    if decision is not None and decision.allowed:
        record_attempt(decision)
    try:
        resp = await session.request(method, url, **kwargs)
    except BaseException as exc:
        _finish(decision, ok=False, error=exc)
        raise
    _finish(decision, ok=True, http_status=getattr(resp, "status", None))
    return resp


async def audited_websocket_connect(
    url: str,
    *,
    adapter: str,
    enforce: bool = True,
    effect_ref: Optional[str] = None,
    connect: Any = None,
    **kwargs: Any,
) -> Any:
    """Audited ``websockets.connect``. Authorizes the ``ws(s)://`` target before
    the handshake; ``connect`` defaults to ``websockets.connect``."""
    if connect is None:
        import websockets

        connect = websockets.connect
    decision = _authorize(url, adapter, effect_ref, enforce)
    if decision is not None and decision.allowed:
        record_attempt(decision)
    try:
        conn = await connect(url, **kwargs)
    except BaseException as exc:
        _finish(decision, ok=False, error=exc)
        raise
    _finish(decision, ok=True)
    return conn
