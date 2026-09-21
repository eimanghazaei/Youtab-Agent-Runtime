import { workspaceIdMatches } from '@/lib/workspace-identity'
import type { ComposerAttachment } from '@/store/composer'

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

// Bounded inline-image policy (Wave 2.6 security correction). A `data:` image is
// NOT inherently trusted: it is accepted only within a bounded MIME allowlist and
// byte-size cap. The APPROVED server-side inline-image validation/scanning policy
// (owned by the Gateway) is still a BLOCKED dependency; these client-side bounds
// are a fail-closed floor, not a substitute for it.
const INLINE_IMAGE_MIME_ALLOWLIST = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const INLINE_IMAGE_MAX_BYTES = 10 * 1024 * 1024 // 10 MiB

function isBoundedInlineImage(previewUrl: string | undefined): boolean {
  if (typeof previewUrl !== 'string' || !previewUrl.startsWith('data:')) {
    return false
  }

  const header = previewUrl.slice(5, previewUrl.indexOf(','))
  const mime = header.split(';')[0]?.trim().toLowerCase()

  if (!mime || !INLINE_IMAGE_MIME_ALLOWLIST.has(mime) || !header.includes('base64')) {
    return false
  }

  const base64 = previewUrl.slice(previewUrl.indexOf(',') + 1)
  // 4 base64 chars ≈ 3 bytes; cheap upper-bound estimate, no decode.
  const approxBytes = Math.floor((base64.length * 3) / 4)

  return approxBytes > 0 && approxBytes <= INLINE_IMAGE_MAX_BYTES
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
    // A server-RETURNED reference from the authenticated file.attach response (or a
    // gateway-resolvable in-app @file: ref). NOTE: refText is only a server-returned
    // REFERENCE — it is NOT an authority or an immutable binding here, and the
    // frontend never treats it as authorization. The Gateway/Runtime MUST revalidate
    // it (resolving @file: in the authenticated workspace) at submit — that is a
    // REQUIRED contract (DR-GW-2), not a fact proven by this frontend.
    if (isNonEmptyString(attachment.refText)) {
      return true
    }

    // Inline image: accepted only within a bounded MIME allowlist + byte cap.
    // A `data:` URL is not inherently trusted; full server validation/scanning is a
    // blocked Gateway dependency. Out-of-bounds → fail closed.
    if (attachment.kind === 'image' && isBoundedInlineImage(attachment.previewUrl)) {
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
