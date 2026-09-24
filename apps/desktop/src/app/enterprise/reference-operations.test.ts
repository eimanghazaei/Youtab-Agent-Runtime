import { describe, expect, it } from 'vitest'

import type {
  OperationDomain,
  OperationManifest,
  OperationReceipt,
  ReferenceScenarioResult
} from './operation-manifest'
import { isCanonicalRuntimeReceipt } from './operation-manifest'
import { executeReference, getReferenceManifests } from './reference-operations'

const DOMAINS: OperationDomain[] = ['crm', 'erp', 'sap', 'cad']

describe('getReferenceManifests', () => {
  it('returns typed manifests for every domain', () => {
    for (const domain of DOMAINS) {
      const manifests = getReferenceManifests(domain)
      expect(manifests.length).toBeGreaterThan(0)

      for (const manifest of manifests) {
        // Structural typing assertions — the exact DR-RT-3a contract.
        expect(manifest.domain).toBe(domain)
        expect(typeof manifest.id).toBe('string')
        expect(typeof manifest.title).toBe('string')
        expect(['low', 'medium', 'high']).toContain(manifest.risk)
        expect(typeof manifest.requiresApproval).toBe('boolean')

        for (const param of manifest.params) {
          expect(['string', 'number', 'boolean', 'enum']).toContain(param.type)

          if (param.type === 'enum') {
            expect(Array.isArray(param.options)).toBe(true)
          }
        }
      }
    }
  })
})

const crmLead = getReferenceManifests('crm').find(m => m.id === 'crm.create_lead') as OperationManifest
const crmMerge = getReferenceManifests('crm').find(m => m.id === 'crm.merge_accounts') as OperationManifest

describe('executeReference', () => {
  it('never fabricates a runtime receipt — always source: reference', async () => {
    const receipt = await executeReference(crmLead, { company: 'Acme' }, { approved: false })
    expect(receipt.source).toBe('reference')
  })

  it('runs a no-approval operation to effect_complete with a deterministic simulatedEffectRef', async () => {
    const a = await executeReference(crmLead, { company: 'Acme', stage: 'new' }, { approved: false })
    const b = await executeReference(crmLead, { stage: 'new', company: 'Acme' }, { approved: false })
    expect(a.status).toBe('effect_complete')
    expect(a.simulatedEffectRef).toMatch(/^ref-[0-9a-f]{8}$/)
    // Deterministic + order-independent.
    expect(a.simulatedEffectRef).toBe(b.simulatedEffectRef)
  })

  it('returns approval_required until approved for a requiresApproval operation', async () => {
    const gated = await executeReference(crmMerge, { survivorId: '1', mergedId: '2' }, { approved: false })
    expect(gated.status).toBe('approval_required')
    expect(gated.simulatedEffectRef).toBeNull()

    const approved = await executeReference(crmMerge, { survivorId: '1', mergedId: '2' }, { approved: true })
    expect(approved.status).toBe('effect_complete')
    expect(approved.simulatedEffectRef).not.toBeNull()
    expect(approved.source).toBe('reference')
  })

  it('never places a secret or local path in the receipt', async () => {
    const receipt = await executeReference(crmLead, { company: 'Acme', stage: 'new' }, { approved: false })
    const serialized = JSON.stringify(receipt)
    expect(serialized).not.toMatch(/[A-Za-z]:\\/) // Windows path
    expect(serialized).not.toMatch(/\/(home|Users|etc)\//) // POSIX path
    expect(serialized.toLowerCase()).not.toContain('secret')
    expect(serialized.toLowerCase()).not.toContain('password')
    expect(serialized.toLowerCase()).not.toContain('token')
  })
})

describe('reference/canonical type separation (security boundary)', () => {
  it('a reference result fails the canonical runtime guard', async () => {
    const result = await executeReference(crmLead, { company: 'Acme', stage: 'new' }, { approved: false })
    expect(result.source).toBe('reference')
    expect(isCanonicalRuntimeReceipt(result)).toBe(false)
  })

  it('a canonical runtime receipt passes the guard', () => {
    const canonical: OperationReceipt = {
      effectId: 'rt-effect-1',
      status: 'effect_complete',
      detail: 'runtime',
      source: 'runtime'
    }

    expect(isCanonicalRuntimeReceipt(canonical)).toBe(true)
  })

  it('names the simulated identifier distinctly — never as canonical effectId', async () => {
    const result = await executeReference(crmLead, { company: 'Acme', stage: 'new' }, { approved: false })
    expect(result).toHaveProperty('simulatedEffectRef')
    expect(result).not.toHaveProperty('effectId')
  })

  it('compile-time: a reference result is NOT assignable to the canonical receipt', () => {
    const reference: ReferenceScenarioResult = {
      simulatedEffectRef: 'ref-deadbeef',
      status: 'effect_complete',
      detail: 'simulation',
      source: 'reference'
    }

    // @ts-expect-error a ReferenceScenarioResult must never be usable as a canonical OperationReceipt
    const canonical: OperationReceipt = reference
    expect(canonical.source).toBe('reference')
  })
})
