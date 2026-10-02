'use client'

import { useStore } from '@nanostores/react'
import { type FC, useCallback, useEffect, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { AlertCircle, ChevronDown, Loader2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $gateway } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import {
  type ApprovalRequest,
  clearApprovalRequest,
  registerApprovalInlineAnchor,
  sessionApprovalInlineVisible,
  sessionApprovalRequest
} from '@/store/prompts'

import type { ToolPart } from './fallback-model'

// Prominent approval card under the tool that raised it. Keep the description
// and complete command visible before Run, without stealing focus from chat.
//
// Binding is POSITIONAL, not command-matched: the desktop `tool.start` payload
// carries no structured args (only tool_id/name/context — see
// tui_gateway/server.py::_on_tool_start), so we cannot join the approval to the
// row by command string. But `approval.request` only ever fires from the
// `terminal` / `execute_code` guards and the agent thread blocks on exactly one
// approval at a time, so the single pending row of those tools IS the row that
// raised it. The command/description text comes from `$approvalRequest` (the
// event payload), which is the only place that data reliably exists.
export const APPROVAL_TOOLS = new Set(['terminal', 'execute_code'])

// Canonical gateway choices (ui-tui/src/components/prompts.tsx).
type ApprovalChoice = 'once' | 'session' | 'always' | 'deny'

export const PendingToolApproval: FC<{ part: ToolPart }> = ({ part }) => {
  // The tool row lives in whichever session's transcript rendered it — read
  // THAT session's approval (works for the primary and every tile).
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionApprovalRequest(sessionId), [sessionId])
  const request = useStore($request)

  if (!request || !APPROVAL_TOOLS.has(part.toolName)) {
    return null
  }

  return <InlineApprovalBar request={request} />
}

const InlineApprovalBar: FC<{ request: ApprovalRequest }> = ({ request }) => {
  useEffect(() => registerApprovalInlineAnchor(request.sessionId), [request.sessionId])

  return <ApprovalBar request={request} surface="inline" />
}

export const PendingApprovalFallback: FC = () => {
  const { t } = useI18n()
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionApprovalRequest(sessionId), [sessionId])
  const $inlineVisible = useMemo(() => sessionApprovalInlineVisible(sessionId), [sessionId])
  const request = useStore($request)
  const inlineVisible = useStore($inlineVisible)

  if (!request || inlineVisible) {
    return null
  }

  return (
    <div
      className="pointer-events-none absolute left-1/2 z-30 w-[calc(100%-2rem)] max-w-2xl -translate-x-1/2"
      data-slot="tool-approval-fallback"
      style={{ bottom: 'calc(var(--composer-measured-height) + 0.875rem)' }}
    >
      <div className="pointer-events-auto rounded-xl border border-primary/30 bg-(--ui-chat-surface-background) px-3 py-2 shadow-lg backdrop-blur-xl [-webkit-backdrop-filter:blur(1rem)]">
        <div className="flex min-w-0 items-center gap-2 text-sm text-primary">
          <AlertCircle className="size-4 shrink-0" />
          <span className="shrink-0 font-medium">{t.assistant.approval.jumpToApproval}</span>
          {request.description && (
            <span className="min-w-0 truncate text-(--ui-text-tertiary)">{request.description}</span>
          )}
        </div>
        <ApprovalBar request={request} surface="floating" />
      </div>
    </div>
  )
}

const isMac = typeof navigator !== 'undefined' && /Mac|iP(hone|ad|od)/.test(navigator.platform)

const ApprovalBar: FC<{ request: ApprovalRequest; surface: 'floating' | 'inline' }> = ({ request, surface }) => {
  const { t } = useI18n()
  const copy = t.assistant.approval
  const gateway = useStore($gateway)
  const [submitting, setSubmitting] = useState<ApprovalChoice | null>(null)
  // "Always allow" persists the pattern to ~/.youtab-agent-runtime/config.yaml permanently, so
  // it goes through a confirm step rather than firing straight from the menu.
  const [confirmAlways, setConfirmAlways] = useState(false)
  // Show the full command by default so approval is informed. Users can
  // collapse a long command without changing the approval request.
  const [showCommand, setShowCommand] = useState(true)
  const busy = submitting !== null
  // false when the backend won't honor a permanent allow (tirith warning) → hide "Always allow".
  const allowPermanent = request.allowPermanent !== false
  const choices = request.choices ?? (request.smartDenied ? ['once', 'deny'] : undefined)
  const allowSession = choices ? choices.includes('session') : true
  const allowAlways = choices ? choices.includes('always') : allowPermanent
  const hasMoreOptions = allowSession || allowAlways
  const hasCommand = request.command.trim().length > 0

  const respond = useCallback(
    async (choice: ApprovalChoice) => {
      // Another bar (or the keyboard path) may have already resolved this
      // approval; the map is the single source of truth, so bail if this
      // session's request is gone.
      if (busy || !sessionApprovalRequest(request.sessionId).get()) {
        return
      }

      if (!gateway) {
        notifyError(new Error(copy.gatewayDisconnected), copy.sendFailed)

        return
      }

      setSubmitting(choice)

      try {
        await gateway.request<{ resolved?: boolean }>('approval.respond', {
          choice,
          session_id: request.sessionId ?? undefined
        })
        triggerHaptic(choice === 'deny' ? 'cancel' : 'submit')
        clearApprovalRequest(request.sessionId)
      } catch (error) {
        notifyError(error, copy.sendFailed)
        setSubmitting(null)
      }
    },
    [busy, copy.gatewayDisconnected, copy.sendFailed, gateway, request.sessionId]
  )

  // ⌘/Ctrl+Enter → Run, Esc → Reject.
  // While the confirm dialog is open it owns the keyboard (Esc closes it), so
  // the strip-level shortcuts stand down to avoid denying the whole approval.
  useEffect(() => {
    if (confirmAlways) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        void respond('once')
      } else if (event.key === 'Escape') {
        event.preventDefault()
        void respond('deny')
      }
    }

    window.addEventListener('keydown', onKeyDown, true)

    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [confirmAlways, respond])

  return (
    <div
      aria-label={t.notifications.native.approvalTitle}
      className="my-3 w-full rounded-xl border-2 border-primary/45 bg-(--ui-widget-surface-background) p-5 shadow-lg"
      data-slot={surface === 'inline' ? 'tool-approval-inline' : 'tool-approval-actions'}
      role="region"
    >
      <p className="mb-2 text-base font-semibold text-foreground">{t.notifications.native.approvalTitle}</p>
      <p className="mb-4 whitespace-pre-wrap break-words text-sm text-foreground">{request.description}</p>
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex h-10 items-stretch overflow-hidden rounded-md border border-primary/40 bg-primary/15 text-primary">
          <Button
            className="h-full gap-2 rounded-none px-4 text-sm font-medium text-primary hover:bg-primary/15 hover:text-primary"
            disabled={busy}
            onClick={() => void respond('once')}
            size="xs"
            variant="ghost"
          >
            {submitting === 'once' ? <Loader2 className="size-3 animate-spin" /> : copy.run}
            {submitting !== 'once' && <span className="text-[0.625rem] text-primary/60">{isMac ? '⌘⏎' : 'Ctrl⏎'}</span>}
          </Button>
          {hasMoreOptions && <span aria-hidden className="w-px self-stretch bg-primary/20" />}
          {hasMoreOptions && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  aria-label={copy.moreOptions}
                  className="h-full w-9 rounded-none px-0 text-primary hover:bg-primary/15 hover:text-primary"
                  disabled={busy}
                  size="xs"
                  variant="ghost"
                >
                  <ChevronDown className="size-3" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-44">
                {allowSession && (
                  <DropdownMenuItem onSelect={() => void respond('session')}>{copy.allowSession}</DropdownMenuItem>
                )}
                {allowAlways && (
                  <DropdownMenuItem
                    onSelect={() => {
                      // Defer one tick so the menu fully unmounts before the dialog
                      // mounts — otherwise Radix's focus-return races the dialog and
                      // dismisses it via onInteractOutside.
                      setTimeout(() => setConfirmAlways(true), 0)
                    }}
                  >
                    {copy.alwaysAllowMenu}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onSelect={() => void respond('deny')} variant="destructive">
                  {copy.reject}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        <Button
          className="h-10 gap-2 rounded-md px-3 text-sm font-medium text-foreground hover:text-foreground"
          disabled={busy}
          onClick={() => void respond('deny')}
          size="xs"
          variant="ghost"
        >
          {submitting === 'deny' ? <Loader2 className="size-3 animate-spin" /> : copy.reject}
          {submitting !== 'deny' && <span className="text-[0.625rem] opacity-55">Esc</span>}
        </Button>

        {hasCommand && (
          <Button
            aria-expanded={showCommand}
            className="h-6 gap-1 rounded-md px-1.5 text-xs font-normal text-(--ui-text-tertiary) hover:text-foreground"
            onClick={() => setShowCommand(value => !value)}
            size="xs"
            variant="ghost"
          >
            {copy.command}
            <ChevronDown className={cn('size-3 transition-transform', showCommand && 'rotate-180')} />
          </Button>
        )}
      </div>

      {showCommand && hasCommand && (
        <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background) px-2.5 py-1.5 font-mono text-xs leading-snug text-foreground">
          {request.command.trim()}
        </pre>
      )}

      <Dialog onOpenChange={setConfirmAlways} open={confirmAlways}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{copy.alwaysTitle}</DialogTitle>
            <DialogDescription>{copy.alwaysDescription(request.description)}</DialogDescription>
          </DialogHeader>

          {request.command.trim() && (
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background) px-2.5 py-1.5 font-mono text-xs leading-snug text-foreground">
              {request.command.trim()}
            </pre>
          )}

          <DialogFooter>
            <Button onClick={() => setConfirmAlways(false)} size="sm" variant="ghost">
              {t.common.cancel}
            </Button>
            <Button
              onClick={() => {
                setConfirmAlways(false)
                void respond('always')
              }}
              size="sm"
              variant="destructive"
            >
              {copy.alwaysAllow}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
