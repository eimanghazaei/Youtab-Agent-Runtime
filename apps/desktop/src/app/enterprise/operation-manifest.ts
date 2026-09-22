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
 * CANONICAL operation receipt — the AUTHORITATIVE Runtime effect record whose
 * `effectId` is a real exact-SHA ledger identifier (DR-RT-3a).
 *
 * The security boundary is enforced STRUCTURALLY: a renderer-local
 * reference/simulation result is a DISTINCT type (`ReferenceScenarioResult`,
 * below) that carries NO `effectId` field at all — its simulated identifier is
 * the differently-named `simulatedEffectRef`. A `ReferenceScenarioResult` is
 * therefore NEVER assignable to `OperationReceipt` (and vice-versa), so a
 * simulation can never be passed where a canonical receipt is required. Only a
 * value with `source === 'runtime'` (see `isCanonicalRuntimeReceipt`) is treated
 * as authoritative.
 *
 * NOTE: `source` retains the `'reference' | 'runtime'` union rather than the
 * `'runtime'` literal so the frozen contract-conformance fixture
 * (`src/contracts/contract-conformance.test.ts`, off-limits to edit) keeps
 * compiling; the field-name separation above is what actually guarantees
 * non-interchangeability.
 */
export interface OperationReceipt {
  /** Real Runtime effect id. Null before an effect lands. */
  effectId: string | null
  status: OperationPhase
  detail: string | null
  source: 'reference' | 'runtime'
}

/**
 * NON-AUTHORITATIVE simulation result from the renderer-local DETERMINISTIC
 * REFERENCE provider. It intentionally does NOT carry an `effectId`: its
 * simulated identifier lives on the distinctly-named `simulatedEffectRef` field
 * and its `source` is LOCKED to `'reference'`. This type is deliberately NOT
 * assignable to `OperationReceipt`, so a simulated result can never be presented
 * as a real Runtime effect.
 */
export interface ReferenceScenarioResult {
  /** Deterministic `ref-…` simulation reference — NOT a Runtime effect id. Null when no effect is simulated. */
  simulatedEffectRef: string | null
  status: OperationPhase
  detail: string | null
  source: 'reference'
}

/**
 * Type guard: narrows to the CANONICAL Runtime receipt. Any authority slot that
 * must not accept a simulation result should gate on this.
 */
export function isCanonicalRuntimeReceipt(r: OperationReceipt | ReferenceScenarioResult): r is OperationReceipt {
  return r.source === 'runtime'
}
