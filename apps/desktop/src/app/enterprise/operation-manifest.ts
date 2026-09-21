// Enterprise operation manifest — the typed contract the Runtime will later fill.
//
// This is the exact DR-RT-3a requirement surface: a real, typed consumer sits on
// top of these types. TODAY the manifests + receipts are produced by a clearly
// labelled DETERMINISTIC REFERENCE provider (see `reference-operations.ts`, every
// value carries `source: 'reference'`). When Runtime ships the real, exact-SHA
// operation API it fills the SAME types and flips `source` to `'runtime'`; no UI
// or hook change is required beyond swapping the provider/execute service.
//
// Nothing here calls a live vendor (CRM/ERP/SAP/CAD) and nothing fabricates a
// `runtime` receipt — that would present fake success as a real Runtime effect.

/** Enterprise back-office domains a manifest can target. */
export type OperationDomain = 'crm' | 'erp' | 'sap' | 'cad'

/** How risky the effect is — drives the risk badge and (with the manifest) approval. */
export type RiskLevel = 'low' | 'medium' | 'high'

/** A single typed parameter the operator supplies before the operation runs. */
export interface OperationParam {
  key: string
  label: string
  type: 'string' | 'number' | 'boolean' | 'enum'
  required: boolean
  /** Allowed values when `type === 'enum'`. */
  options?: string[]
}

/** A typed, executable operation description. Runtime and the reference provider
 *  both emit this exact shape. */
export interface OperationManifest {
  id: string
  domain: OperationDomain
  title: string
  description: string
  params: OperationParam[]
  risk: RiskLevel
  requiresApproval: boolean
}

/** Provenance of a manifest/receipt. `'reference'` today (deterministic reference
 *  provider); `'runtime'` once the real exact-SHA operation API lands (DR-RT-3a). */
export interface OperationSource {
  source: 'reference' | 'runtime'
}

/**
 * Lifecycle of one operation run. The consumer hook drives this machine:
 *
 *   idle → preview → (approval_required → approved →)? executing
 *        → effect_complete → reconciled
 *
 * Terminal failure branches: `denied` (approval refused), `failed` (execute
 * error), `reconcile_failed` (effect landed but post-effect reconciliation
 * could not confirm it).
 */
export type OperationPhase =
  | 'idle'
  | 'preview'
  | 'approval_required'
  | 'approved'
  | 'executing'
  | 'effect_complete'
  | 'reconciled'
  | 'reconcile_failed'
  | 'denied'
  | 'failed'

/**
 * The result of an operation run.
 *
 * For a REAL run, `effectId` and `status` are authoritative values returned by
 * Runtime (the exact-SHA effect ledger). The reference service instead returns
 * clearly-marked reference receipts: `source: 'reference'`, a deterministic
 * `ref-…` effectId, and `detail` text that names the reference nature. A
 * reference receipt is NEVER emitted with `source: 'runtime'`.
 */
export interface OperationReceipt {
  /** Runtime effect id (real) or a deterministic `ref-…` id (reference). Null before an effect lands. */
  effectId: string | null
  status: OperationPhase
  detail: string | null
  source: OperationSource['source']
}
