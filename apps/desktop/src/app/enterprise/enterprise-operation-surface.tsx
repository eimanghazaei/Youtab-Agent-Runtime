// Generic, REAL enterprise operation surface.
//
// Renders any typed manifest: connection status, workspace/org, the available
// operations list, a typed parameter form, the risk badge, the approval
// requirement + approve/deny controls, a preview, an execute control, execution
// status, the effect receipt, the reconciliation result, and an actionable
// failure state.
//
// Every surface shows a visible `source: reference` indicator and a note that
// real Runtime operations are a pending dependency (DR-RT-3a). Controls are
// visible and functional against the deterministic reference service — nothing
// is inert. No secret/credential/local-path is placed in any param or payload.

import { useCallback, useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

import type { OperationManifest, OperationParam, RiskLevel } from './operation-manifest'
import type { ExecuteService, ReconcileService } from './use-enterprise-operations'
import { useEnterpriseOperations } from './use-enterprise-operations'

const RISK_VARIANT: Record<RiskLevel, 'muted' | 'warn' | 'destructive'> = {
  low: 'muted',
  medium: 'warn',
  high: 'destructive'
}

function defaultParamValue(param: OperationParam): string | number | boolean {
  if (param.type === 'boolean') {
    return false
  }

  if (param.type === 'number') {
    return 0
  }

  if (param.type === 'enum') {
    return param.options?.[0] ?? ''
  }

  return ''
}

function initialValues(manifest: OperationManifest): Record<string, string | number | boolean> {
  const values: Record<string, string | number | boolean> = {}

  for (const param of manifest.params) {
    values[param.key] = defaultParamValue(param)
  }

  return values
}

export interface EnterpriseOperationSurfaceProps {
  manifests: OperationManifest[]
  /** Selected workspace/org from session state, else the reference workspace. */
  workspace?: string
  /** Injected once Runtime lands (DR-RT-3a); defaults to the reference service. */
  execute?: ExecuteService
  reconcile?: ReconcileService
}

export function EnterpriseOperationSurface({
  manifests,
  workspace = 'reference',
  execute,
  reconcile
}: EnterpriseOperationSurfaceProps) {
  const domain = manifests[0]?.domain ?? 'crm'

  const { phase, receipt, error, reconciled, preview, runOperation, deny, reset } = useEnterpriseOperations(domain, {
    execute,
    reconcile
  })

  const [selectedId, setSelectedId] = useState(manifests[0]?.id ?? '')
  const selected = useMemo(() => manifests.find(m => m.id === selectedId) ?? manifests[0], [manifests, selectedId])

  const [values, setValues] = useState<Record<string, string | number | boolean>>(() =>
    selected ? initialValues(selected) : {}
  )

  const selectManifest = useCallback(
    (id: string) => {
      setSelectedId(id)
      const next = manifests.find(m => m.id === id)
      setValues(next ? initialValues(next) : {})
      reset()
    },
    [manifests, reset]
  )

  const setValue = useCallback((key: string, value: string | number | boolean) => {
    setValues(prev => ({ ...prev, [key]: value }))
  }, [])

  if (!selected) {
    return (
      <section aria-label="Enterprise operations" className="p-4 text-sm text-muted-foreground">
        No reference operations available for this domain.
      </section>
    )
  }

  const showApproval = phase === 'approval_required'

  return (
    <section aria-label={`${selected.domain.toUpperCase()} operations`} className="flex flex-col gap-4 p-4">
      {/* Connection + provenance header — always visible. */}
      <header className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">{selected.domain.toUpperCase()} operations</span>
        <Badge data-testid="source-indicator" variant="outline">
          source: reference
        </Badge>
        <Badge data-testid="connection-status" variant="muted">
          connected (reference)
        </Badge>
        <Badge data-testid="workspace-indicator" variant="muted">
          workspace: {workspace}
        </Badge>
      </header>
      <p className="text-xs text-muted-foreground" data-testid="runtime-pending-note">
        Running against a deterministic reference. Real Runtime operations are a pending dependency (DR-RT-3a).
      </p>

      {/* Available operations list. */}
      <div>
        <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="enterprise-op-select">
          Operation
        </label>
        <select
          className="w-full rounded-[3px] border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm"
          id="enterprise-op-select"
          onChange={event => selectManifest(event.target.value)}
          value={selected.id}
        >
          {manifests.map(manifest => (
            <option key={manifest.id} value={manifest.id}>
              {manifest.title}
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-muted-foreground">{selected.description}</p>
      </div>

      {/* Risk + approval requirement. */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge data-testid="risk-badge" variant={RISK_VARIANT[selected.risk]}>
          risk: {selected.risk}
        </Badge>
        {selected.requiresApproval ? (
          <Badge data-testid="approval-required-badge" variant="warn">
            approval required
          </Badge>
        ) : (
          <Badge variant="muted">no approval needed</Badge>
        )}
      </div>

      {/* Typed parameter form. */}
      <fieldset className="flex flex-col gap-3 border-0 p-0">
        <legend className="sr-only">Operation parameters</legend>
        {selected.params.map(param => {
          const inputId = `enterprise-param-${param.key}`
          const value = values[param.key]

          return (
            <div className="flex flex-col gap-1" key={param.key}>
              <label className="text-xs font-medium" htmlFor={inputId}>
                {param.label}
                {param.required ? <span aria-hidden="true"> *</span> : null}
              </label>
              {param.type === 'boolean' ? (
                <input
                  checked={Boolean(value)}
                  className="size-4 self-start"
                  id={inputId}
                  onChange={event => setValue(param.key, event.target.checked)}
                  type="checkbox"
                />
              ) : param.type === 'enum' ? (
                <select
                  className="rounded-[3px] border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm"
                  id={inputId}
                  onChange={event => setValue(param.key, event.target.value)}
                  value={String(value)}
                >
                  {(param.options ?? []).map(option => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className="rounded-[3px] border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm"
                  id={inputId}
                  onChange={event =>
                    setValue(param.key, param.type === 'number' ? Number(event.target.value) : event.target.value)
                  }
                  required={param.required}
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={String(value)}
                />
              )}
            </div>
          )
        })}
      </fieldset>

      {/* Controls — all visible and functional against the reference service. */}
      <div className="flex flex-wrap gap-2">
        <Button data-testid="preview-button" onClick={preview} type="button" variant="secondary">
          Preview
        </Button>
        <Button
          data-testid="execute-button"
          onClick={() => void runOperation(selected, values)}
          type="button"
          variant="default"
        >
          Execute (reference)
        </Button>
        {phase !== 'idle' ? (
          <Button data-testid="reset-button" onClick={reset} type="button" variant="text">
            Reset
          </Button>
        ) : null}
      </div>

      {/* Preview panel. */}
      {phase === 'preview' ? (
        <div className="rounded-[3px] bg-muted/40 p-3 text-xs" data-testid="preview-panel">
          <div className="mb-1 font-medium">Preview (reference)</div>
          <ul className="flex flex-col gap-0.5">
            {selected.params.map(param => (
              <li key={param.key}>
                <span className="text-muted-foreground">{param.label}: </span>
                <span>{String(values[param.key])}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Approval controls. */}
      {showApproval ? (
        <div
          aria-label="Approval"
          className="flex flex-wrap items-center gap-2"
          data-testid="approval-panel"
          role="group"
        >
          <span className="text-xs text-muted-foreground">This operation needs approval before it runs.</span>
          <Button
            data-testid="approve-button"
            onClick={() => void runOperation(selected, values, { approved: true })}
            type="button"
            variant="default"
          >
            Approve
          </Button>
          <Button data-testid="deny-button" onClick={deny} type="button" variant="destructive">
            Deny
          </Button>
        </div>
      ) : null}

      {/* Execution status. */}
      <div aria-live="polite" className="text-xs" data-testid="phase-status">
        <span className="text-muted-foreground">status: </span>
        <span className="font-medium">{phase}</span>
      </div>

      {/* Effect receipt. */}
      {receipt ? (
        <div className="rounded-[3px] border border-(--ui-stroke-secondary) p-3 text-xs" data-testid="receipt">
          <div className="mb-1 font-medium">Effect receipt</div>
          <div>
            <span className="text-muted-foreground">effectId: </span>
            <span data-testid="receipt-effect-id">{receipt.effectId ?? '—'}</span>
          </div>
          <div>
            <span className="text-muted-foreground">status: </span>
            <span>{receipt.status}</span>
          </div>
          <div>
            <span className="text-muted-foreground">source: </span>
            <Badge data-testid="receipt-source" variant="outline">
              {receipt.source}
            </Badge>
          </div>
          {receipt.detail ? <p className="mt-1 text-muted-foreground">{receipt.detail}</p> : null}
        </div>
      ) : null}

      {/* Reconciliation result. */}
      {phase === 'reconciled' && reconciled ? (
        <div className="text-xs text-emerald-600 dark:text-emerald-400" data-testid="reconciliation-ok">
          Reconciled: effect confirmed against the reference ledger.
        </div>
      ) : null}

      {/* Actionable failure state. */}
      {(phase === 'failed' || phase === 'denied' || phase === 'reconcile_failed') && error ? (
        <div
          className="flex flex-col gap-2 rounded-[3px] bg-destructive/10 p-3 text-xs"
          data-testid="failure-state"
          role="alert"
        >
          <span className="font-medium text-destructive">
            {phase === 'denied'
              ? 'Denied'
              : phase === 'reconcile_failed'
                ? 'Reconciliation failed'
                : 'Operation failed'}
          </span>
          <span>{error}</span>
          <div>
            <Button data-testid="failure-retry" onClick={reset} type="button" variant="secondary">
              Start over
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
