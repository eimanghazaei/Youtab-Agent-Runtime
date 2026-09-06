"""Real-principal HTTP client for the Agent Runtime benchmark harness (WAVE-26).

This client authenticates to ``/api/runtime/v1`` exactly the way the production
gateway does — there is **no benchmark-only auth path** and no bypass. Every
request carries:

* the service bearer ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` (``Authorization:
  Bearer ...``), proving *which service* is calling; and
* the gateway-verified end-user identity headers
  (``X-Youtab-Tenant-Id`` / ``X-Youtab-User-Id`` / ``X-Youtab-Roles`` /
  ``X-Youtab-Correlation-Id``), establishing the principal ``(tenant, user)``.

Every **mutating** command (create-run, cancel, retry) additionally carries the
signed-command envelope produced by the canonical signer in
:mod:`youtab_agent_cli.runtime_command_auth`
(``x-youtab-runtime-{timestamp,nonce,signature}`` over
``canonical_string`` / ``compute_signature``). This mirrors the real helpers in
``tests/youtab_agent_cli/test_runtime_api.py`` (``_identity_headers`` / ``_sign``)
so the benchmark exercises the true security boundary — including replay (409) and
tamper (401) rejection — rather than a mock.

The :class:`~youtab_runtime.run_journal.Principal` frozen contract is reused for
the principal notion: identity is exactly ``(tenant, user)``; roles are an
authorization input carried on the wire but are never part of identity.

httpx is the transport. The base URL may point at a live runtime, or the client
may be constructed over an in-process ``httpx`` transport (e.g. httpx.Client with
an ASGI transport) — the auth path is identical either way.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Mapping, Optional

import httpx

from youtab_agent_cli import runtime_command_auth as rca
from youtab_runtime.run_journal import Principal

_API_PREFIX = "/api/runtime/v1"


class BenchmarkAuthError(RuntimeError):
    """Raised for misconfiguration of the benchmark client itself (never a bypass)."""


def _new_correlation() -> str:
    # Matches runtime_command_auth._CORRELATION_RE ([A-Za-z0-9_.:-]{8,128}).
    return f"bench-{uuid.uuid4().hex}"


def _new_nonce() -> str:
    return f"n-{uuid.uuid4().hex}"


class AuthClient:
    """Authenticated benchmark client for one principal against one runtime base URL.

    Parameters
    ----------
    base_url:
        Origin of the runtime service, e.g. ``http://127.0.0.1:8099``. The
        ``/api/runtime/v1`` prefix is added by this client.
    service_secret:
        The shared ``YOUTAB_AGENT_RUNTIME_SERVICE_SECRET`` used both as the bearer
        and as the HMAC signing key (as the production gateway does).
    principal:
        The end-user identity ``(tenant, user)`` — a
        :class:`youtab_runtime.run_journal.Principal`.
    roles:
        Authorization roles forwarded on ``X-Youtab-Roles``. Not identity.
    client:
        An optional pre-built ``httpx.Client`` (e.g. wrapping an ASGI transport
        for in-process tests). When omitted a plain client is created and owned by
        this instance.
    """

    def __init__(
        self,
        base_url: str,
        service_secret: str,
        principal: Principal,
        *,
        roles: Iterable[str] = ("member",),
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(principal, Principal):
            raise BenchmarkAuthError("principal must be a run_journal.Principal")
        if not service_secret or len(service_secret) < 43:
            # Mirror the engine's fail-closed floor: a short/absent secret cannot
            # sign a command, so refuse rather than emit an unsigned request.
            raise BenchmarkAuthError(
                "service secret is missing or below the 43-char signing floor"
            )
        self._base_url = base_url.rstrip("/")
        self._secret = service_secret
        self.principal = principal
        self._roles = ",".join(r.strip() for r in roles if r and r.strip())
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AuthClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- header construction (identical shape to the production gateway) ----

    def _identity_headers(self, correlation: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._secret}",
            "X-Youtab-Tenant-Id": self.principal.tenant,
            "X-Youtab-User-Id": self.principal.user,
            "X-Youtab-Roles": self._roles,
            "X-Youtab-Correlation-Id": correlation,
        }

    def _signature_headers(
        self, method: str, path: str, body: bytes, correlation: str, nonce: str
    ) -> dict[str, str]:
        ts = str(int(time.time()))
        canonical = rca.canonical_string(
            method=method,
            path=path,
            tenant=self.principal.tenant,
            user=self.principal.user,
            timestamp=ts,
            nonce=nonce,
            body=body,
            correlation=correlation,
        )
        signature = rca.compute_signature(self._secret, canonical)
        return {
            rca.SIGNATURE_HEADER: signature,
            rca.TIMESTAMP_HEADER: ts,
            rca.NONCE_HEADER: nonce,
        }

    # -- request primitives ------------------------------------------------

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> httpx.Response:
        correlation = _new_correlation()
        headers = self._identity_headers(correlation)
        return self._client.get(
            self._base_url + path, headers=headers, params=dict(params or {})
        )

    def _signed_post(
        self,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        correlation: Optional[str] = None,
        nonce: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> httpx.Response:
        # Serialise the body EXACTLY once: the identical bytes are both hashed
        # into the signature and sent on the wire, so the signature verifies.
        body = json.dumps(dict(payload or {}), separators=(",", ":")).encode("utf-8")
        correlation = correlation or _new_correlation()
        nonce = nonce or _new_nonce()
        headers = self._identity_headers(correlation)
        headers.update(self._signature_headers("POST", path, body, correlation, nonce))
        headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._client.post(self._base_url + path, content=body, headers=headers)

    # -- public API (mirrors the /api/runtime/v1 surface) ------------------

    def create_run(
        self,
        agent: str,
        task: str,
        *,
        engine: Optional[str] = None,
        goal_mode: Optional[bool] = None,
        title: Optional[str] = None,
        skills: Optional[Iterable[str]] = None,
        max_runtime_seconds: Optional[int] = None,
        limits: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> httpx.Response:
        """POST /runs — create + dispatch a run (signed, optionally idempotent)."""
        payload: dict[str, Any] = {"agent": agent, "task": task}
        if engine is not None:
            payload["engine"] = engine
        if goal_mode is not None:
            payload["goal_mode"] = goal_mode
        if title is not None:
            payload["title"] = title
        if skills is not None:
            payload["skills"] = list(skills)
        if max_runtime_seconds is not None:
            payload["max_runtime_seconds"] = max_runtime_seconds
        # Authoritative per-run limits (WAVE-30B §8) — the runtime clamps + enforces.
        if limits:
            payload["limits"] = dict(limits)
        return self._signed_post(
            f"{_API_PREFIX}/runs", payload, idempotency_key=idempotency_key
        )

    def preflight(self, *, engine: Optional[str] = None) -> dict[str, Any]:
        """GET /preflight — the authenticated safety posture used to gate a live
        run (build SHA, redaction, budget enforcement, ceilings). When ``engine``
        is given it is passed as a query param so the runtime returns the
        effective per-engine binding attestation (WAVE-30D §B3). Raises on a
        non-200 rather than returning an unverified posture."""
        params = {"engine": engine} if engine else None
        resp = self._get(f"{_API_PREFIX}/preflight", params=params)
        if resp.status_code != 200:
            raise BenchmarkAuthError(
                f"preflight failed with HTTP {resp.status_code}"
            )
        return resp.json()

    def get_run(self, run_id: str) -> httpx.Response:
        """GET /runs/{run_id} — durable run detail (principal-scoped, 404 on mismatch)."""
        return self._get(f"{_API_PREFIX}/runs/{run_id}")

    def list_runs(self) -> httpx.Response:
        """GET /runs — runs owned by this principal."""
        return self._get(f"{_API_PREFIX}/runs")

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 500) -> httpx.Response:
        """GET /runs/{run_id}/events — resumable ordered event cursor."""
        return self._get(
            f"{_API_PREFIX}/runs/{run_id}/events",
            params={"after": max(0, int(after)), "limit": int(limit)},
        )

    def list_artifacts(self, run_id: str) -> httpx.Response:
        """GET /runs/{run_id}/artifacts — artifact refs for an owned run."""
        return self._get(f"{_API_PREFIX}/runs/{run_id}/artifacts")

    def get_artifact(self, run_id: str, artifact_id: int) -> httpx.Response:
        """GET /runs/{run_id}/artifacts/{artifact_id} — one artifact's bytes."""
        return self._get(f"{_API_PREFIX}/runs/{run_id}/artifacts/{int(artifact_id)}")

    def cancel(self, run_id: str) -> httpx.Response:
        """POST /runs/{run_id}/cancel — record cancel intent (signed, idempotent)."""
        return self._signed_post(f"{_API_PREFIX}/runs/{run_id}/cancel")

    def retry(self, run_id: str) -> httpx.Response:
        """POST /runs/{run_id}/retry — create a fresh run from the same spec (signed)."""
        return self._signed_post(f"{_API_PREFIX}/runs/{run_id}/retry")

    # -- polling helper (state-based; used by oracles) ---------------------

    def wait_terminal(
        self, run_id: str, *, timeout: float = 30.0, poll_interval: float = 0.3
    ) -> dict[str, Any]:
        """Poll the ordered event cursor until the run is terminal or ``timeout``.

        Returns the last events response (``{events, cursor, status, terminal}``).
        Raises :class:`TimeoutError` if the run never reaches a terminal state —
        an honest, observable failure (never silently coerced to success).
        """
        deadline = time.time() + timeout
        cursor = 0
        last: dict[str, Any] = {}
        while time.time() < deadline:
            resp = self.list_events(run_id, after=cursor)
            resp.raise_for_status()
            last = resp.json()
            cursor = last.get("cursor", cursor)
            if last.get("terminal"):
                return last
            time.sleep(poll_interval)
        raise TimeoutError(f"run {run_id} did not reach a terminal state in {timeout}s")
