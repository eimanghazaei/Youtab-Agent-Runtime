// Real consumer hook that drives the governance chain
// (request → approval → decision → effect → receipt → reconciliation) through a
// governance service and exposes the current phase + receipt to the UI.
//
// The hook itself is REAL and transport-agnostic: today it is backed by the
// deterministic `ReferenceGovernanceService` (source: 'reference'); once Runtime
// ships the real approval + effect-ledger API (DR-RT-3b) the same hook binds to
// a `source: 'runtime'` service with no change to the panel.

import { useCallback, useMemo, useState } from 'react'

import type { GovernanceDecision, GovernanceReceipt, GovernanceRequest } from './governance-model'
import { type ReferenceGovernanceConfig, ReferenceGovernanceService } from './reference-governance'

/** Minimal service surface the hook depends on (satisfied by the reference impl). */
export interface GovernanceService {
  submitRequest(req: GovernanceRequest): GovernanceReceipt
  decide(requestId: string, decision: GovernanceDecision, currentPayloadHash?: string): GovernanceReceipt
  completeEffect(requestId: string): GovernanceReceipt
  reconcile(requestId: string): GovernanceReceipt
  getReceipt(requestId: string): GovernanceReceipt | null
}

export interface UseGovernanceResult {
  receipt: GovernanceReceipt | null
  submit: (req: GovernanceRequest, currentPayloadHash?: string) => GovernanceReceipt
  decide: (decision: GovernanceDecision) => GovernanceReceipt | null
  runEffect: () => GovernanceReceipt | null
  reconcile: () => GovernanceReceipt | null
  reset: () => void
  /**
   * The live governance service (reference today, Runtime later). Exposed so the
   * panel can drive multi-step adversarial scenarios (replay, payload tamper)
   * against the REAL service synchronously — no fabricated receipts.
   */
  service: GovernanceService
  /** Display a receipt produced by a direct service call. Clears the tracked
   *  request id (adversarial outcomes are terminal — nothing more to decide). */
  show: (receipt: GovernanceReceipt) => void
}

export function useGovernance(serviceOrConfig: GovernanceService | ReferenceGovernanceConfig): UseGovernanceResult {
  const service = useMemo<GovernanceService>(() => {
    if ('submitRequest' in serviceOrConfig) {
      return serviceOrConfig
    }

    return new ReferenceGovernanceService(serviceOrConfig)
    // A caller passing a fresh config object each render would recreate the
    // service; callers pass a stable service or a stable config (the panel
    // memoizes its config), so keying on identity is correct.
  }, [serviceOrConfig])

  const [receipt, setReceipt] = useState<GovernanceReceipt | null>(null)
  const [requestId, setRequestId] = useState<string | null>(null)
  const [lastPayloadHash, setLastPayloadHash] = useState<string | undefined>(undefined)

  const submit = useCallback(
    (req: GovernanceRequest, currentPayloadHash?: string) => {
      const next = service.submitRequest(req)
      setRequestId(req.id)
      setLastPayloadHash(currentPayloadHash ?? req.payloadHash)
      setReceipt(next)

      return next
    },
    [service]
  )

  const decide = useCallback(
    (decision: GovernanceDecision) => {
      if (requestId === null) {
        return null
      }

      const next = service.decide(requestId, decision, lastPayloadHash)
      setReceipt(next)

      return next
    },
    [service, requestId, lastPayloadHash]
  )

  const runEffect = useCallback(() => {
    if (requestId === null) {
      return null
    }

    const next = service.completeEffect(requestId)
    setReceipt(next)

    return next
  }, [service, requestId])

  const reconcile = useCallback(() => {
    if (requestId === null) {
      return null
    }

    const next = service.reconcile(requestId)
    setReceipt(next)

    return next
  }, [service, requestId])

  const reset = useCallback(() => {
    setRequestId(null)
    setLastPayloadHash(undefined)
    setReceipt(null)
  }, [])

  const show = useCallback((next: GovernanceReceipt) => {
    // Adversarial outcomes are terminal: forget the tracked request so the
    // normal approve/effect controls stay disabled until a fresh submit.
    setRequestId(null)
    setLastPayloadHash(undefined)
    setReceipt(next)
  }, [])

  return { receipt, submit, decide, runEffect, reconcile, reset, service, show }
}
