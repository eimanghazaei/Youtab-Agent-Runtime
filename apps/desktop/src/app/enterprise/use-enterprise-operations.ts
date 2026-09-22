// The REAL enterprise-operation consumer hook.
//
// It drives the operation phase state machine on top of an execute service.
// Today the service is the deterministic reference one (`executeReference`);
// when Runtime ships the exact-SHA operation API (DR-RT-3a) the same hook points
// at it by injecting a different `execute`/`reconcile` — no state-machine change.
//
// Phase machine:
//   idle
//     └─ preview()      → preview
//     └─ run(approved)  → (approval_required)? → approved → executing
//                         → effect_complete → reconciled
//        failure branches: denied, failed, reconcile_failed
//
// No secret/credential/local-path is ever placed in params, receipts, or state.

import { useCallback, useMemo, useState } from 'react'

import type { OperationManifest, OperationPhase, OperationReceipt, ReferenceScenarioResult } from './operation-manifest'
import { isCanonicalRuntimeReceipt } from './operation-manifest'
import type { OperationDomain } from './operation-manifest'
import { executeReference, getReferenceManifests } from './reference-operations'

/**
 * A run result is either the CANONICAL Runtime receipt or a renderer-local
 * reference simulation — never conflated. The hook stores this union; only a
 * value that passes `isCanonicalRuntimeReceipt` is ever placed in the canonical
 * authority slot.
 */
export type OperationRunResult = OperationReceipt | ReferenceScenarioResult

/** Read the effect identifier from either result kind (real id vs simulated ref). */
export function runResultEffectRef(result: OperationRunResult): string | null {
  return isCanonicalRuntimeReceipt(result) ? result.effectId : result.simulatedEffectRef
}

/** Execute service shape. The reference impl and (later) the Runtime impl both satisfy it. */
export type ExecuteService = (
  manifest: OperationManifest,
  params: Record<string, unknown>,
  options: { approved: boolean }
) => Promise<OperationRunResult>

/** Reconciliation service: confirms an effect actually landed. */
export type ReconcileService = (result: OperationRunResult) => Promise<boolean>

/** Default reconcile for the reference provider: a completed reference effect reconciles. */
function defaultReconcile(result: OperationRunResult): Promise<boolean> {
  return Promise.resolve(result.status === 'effect_complete' && runResultEffectRef(result) != null)
}

export interface UseEnterpriseOperationsOptions {
  /** Swap to the Runtime execute service when DR-RT-3a lands. Defaults to the reference service. */
  execute?: ExecuteService
  /** Swap to the Runtime reconcile service when DR-RT-3a lands. */
  reconcile?: ReconcileService
}

export interface RunOptions {
  approved?: boolean
}

export interface EnterpriseOperationsState {
  /** Manifests for the active domain (reference today). */
  manifests: OperationManifest[]
  phase: OperationPhase
  /** The most recent run result — canonical receipt OR reference simulation. */
  receipt: OperationRunResult | null
  /**
   * AUTHORITY slot: holds ONLY a CANONICAL Runtime receipt (gated via
   * `isCanonicalRuntimeReceipt`). A reference simulation never lands here, so
   * downstream authority logic can trust this is a real Runtime effect.
   */
  canonicalReceipt: OperationReceipt | null
  /** Human-readable failure reason for `failed`/`denied`/`reconcile_failed`. */
  error: string | null
  /** Whether the last completed run reconciled successfully. */
  reconciled: boolean
  /** Enter the preview phase for an operation the operator is composing. */
  preview: () => void
  /** Drive the full machine for a manifest+params. Re-call with `{ approved: true }` to clear an approval gate. */
  runOperation: (manifest: OperationManifest, params: Record<string, unknown>, options?: RunOptions) => Promise<void>
  /** Operator refuses a pending approval → terminal `denied`. */
  deny: () => void
  /** Return to `idle`, clearing receipt/error. */
  reset: () => void
}

export function useEnterpriseOperations(
  domain: OperationDomain,
  options: UseEnterpriseOperationsOptions = {}
): EnterpriseOperationsState {
  const execute = options.execute ?? executeReference
  const reconcile = options.reconcile ?? defaultReconcile

  const manifests = useMemo(() => getReferenceManifests(domain), [domain])

  const [phase, setPhase] = useState<OperationPhase>('idle')
  const [receipt, setReceipt] = useState<OperationRunResult | null>(null)
  const [canonicalReceipt, setCanonicalReceipt] = useState<OperationReceipt | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reconciled, setReconciled] = useState(false)

  const preview = useCallback(() => {
    setPhase('preview')
    setReceipt(null)
    setCanonicalReceipt(null)
    setError(null)
    setReconciled(false)
  }, [])

  const reset = useCallback(() => {
    setPhase('idle')
    setReceipt(null)
    setCanonicalReceipt(null)
    setError(null)
    setReconciled(false)
  }, [])

  const deny = useCallback(() => {
    setPhase('denied')
    setError('Operation denied by operator.')
    setReconciled(false)
    setCanonicalReceipt(null)
    setReceipt({
      simulatedEffectRef: null,
      status: 'denied',
      detail: 'Operator denied the pending approval.',
      source: 'reference'
    })
  }, [])

  const runOperation = useCallback(
    async (manifest: OperationManifest, params: Record<string, unknown>, runOpts: RunOptions = {}) => {
      const approved = runOpts.approved ?? false
      setError(null)
      setReconciled(false)
      setCanonicalReceipt(null)

      // Approval gate — surface it before any effect is attempted.
      if (manifest.requiresApproval && !approved) {
        setPhase('approval_required')
        setReceipt({
          simulatedEffectRef: null,
          status: 'approval_required',
          detail: 'This operation requires operator approval before it will run.',
          source: 'reference'
        })

        return
      }

      if (manifest.requiresApproval) {
        setPhase('approved')
      }

      setPhase('executing')
      let result: OperationRunResult

      try {
        result = await execute(manifest, params, { approved })
      } catch (cause) {
        setPhase('failed')
        setError(cause instanceof Error ? cause.message : 'Operation failed to execute.')
        setReceipt({
          simulatedEffectRef: null,
          status: 'failed',
          detail: 'Execution raised an error.',
          source: 'reference'
        })

        return
      }

      // The service can still return an approval gate (defence in depth).
      if (result.status === 'approval_required') {
        setPhase('approval_required')
        setReceipt(result)

        return
      }

      setPhase('effect_complete')
      setReceipt(result)

      // Authority gate: ONLY a canonical Runtime receipt reaches the authority
      // slot. A reference simulation is displayed but never treated as real.
      if (isCanonicalRuntimeReceipt(result)) {
        setCanonicalReceipt(result)
      }

      // Post-effect reconciliation.
      let ok: boolean

      try {
        ok = await reconcile(result)
      } catch (cause) {
        setPhase('reconcile_failed')
        setError(cause instanceof Error ? cause.message : 'Reconciliation raised an error.')

        return
      }

      if (ok) {
        setPhase('reconciled')
        setReconciled(true)
      } else {
        setPhase('reconcile_failed')
        setError('Effect completed but could not be reconciled.')
      }
    },
    [execute, reconcile]
  )

  return { manifests, phase, receipt, canonicalReceipt, error, reconciled, preview, runOperation, deny, reset }
}
