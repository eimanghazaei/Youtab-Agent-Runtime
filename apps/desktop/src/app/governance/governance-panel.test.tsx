import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { GovernanceRequest } from './governance-model'
import { GovernancePanel } from './governance-panel'
import type { ReferenceGovernanceConfig } from './reference-governance'

const CONFIG: ReferenceGovernanceConfig = {
  activeWorkspaceId: 'ws-canonical',
  revokedDelegationIds: new Set(['del-revoked']),
  ttlTicks: 3
}

function req(over: Partial<GovernanceRequest> = {}): GovernanceRequest {
  return {
    id: over.id ?? 'panel-req',
    action: over.action ?? 'workspace.file.write',
    workspaceId: over.workspaceId ?? 'ws-canonical',
    payloadHash: over.payloadHash ?? 'sha256:panel',
    requiresApproval: over.requiresApproval ?? true,
    delegationId: over.delegationId === undefined ? 'del-1' : over.delegationId
  }
}

function renderPanel(request: GovernanceRequest, config = CONFIG) {
  return render(<GovernancePanel config={config} request={request} />)
}

function phaseAttr() {
  return screen.getByTestId('governance-phase').getAttribute('data-phase')
}

function click(name: RegExp) {
  act(() => {
    fireEvent.click(screen.getByRole('button', { name }))
  })
}

describe('GovernancePanel', () => {
  afterEach(() => cleanup())

  it('always shows the source: reference indicator and the DR-RT-3b pending note', () => {
    renderPanel(req())
    expect(screen.getByTestId('governance-source-indicator').textContent).toContain('source: reference')
    expect(screen.getByText(/pending \(DR-RT-3b\)/i)).toBeDefined()
  })

  it('renders approval_required then approved → effect_complete → reconcile', () => {
    renderPanel(req({ id: 'flow-2' }))
    click(/submit request/i)
    expect(phaseAttr()).toBe('approval_required')
    click(/approve/i)
    expect(phaseAttr()).toBe('approved')
    click(/run effect/i)
    expect(phaseAttr()).toBe('effect_complete')
    click(/reconcile/i)
    expect(['reconcile_succeeded', 'reconcile_failed']).toContain(phaseAttr())
  })

  it('renders denied', () => {
    renderPanel(req({ id: 'deny-me' }))
    click(/submit request/i)
    click(/deny/i)
    expect(phaseAttr()).toBe('denied')
    expect(screen.getByTestId('governance-phase-label').textContent).toBe('Denied')
  })

  it('renders replay_rejected on a second submit', () => {
    renderPanel(req({ id: 'replay-me' }))
    click(/submit request/i)
    click(/submit request/i)
    expect(phaseAttr()).toBe('replay_rejected')
  })

  it('renders revoked_delegation', () => {
    renderPanel(req({ id: 'rev-me', delegationId: 'del-revoked' }))
    click(/submit request/i)
    expect(phaseAttr()).toBe('revoked_delegation')
  })

  it('renders workspace_mismatch', () => {
    renderPanel(req({ id: 'ws-me', workspaceId: 'ws-other' }))
    click(/submit request/i)
    expect(phaseAttr()).toBe('workspace_mismatch')
  })

  it('never displays a fabricated runtime effectId/receiptId for reference receipts', () => {
    renderPanel(req({ id: 'truthful' }))
    click(/submit request/i)
    click(/approve/i)
    click(/run effect/i)
    const region = screen.getByTestId('governance-phase')
    expect(region.textContent).toContain('source: reference')
    expect(region.textContent).toContain('effectId: —')
    expect(region.textContent).toContain('receiptId: —')
  })

  it('disables Approve/Deny until a request requiring approval is pending', () => {
    renderPanel(req({ id: 'gating' }))
    expect((screen.getByRole('button', { name: /approve/i }) as HTMLButtonElement).disabled).toBe(true)
    click(/submit request/i)
    expect((screen.getByRole('button', { name: /approve/i }) as HTMLButtonElement).disabled).toBe(false)
  })
})
