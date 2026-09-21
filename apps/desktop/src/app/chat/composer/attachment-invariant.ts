import { workspaceIdMatches } from '@/lib/workspace-identity'
import type { ComposerAttachment } from '@/store/composer'

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

/**
 * The STRICT clean-file / legacy attachability invariant (Wave 2.4 item 1,
 * hardened in Wave 2.5 per the exact-SHA review).
 *
 * **Authority rule:** a usable attachment state must be DERIVED FROM AN
 * AUTHENTICATED SERVER RESPONSE. A client-controlled value — most notably the
 * client-set `attachedSessionId` — is NEVER authority and is ignored here.
 *
 * An attachment may enter a Chat / Agent Run payload only when:
 *
 * - **Legacy path** — `uploadState === undefined` AND it carries a server-issued
 *   binding that produces a real gateway reference:
 *   - a non-empty `refText` — issued by the authenticated `file.attach` response
 *     (which requires `attached === true` + `ref_text`), or a gateway-resolvable
 *     in-app `@file:` reference; OR
 *   - an inline image: `kind === 'image'` with a `data:` `previewUrl` (the user's
 *     own image bytes, already staged via `image.attach[_bytes]`; images have no
 *     scan lifecycle).
 *   `attachedSessionId` alone is NOT accepted — a local-path-only or
 *   forged-session attachment has no server binding and is rejected (this closes
 *   the remaining fail-open exception).
 *
 * - **Scan-managed path** — `uploadState === 'clean'` AND a non-empty server
 *   `fileId` AND a non-empty issued `workspaceId` AND that workspace matches the
 *   authenticated canonical workspace (`workspaceIdMatches`). No canonical
 *   workspace authority is integrated yet, so this branch rejects everything
 *   (fail-closed).
 *
 * Every other state or missing field is rejected.
 */
export function isAttachmentAttachable(attachment: ComposerAttachment): boolean {
  const state = attachment.uploadState

  // Legacy (no scan lifecycle) — requires a server-issued binding, never a
  // client-controlled id.
  if (state === undefined) {
    // Server-issued file ref (from file.attach's authenticated response) or a
    // gateway-resolvable in-app @file: ref.
    if (isNonEmptyString(attachment.refText)) {
      return true
    }

    // Inline image bytes staged via the authenticated image.attach[_bytes].
    if (attachment.kind === 'image' && attachment.previewUrl?.startsWith('data:') === true) {
      return true
    }

    // No server binding (local-path-only, or a forged attachedSessionId): reject.
    return false
  }

  // Scan-managed: only a clean, server-identified, canonical-workspace-bound file.
  if (state === 'clean') {
    return (
      isNonEmptyString(attachment.fileId) &&
      isNonEmptyString(attachment.workspaceId) &&
      workspaceIdMatches(attachment.workspaceId)
    )
  }

  return false
}
