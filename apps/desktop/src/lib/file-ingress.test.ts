import { describe, expect, it } from 'vitest'

import { ingestFile, isAttachable } from './file-ingress'

function input(overrides: Partial<Parameters<typeof ingestFile>[0]> = {}) {
  return { bytes: new ArrayBuffer(8), safeName: 'notes.txt', sizeBytes: 8, workspaceId: 'ws-1', ...overrides }
}

describe('file-ingress consumer (fail-closed, no invented method)', () => {
  it('fails closed with scanner_unavailable — never fabricates a clean result or invents a method', async () => {
    const result = await ingestFile(input())

    expect(result.state).toBe('scanner_unavailable')
    expect(result.reasonCode).toBe('gateway_ingress_contract_unavailable')
    expect(result.fileId).toBe('')
    expect(isAttachable(result)).toBe(false)
  })

  it('rejects a missing canonical workspace id fail-closed (workspace_denied) — no profile substitution', async () => {
    const result = await ingestFile(input({ workspaceId: null }))

    expect(result.state).toBe('workspace_denied')
    expect(result.reasonCode).toBe('no_canonical_workspace')
    expect(isAttachable(result)).toBe(false)
  })

  it('honours an already-aborted signal as interrupted (no clean artifact)', async () => {
    const controller = new AbortController()
    controller.abort()

    const result = await ingestFile(input(), { signal: controller.signal })

    expect(result.state).toBe('interrupted')
    expect(isAttachable(result)).toBe(false)
  })

  it('never emits an absolute local path in the result (safeName only)', async () => {
    const result = await ingestFile(input({ safeName: 'report.pdf' }))

    expect(JSON.stringify(result)).not.toContain('/Users/')
    expect(result.safeName).toBe('report.pdf')
  })

  it('isAttachable requires state=clean AND a non-empty server-issued fileId', () => {
    expect(isAttachable({ state: 'clean', fileId: 'f1' })).toBe(true)
    expect(isAttachable({ state: 'clean', fileId: '' })).toBe(false)
    expect(isAttachable({ state: 'quarantined', fileId: 'f1' })).toBe(false)
    expect(isAttachable({ state: 'scanner_unavailable', fileId: '' })).toBe(false)
    expect(isAttachable({ state: 'rejected', fileId: 'f1' })).toBe(false)
  })
})
