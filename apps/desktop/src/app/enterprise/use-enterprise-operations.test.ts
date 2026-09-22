// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { OperationManifest, ReferenceScenarioResult } from './operation-manifest'
import { runResultEffectRef, useEnterpriseOperations } from './use-enterprise-operations'

const noApproval: OperationManifest = {
  id: 'crm.create_lead',
  domain: 'crm',
  title: 'Create lead',
  description: '',
  params: [{ key: 'company', label: 'Company', type: 'string', required: true }],
  risk: 'low',
  requiresApproval: false
}

const withApproval: OperationManifest = { ...noApproval, id: 'crm.merge_accounts', requiresApproval: true }

const okReceipt: ReferenceScenarioResult = {
  simulatedEffectRef: 'ref-00000001',
  status: 'effect_complete',
  detail: 'ref',
  source: 'reference'
}

describe('useEnterpriseOperations phase machine', () => {
  it('exposes reference manifests for the domain', () => {
    const { result } = renderHook(() => useEnterpriseOperations('crm'))
    expect(result.current.manifests.length).toBeGreaterThan(0)
    expect(result.current.phase).toBe('idle')
  })

  it('preview → executing → effect_complete → reconciled for a happy path', async () => {
    const { result } = renderHook(() =>
      useEnterpriseOperations('crm', {
        execute: () => Promise.resolve(okReceipt),
        reconcile: () => Promise.resolve(true)
      })
    )

    act(() => result.current.preview())
    expect(result.current.phase).toBe('preview')

    await act(async () => {
      await result.current.runOperation(noApproval, { company: 'Acme' })
    })

    await waitFor(() => expect(result.current.phase).toBe('reconciled'))
    expect(result.current.reconciled).toBe(true)
    expect(result.current.receipt?.source).toBe('reference')
  })

  it('gates on approval, then approved → executing → reconciled after approve', async () => {
    const { result } = renderHook(() =>
      useEnterpriseOperations('crm', {
        execute: () => Promise.resolve(okReceipt),
        reconcile: () => Promise.resolve(true)
      })
    )

    await act(async () => {
      await result.current.runOperation(withApproval, {})
    })
    expect(result.current.phase).toBe('approval_required')
    expect(result.current.receipt ? runResultEffectRef(result.current.receipt) : null).toBeNull()

    await act(async () => {
      await result.current.runOperation(withApproval, {}, { approved: true })
    })
    await waitFor(() => expect(result.current.phase).toBe('reconciled'))
  })

  it('deny → terminal denied branch', async () => {
    const { result } = renderHook(() => useEnterpriseOperations('crm'))
    await act(async () => {
      await result.current.runOperation(withApproval, {})
    })
    expect(result.current.phase).toBe('approval_required')
    act(() => result.current.deny())
    expect(result.current.phase).toBe('denied')
    expect(result.current.error).toBeTruthy()
  })

  it('failed branch when the execute service throws', async () => {
    const { result } = renderHook(() =>
      useEnterpriseOperations('crm', {
        execute: () => Promise.reject(new Error('boom'))
      })
    )

    await act(async () => {
      await result.current.runOperation(noApproval, { company: 'Acme' })
    })
    expect(result.current.phase).toBe('failed')
    expect(result.current.error).toBe('boom')
  })

  it('reconcile_failed branch when reconciliation returns false', async () => {
    const { result } = renderHook(() =>
      useEnterpriseOperations('crm', {
        execute: () => Promise.resolve(okReceipt),
        reconcile: () => Promise.resolve(false)
      })
    )

    await act(async () => {
      await result.current.runOperation(noApproval, { company: 'Acme' })
    })
    await waitFor(() => expect(result.current.phase).toBe('reconcile_failed'))
    expect(result.current.error).toBeTruthy()
  })

  it('reset returns to idle', async () => {
    const { result } = renderHook(() => useEnterpriseOperations('crm'))
    act(() => result.current.preview())
    act(() => result.current.reset())
    expect(result.current.phase).toBe('idle')
    expect(result.current.receipt).toBeNull()
  })
})
