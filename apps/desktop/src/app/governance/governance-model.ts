// Governance UI foundation — the smallest reasonable TYPED contract for the
// Agent Runtime approval + effect-ledger model.
//
// STATUS / PROVENANCE (DR-RT-3b):
// Runtime does NOT yet expose a generic approval + effect-ledger REST API. The
// only shipped primitive is a 2-tool event-driven inline approval
// (`components/assistant-ui/tool/approval.tsx`). Until Runtime delivers the
// real exact-SHA API, this contract is driven by a DETERMINISTIC REFERENCE
// service (`reference-governance.ts`).
//
// TRUTHFULNESS RULE (hard): real `effectId` / `receiptId` / latency come ONLY
// from Runtime. The reference service returns clearly-marked reference receipts
// carrying `source: 'reference'`; it MUST NOT fabricate `effectId` / `receiptId`
// — those stay `null` for every reference receipt and are non-null only when a
// receipt genuinely originates from Runtime (`source: 'runtime'`). No invented
// receipt/effect id, latency, or backend state may ever be presented as real.

/**
 * The full request → approval → decision → effect → receipt → reconciliation
 * chain, including every adversarial terminal/intermediate state the governance
 * model must make visible and truthful.
 */
export type GovernancePhase =
  | 'approval_required'
  | 'approved'
  | 'denied'
  | 'expired'
  | 'payload_modified'
  | 'replay_rejected'
  | 'revoked_delegation'
  | 'workspace_mismatch'
  | 'effect_complete'
  | 'reconcile_succeeded'
  | 'reconcile_failed'

/** A governance-controlled action request awaiting a decision. */
export interface GovernanceRequest {
  id: string
  action: string
  workspaceId: string
  /**
   * Content hash of the request payload. Bound at submit time and re-checked at
   * decision time — a mismatch is an adversarial `payload_modified` outcome.
   * MUST NOT contain secrets or local filesystem paths.
   */
  payloadHash: string
  requiresApproval: boolean
  /** Delegation authorizing the action, or `null` for a direct request. */
  delegationId: string | null
}

/**
 * CANONICAL governance outcome record — the AUTHORITATIVE Runtime approval/effect
 * result. Only Runtime (the exact-SHA approval + effect-ledger API, DR-RT-3b)
 * carries real `effectId` / `receiptId` ledger identifiers; a value is treated as
 * authoritative only when `source === 'runtime'`.
 *
 * A renderer-local simulation is the DISTINCT `ReferenceGovernanceResult` type
 * below — it has NO `effectId` / `receiptId` fields at all (its simulated slots
 * are named `simulatedEffectRef` / `simulatedReceiptRef` and are always `null`),
 * so a simulation is never assignable to this type and can never be passed where
 * a canonical receipt is required. Only a value with `source === 'runtime'` (see
 * `isCanonicalGovernanceReceipt`) is treated as authoritative.
 *
 * NOTE: `source` retains the `'reference' | 'runtime'` union rather than the
 * `'runtime'` literal so the frozen contract-conformance fixture
 * (`src/contracts/contract-conformance.test.ts`, off-limits to edit) keeps
 * compiling; the field-name separation above is what guarantees
 * non-interchangeability.
 */
export interface GovernanceReceipt {
  requestId: string
  phase: GovernancePhase
  effectId: string | null
  receiptId: string | null
  source: 'reference' | 'runtime'
  /** Human-readable explanation of the phase (why denied/expired/etc.). */
  detail: string | null
}

/**
 * NON-AUTHORITATIVE governance simulation from the deterministic REFERENCE
 * service. `source` is LOCKED to `'reference'`. It deliberately does NOT expose
 * `effectId` / `receiptId`: its simulated identifiers live on the distinctly
 * named `simulatedEffectRef` / `simulatedReceiptRef` fields, both always `null`,
 * so nothing here can be presented as a real Runtime ledger identifier.
 */
export interface ReferenceGovernanceResult {
  requestId: string
  phase: GovernancePhase
  source: 'reference'
  detail: string | null
  simulatedEffectRef: null
  simulatedReceiptRef: null
}

/** Type guard: narrows to the CANONICAL Runtime governance receipt. */
export function isCanonicalGovernanceReceipt(r: GovernanceReceipt | ReferenceGovernanceResult): r is GovernanceReceipt {
  return r.source === 'runtime'
}

/** Read the effect identifier from either result kind (real id vs simulated ref). */
export function governanceEffectRef(r: GovernanceReceipt | ReferenceGovernanceResult): string | null {
  return isCanonicalGovernanceReceipt(r) ? r.effectId : r.simulatedEffectRef
}

/** Read the receipt identifier from either result kind (real id vs simulated ref). */
export function governanceReceiptRef(r: GovernanceReceipt | ReferenceGovernanceResult): string | null {
  return isCanonicalGovernanceReceipt(r) ? r.receiptId : r.simulatedReceiptRef
}

/** Decision an approver can take on a pending request. */
export type GovernanceDecision = 'approve' | 'deny'

/** Phases that terminate the chain (no further transition possible). */
export const TERMINAL_PHASES: ReadonlySet<GovernancePhase> = new Set<GovernancePhase>([
  'denied',
  'expired',
  'payload_modified',
  'replay_rejected',
  'revoked_delegation',
  'workspace_mismatch',
  'reconcile_succeeded',
  'reconcile_failed'
])

/** Guard: a reference result must never carry a fabricated runtime identifier. */
export function isTruthfulReceipt(receipt: GovernanceReceipt | ReferenceGovernanceResult): boolean {
  if (isCanonicalGovernanceReceipt(receipt)) {
    return true
  }

  // Reference simulation: its distinctly-named simulated slots are always null.
  return receipt.simulatedEffectRef === null && receipt.simulatedReceiptRef === null
}
