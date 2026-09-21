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
 * The outcome record for a request at its current phase.
 *
 * `effectId` / `receiptId` are non-null ONLY when `source === 'runtime'`. For
 * `source === 'reference'` they are always `null` — the reference service never
 * fabricates ledger identifiers.
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

/** Guard: a reference receipt must never carry a fabricated runtime identifier. */
export function isTruthfulReceipt(receipt: GovernanceReceipt): boolean {
  if (receipt.source === 'reference') {
    return receipt.effectId === null && receipt.receiptId === null
  }

  return true
}
