// Deterministic REFERENCE approval + effect-ledger service (DR-RT-3b).
//
// This is a REAL, fully-deterministic implementation of the approval + effect
// chain — NOT a stub and NOT live Runtime state. Every receipt it emits carries
// `source: 'reference'` and never a fabricated `effectId` / `receiptId`. It
// exists only until Runtime ships the real exact-SHA approval + effect-ledger
// API, at which point the same `use-governance` consumer swaps to a
// `source: 'runtime'` transport with no UI change.
//
// Determinism: no wall-clock, no randomness. Time is an explicit injected
// "clock tick" so expiry is reproducible in tests and in the UI. Reconciliation
// success/failure is a pure function of the request id, so the same input
// always yields the same terminal phase.

import type { GovernanceDecision, GovernancePhase, GovernanceReceipt, GovernanceRequest } from './governance-model'

/** Configuration for a deterministic reference service instance. */
export interface ReferenceGovernanceConfig {
  /** The single canonical/active workspace; requests for others mismatch. */
  activeWorkspaceId: string
  /** Delegation ids that have been revoked. */
  revokedDelegationIds?: ReadonlySet<string>
  /** Time-to-live, in clock ticks, before an undecided request expires. */
  ttlTicks?: number
}

interface StoredRequest {
  request: GovernanceRequest
  /** The payloadHash captured at submit time (source of truth for tamper check). */
  submittedPayloadHash: string
  submittedAtTick: number
  phase: GovernancePhase
  detail: string | null
}

const SECRET_PATTERNS: readonly RegExp[] = [
  // Windows drive path, POSIX absolute path, or an obvious key/token literal.
  /[a-zA-Z]:\\/,
  /(^|[\s"'([])\/(?:home|users|etc|var|tmp|root)\//i,
  /\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9]{8,}/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/
]

/** Reject payload hashes that leak a secret or a local path. */
export function assertNoSecretInPayload(payloadHash: string): void {
  for (const pattern of SECRET_PATTERNS) {
    if (pattern.test(payloadHash)) {
      throw new Error('reference-governance: payloadHash must not contain a secret or local path')
    }
  }
}

/** Stable, non-cryptographic digest used only to fork reconcile outcomes. */
function stableDigest(value: string): number {
  let hash = 2166136261

  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }

  return hash >>> 0
}

function referenceReceipt(requestId: string, phase: GovernancePhase, detail: string | null): GovernanceReceipt {
  // Reference receipts NEVER carry a fabricated effectId/receiptId.
  return { requestId, phase, effectId: null, receiptId: null, source: 'reference', detail }
}

/**
 * A deterministic reference approval + effect-ledger service. All state lives in
 * memory and is a pure function of the calls made against it.
 */
export class ReferenceGovernanceService {
  private readonly activeWorkspaceId: string
  private readonly revokedDelegationIds: ReadonlySet<string>
  private readonly ttlTicks: number
  private readonly requests = new Map<string, StoredRequest>()
  private tick = 0

  constructor(config: ReferenceGovernanceConfig) {
    this.activeWorkspaceId = config.activeWorkspaceId
    this.revokedDelegationIds = config.revokedDelegationIds ?? new Set()
    this.ttlTicks = config.ttlTicks ?? 3
  }

  /** Advance the deterministic clock (drives expiry). */
  advance(ticks = 1): void {
    this.tick += ticks
  }

  /** Current deterministic clock value. */
  now(): number {
    return this.tick
  }

  /**
   * Submit a request. Detects replay (same id twice), workspace mismatch and
   * revoked delegation up-front. Returns the resulting receipt.
   */
  submitRequest(request: GovernanceRequest): GovernanceReceipt {
    assertNoSecretInPayload(request.payloadHash)

    if (this.requests.has(request.id)) {
      // Replay of an already-seen requestId is rejected — the original record
      // is preserved and a rejection receipt is returned.
      return referenceReceipt(request.id, 'replay_rejected', 'requestId already submitted; replay rejected')
    }

    if (request.workspaceId !== this.activeWorkspaceId) {
      const stored: StoredRequest = {
        request,
        submittedPayloadHash: request.payloadHash,
        submittedAtTick: this.tick,
        phase: 'workspace_mismatch',
        detail: `request workspace "${request.workspaceId}" is not the active workspace`
      }

      this.requests.set(request.id, stored)

      return referenceReceipt(request.id, stored.phase, stored.detail)
    }

    if (request.delegationId !== null && this.revokedDelegationIds.has(request.delegationId)) {
      const stored: StoredRequest = {
        request,
        submittedPayloadHash: request.payloadHash,
        submittedAtTick: this.tick,
        phase: 'revoked_delegation',
        detail: `delegation "${request.delegationId}" is revoked`
      }

      this.requests.set(request.id, stored)

      return referenceReceipt(request.id, stored.phase, stored.detail)
    }

    const phase: GovernancePhase = request.requiresApproval ? 'approval_required' : 'approved'

    const stored: StoredRequest = {
      request,
      submittedPayloadHash: request.payloadHash,
      submittedAtTick: this.tick,
      phase,
      detail: request.requiresApproval ? 'awaiting approver decision' : 'auto-approved (no approval required)'
    }

    this.requests.set(request.id, stored)

    return referenceReceipt(request.id, stored.phase, stored.detail)
  }

  /**
   * Record an approve/deny decision. Re-checks payload integrity and expiry at
   * decision time. `currentPayloadHash` lets a caller prove the payload was not
   * modified between submit and decision.
   */
  decide(requestId: string, decision: GovernanceDecision, currentPayloadHash?: string): GovernanceReceipt {
    const stored = this.requests.get(requestId)

    if (!stored) {
      return referenceReceipt(requestId, 'replay_rejected', 'unknown requestId')
    }

    // Terminal states cannot be re-decided.
    if (stored.phase !== 'approval_required') {
      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    // Expiry: past TTL with no decision.
    if (this.tick - stored.submittedAtTick > this.ttlTicks) {
      stored.phase = 'expired'
      stored.detail = `request expired after ${this.ttlTicks} ticks with no decision`

      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    // Payload tamper check.
    if (currentPayloadHash !== undefined && currentPayloadHash !== stored.submittedPayloadHash) {
      stored.phase = 'payload_modified'
      stored.detail = 'payloadHash changed between submit and decision'

      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    if (decision === 'deny') {
      stored.phase = 'denied'
      stored.detail = 'approver denied the request'

      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    stored.phase = 'approved'
    stored.detail = 'approver approved the request'

    return referenceReceipt(requestId, stored.phase, stored.detail)
  }

  /**
   * Execute the approved effect. Only an `approved` request produces an effect.
   * The reference effect carries NO effectId (that is Runtime-only).
   */
  completeEffect(requestId: string): GovernanceReceipt {
    const stored = this.requests.get(requestId)

    if (!stored) {
      return referenceReceipt(requestId, 'replay_rejected', 'unknown requestId')
    }

    if (stored.phase !== 'approved') {
      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    stored.phase = 'effect_complete'
    stored.detail = 'reference effect executed (no runtime effectId — pending DR-RT-3b)'

    return referenceReceipt(requestId, stored.phase, stored.detail)
  }

  /**
   * Reconcile a completed effect against the ledger. Deterministic: the outcome
   * is a pure function of the request id (even digest → succeeded, odd → failed)
   * so both branches are reachable and reproducible.
   */
  reconcile(requestId: string): GovernanceReceipt {
    const stored = this.requests.get(requestId)

    if (!stored) {
      return referenceReceipt(requestId, 'replay_rejected', 'unknown requestId')
    }

    if (stored.phase !== 'effect_complete') {
      return referenceReceipt(requestId, stored.phase, stored.detail)
    }

    const succeeded = stableDigest(requestId) % 2 === 0
    stored.phase = succeeded ? 'reconcile_succeeded' : 'reconcile_failed'
    stored.detail = succeeded
      ? 'reference ledger reconciliation matched'
      : 'reference ledger reconciliation mismatch (effect vs ledger divergence)'

    return referenceReceipt(requestId, stored.phase, stored.detail)
  }

  /** Current receipt for a request, or `null` if unknown. */
  getReceipt(requestId: string): GovernanceReceipt | null {
    const stored = this.requests.get(requestId)

    if (!stored) {
      return null
    }

    return referenceReceipt(requestId, stored.phase, stored.detail)
  }
}
