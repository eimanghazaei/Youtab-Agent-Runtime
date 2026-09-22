import { describe, expect, it } from 'vitest'

import { isCanonicalGovernanceReceipt, isTruthfulReceipt } from './governance-model'
import type { GovernanceReceipt, GovernanceRequest, ReferenceGovernanceResult } from './governance-model'
import { assertNoSecretInPayload, ReferenceGovernanceService } from './reference-governance'

function makeRequest(over: Partial<GovernanceRequest> = {}): GovernanceRequest {
  return {
    id: over.id ?? 'req-1',
    action: over.action ?? 'workspace.file.write',
    workspaceId: over.workspaceId ?? 'ws-canonical',
    payloadHash: over.payloadHash ?? 'sha256:abc',
    requiresApproval: over.requiresApproval ?? true,
    delegationId: over.delegationId === undefined ? 'del-1' : over.delegationId
  }
}

function newService() {
  return new ReferenceGovernanceService({
    activeWorkspaceId: 'ws-canonical',
    revokedDelegationIds: new Set(['del-revoked']),
    ttlTicks: 3
  })
}

describe('ReferenceGovernanceService — happy path', () => {
  it('drives approval → approved → effect_complete → terminal reconcile', () => {
    const svc = newService()
    expect(svc.submitRequest(makeRequest({ id: 'happy-2' })).phase).toBe('approval_required')
    expect(svc.decide('happy-2', 'approve').phase).toBe('approved')
    expect(svc.completeEffect('happy-2').phase).toBe('effect_complete')
    const reconciled = svc.reconcile('happy-2')
    expect(['reconcile_succeeded', 'reconcile_failed']).toContain(reconciled.phase)
  })

  it('reaches both reconcile_succeeded and reconcile_failed deterministically', () => {
    const outcomes = new Set<string>()

    for (let i = 0; i < 40; i += 1) {
      const svc = newService()
      const id = `recon-${i}`
      svc.submitRequest(makeRequest({ id }))
      svc.decide(id, 'approve')
      svc.completeEffect(id)
      outcomes.add(svc.reconcile(id).phase)
    }

    expect(outcomes.has('reconcile_succeeded')).toBe(true)
    expect(outcomes.has('reconcile_failed')).toBe(true)
  })

  it('is deterministic: same id always yields the same reconcile outcome', () => {
    const run = (id: string) => {
      const svc = newService()
      svc.submitRequest(makeRequest({ id }))
      svc.decide(id, 'approve')
      svc.completeEffect(id)

      return svc.reconcile(id).phase
    }

    expect(run('stable-x')).toBe(run('stable-x'))
  })
})

describe('ReferenceGovernanceService — adversarial states', () => {
  it('deny', () => {
    const svc = newService()
    svc.submitRequest(makeRequest({ id: 'd' }))
    expect(svc.decide('d', 'deny').phase).toBe('denied')
  })

  it('expiry after TTL', () => {
    const svc = newService()
    svc.submitRequest(makeRequest({ id: 'e' }))
    svc.advance(4) // ttlTicks = 3, so > 3 expires
    expect(svc.decide('e', 'approve').phase).toBe('expired')
  })

  it('replay rejected on duplicate id', () => {
    const svc = newService()
    svc.submitRequest(makeRequest({ id: 'r' }))
    expect(svc.submitRequest(makeRequest({ id: 'r' })).phase).toBe('replay_rejected')
  })

  it('payload modified between submit and decision', () => {
    const svc = newService()
    svc.submitRequest(makeRequest({ id: 'p', payloadHash: 'sha256:orig' }))
    expect(svc.decide('p', 'approve', 'sha256:tampered').phase).toBe('payload_modified')
  })

  it('revoked delegation', () => {
    const svc = newService()
    expect(svc.submitRequest(makeRequest({ id: 'rev', delegationId: 'del-revoked' })).phase).toBe('revoked_delegation')
  })

  it('workspace mismatch', () => {
    const svc = newService()
    expect(svc.submitRequest(makeRequest({ id: 'w', workspaceId: 'ws-other' })).phase).toBe('workspace_mismatch')
  })
})

describe('ReferenceGovernanceService — truthfulness', () => {
  it('every reference receipt has null effectId/receiptId and source reference', () => {
    const svc = newService()

    const receipts = [
      svc.submitRequest(makeRequest({ id: 't1' })),
      svc.decide('t1', 'approve'),
      svc.completeEffect('t1'),
      svc.reconcile('t1')
    ]

    for (const r of receipts) {
      expect(r.source).toBe('reference')
      expect(r.simulatedEffectRef).toBeNull()
      expect(r.simulatedReceiptRef).toBeNull()
      expect(isTruthfulReceipt(r)).toBe(true)
    }
  })

  it('rejects a payloadHash containing a Windows path', () => {
    expect(() => assertNoSecretInPayload('C:\\Users\\eiman\\secret.key')).toThrow()
  })

  it('rejects a payloadHash containing a POSIX secret path', () => {
    expect(() => assertNoSecretInPayload('/home/eiman/.ssh/id_rsa')).toThrow()
  })

  it('rejects a payloadHash containing a token literal', () => {
    expect(() => assertNoSecretInPayload('sk-ABCDEFGH12345678')).toThrow()
  })

  it('accepts a clean digest', () => {
    expect(() => assertNoSecretInPayload('sha256:deadbeef')).not.toThrow()
  })

  it('submitRequest throws when the payload leaks a secret', () => {
    const svc = newService()
    expect(() => svc.submitRequest(makeRequest({ id: 's', payloadHash: 'C:\\keys\\a.pem' }))).toThrow()
  })
})

describe('reference/canonical governance type separation (security boundary)', () => {
  it('a reference result fails the canonical runtime guard', () => {
    const svc = newService()
    const result = svc.submitRequest(makeRequest({ id: 'sep-1' }))
    expect(result.source).toBe('reference')
    expect(isCanonicalGovernanceReceipt(result)).toBe(false)
  })

  it('a canonical runtime receipt passes the guard', () => {
    const canonical: GovernanceReceipt = {
      requestId: 'rt-1',
      phase: 'effect_complete',
      effectId: 'rt-effect-1',
      receiptId: 'rt-receipt-1',
      source: 'runtime',
      detail: 'runtime'
    }

    expect(isCanonicalGovernanceReceipt(canonical)).toBe(true)
  })

  it('names the simulated identifiers distinctly — never as canonical effectId/receiptId', () => {
    const svc = newService()
    const result = svc.submitRequest(makeRequest({ id: 'sep-2' }))
    expect(result).toHaveProperty('simulatedEffectRef')
    expect(result).toHaveProperty('simulatedReceiptRef')
    expect(result).not.toHaveProperty('effectId')
    expect(result).not.toHaveProperty('receiptId')
  })

  it('compile-time: a reference result is NOT assignable to the canonical receipt', () => {
    const reference: ReferenceGovernanceResult = {
      requestId: 'ref-1',
      phase: 'approval_required',
      source: 'reference',
      detail: 'simulation',
      simulatedEffectRef: null,
      simulatedReceiptRef: null
    }

    // @ts-expect-error a ReferenceGovernanceResult must never be usable as a canonical GovernanceReceipt
    const canonical: GovernanceReceipt = reference
    expect(canonical.source).toBe('reference')
  })
})
