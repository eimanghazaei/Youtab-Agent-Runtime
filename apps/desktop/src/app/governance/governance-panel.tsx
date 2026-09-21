'use client'

// Governance panel — REAL UI that renders the full approval + effect-ledger
// chain and every adversarial state, driven by `useGovernance`.
//
// MOUNT POINT (documented, not auto-wired): render <GovernancePanel /> from a
// governance route/overlay, e.g. add to `src/app/routes.ts` a `/governance`
// entry that renders this component. It is intentionally self-contained.
//
// Provenance is always visible: a `source: reference` indicator plus a note
// that the real approval + effect-ledger API is a pending dependency (DR-RT-3b).

import { type FC, useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { AlertCircle, AlertTriangle, CheckCircle2, Clock, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import type { GovernancePhase, GovernanceReceipt, GovernanceRequest } from './governance-model'
import { TERMINAL_PHASES } from './governance-model'
import type { ReferenceGovernanceConfig } from './reference-governance'
import { useGovernance } from './use-governance'

const PHASE_LABEL: Record<GovernancePhase, string> = {
  approval_required: 'Approval required',
  approved: 'Approved',
  denied: 'Denied',
  expired: 'Expired',
  payload_modified: 'Payload modified',
  replay_rejected: 'Replay rejected',
  revoked_delegation: 'Revoked delegation',
  workspace_mismatch: 'Workspace mismatch',
  effect_complete: 'Effect completed',
  reconcile_succeeded: 'Reconciliation succeeded',
  reconcile_failed: 'Reconciliation failed'
}

type Tone = 'pending' | 'ok' | 'warn' | 'error'

const PHASE_TONE: Record<GovernancePhase, Tone> = {
  approval_required: 'pending',
  approved: 'ok',
  denied: 'error',
  expired: 'warn',
  payload_modified: 'error',
  replay_rejected: 'error',
  revoked_delegation: 'error',
  workspace_mismatch: 'error',
  effect_complete: 'ok',
  reconcile_succeeded: 'ok',
  reconcile_failed: 'error'
}

const TONE_CLASS: Record<Tone, string> = {
  pending: 'text-(--ui-text-secondary)',
  ok: 'text-emerald-600 dark:text-emerald-400',
  warn: 'text-amber-600 dark:text-amber-400',
  error: 'text-destructive'
}

const PhaseIcon: FC<{ tone: Tone }> = ({ tone }) => {
  if (tone === 'ok') {
    return <CheckCircle2 aria-hidden className={TONE_CLASS.ok} />
  }

  if (tone === 'warn') {
    return <Clock aria-hidden className={TONE_CLASS.warn} />
  }

  if (tone === 'error') {
    return <AlertTriangle aria-hidden className={TONE_CLASS.error} />
  }

  return <AlertCircle aria-hidden className={TONE_CLASS.pending} />
}

export interface GovernancePanelProps {
  /** The request under review. Payload hash must be secret/path free. */
  request?: GovernanceRequest
  /** Reference service configuration (active workspace, revoked delegations, TTL). */
  config?: ReferenceGovernanceConfig
}

const DEFAULT_REQUEST: GovernanceRequest = {
  id: 'ref-req-001',
  action: 'workspace.file.write',
  workspaceId: 'ws-canonical',
  payloadHash: 'sha256:reference-0001',
  requiresApproval: true,
  delegationId: 'del-ref-1'
}

const DEFAULT_CONFIG: ReferenceGovernanceConfig = {
  activeWorkspaceId: 'ws-canonical',
  revokedDelegationIds: new Set(['del-revoked']),
  ttlTicks: 3
}

export const GovernancePanel: FC<GovernancePanelProps> = ({ request, config }) => {
  const activeRequest = useMemo(() => request ?? DEFAULT_REQUEST, [request])
  const activeConfig = useMemo(() => config ?? DEFAULT_CONFIG, [config])

  const { receipt, submit, decide, runEffect, reconcile, reset } = useGovernance(activeConfig)

  const phase = receipt?.phase ?? null
  const isTerminal = phase !== null && TERMINAL_PHASES.has(phase)

  return (
    <section aria-labelledby="governance-heading" className="flex flex-col gap-4 p-4 text-sm">
      <header className="flex flex-col gap-1">
        <h2 className="text-base font-semibold text-(--ui-text-primary)" id="governance-heading">
          Governance — approval &amp; effect ledger
        </h2>
        <p
          className="inline-flex w-fit items-center gap-1.5 rounded-[3px] bg-(--ui-bg-quaternary) px-2 py-0.5 text-xs font-medium text-(--ui-text-secondary)"
          data-testid="governance-source-indicator"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-amber-500" />
          source: reference
        </p>
        <p className="text-xs text-(--ui-text-secondary)">
          Real approval + effect-ledger API pending (DR-RT-3b). Records below are a deterministic reference — effect /
          receipt identifiers stay empty until Runtime supplies them.
        </p>
      </header>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-(--ui-text-secondary)">Request</dt>
        <dd className="font-mono text-(--ui-text-primary)">{activeRequest.id}</dd>
        <dt className="text-(--ui-text-secondary)">Action</dt>
        <dd className="font-mono text-(--ui-text-primary)">{activeRequest.action}</dd>
        <dt className="text-(--ui-text-secondary)">Workspace</dt>
        <dd className="font-mono text-(--ui-text-primary)">{activeRequest.workspaceId}</dd>
        <dt className="text-(--ui-text-secondary)">Payload hash</dt>
        <dd className="font-mono text-(--ui-text-primary)">{activeRequest.payloadHash}</dd>
        <dt className="text-(--ui-text-secondary)">Delegation</dt>
        <dd className="font-mono text-(--ui-text-primary)">{activeRequest.delegationId ?? '—'}</dd>
      </dl>

      <div
        aria-live="polite"
        className="flex flex-col gap-1 rounded-[4px] border border-(--ui-stroke-secondary) p-3"
        data-phase={phase ?? 'idle'}
        data-testid="governance-phase"
      >
        {phase === null ? (
          <p className="text-(--ui-text-secondary)">No request submitted yet.</p>
        ) : (
          <ReceiptView receipt={receipt as GovernanceReceipt} />
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <Button onClick={() => submit(activeRequest)} size="sm" type="button" variant="secondary">
          Submit request
        </Button>
        <Button
          disabled={phase !== 'approval_required'}
          onClick={() => decide('approve')}
          size="sm"
          type="button"
          variant="default"
        >
          Approve
        </Button>
        <Button
          disabled={phase !== 'approval_required'}
          onClick={() => decide('deny')}
          size="sm"
          type="button"
          variant="destructive"
        >
          Deny
        </Button>
        <Button disabled={phase !== 'approved'} onClick={() => runEffect()} size="sm" type="button" variant="secondary">
          Run effect
        </Button>
        <Button
          disabled={phase !== 'effect_complete'}
          onClick={() => reconcile()}
          size="sm"
          type="button"
          variant="outline"
        >
          <RefreshCw aria-hidden />
          Reconcile
        </Button>
        <Button
          disabled={!isTerminal && phase !== 'approved' && phase !== 'effect_complete'}
          onClick={reset}
          size="sm"
          type="button"
          variant="text"
        >
          Reset
        </Button>
      </div>
    </section>
  )
}

const ReceiptView: FC<{ receipt: GovernanceReceipt }> = ({ receipt }) => {
  const tone = PHASE_TONE[receipt.phase]

  return (
    <>
      <p className={cn('inline-flex items-center gap-1.5 font-medium', TONE_CLASS[tone])}>
        <PhaseIcon tone={tone} />
        <span data-testid="governance-phase-label">{PHASE_LABEL[receipt.phase]}</span>
      </p>
      {receipt.detail ? <p className="text-xs text-(--ui-text-secondary)">{receipt.detail}</p> : null}
      <p className="text-[0.6875rem] text-(--ui-text-secondary)">
        source: {receipt.source} · effectId: {receipt.effectId ?? '—'} · receiptId: {receipt.receiptId ?? '—'}
      </p>
    </>
  )
}

export default GovernancePanel
