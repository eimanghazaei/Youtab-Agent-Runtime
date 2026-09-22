# Gateway LIVE binding contract (machine-readable)

LIVE enterprise execution stays **fail-closed** in the Runtime until an exact
approved Gateway SHA supplies a valid signed LIVE manifest AND real vendor
executors (IR-3). No Gateway SHA or localhost assumption is hard-coded in
production code. This file is the exact contract the Gateway must satisfy.

## Discovery response (`GET /capabilities`, authenticated)
The Gateway must return an Ed25519-signed manifest matching
`youtab_runtime.enterprise.manifest_crypto` envelope v2:

```json
{
  "algorithm": "ed25519",
  "schema_version": "youtab.enterprise.manifest/v1",
  "issuer": "<gateway issuer id>",
  "key_id": "<rotating key id>",
  "issued_at": 0,
  "expires_at": 0,
  "tenant_scope": "<authenticated tenant>",
  "workspace_scope": "<authenticated workspace>",
  "manifest_digest": "<sha256 over capability entries>",
  "signature": "<hex ed25519 over the envelope>",
  "capabilities": [
    {
      "capability_id": "crm.contact.update.commit",
      "capability_version": "1.0.0",
      "operation_id": "crm.contact.update.commit",
      "request_schema": {"business_key": "str", "field": "str", "value": "str"},
      "response_schema": {"record_id": "str", "revision": "str",
                          "provenance": "str", "result_digest": "str"},
      "request_schema_digest": "<sha256>",
      "response_schema_digest": "<sha256>",
      "tenant_scope": "<tenant>",
      "workspace_scope": "<workspace>",
      "risk_class": "read|low|medium|high",
      "approval_required": true,
      "idempotency": {"supported": true, "semantics": "exactly_once|at_least_once|none"},
      "receipt_supported": true,
      "reconciliation_supported": true,
      "provenance": "LIVE",
      "provider": "<vendor provider id>",
      "signature": "<hex ed25519 binding id+version+operation+schema digests>"
    }
  ]
}
```

## Requirements the Runtime enforces on receipt
- Only public verification keys are provisioned in the Runtime; the Gateway/KMS
  holds the private keys.
- `algorithm` MUST be `ed25519`; any other/absent value is algorithm confusion → reject.
- Issuer + key id MUST be trusted and non-revoked; expiry window valid.
- `tenant_scope`/`workspace_scope` MUST match the authenticated caller.
- Each capability’s `provider`/`(capability_id, version)` MUST be server-side allowlisted.
- A `LIVE` capability requires a real vendor executor bound at an approved Gateway
  SHA; until then the connector refuses it (no reference substitution).

## Health / availability
- `GET /healthz` → `{ "status": "ok", "gateway_sha": "<40-hex>", "capabilities_available": true }`.
- The Runtime records the exact `gateway_sha` on every LIVE receipt.

## Status gate
- `REFERENCE ENTERPRISE PATH VERIFIED` is allowed once the Lane-1 adapter +
  canonical ledger integration passes (this branch).
- `LIVE ENTERPRISE VERIFIED` is NOT allowed until an exact approved Gateway SHA
  provides valid signed LIVE manifests and real vendor executors.
