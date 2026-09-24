// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { EnterpriseOperationSurface } from './enterprise-operation-surface'
import type { OperationManifest } from './operation-manifest'
import { getReferenceManifests } from './reference-operations'

afterEach(cleanup)

const crm = getReferenceManifests('crm')
const gated = crm.find(m => m.id === 'crm.merge_accounts') as OperationManifest

describe('EnterpriseOperationSurface', () => {
  it('always shows the source: reference indicator, connection, workspace and DR-RT-3a note', () => {
    render(<EnterpriseOperationSurface manifests={crm} workspace="acme-org" />)
    expect(screen.getByTestId('source-indicator').textContent).toContain('source: reference')
    expect(screen.getByTestId('connection-status').textContent).toContain('reference')
    expect(screen.getByTestId('workspace-indicator').textContent).toContain('acme-org')
    expect(screen.getByTestId('runtime-pending-note').textContent).toContain('DR-RT-3a')
  })

  it('renders risk, params and preview', () => {
    render(<EnterpriseOperationSurface manifests={crm} />)
    expect(screen.getByTestId('risk-badge')).toBeTruthy()
    // First manifest (create_lead) params are rendered with associated labels.
    expect(screen.getByLabelText(/Company/)).toBeTruthy()

    fireEvent.click(screen.getByTestId('preview-button'))
    expect(screen.getByTestId('preview-panel')).toBeTruthy()
  })

  it('runs a no-approval operation to a reconciled receipt with source reference', async () => {
    render(<EnterpriseOperationSurface manifests={crm} />)
    fireEvent.click(screen.getByTestId('execute-button'))
    await waitFor(() => expect(screen.getByTestId('phase-status').textContent).toContain('reconciled'))
    expect(screen.getByTestId('receipt-source').textContent).toContain('reference')
    expect(screen.getByTestId('receipt-effect-id').textContent).toMatch(/^ref-/)
    expect(screen.getByTestId('reconciliation-ok')).toBeTruthy()
  })

  it('surfaces approval controls and approve completes the operation', async () => {
    render(<EnterpriseOperationSurface manifests={[gated]} />)
    fireEvent.click(screen.getByTestId('execute-button'))
    await waitFor(() => expect(screen.getByTestId('approval-panel')).toBeTruthy())
    expect(screen.getByTestId('approval-required-badge')).toBeTruthy()

    fireEvent.click(screen.getByTestId('approve-button'))
    await waitFor(() => expect(screen.getByTestId('phase-status').textContent).toContain('reconciled'))
  })

  it('deny yields an actionable failure state', async () => {
    render(<EnterpriseOperationSurface manifests={[gated]} />)
    fireEvent.click(screen.getByTestId('execute-button'))
    await waitFor(() => expect(screen.getByTestId('deny-button')).toBeTruthy())
    fireEvent.click(screen.getByTestId('deny-button'))
    expect(screen.getByTestId('failure-state')).toBeTruthy()
    expect(screen.getByTestId('failure-retry')).toBeTruthy()
  })

  it('renders a failed state when the injected execute service throws', async () => {
    render(<EnterpriseOperationSurface execute={() => Promise.reject(new Error('svc down'))} manifests={crm} />)
    fireEvent.click(screen.getByTestId('execute-button'))
    await waitFor(() => expect(screen.getByTestId('failure-state').textContent).toContain('svc down'))
  })
})
