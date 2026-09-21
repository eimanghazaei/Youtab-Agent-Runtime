// NOTE (Wave 2.2): the backend exposes NO canonical durable inference receipt
// (no receipt_id / effect_id, and no server request-id on prompt.submit's result
// or message.complete). This card therefore presents a truthful "Inference
// summary" of the last round-trip from live session/model/usage state — it is
// NOT, and must not be described as, a durable backend receipt. The durable
// receipt requirement stays BLOCKED until the Backend supplies it. Internal
// identifiers keep the `…Receipt…` name for continuity; only the visible label
// is the truthful "Inference summary".
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { displayModelName } from '@/lib/model-status-label'
import { cn } from '@/lib/utils'

import type { InferenceReceipt, InferenceReceiptStatus } from './use-inference-receipt'

interface InferenceReceiptCardProps {
  receipt: InferenceReceipt | null
  className?: string
}

const STATUS_ICON: Record<InferenceReceiptStatus, string> = {
  failed: 'error',
  'invalid-credential': 'key',
  pending: 'loading',
  success: 'pass',
  'unavailable-provider': 'warning'
}

const STATUS_TONE: Record<InferenceReceiptStatus, string> = {
  failed: 'text-(--ui-text-error,#f87171)',
  'invalid-credential': 'text-(--ui-text-error,#f87171)',
  pending: 'text-(--ui-text-tertiary)',
  success: 'text-(--ui-text-success,#4ade80)',
  'unavailable-provider': 'text-(--ui-text-warning,#facc15)'
}

/**
 * A visible receipt for the LAST real inference round-trip on a session view.
 *
 * It is a truthful reflection of backend-provided state — never a fabricated
 * "connected"/"succeeded" card. When a field the backend does not carry (a
 * server request-id, custom endpoint, token usage, latency) is missing, the
 * row reads "not reported by backend" instead of inventing a value. When the
 * turn failed on missing/invalid credentials or an unconfigured provider, the
 * card shows an explicit, actionable set-up state.
 */
export function InferenceReceiptCard({ receipt, className }: InferenceReceiptCardProps) {
  const { t } = useI18n()
  const copy = t.modelReceipt

  if (!receipt) {
    return (
      <section
        aria-label={copy.title}
        className={cn(
          'rounded-md border border-(--ui-border,rgba(255,255,255,0.1)) px-3 py-2 text-xs text-(--ui-text-tertiary)',
          className
        )}
      >
        {copy.empty}
      </section>
    )
  }

  const { status } = receipt

  const statusLabel: Record<InferenceReceiptStatus, string> = {
    failed: copy.statusFailed,
    'invalid-credential': copy.statusInvalidCredential,
    pending: copy.statusPending,
    success: copy.statusSuccess,
    'unavailable-provider': copy.statusUnavailableProvider
  }

  const notReported = <span className="text-(--ui-text-tertiary) italic">{copy.notReported}</span>

  const usage = receipt.usage

  const hasTokens =
    !!usage && (typeof usage.input === 'number' || typeof usage.output === 'number' || typeof usage.total === 'number')

  const cost = typeof usage?.cost_usd === 'number' ? usage.cost_usd : null

  const actionHint =
    status === 'unavailable-provider'
      ? receipt.provider
        ? copy.unavailableHint(receipt.provider)
        : copy.unavailableHintGeneric
      : status === 'invalid-credential'
        ? copy.invalidCredentialHint
        : null

  return (
    <section
      aria-label={copy.title}
      className={cn(
        'rounded-md border border-(--ui-border,rgba(255,255,255,0.1)) bg-(--ui-panel-background,transparent) px-3 py-2.5 text-xs',
        className
      )}
    >
      <header className="mb-2 flex items-center gap-1.5">
        <Codicon
          className={cn(STATUS_TONE[status], status === 'pending' && 'animate-spin')}
          name={STATUS_ICON[status]}
          size="0.85rem"
        />
        <span className="font-semibold text-foreground">{copy.title}</span>
        <span className={cn('ml-auto font-medium', STATUS_TONE[status])} data-testid="receipt-status">
          {statusLabel[status]}
        </span>
      </header>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-(--ui-text-tertiary)">{copy.model}</dt>
        <dd className="truncate text-foreground" data-testid="receipt-model">
          {receipt.model ? displayModelName(receipt.model) : notReported}
        </dd>

        <dt className="text-(--ui-text-tertiary)">{copy.provider}</dt>
        <dd className="truncate text-foreground" data-testid="receipt-provider">
          {receipt.provider || notReported}
        </dd>

        <dt className="text-(--ui-text-tertiary)">{copy.endpoint}</dt>
        <dd className="truncate text-foreground" data-testid="receipt-endpoint">
          {receipt.endpoint ? receipt.endpoint : notReported}
        </dd>

        <dt className="text-(--ui-text-tertiary)">{copy.runId}</dt>
        <dd className="truncate font-mono text-foreground" data-testid="receipt-run-id">
          {receipt.runId ? receipt.runId : notReported}
        </dd>

        <dt className="text-(--ui-text-tertiary)">{copy.tokens}</dt>
        <dd className="truncate text-foreground" data-testid="receipt-tokens">
          {hasTokens
            ? copy.tokensDetail(Number(usage?.input ?? 0), Number(usage?.output ?? 0), Number(usage?.total ?? 0))
            : notReported}
        </dd>

        <dt className="text-(--ui-text-tertiary)">{copy.latency}</dt>
        <dd className="truncate text-foreground" data-testid="receipt-latency">
          {typeof receipt.latencyMs === 'number' ? copy.latencyDetail(receipt.latencyMs) : notReported}
        </dd>

        {cost !== null ? (
          <>
            <dt className="text-(--ui-text-tertiary)">{copy.cost}</dt>
            <dd className="truncate text-foreground" data-testid="receipt-cost">
              {copy.costDetail(cost)}
            </dd>
          </>
        ) : null}
      </dl>

      {status === 'failed' && receipt.error ? (
        <p className="mt-2 text-(--ui-text-error,#f87171)" data-testid="receipt-error" role="alert">
          {receipt.error}
        </p>
      ) : null}

      {actionHint ? (
        <p
          className="mt-2 flex items-start gap-1.5 text-(--ui-text-secondary,inherit)"
          data-testid="receipt-action-hint"
          role="alert"
        >
          <Codicon className="mt-0.5 shrink-0 text-(--ui-text-tertiary)" name="lightbulb" size="0.75rem" />
          <span>{actionHint}</span>
        </p>
      ) : null}
    </section>
  )
}
