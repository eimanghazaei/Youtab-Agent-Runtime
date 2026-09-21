import { workspaceIdMatches } from '@/lib/workspace-identity'
import type { ComposerAttachment } from '@/store/composer'

/**
 * The STRICT clean-file / legacy attachability invariant (Wave 2.4, item 1).
 *
 * An attachment may enter a Chat / Agent Run payload only when:
 *
 * - **Legacy path** — `uploadState === undefined` AND either (a) it completed
 *   into a session (`attachedSessionId` is set — a staged local file), or (b) it
 *   is a pure in-app context reference that needs no upload (a resolvable
 *   `refText` and NO local `path`, e.g. an `@file:` ref the user added inline).
 *   An undefined state ALONE is not enough, and a local file with a `path` but no
 *   session was never staged — that local-path-only attachment is the fail-open
 *   bypass the review flagged and is rejected.
 *
 * - **Scan-managed path** — `uploadState === 'clean'` AND there is a non-empty
 *   server-issued `fileId` AND a non-empty issued `workspaceId` AND that issued
 *   workspace matches the authenticated canonical workspace
 *   (`workspaceIdMatches`). Because no canonical workspace authority is integrated
 *   yet, this branch currently rejects everything (fail-closed).
 *
 * Every other state or missing field is rejected: undefined-without-session,
 * forged-clean-without-fileId, clean-without-workspace, workspace-mismatch,
 * uploading / scanning / quarantined / rejected / oversized / quota_exceeded /
 * unsupported_type / scanner_unavailable / workspace_denied / interrupted / error.
 */
export function isAttachmentAttachable(attachment: ComposerAttachment): boolean {
  const state = attachment.uploadState

  // Legacy (no scan lifecycle).
  if (state === undefined) {
    // (a) Staged local file that completed into a session.
    if (typeof attachment.attachedSessionId === 'string' && attachment.attachedSessionId.length > 0) {
      return true
    }

    // (b) Pure in-app context ref (resolvable refText, no local path) — needs no
    // upload and carries no local-path leak.
    if (typeof attachment.refText === 'string' && attachment.refText.length > 0 && !attachment.path) {
      return true
    }

    // A local file with a path but no session was never staged: reject (fail-open).
    return false
  }

  // Scan-managed: only a clean, server-identified, canonical-workspace-bound file.
  if (state === 'clean') {
    return (
      typeof attachment.fileId === 'string' &&
      attachment.fileId.length > 0 &&
      typeof attachment.workspaceId === 'string' &&
      attachment.workspaceId.length > 0 &&
      workspaceIdMatches(attachment.workspaceId)
    )
  }

  return false
}
