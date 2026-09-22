import { describe, expect, it } from 'vitest'

import drRt3aEnterpriseOperation from './fixtures/dr-rt-3a.enterprise-operation.json'
import { compareExpectationToProviderSchema } from './provider-schema-compare'

// Codex #3 binding readiness: prove the comparison seam works against a
// provider-shaped schema, so when a real Runtime/Gateway SHA arrives its
// EXPORTED schema is diffed against this expectation — not the fixture against
// itself. No provider is verified here; this only exercises the comparator.

const expectation = drRt3aEnterpriseOperation.contract as Record<string, unknown>

describe('compareExpectationToProviderSchema (binding readiness)', () => {
  it('reports a match when a provider schema mirrors the expectation exactly', () => {
    // A synthetic provider export identical in shape to the expectation.
    const providerSchema = JSON.parse(JSON.stringify(expectation)) as Record<string, unknown>

    const result = compareExpectationToProviderSchema(expectation, providerSchema)

    expect(result.matches).toBe(true)
    expect(result.drift).toEqual([])
  })

  it('reports drift when the provider drops an OperationReceipt field', () => {
    const providerSchema = JSON.parse(JSON.stringify(expectation)) as Record<string, unknown>
    const receipt = providerSchema.OperationReceipt as Record<string, unknown>
    delete receipt.effectId

    const result = compareExpectationToProviderSchema(expectation, providerSchema)

    expect(result.matches).toBe(false)
    const receiptDrift = result.drift.find(d => d.path === 'OperationReceipt')
    expect(receiptDrift?.missingInProvider).toContain('effectId')
  })

  it('reports drift when the provider adds an unexpected OperationDomain member', () => {
    const providerSchema = JSON.parse(JSON.stringify(expectation)) as Record<string, unknown>
    providerSchema.OperationDomain = [...(providerSchema.OperationDomain as string[]), 'plm']

    const result = compareExpectationToProviderSchema(expectation, providerSchema)

    expect(result.matches).toBe(false)
    const domainDrift = result.drift.find(d => d.path === 'OperationDomain')
    expect(domainDrift?.extraInProvider).toContain('plm')
  })

  it('never marks a provider verified — it only returns structural drift', () => {
    const result = compareExpectationToProviderSchema(expectation, {})
    // An empty provider schema is maximal drift, never a silent pass.
    expect(result.matches).toBe(false)
    expect('verified' in result).toBe(false)
    expect('bound' in result).toBe(false)
  })
})
