"""Lane-1-integrated governed enterprise connector (production path).

Unlike the foundation :class:`~youtab_runtime.enterprise.connector.GovernedConnector`
(which delegates authority to an injected boundary and is convenient for the
reference/test double), this connector wires the REAL Lane-1 spine in the exact
order Lane-1's own ``grant_fs`` uses, so single-use approvals, the canonical
effect ledger, and worker leases behave correctly under retry/restart:

    verify capability (already-verified manifest)
    -> typed input + tenant/workspace binding
    -> canonical workspace (Lane-1 adapter)
    -> compute canonical effect identity (tenant, principal, workspace,
       capability id+version, operation, request digest, provider, idem key)
    -> if the effect does not yet exist: CONSUME a single-use, digest-bound
       Lane-1 approval (a retry never re-spends it)
    -> begin_effect + try_claim UNDER A WORKER LEASE (canonical ledger)
    -> worker subprocess (typed request) — only the single claim winner runs it
    -> holder-only, state-checked settle: mark_committed (exactly once) or
       mark_unknown (outcome unproven, never blind-retried)
    -> reconciliation is query/verify only and needs provider/reference evidence
       to move an ambiguous effect terminal.

Non-mutating operations (read/preview/CAD compute) create zero effect. Commit
creates exactly one. A LIVE-provenance capability is refused (no bound vendor
worker). Nothing the worker prints can forge a receipt — the Runtime derives and
persists it from canonical ledger state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from youtab_runtime import effect_ledger as _ledger
from youtab_runtime.enterprise import recovery as _recovery
from youtab_runtime.enterprise import reference_providers as providers
from youtab_runtime.enterprise.authority import AuthorityError, RuntimeIdempotencyPolicy
from youtab_runtime.enterprise.connector import ConnectorRequest, Receipt
from youtab_runtime.enterprise.lane1_adapter import Lane1AuthorityAdapter
from youtab_runtime.enterprise.manifest import (
    CapabilityManifest,
    ManifestError,
    Provenance,
    digest_of,
)
from youtab_runtime.enterprise.worker_boundary import (
    WorkerBoundary,
    WorkerBoundaryError,
)
from youtab_runtime.run_journal import Principal

__all__ = ["Lane1GovernedConnectorError", "Lane1GovernedConnector"]


class Lane1GovernedConnectorError(RuntimeError):
    """A fail-closed production-connector refusal."""


class Lane1GovernedConnector:
    def __init__(
        self,
        manifest: CapabilityManifest,
        adapter: Lane1AuthorityAdapter,
        idempotency_policy: RuntimeIdempotencyPolicy,
        worker: WorkerBoundary,
        *,
        production: bool,
        db_path: Optional[Path] = None,
        deadline_seconds: float = 15.0,
    ) -> None:
        if getattr(adapter, "is_reference", True):
            raise Lane1GovernedConnectorError(
                "production connector requires the real Lane-1 authority adapter"
            )
        if not isinstance(manifest, CapabilityManifest):
            raise Lane1GovernedConnectorError("manifest must be a CapabilityManifest")
        self._manifest = manifest
        self._authority = adapter
        self._idem = idempotency_policy
        self._worker = worker
        self._production = production
        self._db_path = db_path
        self._deadline = deadline_seconds

    # -- shared preparation ---------------------------------------------- #
    def _prepare(self, req: ConnectorRequest):
        if not isinstance(req.principal, Principal):
            raise Lane1GovernedConnectorError("principal must be a Principal")
        cap = self._manifest.get(req.capability_id)
        if cap.operation_id not in providers.OPERATIONS:
            raise Lane1GovernedConnectorError(
                f"operation {cap.operation_id!r} not supported by Runtime providers"
            )
        if cap.provenance is Provenance.LIVE:
            raise Lane1GovernedConnectorError(
                f"LIVE capability {cap.capability_id!r} has no bound vendor worker "
                "(fail-closed until an approved Gateway SHA supplies it)"
            )
        cap.request_schema.validate_payload(req.payload, "input")
        # Invariant: a mutating (commit-class) capability MUST require approval.
        # A manifest that marks a commit operation approval_required=False would
        # turn per-effect signed authorization into a blanket standing grant —
        # refuse it fail-closed rather than execute an unauthorized mutation.
        if providers.operation_class(cap.operation_id) == "commit" and not cap.approval_required:
            raise Lane1GovernedConnectorError(
                f"commit capability {cap.capability_id!r} must require approval"
            )
        if cap.tenant_scope != req.principal.tenant:
            raise Lane1GovernedConnectorError("capability tenant binding mismatch")
        if cap.workspace_scope != req.raw_workspace:
            raise Lane1GovernedConnectorError("capability workspace binding mismatch")
        ws = self._authority.canonicalize_workspace(
            req.principal.tenant, req.raw_workspace
        )
        request_digest = providers.request_digest_of(
            cap.operation_id, ws.workspace, req.business_key, req.payload
        )
        return cap, ws, request_digest

    def _effect_identity(self, cap, ws, request_digest, provider_key):
        return {
            "ws": ws.workspace,
            "cap": cap.capability_id,
            "ver": cap.capability_version,
            "op": cap.operation_id,
            "req": request_digest,
            "provider": cap.provider,
            "idem": provider_key or "",
        }

    def _run_worker(self, cap, ws, req, request_digest, payload=None) -> Mapping[str, Any]:
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
        if output.get("provenance") != cap.provenance.value:
            raise Lane1GovernedConnectorError("provenance mismatch in worker output")
        return output

    def _receipt(self, cap, ws, req, request_digest, approval_id, lease_token,
                 provider_key, *, effect_id, effect_state, deduplicated, output,
                 reconciled=None, op_class="commit", provider_result_digest=None,
                 result_digest=None) -> Receipt:
        return Receipt(
            tenant=req.principal.tenant, user=req.principal.user,
            workspace_canonical=ws.workspace, capability_id=cap.capability_id,
            capability_version=cap.capability_version, operation_id=cap.operation_id,
            operation_class=op_class, request_digest=request_digest,
            approval_id=approval_id, lease_token=lease_token, effect_id=effect_id,
            effect_state=effect_state, provider=cap.provider,
            provenance=cap.provenance.value, provider_idempotency_key=provider_key,
            provider_result_digest=provider_result_digest
            or (output.get("result_digest") if output else None),
            result_digest=result_digest if result_digest is not None
            else digest_of(dict(output or {})),
            reconciled=reconciled, deduplicated=deduplicated, output=dict(output or {}),
        )

    # -- public API ------------------------------------------------------- #
    def execute(self, req: ConnectorRequest, *, authorization=None) -> Receipt:
        """Execute one governed operation.

        ``authorization`` is an externally-signed
        :class:`~youtab_runtime.effect_authorization.EffectAuthorization`,
        required for commit-class operations. The Runtime only verifies and
        single-use-consumes it — it never mints authority.
        """
        cap, ws, request_digest = self._prepare(req)
        op_class = providers.operation_class(cap.operation_id)
        if op_class == "reconcile":
            return self._reconcile(cap, ws, request_digest, req)

        provider_key = self._idem.derive_provider_key(
            cap, request_digest=request_digest, workspace_canonical=ws.workspace,
            approval_id=(authorization.authorization_id if authorization else "none"),
        )

        if op_class in ("read", "preview"):
            output = self._run_worker(cap, ws, req, request_digest)
            return self._receipt(
                cap, ws, req, request_digest, "none", "", provider_key,
                effect_id="", effect_state="non_mutating", deduplicated=False,
                output=output, op_class=op_class,
            )

        # commit: canonical effect identity.
        scope = self._effect_identity(cap, ws, request_digest, provider_key)
        action = cap.operation_id
        effect_id = _ledger.compute_effect_id(req.run_id, req.principal, action, scope)

        # A signed authorization is verified + single-use consumed on FIRST
        # creation of every commit-class effect — unconditionally, never keyed on
        # a manifest boolean (the commit invariant is enforced in _prepare). The
        # Runtime never self-approves. NOTE (fail-closed): consumption is durable
        # and precedes begin_effect; a crash between the two makes a retry
        # re-present the now-consumed authorization and be refused — it requires a
        # freshly signed authorization, which is safe (never double-executes).
        authorization_id = "none"
        existing = _ledger.get_effect(effect_id, req.principal, db_path=self._db_path)
        if existing is None:
            if authorization is None:
                raise Lane1GovernedConnectorError(
                    f"operation {cap.operation_id!r} requires a signed authorization"
                )
            try:
                authorization_id = self._authority.consume_authorization(
                    authorization, capability_id=cap.capability_id,
                    operation_id=cap.operation_id, request_digest=request_digest,
                    principal=req.principal, workspace=ws,
                )
            except AuthorityError as exc:
                # Fail closed BEFORE any effect is created — never self-approve.
                raise Lane1GovernedConnectorError(
                    f"authorization refused: {exc}"
                ) from exc
        elif existing is not None:
            authorization_id = str(existing.detail.get("authorization_id", "none"))

        consumed_now = existing is None and authorization_id != "none"
        lease = self._authority.acquire_worker_lease(
            operation_id=cap.operation_id, workspace=ws,
            authorization_id=authorization_id,
        )
        # If effect creation/reservation fails AFTER a fresh single-use consume,
        # the authorization is already spent but no effect exists. Record a
        # deterministic recovery row (never silently lose it) and fail closed —
        # the worker has not run, so nothing external executed and a retry cannot
        # double-execute (it re-presents a now-consumed authorization → refused).
        try:
            _ledger.begin_effect(
                req.run_id, req.principal, action, scope,
                correlation_id=req.correlation_id,
                detail={
                    "operation_id": cap.operation_id, "workspace": ws.workspace,
                    "authorization_id": authorization_id, "request_digest": request_digest,
                    "provenance": cap.provenance.value, "mutating": True,
                    "capability_version": cap.capability_version,
                    "provider": cap.provider, "provider_idempotency_key": provider_key,
                },
                db_path=self._db_path,
            )
            won, record = _ledger.try_claim(
                effect_id, req.principal,
                detail=self._authority.lease_detail(lease.lease_token),
                db_path=self._db_path,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised fail-closed below
            if consumed_now:
                _recovery.record_orphaned_authorization(
                    authorization_id, effect_id,
                    self._authority.effect_digest_for(
                        cap.capability_id, ws.workspace, request_digest
                    ),
                    db_path=self._db_path,
                )
            raise Lane1GovernedConnectorError(
                f"{_recovery.ORPHAN_REASON}: effect not created after consume "
                f"({type(exc).__name__})" if consumed_now
                else f"effect claim failed: {exc}"
            ) from exc
        if not won:
            d = record.detail
            return self._receipt(
                cap, ws, req, request_digest, authorization_id,
                str(d.get("lease", {}).get("owner", "")), provider_key,
                effect_id=effect_id, effect_state=record.state.value,
                deduplicated=True, output=dict(d.get("output", {})),
                provider_result_digest=d.get("provider_result_digest"),
                result_digest=d.get("result_digest"),
            )

        # We hold the lease: run the worker exactly once.
        try:
            output = self._run_worker(cap, ws, req, request_digest)
        except (WorkerBoundaryError, ManifestError, Lane1GovernedConnectorError) as exc:
            self._authority.assert_lease_holder(effect_id, req.principal, lease.lease_token)
            _ledger.mark_unknown(
                effect_id, req.principal,
                detail={"error": f"{type(exc).__name__}: {exc}"}, db_path=self._db_path,
            )
            raise Lane1GovernedConnectorError(f"worker execution failed: {exc}") from exc

        # Holder-only + state-checked settle: a lost/reconciled lease blocks commit.
        self._authority.assert_lease_holder(effect_id, req.principal, lease.lease_token)
        current = _ledger.get_effect(effect_id, req.principal, db_path=self._db_path)
        if current is None or current.state.value != "in_progress":
            raise Lane1GovernedConnectorError(
                "lease lost or effect reconciled before commit — refusing to settle"
            )
        committed = _ledger.mark_committed(
            effect_id, req.principal,
            detail={
                "output": dict(output),
                "provider_result_digest": output.get("result_digest"),
                "result_digest": digest_of(dict(output)),
            },
            db_path=self._db_path,
        )
        return self._receipt(
            cap, ws, req, request_digest, authorization_id, lease.lease_token, provider_key,
            effect_id=effect_id, effect_state=committed.state.value,
            deduplicated=False, output=output,
        )

    def _reconcile(self, cap, ws, request_digest, req: ConnectorRequest) -> Receipt:
        checked_op = req.payload["checked_operation_id"]
        business_key = req.payload["business_key"]
        # Recompute the committed effect identity to LOOK IT UP — query only.
        prior = None
        for rec in _ledger.list_effects(req.run_id, req.principal, db_path=self._db_path):
            if rec.detail.get("operation_id") == checked_op and \
                    rec.detail.get("workspace") == ws.workspace and \
                    rec.effect_type == checked_op:
                prior = rec
                break
        if prior is None:
            raise Lane1GovernedConnectorError(
                "no prior effect to reconcile (query-only)"
            )
        recorded_digest = prior.detail.get("provider_result_digest")
        recon_payload = {
            "checked_operation_id": checked_op, "business_key": business_key,
            "original_request_digest": prior.detail.get("request_digest", ""),
        }
        output = self._run_worker(cap, ws, req, request_digest, payload=recon_payload)
        evidence_matches = (
            recorded_digest is not None
            and output.get("expected_digest") == recorded_digest
        )
        # Only WITH verifiable evidence may an ambiguous effect be moved
        # terminal; Lane-1 refuses forged/mismatched/stale evidence.
        if prior.state.value in ("unknown", "reconciliation_required") and evidence_matches:
            # Independent recomputation for the REFERENCE trust model: the worker
            # was just re-run deterministically above, so its freshly recomputed
            # digest is what Lane-1 must reproduce — never an echo of the stored
            # digest. If they diverge, Lane-1 leaves the effect ambiguous.
            self._authority.reconcile_to_terminal(
                prior.effect_id, req.principal,
                operation_digest=prior.target_scope_digest,
                workspace_id=ws.workspace, outcome="succeeded",
                result_digest=str(recorded_digest), provenance="reference",
                capability=cap.capability_id,
                recompute_reference=lambda: str(output.get("expected_digest")),
            )
            # No evidence -> leave it ambiguous (never blind-terminal).
        reconciled = bool(evidence_matches and prior.state.value in
                          ("committed", "unknown", "reconciliation_required"))
        final = _ledger.get_effect(prior.effect_id, req.principal, db_path=self._db_path)
        final_state = final.state.value if final is not None else prior.state.value
        return self._receipt(
            cap, ws, req, request_digest, "none", "", None,
            effect_id=prior.effect_id, effect_state=final_state,
            deduplicated=False, output=output, reconciled=reconciled,
            op_class="reconcile", provider_result_digest=recorded_digest,
        )

    def get_commit_receipt(
        self, run_id: str, principal: Principal, raw_workspace: str,
        capability_id: str, business_key: str,
    ) -> Optional[dict]:
        cap = self._manifest.get(capability_id)
        ws = self._authority.canonicalize_workspace(principal.tenant, raw_workspace)
        request_digest = providers.request_digest_of(
            cap.operation_id, ws.workspace, business_key, {}
        )
        # Look up by principal+workspace-scoped scan (workspace folded into id).
        for rec in _ledger.list_effects(run_id, principal, db_path=self._db_path):
            if rec.effect_type == cap.operation_id and \
                    rec.detail.get("workspace") == ws.workspace and \
                    rec.detail.get("capability_version") == cap.capability_version:
                return {
                    "effect_id": rec.effect_id, "state": rec.state.value,
                    "workspace_canonical": ws.workspace,
                    "output": rec.detail.get("output", {}),
                    "result_digest": rec.detail.get("result_digest"),
                }
        return None
