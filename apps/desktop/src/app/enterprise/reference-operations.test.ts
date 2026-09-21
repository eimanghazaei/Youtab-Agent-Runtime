import { describe, expect, it } from 'vitest'

import type { OperationDomain, OperationManifest } from './operation-manifest'
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

  it('runs a no-approval operation to effect_complete with a deterministic effectId', async () => {
    const a = await executeReference(crmLead, { company: 'Acme', stage: 'new' }, { approved: false })
    const b = await executeReference(crmLead, { stage: 'new', company: 'Acme' }, { approved: false })
    expect(a.status).toBe('effect_complete')
    expect(a.effectId).toMatch(/^ref-[0-9a-f]{8}$/)
    // Deterministic + order-independent.
    expect(a.effectId).toBe(b.effectId)
  })

  it('returns approval_required until approved for a requiresApproval operation', async () => {
    const gated = await executeReference(crmMerge, { survivorId: '1', mergedId: '2' }, { approved: false })
    expect(gated.status).toBe('approval_required')
    expect(gated.effectId).toBeNull()

    const approved = await executeReference(crmMerge, { survivorId: '1', mergedId: '2' }, { approved: true })
    expect(approved.status).toBe('effect_complete')
    expect(approved.effectId).not.toBeNull()
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
