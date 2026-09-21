import { useStore } from '@nanostores/react'

import { useSessionView } from '@/app/chat/session-view'
import type { ChatMessage } from '@/lib/chat-messages'
import { isProviderSetupErrorMessage } from '@/lib/provider-setup-errors'
import { $currentUsage } from '@/store/session'
import type { UsageStats } from '@/types/youtab'

/**
 * The truthful state of the last real inference round-trip on a session view.
 *
 * `pending` — a turn is in flight (busy/awaiting); `success` — a completed
 * assistant reply exists; the three failure variants classify a real terminal
 * error string (never a fabricated status). `null` means no run has happened
 * yet on this surface, so there is nothing to show.
 */
export type InferenceReceiptStatus = 'failed' | 'invalid-credential' | 'pending' | 'success' | 'unavailable-provider'

export interface InferenceReceipt {
  status: InferenceReceiptStatus
  /** Selected model id (SessionView `$model` → store `$currentModel`). */
  model: string
  /** Selected provider slug (SessionView `$provider` → store `$currentProvider`). */
  provider: string
  /** Runtime/session id — the ONLY run identifier the backend surfaces today.
   *  There is no separate server request-id on `prompt.submit`'s result (it
   *  returns nothing) or on the `message.complete` payload, so a distinct
   *  request-id is deliberately not modeled here. Null before the first run. */
  runId: string | null
  /** Custom/self-hosted endpoint for a user-defined provider, when the surface
   *  can supply it (e.g. `ModelOptionProvider.api_url` / `CustomEndpoint.base_url`).
   *  Not carried on a per-run store, so null means "not reported by backend". */
  endpoint?: null | string
  /** Real per-turn usage merged from the `message.complete` payload
   *  (gateway-event.ts). Partial/empty when the backend reported no usage. */
  usage?: null | Partial<UsageStats>
  /** Client-measured round-trip latency in ms, when the surface captured it.
   *  The backend does NOT carry latency on `message.complete`, so this is null
   *  unless a caller measured it locally. */
  latencyMs?: null | number
  /** The real terminal error string for a failure state. Null otherwise. */
  error?: null | string
}

/**
 * Classify a REAL terminal error string into a receipt state. Reads only the
 * backend-provided message — no fabrication:
 * - provider-setup errors ("No provider configured", missing key) →
 *   `unavailable-provider` (actionable "set up X");
 * - explicit auth/credential rejections → `invalid-credential`;
 * - everything else → a truthful `failed`.
 */
export function classifyInferenceError(message: null | string | undefined): InferenceReceiptStatus {
  const text = (message ?? '').trim()

  if (!text) {
    return 'failed'
  }

  if (isProviderSetupErrorMessage(text)) {
    return 'unavailable-provider'
  }

  if (
    /\b(401|403)\b|unauthor|authentication|invalid[\s-]*(api[\s-]*)?key|invalid[\s-]*credential|forbidden/i.test(text)
  ) {
    return 'invalid-credential'
  }

  return 'failed'
}

function lastVisibleAssistant(messages: readonly ChatMessage[]): ChatMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]

    if (message.role === 'assistant' && !message.hidden) {
      return message
    }
  }

  return undefined
}

/**
 * Build the receipt for the most recent real inference round-trip from the
 * live session-view stores. Pure derivation so it can be unit-tested directly.
 */
export function deriveInferenceReceipt(input: {
  busy: boolean
  awaitingResponse: boolean
  model: string
  provider: string
  runtimeId: string | null
  messages: readonly ChatMessage[]
  usage?: null | Partial<UsageStats>
  endpoint?: null | string
  latencyMs?: null | number
}): InferenceReceipt | null {
  const assistant = lastVisibleAssistant(input.messages)

  // Nothing has run on this surface yet — no runtime session and no reply.
  if (!input.runtimeId && !assistant) {
    return null
  }

  const base = {
    model: input.model,
    provider: input.provider,
    runId: input.runtimeId,
    endpoint: input.endpoint ?? null,
    usage: input.usage ?? null,
    latencyMs: input.latencyMs ?? null
  }

  if (input.busy || input.awaitingResponse) {
    return { ...base, status: 'pending', error: null }
  }

  if (assistant?.error) {
    return { ...base, status: classifyInferenceError(assistant.error), error: assistant.error }
  }

  if (assistant) {
    return { ...base, status: 'success', error: null }
  }

  return null
}

/**
 * Live receipt for the current session view. Reads the SAME stores the model
 * menu and status bar use — it does not duplicate model/provider/usage state.
 * Pass `endpoint`/`latencyMs` from a surface that can honestly supply them.
 */
export function useInferenceReceipt(options?: {
  endpoint?: null | string
  latencyMs?: null | number
}): InferenceReceipt | null {
  const view = useSessionView()
  const model = useStore(view.$model)
  const provider = useStore(view.$provider)
  const runtimeId = useStore(view.$runtimeId)
  const busy = useStore(view.$busy)
  const awaitingResponse = useStore(view.$awaitingResponse)
  const messages = useStore(view.$messages)
  const usage = useStore($currentUsage)

  return deriveInferenceReceipt({
    awaitingResponse,
    busy,
    endpoint: options?.endpoint ?? null,
    latencyMs: options?.latencyMs ?? null,
    messages,
    model,
    provider,
    runtimeId,
    usage
  })
}
