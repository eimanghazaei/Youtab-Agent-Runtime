import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AttachmentList, isAttachmentAttachable } from '@/app/chat/composer/attachments'
import { useComposerActions } from '@/app/chat/hooks/use-composer-actions'
import { useFileDropZone } from '@/app/chat/hooks/use-file-drop-zone'
import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import type { YoutabFolderGrant } from '@/global'
import { useI18n } from '@/i18n'
import { readDesktopFileDataUrl } from '@/lib/desktop-fs'
import { ingestFile } from '@/lib/file-ingress'
import {
  FolderGrantsUnavailableError,
  isFolderGrantsAvailable,
  listFolderGrants,
  requestFolderGrant,
  revokeFolderGrant
} from '@/lib/folder-grants'
import { FileText, FolderOpen, Loader2, Lock, Plus, RefreshCw, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { resolveCanonicalWorkspaceId } from '@/lib/workspace-identity'
import { type ComposerAttachment, type ComposerAttachmentScope, createComposerAttachmentScope } from '@/store/composer'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $currentCwd } from '@/store/session'

import { useFileCapability } from './use-file-capability'

// Decode a base64 data URL to raw bytes for the gateway ingress. Returns null
// when the payload isn't a usable data URL (never throws a path into a log).
function dataUrlToArrayBuffer(dataUrl: string): ArrayBuffer | null {
  const comma = dataUrl.indexOf(',')

  if (comma < 0) {
    return null
  }

  try {
    const binary = atob(dataUrl.slice(comma + 1))
    const bytes = new Uint8Array(binary.length)

    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i)
    }

    return bytes.buffer
  } catch {
    return null
  }
}

/**
 * The Local files panel: mounted in the shipped right-sidebar surface. Attach
 * files from this machine and (when the Local Runtime is present) grant a folder
 * to it.
 *
 * Codex Wave 2.3: the file ingest is FAIL-CLOSED. There is no integrated Gateway
 * ingress contract and no backend workspace authority yet, so a staged file is
 * never transmitted and never marked clean — it surfaces a truthful
 * `workspace_denied` / `scanner_unavailable` state. Only a `clean` file with a
 * non-empty server-issued `fileId` is attachable (there are none today). Folder
 * grants call the REAL bridge, or render a truthful "Local Runtime required"
 * state when it is absent.
 *
 * No absolute local path is ever sent onward or logged: grants expose `safeLabel`
 * only, and the (future) ingress will carry a display `safeName` + bytes, never a
 * path.
 */
export function LocalFilesPanel() {
  const { t } = useI18n()
  const copy = t.localFiles
  const currentCwd = useStore($currentCwd)
  // Used ONLY by the existing composer-actions pipeline (pick/drop staging); the
  // local-file ingest deliberately does NOT use it (fail-closed, see stageAttachment).
  const { requestGateway } = useGatewayRequest()

  // A dedicated attachment scope so these chips never collide with the chat
  // composer's rail.
  const scope = useMemo<ComposerAttachmentScope>(() => createComposerAttachmentScope(), [])
  const attachments = useStore(scope.$attachments)

  // In-flight ingest aborts, keyed by attachment id, so cancel is real.
  const abortsRef = useRef(new Map<string, AbortController>())

  const stageAttachment = useCallback(
    async (attachment: ComposerAttachment) => {
      if (!attachment.path) {
        return
      }

      const controller = new AbortController()
      abortsRef.current.set(attachment.id, controller)

      // Codex Wave 2.3 (item 3): the workspace id MUST come from the real backend
      // authority — never the gateway profile/cwd. With no authority integrated,
      // resolve returns null and we fail closed (transmit nothing, no path read).
      const workspaceId = resolveCanonicalWorkspaceId()

      if (!workspaceId) {
        scope.setUploadState(attachment.id, 'workspace_denied')
        abortsRef.current.delete(attachment.id)

        return
      }

      scope.setUploadState(attachment.id, 'scanning')

      try {
        const dataUrl = await readDesktopFileDataUrl(attachment.path)

        if (controller.signal.aborted) {
          return
        }

        const bytes = dataUrlToArrayBuffer(dataUrl)

        if (!bytes) {
          scope.setUploadState(attachment.id, 'interrupted')

          return
        }

        // Fail-closed consumer: nothing is transmitted until the real integrated
        // Gateway ingress contract lands. The AbortController is threaded so cancel
        // stays real. No invented method / response shape.
        const result = await ingestFile(
          { bytes, safeName: attachment.label, sizeBytes: bytes.byteLength, workspaceId },
          { signal: controller.signal }
        )

        if (controller.signal.aborted) {
          return
        }

        // Replace in place — a late result must not resurrect a removed chip.
        // 'selected' isn't a composer pill state; treat it as still-scanning.
        const updated: ComposerAttachment = {
          ...attachment,
          fileId: result.fileId || undefined,
          uploadState: result.state === 'selected' ? 'scanning' : result.state
        }

        scope.update(updated)
      } catch {
        if (!controller.signal.aborted) {
          scope.setUploadState(attachment.id, 'interrupted')
        }
      } finally {
        abortsRef.current.delete(attachment.id)
      }
    },
    [scope]
  )

  const actionsScope = useMemo(
    () => ({
      add: (attachment: ComposerAttachment) => {
        scope.add(attachment)
        void stageAttachment(attachment)
      },
      remove: (id: string) => scope.remove(id),
      target: 'local-files'
    }),
    [scope, stageAttachment]
  )

  const { attachDroppedItems, pickContextPaths } = useComposerActions({
    activeSessionId: null,
    currentCwd,
    requestGateway,
    scope: actionsScope
  })

  const { dragKind, dropHandlers } = useFileDropZone({
    onDropFiles: files => void attachDroppedItems(files)
  })

  const cancelAttachment = useCallback(
    (id: string) => {
      abortsRef.current.get(id)?.abort()
      abortsRef.current.delete(id)
      scope.remove(id)
    },
    [scope]
  )

  const retryAttachment = useCallback(
    (attachment: ComposerAttachment) => {
      void stageAttachment(attachment)
    },
    [stageAttachment]
  )

  // A workspace change (cwd or gateway profile) invalidates every pending stage
  // and foreign-workspace binding — clear the panel. Previous value lives in the
  // effect closure (not a render-synced ref); the initial subscribe fire no-ops.
  useEffect(() => {
    const workspaceKey = () => [$currentCwd.get(), $activeGatewayProfile.get()].join('\n')
    let previous = workspaceKey()

    const onWorkspaceChange = () => {
      const next = workspaceKey()

      if (next === previous) {
        return
      }

      previous = next

      for (const controller of abortsRef.current.values()) {
        controller.abort()
      }

      abortsRef.current.clear()
      scope.clear()
    }

    const unsubscribeCwd = $currentCwd.subscribe(onWorkspaceChange)
    const unsubscribeProfile = $activeGatewayProfile.subscribe(onWorkspaceChange)

    return () => {
      unsubscribeCwd()
      unsubscribeProfile()
    }
  }, [scope])

  // ── Folder grants ────────────────────────────────────────────────────────
  const grantsAvailable = isFolderGrantsAvailable()
  // Capability state derived from a REAL Gateway runtime signal (live $gatewayState
  // + GET /api/status), NOT a static flag (Wave 2.5 items 2/3/4). The Add/drop
  // control stays visible; it is disabled with the exact dependency status +
  // remediation while unavailable/loading/error, and activates in place when the
  // approved Gateway advertises the capability.
  const fileCapability = useFileCapability()
  const fileScanReady = fileCapability.state === 'available'
  const [grants, setGrants] = useState<YoutabFolderGrant[]>([])
  const [grantBusy, setGrantBusy] = useState(false)

  const refreshGrants = useCallback(async () => {
    if (!isFolderGrantsAvailable()) {
      setGrants([])

      return
    }

    try {
      setGrants(await listFolderGrants())
    } catch (error) {
      if (!(error instanceof FolderGrantsUnavailableError)) {
        notifyError(error, copy.grantFailed)
      }

      setGrants([])
    }
  }, [copy.grantFailed])

  useEffect(() => {
    void refreshGrants()
  }, [refreshGrants])

  const onGrantFolder = useCallback(async () => {
    setGrantBusy(true)

    try {
      await requestFolderGrant({ readOnly: true })
      await refreshGrants()
    } catch (error) {
      if (!(error instanceof FolderGrantsUnavailableError)) {
        notifyError(error, copy.grantFailed)
      }
    } finally {
      setGrantBusy(false)
    }
  }, [copy.grantFailed, refreshGrants])

  const onRevokeGrant = useCallback(
    async (grantId: string) => {
      try {
        await revokeFolderGrant(grantId)
        await refreshGrants()
      } catch (error) {
        if (!(error instanceof FolderGrantsUnavailableError)) {
          notifyError(error, copy.revokeFailed)
        }
      }
    },
    [copy.revokeFailed, refreshGrants]
  )

  const activeGrants = grants.filter(grant => grant.status === 'active')
  const pendingCount = attachments.filter(item => !isAttachmentAttachable(item)).length

  return (
    <section aria-label={copy.title} className="flex flex-col gap-4 p-3" data-slot="local-files-panel">
      <header className="flex flex-col gap-1">
        <h2 className="text-sm font-semibold text-foreground/90">{copy.title}</h2>
        <p className="text-xs text-muted-foreground">{copy.description}</p>
      </header>

      <div className="flex flex-col gap-2">
        {/* Wave 2.5 (item 4): the Add control stays VISIBLE at all times. It is
            disabled with the exact dependency status + remediation while the
            capability is loading/unavailable/error, and activates IN PLACE (same
            DOM) once the approved Gateway advertises the capability. */}
        <div className="flex items-center gap-2">
          <button
            aria-disabled={!fileScanReady}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-lg border border-border/60 bg-background/60 px-2.5 py-1.5 text-xs font-medium text-foreground/90 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
              fileScanReady ? 'hover:border-primary/35 hover:bg-accent/45' : 'cursor-not-allowed opacity-55'
            )}
            disabled={!fileScanReady}
            onClick={() => void pickContextPaths('file')}
            type="button"
          >
            <Plus className="size-3.5" />
            {copy.addFiles}
          </button>
          {fileScanReady && pendingCount > 0 && (
            <span aria-live="polite" className="text-[0.7rem] text-muted-foreground">
              {copy.pending(pendingCount)}
            </span>
          )}
        </div>

        <div
          aria-disabled={!fileScanReady}
          aria-label={copy.dropZone}
          className={cn(
            'relative flex min-h-16 flex-col items-center justify-center gap-1 rounded-xl border border-dashed px-3 py-4 text-center text-xs transition-colors',
            !fileScanReady
              ? 'border-border/45 text-muted-foreground/70 opacity-55'
              : dragKind === 'files'
                ? 'border-primary/60 bg-primary/5 text-foreground'
                : 'border-border/55 text-muted-foreground'
          )}
          {...(fileScanReady ? dropHandlers : {})}
        >
          <FileText className="size-4 opacity-60" />
          <span>{fileScanReady && dragKind === 'files' ? copy.dropZoneActive : copy.dropZone}</span>
        </div>

        {/* Truthful capability status: loading / unavailable / error — derived from
            the live gateway signal, with the exact dependency + remediation. */}
        {!fileScanReady && (
          <div
            className="flex items-start gap-1.5 rounded-lg border border-border/45 bg-background/40 px-2.5 py-2 text-[0.7rem] text-muted-foreground"
            data-capability-state={fileCapability.state}
            data-slot="file-ingest-status"
            role="status"
          >
            {fileCapability.state === 'loading' ? (
              <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin opacity-70" />
            ) : (
              <Lock className="mt-0.5 size-3.5 shrink-0 opacity-70" />
            )}
            <span>
              <span className="font-medium text-foreground/80">{copy.ingestUnavailable}</span>
              {fileCapability.reason ? <> — {fileCapability.reason}</> : null}
            </span>
          </div>
        )}

        {attachments.length === 0 ? (
          <p className="text-[0.7rem] text-muted-foreground/80">{copy.empty}</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            <AttachmentList attachments={attachments} onRemove={id => cancelAttachment(id)} />
            <ul className="flex flex-col gap-1">
              {attachments.map(attachment => {
                const isPending = attachment.uploadState === 'scanning' || attachment.uploadState === 'uploading'

                const isProblem =
                  !isPending && !isAttachmentAttachable(attachment) && attachment.uploadState !== undefined

                if (!isPending && !isProblem) {
                  return null
                }

                return (
                  <li className="flex items-center justify-end gap-1.5" key={attachment.id}>
                    {isProblem && (
                      <button
                        aria-label={copy.retry(attachment.label)}
                        className="inline-flex items-center gap-1 rounded-md border border-border/60 px-1.5 py-0.5 text-[0.65rem] text-foreground/80 hover:bg-accent/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                        onClick={() => retryAttachment(attachment)}
                        type="button"
                      >
                        <RefreshCw className="size-3" />
                      </button>
                    )}
                    <button
                      aria-label={copy.cancel(attachment.label)}
                      className="inline-flex items-center gap-1 rounded-md border border-border/60 px-1.5 py-0.5 text-[0.65rem] text-foreground/80 hover:bg-accent/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      onClick={() => cancelAttachment(attachment.id)}
                      type="button"
                    >
                      <Trash2 className="size-3" />
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2 border-t border-border/40 pt-3">
        <div className="flex flex-col gap-0.5">
          <h3 className="text-xs font-semibold text-foreground/85">{copy.grantsTitle}</h3>
          <p className="text-[0.7rem] text-muted-foreground">{copy.grantsDescription}</p>
        </div>

        {grantsAvailable ? (
          <>
            <button
              className="inline-flex w-fit items-center gap-1.5 rounded-lg border border-border/60 bg-background/60 px-2.5 py-1.5 text-xs font-medium text-foreground/90 transition-colors hover:border-primary/35 hover:bg-accent/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:opacity-60"
              disabled={grantBusy}
              onClick={() => void onGrantFolder()}
              type="button"
            >
              {grantBusy ? <Loader2 className="size-3.5 animate-spin" /> : <FolderOpen className="size-3.5" />}
              {copy.grantFolderReadOnly}
            </button>

            {activeGrants.length === 0 ? (
              <p className="text-[0.7rem] text-muted-foreground/80">{copy.noGrants}</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {activeGrants.map(grant => (
                  <li
                    className="flex items-center gap-2 rounded-lg border border-border/50 bg-background/40 px-2 py-1.5"
                    key={grant.grantId}
                  >
                    <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground/90">{grant.safeLabel}</span>
                    <span className="shrink-0 rounded-full border border-border/50 px-1.5 py-0.5 text-[0.6rem] text-muted-foreground">
                      {grant.permission === 'read-write' ? copy.readWriteBadge : copy.readOnlyBadge}
                    </span>
                    <button
                      aria-label={copy.revokeGrant(grant.safeLabel)}
                      className="inline-flex shrink-0 items-center rounded-md p-1 text-muted-foreground hover:bg-accent/45 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      onClick={() => void onRevokeGrant(grant.grantId)}
                      type="button"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <div
            className="flex items-start gap-2 rounded-lg border border-border/50 bg-muted/20 px-2.5 py-2"
            data-slot="runtime-required"
            role="status"
          >
            <Lock className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <div className="flex flex-col gap-0.5">
              <span className="text-xs font-medium text-foreground/85">{copy.runtimeRequired}</span>
              <span className="text-[0.7rem] text-muted-foreground">{copy.runtimeRequiredHint}</span>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
