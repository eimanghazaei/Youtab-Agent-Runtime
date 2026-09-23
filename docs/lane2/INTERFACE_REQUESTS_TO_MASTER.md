# Lane 2 — Exact interface requests to the Master (Runtime integration)

Status: **LANE 2 IMPLEMENTED_NOT_VERIFIED / REQUEST CHANGES** (Review v1.0).

Lane 2 must NOT edit Lane-1-owned files (`effect_ledger.py`, `folder_grant.py`,
`grant_fs.py`, `worker_admission.py`) or reach into private symbols. The
following typed public interfaces are required from the Master / Lane-1 / Gateway
so the governed enterprise path can move from IMPLEMENTED_NOT_VERIFIED to
verified. Until each arrives, Lane 2 integrates against the typed Protocols in
`youtab_runtime/enterprise/authority.py` with fail-closed defaults, and the
corresponding binding is reported IMPLEMENTED_NOT_VERIFIED.

## IR-1 — Public provider-idempotency interface on the effect ledger
Lane 2 must NOT depend on the private `effect_ledger._PROVIDER_IDEMPOTENCY_SUPPORTED`.
Requested public API (Lane-1 owned, additive):

```python
def provider_accepts_idempotency_key(provider: str) -> bool: ...
def record_provider_idempotency_key(
    effect_id: str, principal: Principal, provider: str, key: str, *, db_path=None
) -> EffectRecord: ...
```

Until delivered: Lane 2 derives a Runtime-owned idempotency **reservation** key
from the verified manifest (capability id+version, operation id, request digest,
canonical workspace, approval id) via `enterprise/authority.py:RuntimeIdempotencyPolicy`,
records it in the receipt, and does **not** claim provider exactly-once.

## IR-2 — Lane-1 authority boundary delivery contract
Lane 2 requires a typed authority boundary (Protocol in
`enterprise/authority.py:AuthorityBoundary`) providing, per request:
- `canonicalize_workspace(raw) -> CanonicalWorkspace` (verified canonical form);
- `verify_approval(approval_id, operation_id, request_digest, delegation, expiry) -> ApprovalGrant`
  (issuer/provenance-checked, operation- and request-digest-bound);
- `reserve_idempotency(effect_key) -> IdempotencyReservation`;
- `acquire_worker_lease(operation_id, workspace, approval_id) -> WorkerLease`.

Until Lane-1 delivers a concrete implementation, the connector uses
`UnavailableAuthorityBoundary` (refuses every call, fail-closed). Tests use
`ReferenceAuthorityBoundary`, an explicit **test/reference double** that can
never be selected in production (guarded by `production=True`).

## IR-3 — Gateway vendor adapter + LIVE provenance manifests
CRM/ERP/SAP/CAD vendor adapters live in the Gateway. No accessible Gateway SHA
supplies them, so every LIVE-provenance capability stays IMPLEMENTED_NOT_VERIFIED.
Lane 2 executes REFERENCE-provenance capabilities through the same worker/
transport contract a vendor executor would use; the connector refuses to run a
LIVE capability with a reference executor and vice versa (no substitution).

## IR-4 — Signing authority / KMS key registry
The manifest signer/verifier here uses HMAC-SHA256 against an injected
`KeyRegistry`. Production requires the Gateway/KMS issuer public keys and key ids
wired into that registry (and the test-only signer disabled). Requested: the
canonical issuer identity + key-id set and the verification algorithm the
Gateway commits to (HMAC vs asymmetric), so the verifier can be pinned.
