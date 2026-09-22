"""Governed generic enterprise connector (Runtime side).

Ties the whole governed path together for CRM/ERP/SAP/CAD, importing no vendor
SDK. Per request the flow is:

    verify capability (signed manifest)
    -> typed input validation
    -> canonical workspace + tenant/workspace binding (authority)
    -> approval verification bound to operation + request digest (authority)
    -> idempotency reservation + Runtime provider-key policy
    -> worker lease (authority)
    -> effect-ledger admission (begin_effect) + try_claim   [commit only]
    -> Runtime worker subprocess (typed request across the boundary)
    -> typed output validation + provenance check
    -> commit-once effect + Runtime-minted immutable receipt
    -> reconciliation is query/verify only.

Non-mutating operations (``read`` / ``preview`` / CAD compute) create **zero**
business effect: they run through the worker and return an ephemeral receipt with
no ledger row. ``commit`` creates **exactly one** effect. ``reconcile`` reads the
prior committed effect and verifies it, creating no effect.

Authority comes only from the injected :class:`AuthorityBoundary`; nothing in the
request can grant authority, and nothing the worker prints can forge a receipt.
A LIVE-provenance capability has no bound vendor worker here, so it is refused
(IMPLEMENTED_NOT_VERIFIED) — never silently served by the reference worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from youtab_runtime import effect_ledger
from youtab_runtime.enterprise import providers
from youtab_runtime.enterprise.authority import (
    RuntimeIdempotencyPolicy,
)
from youtab_runtime.enterprise.manifest import (
    CapabilityManifest,
    ManifestError,
    ManifestVerifier,
    Provenance,
    digest_of,
)
from youtab_runtime.enterprise.worker_boundary import (
    WorkerBoundary,
    WorkerBoundaryError,
)
from youtab_runtime.run_journal import Principal

__all__ = [
    "GovernedConnectorError",
    "ConnectorRequest",
    "Receipt",
    "GovernedConnector",
]


class GovernedConnectorError(RuntimeError):
    """A fail-closed governed-connector refusal."""


@dataclass(frozen=True)
class ConnectorRequest:
    capability_id: str
    run_id: str
    principal: Principal
    raw_workspace: str
    business_key: str
    payload: Mapping[str, Any]
    approval_id: Optional[str] = None
    delegation: str = "direct"
    correlation_id: Optional[str] = None


@dataclass(frozen=True)
class Receipt:
    tenant: str
    user: str
    workspace_canonical: str
    capability_id: str
    capability_version: str
    operation_id: str
    operation_class: str
    request_digest: str
    approval_id: str
    lease_token: str
    effect_id: str
    effect_state: str
    provider: str
    provenance: str
    provider_idempotency_key: Optional[str]
    provider_result_digest: Optional[str]
    result_digest: str
    reconciled: Optional[bool]
    deduplicated: bool
    output: Mapping[str, Any]


class GovernedConnector:
    def __init__(
        self,
        signed_manifest: Mapping[str, Any],
        verifier: ManifestVerifier,
        authority: Any,
        idempotency_policy: RuntimeIdempotencyPolicy,
        worker: WorkerBoundary,
        *,
        production: bool,
        db_path: Optional[Path] = None,
        deadline_seconds: float = 15.0,
    ) -> None:
        if production and getattr(authority, "is_reference", False):
            raise GovernedConnectorError(
                "reference authority boundary cannot be selected in production"
            )
        self._manifest: CapabilityManifest = verifier.verify(signed_manifest)
        self._authority = authority
        self._idem = idempotency_policy
        self._worker = worker
        self._production = production
        self._db_path = db_path
        self._deadline = deadline_seconds

    # -- helpers ---------------------------------------------------------- #
    def _prepare(self, req: ConnectorRequest):
        if not isinstance(req.principal, Principal):
            raise GovernedConnectorError("principal must be a Principal")
        cap = self._manifest.get(req.capability_id)
        if cap.operation_id not in providers.OPERATIONS:
            raise GovernedConnectorError(
                f"operation {cap.operation_id!r} not supported by Runtime providers"
            )
        # A LIVE capability has no bound vendor worker in the Runtime — refuse
        # rather than let the reference worker masquerade as the vendor.
        if cap.provenance is Provenance.LIVE:
            raise GovernedConnectorError(
                f"LIVE capability {cap.capability_id!r} has no bound vendor worker "
                "(IMPLEMENTED_NOT_VERIFIED — see IR-3)"
            )
        # Typed input validation against the SIGNED request schema.
        cap.request_schema.validate_payload(req.payload, "input")
        # Tenant/workspace binding from the verified manifest.
        if cap.tenant_scope != req.principal.tenant:
            raise GovernedConnectorError(
                f"capability bound to tenant {cap.tenant_scope!r}, "
                f"refused for {req.principal.tenant!r}"
            )
        if cap.workspace_scope != req.raw_workspace:
            raise GovernedConnectorError(
                f"capability bound to workspace {cap.workspace_scope!r}, "
                f"refused for {req.raw_workspace!r}"
            )
        ws = self._authority.canonicalize_workspace(
            req.principal.tenant, req.raw_workspace
        )
        request_digest = providers.request_digest_of(
            cap.operation_id, ws.workspace, req.business_key, req.payload
        )
        return cap, ws, request_digest

    def _authorize(self, cap, ws, request_digest, req: ConnectorRequest) -> str:
        if cap.approval_required:
            if not req.approval_id:
                raise GovernedConnectorError(
                    f"operation {cap.operation_id!r} requires approval"
                )
            grant = self._authority.verify_approval(
                approval_id=req.approval_id,
                operation_id=cap.operation_id,
                request_digest=request_digest,
                delegation=req.delegation,
                principal_tenant=req.principal.tenant,
                principal_user=req.principal.user,
                workspace=ws,
            )
            return grant.approval_id
        return "none"

    def _run_worker(self, cap, ws, req: ConnectorRequest, request_digest,
                    payload: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        import time as _time

        worker_req = {
            "operation_id": cap.operation_id,
            "workspace": ws.workspace,
            "business_key": req.business_key,
            "payload": dict(payload if payload is not None else req.payload),
            "request_digest": request_digest,
            "provenance": cap.provenance.value,
            "deadline_epoch": _time.time() + self._deadline,
        }
        wres = self._worker.run(worker_req, deadline_seconds=self._deadline)
        output = cap.response_schema.validate_payload(wres.result, "output")
        # Provenance confusion guard: the worker output must match the
        # capability's declared provenance exactly.
        if output.get("provenance") != cap.provenance.value:
            raise GovernedConnectorError("provenance mismatch in worker output")
        return output

    # -- public API ------------------------------------------------------- #
    def execute(self, req: ConnectorRequest) -> Receipt:
        cap, ws, request_digest = self._prepare(req)
        op_class = providers.operation_class(cap.operation_id)
        if op_class == "reconcile":
            return self._reconcile(cap, ws, request_digest, req)

        approval_id = self._authorize(cap, ws, request_digest, req)
        # Idempotency reservation + Runtime provider-key policy (no ledger private).
        self._authority.reserve_idempotency(effect_key=request_digest, workspace=ws)
        provider_key = self._idem.derive_provider_key(
            cap, request_digest=request_digest,
            workspace_canonical=ws.workspace, approval_id=approval_id,
        )
        lease = self._authority.acquire_worker_lease(
            operation_id=cap.operation_id, workspace=ws, approval_id=approval_id
        )

        if op_class in ("read", "preview"):
            # Zero business effect: run through the worker, return an ephemeral
            # receipt, never touch the ledger.
            output = self._run_worker(cap, ws, req, request_digest)
            return Receipt(
                tenant=req.principal.tenant, user=req.principal.user,
                workspace_canonical=ws.workspace,
                capability_id=cap.capability_id,
                capability_version=cap.capability_version,
                operation_id=cap.operation_id, operation_class=op_class,
                request_digest=request_digest, approval_id=approval_id,
                lease_token=lease.lease_token, effect_id="",
                effect_state="non_mutating", provider=cap.provider,
                provenance=cap.provenance.value,
                provider_idempotency_key=provider_key,
                provider_result_digest=output.get("result_digest"),
                result_digest=digest_of(dict(output)),
                reconciled=None, deduplicated=False, output=dict(output),
            )

        # op_class == "commit": exactly one effect.
        target = {"workspace": ws.workspace, "business_key": req.business_key}
        record = effect_ledger.begin_effect(
            req.run_id, req.principal, logical_action=cap.operation_id,
            target_scope=target, correlation_id=req.correlation_id,
            detail={
                "operation_id": cap.operation_id, "workspace": ws.workspace,
                "approval_id": approval_id, "request_digest": request_digest,
                "provenance": cap.provenance.value, "mutating": True,
                "capability_version": cap.capability_version,
                "provider_idempotency_key": provider_key,
                "lease_token": lease.lease_token,
            },
            db_path=self._db_path,
        )
        effect_id = record.effect_id
        won, record = effect_ledger.try_claim(
            effect_id, req.principal, correlation_id=req.correlation_id,
            db_path=self._db_path,
        )
        if not won:
            # Replay / crash-stranded / concurrent: return the durable state,
            # NEVER re-run the worker (no duplicate effect).
            detail = record.detail
            return self._receipt_from_record(
                cap, ws, req, request_digest, approval_id, lease.lease_token,
                provider_key, record, deduplicated=True,
                output=dict(detail.get("output", {})),
            )
        try:
            output = self._run_worker(cap, ws, req, request_digest)
        except (WorkerBoundaryError, ManifestError, GovernedConnectorError) as exc:
            # Outcome unproven -> unknown (reconciliation-required), never retried.
            effect_ledger.mark_unknown(
                effect_id, req.principal,
                detail={"error": f"{type(exc).__name__}: {exc}"},
                db_path=self._db_path,
            )
            raise GovernedConnectorError(f"worker execution failed: {exc}") from exc

        committed = effect_ledger.mark_committed(
            effect_id, req.principal,
            detail={
                "output": dict(output),
                "provider_result_digest": output.get("result_digest"),
                "result_digest": digest_of(dict(output)),
            },
            db_path=self._db_path,
        )
        return self._receipt_from_record(
            cap, ws, req, request_digest, approval_id, lease.lease_token,
            provider_key, committed, deduplicated=False, output=dict(output),
        )

    def _receipt_from_record(self, cap, ws, req, request_digest, approval_id,
                             lease_token, provider_key, record, *, deduplicated,
                             output) -> Receipt:
        detail = record.detail
        return Receipt(
            tenant=req.principal.tenant, user=req.principal.user,
            workspace_canonical=ws.workspace,
            capability_id=cap.capability_id,
            capability_version=cap.capability_version,
            operation_id=cap.operation_id, operation_class="commit",
            request_digest=request_digest, approval_id=approval_id,
            lease_token=lease_token, effect_id=record.effect_id,
            effect_state=record.state.value, provider=cap.provider,
            provenance=cap.provenance.value,
            provider_idempotency_key=provider_key,
            provider_result_digest=detail.get("provider_result_digest")
            or (output.get("result_digest") if output else None),
            result_digest=detail.get("result_digest", digest_of(dict(output or {}))),
            reconciled=None, deduplicated=deduplicated, output=dict(output or {}),
        )

    def _reconcile(self, cap, ws, request_digest, req: ConnectorRequest) -> Receipt:
        """Query the prior committed effect and verify it — no new effect."""
        checked_op = req.payload["checked_operation_id"]
        business_key = req.payload["business_key"]
        target = {"workspace": ws.workspace, "business_key": business_key}
        commit_effect_id = effect_ledger.compute_effect_id(
            req.run_id, req.principal, checked_op, target
        )
        prior = effect_ledger.get_effect(
            commit_effect_id, req.principal, db_path=self._db_path
        )
        if prior is None:
            raise GovernedConnectorError(
                "no prior committed effect to reconcile (query-only)"
            )
        recorded_digest = prior.detail.get("provider_result_digest")
        # Run the reconcile worker to recompute the expected outcome digest.
        recon_payload = {
            "checked_operation_id": checked_op,
            "business_key": business_key,
            "original_request_digest": prior.detail.get("request_digest", ""),
        }
        output = self._run_worker(cap, ws, req, request_digest, payload=recon_payload)
        reconciled = (
            prior.state.value == "committed"
            and recorded_digest is not None
            and output.get("expected_digest") == recorded_digest
        )
        return Receipt(
            tenant=req.principal.tenant, user=req.principal.user,
            workspace_canonical=ws.workspace,
            capability_id=cap.capability_id,
            capability_version=cap.capability_version,
            operation_id=cap.operation_id, operation_class="reconcile",
            request_digest=request_digest, approval_id="none",
            lease_token="", effect_id=commit_effect_id,
            effect_state=prior.state.value, provider=cap.provider,
            provenance=cap.provenance.value, provider_idempotency_key=None,
            provider_result_digest=recorded_digest,
            result_digest=digest_of(dict(output)),
            reconciled=bool(reconciled), deduplicated=False, output=dict(output),
        )

    def get_commit_receipt(
        self, run_id: str, principal: Principal, raw_workspace: str,
        capability_id: str, business_key: str,
    ) -> Optional[dict]:
        """Workspace-bound receipt lookup (item 8).

        Recomputes the effect identity folding the canonical workspace, so the
        SAME tenant+user reading from a DIFFERENT workspace gets a different
        effect id and cannot see the receipt. Also verifies the stored canonical
        workspace matches, so isolation never relies on Principal alone.
        """
        cap = self._manifest.get(capability_id)
        ws = self._authority.canonicalize_workspace(principal.tenant, raw_workspace)
        target = {"workspace": ws.workspace, "business_key": business_key}
        effect_id = effect_ledger.compute_effect_id(
            run_id, principal, cap.operation_id, target
        )
        rec = effect_ledger.get_effect(effect_id, principal, db_path=self._db_path)
        if rec is None:
            return None
        if rec.detail.get("workspace") != ws.workspace:
            raise GovernedConnectorError("receipt workspace binding mismatch")
        return {
            "effect_id": effect_id,
            "state": rec.state.value,
            "workspace_canonical": ws.workspace,
            "output": rec.detail.get("output", {}),
            "result_digest": rec.detail.get("result_digest"),
        }
