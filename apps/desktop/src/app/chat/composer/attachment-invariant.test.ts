import { describe, expect, it } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'

import { isAttachmentAttachable } from './attachment-invariant'

function att(overrides: Partial<ComposerAttachment> = {}): ComposerAttachment {
  return { id: 'a', kind: 'file', label: 'report.pdf', ...overrides }
}

describe('isAttachmentAttachable — strict invariant (Wave 2.4 fail-open closure)', () => {
  // ── Legacy path ──────────────────────────────────────────────────────────
  it('rejects a legacy (undefined state) attachment with NO attachedSessionId', () => {
    // This is the fail-open bypass the review flagged: undefined alone must NOT pass.
    expect(isAttachmentAttachable(att({ uploadState: undefined }))).toBe(false)
  })

  it('accepts a legacy attachment only once it has a valid attachedSessionId', () => {
    expect(isAttachmentAttachable(att({ uploadState: undefined, attachedSessionId: 'sess-1' }))).toBe(true)
    expect(isAttachmentAttachable(att({ uploadState: undefined, attachedSessionId: '' }))).toBe(false)
  })

  it('rejects a local-path-only attachment (path but no session, no clean scan)', () => {
    expect(isAttachmentAttachable(att({ path: '/Users/me/secret/report.pdf' }))).toBe(false)
  })

  it('accepts a pure in-app context ref (resolvable refText, no local path, no upload needed)', () => {
    expect(isAttachmentAttachable(att({ uploadState: undefined, refText: '@file:`docs/readme.md`' }))).toBe(true)
  })

  it('still rejects a refText that also carries a local path but no session (path-leak guard)', () => {
    expect(isAttachmentAttachable(att({ uploadState: undefined, refText: '@file:x', path: '/Users/me/x' }))).toBe(false)
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
