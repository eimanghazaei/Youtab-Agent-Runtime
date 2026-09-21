import { describe, expect, it } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'

import { isAttachmentAttachable } from './attachment-invariant'

function att(overrides: Partial<ComposerAttachment> = {}): ComposerAttachment {
  return { id: 'a', kind: 'file', label: 'report.pdf', ...overrides }
}

describe('isAttachmentAttachable — strict invariant (Wave 2.5 authority hardening)', () => {
  // ── Legacy path: only a SERVER-issued binding, never a client id ──────────
  it('rejects a legacy (undefined state) attachment with no server binding', () => {
    expect(isAttachmentAttachable(att({ uploadState: undefined }))).toBe(false)
  })

  it('rejects a FORGED attachedSessionId — a client-controlled id is never authority', () => {
    // The remaining fail-open exception is closed: attachedSessionId alone (no
    // server refText / no inline image) must NOT make an attachment attachable.
    expect(isAttachmentAttachable(att({ uploadState: undefined, attachedSessionId: 'forged-sess' }))).toBe(false)
    expect(
      isAttachmentAttachable(att({ uploadState: undefined, attachedSessionId: 'forged', path: '/Users/me/x' }))
    ).toBe(false)
  })

  it('rejects a local-path-only attachment (path but no server ref)', () => {
    expect(isAttachmentAttachable(att({ path: '/Users/me/secret/report.pdf' }))).toBe(false)
  })

  it('accepts a server-issued file ref (non-empty refText from file.attach), even alongside a local path', () => {
    // refText is issued by the authenticated file.attach response; the submit path
    // sends the ref, never the local path, so this is safe and correct.
    expect(isAttachmentAttachable(att({ uploadState: undefined, refText: '@file:`abc123`' }))).toBe(true)
    expect(
      isAttachmentAttachable(att({ uploadState: undefined, refText: '@file:`abc123`', path: '/Users/me/x' }))
    ).toBe(true)
    expect(isAttachmentAttachable(att({ uploadState: undefined, refText: '' }))).toBe(false)
  })

  it('accepts a bounded inline image (allowlisted MIME + base64) but rejects non-inline images', () => {
    expect(
      isAttachmentAttachable(att({ kind: 'image', uploadState: undefined, previewUrl: 'data:image/png;base64,AAAA' }))
    ).toBe(true)
    expect(isAttachmentAttachable(att({ kind: 'image', uploadState: undefined, previewUrl: 'blob:local' }))).toBe(false)
    expect(isAttachmentAttachable(att({ kind: 'image', uploadState: undefined, attachedSessionId: 's' }))).toBe(false)
  })

  it('bounds inline images: rejects a disallowed MIME, non-base64, and an oversized payload', () => {
    // SVG (script-capable) is NOT allowlisted.
    expect(
      isAttachmentAttachable(
        att({ kind: 'image', uploadState: undefined, previewUrl: 'data:image/svg+xml;base64,AAAA' })
      )
    ).toBe(false)
    // Not base64-encoded.
    expect(
      isAttachmentAttachable(att({ kind: 'image', uploadState: undefined, previewUrl: 'data:image/png,AAAA' }))
    ).toBe(false)
    // Over the 10 MiB byte cap (~14M base64 chars).
    const huge = `data:image/png;base64,${'A'.repeat(14_000_001)}`
    expect(isAttachmentAttachable(att({ kind: 'image', uploadState: undefined, previewUrl: huge }))).toBe(false)
  })

  // ── Scan-managed path ────────────────────────────────────────────────────
  it('rejects a forged clean state with no server fileId', () => {
    expect(isAttachmentAttachable(att({ uploadState: 'clean' }))).toBe(false)
    expect(isAttachmentAttachable(att({ uploadState: 'clean', fileId: '' }))).toBe(false)
  })

  it('rejects a clean file with a fileId but no issued workspaceId', () => {
    expect(isAttachmentAttachable(att({ uploadState: 'clean', fileId: 'f1' }))).toBe(false)
    expect(isAttachmentAttachable(att({ uploadState: 'clean', fileId: 'f1', workspaceId: '' }))).toBe(false)
  })

  it('rejects a clean file whose workspace does NOT match the canonical authority', () => {
    // No canonical authority is integrated (resolveCanonicalWorkspaceId()->null),
    // so even a well-formed clean+fileId+workspaceId is rejected fail-closed.
    expect(isAttachmentAttachable(att({ uploadState: 'clean', fileId: 'f1', workspaceId: 'ws-forged' }))).toBe(false)
  })

  // ── Every other lifecycle state ──────────────────────────────────────────
  it.each([
    'uploading',
    'scanning',
    'quarantined',
    'rejected',
    'oversized',
    'quota_exceeded',
    'unsupported_type',
    'scanner_unavailable',
    'workspace_denied',
    'interrupted',
    'error'
  ] as const)('rejects the non-clean lifecycle state %s (even with a fileId + workspaceId)', state => {
    expect(isAttachmentAttachable(att({ uploadState: state, fileId: 'f1', workspaceId: 'ws-1' }))).toBe(false)
  })

  it('rejects payload mutation that swaps a rejected file to look clean without a fileId', () => {
    const rejected = att({ uploadState: 'rejected', fileId: '', workspaceId: '' })
    const mutated: ComposerAttachment = { ...rejected, uploadState: 'clean' }
    // Still rejected: no fileId / no workspace / no canonical match.
    expect(isAttachmentAttachable(mutated)).toBe(false)
  })
})
