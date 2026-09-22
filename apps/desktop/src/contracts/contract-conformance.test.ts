// Frontend contract-conformance tests.
//
// WHAT THESE PROVE: the published sibling contracts (fixtures/*.json) are
// well-formed, are labelled PENDING_INTEGRATION, and the FRONTEND's own typed
// consumers (@/app/enterprise/operation-manifest, @/app/governance/governance-model)
// are shape-compatible with the 3a/3b contracts — union members and field names
// match exactly, so drift on either side fails the build.
//
// WHAT THESE DO NOT PROVE: nothing here marks any sibling backend (Gateway /
// Runtime) VERIFIED. A backend is real only when its exact SHA is integrated and
// independently reviewed. The final describe block asserts that invariant.

import { describe, expect, it } from 'vitest'

import type {
  OperationDomain,
  OperationManifest,
  OperationParam,
  OperationPhase,
  OperationReceipt,
  RiskLevel
} from '@/app/enterprise/operation-manifest'
import type { GovernancePhase, GovernanceReceipt, GovernanceRequest } from '@/app/governance/governance-model'

import { CONTRACTS, SUPPORTED_FILE_CAPABILITY_SCHEMA } from './contract-manifest'
import drGw1 from './fixtures/dr-gw-1.file-ingress.json'
import drGw3 from './fixtures/dr-gw-3.canonical-workspace.json'
import drRt3a from './fixtures/dr-rt-3a.enterprise-operation.json'
import drRt3b from './fixtures/dr-rt-3b.governance.json'

const EXPECTED_IDS = ['DR-GW-1', 'DR-GW-2', 'DR-GW-3', 'DR-RT-1', 'DR-RT-3a', 'DR-RT-3b'] as const

// Compile-time exhaustive key/member sets. A `Record<Union, true>` (or
// `Record<keyof Interface, true>`) fails to type-check if the frontend type
// gains or loses a member — that is the compile-time-style drift guard. Its keys
// are then compared to the fixture at runtime.
const OPERATION_DOMAIN: Record<OperationDomain, true> = { crm: true, erp: true, sap: true, cad: true }
const RISK_LEVEL: Record<RiskLevel, true> = { low: true, medium: true, high: true }

const OPERATION_PARAM_KEYS: Record<keyof OperationParam, true> = {
  key: true,
  label: true,
  type: true,
  required: true,
  options: true
}

const OPERATION_PARAM_TYPE: Record<OperationParam['type'], true> = {
  string: true,
  number: true,
  boolean: true,
  enum: true
}

const OPERATION_MANIFEST_KEYS: Record<keyof OperationManifest, true> = {
  id: true,
  domain: true,
  title: true,
  description: true,
  params: true,
  risk: true,
  requiresApproval: true
}

const OPERATION_PHASE: Record<OperationPhase, true> = {
  idle: true,
  preview: true,
  approval_required: true,
  approved: true,
  executing: true,
  effect_complete: true,
  reconciled: true,
  reconcile_failed: true,
  denied: true,
  failed: true
}

const OPERATION_RECEIPT_KEYS: Record<keyof OperationReceipt, true> = {
  effectId: true,
  status: true,
  detail: true,
  source: true
}

const GOVERNANCE_PHASE: Record<GovernancePhase, true> = {
  approval_required: true,
  approved: true,
  denied: true,
  expired: true,
  payload_modified: true,
  replay_rejected: true,
  revoked_delegation: true,
  workspace_mismatch: true,
  effect_complete: true,
  reconcile_succeeded: true,
  reconcile_failed: true
}

const GOVERNANCE_REQUEST_KEYS: Record<keyof GovernanceRequest, true> = {
  id: true,
  action: true,
  workspaceId: true,
  payloadHash: true,
  requiresApproval: true,
  delegationId: true
}

const GOVERNANCE_RECEIPT_KEYS: Record<keyof GovernanceReceipt, true> = {
  requestId: true,
  phase: true,
  effectId: true,
  receiptId: true,
  source: true,
  detail: true
}

/** Assert two collections are equal as sets (order-independent). */
function expectSameSet(actual: readonly string[], expected: readonly string[]): void {
  expect([...actual].sort()).toEqual([...expected].sort())
}

describe('contract fixtures are well-formed and PENDING_INTEGRATION', () => {
  it('exposes exactly the six published contracts', () => {
    expectSameSet(Object.keys(CONTRACTS), EXPECTED_IDS)
  })

  it.each(EXPECTED_IDS)('contract %s is well-formed and pending integration', id => {
    const entry = CONTRACTS[id]

    expect(entry).toBeDefined()
    expect(entry.id).toBe(id)
    expect(typeof entry.title).toBe('string')
    expect(entry.title.length).toBeGreaterThan(0)
    expect(entry.ownerSession === 'Gateway' || entry.ownerSession === 'Runtime').toBe(true)
    expect(entry.status).toBe('PENDING_INTEGRATION')

    const fixture = entry.fixture as Record<string, unknown>

    expect(fixture.status).toBe('PENDING_INTEGRATION')
    expect(typeof fixture.contractLabel).toBe('string')
    expect(String(fixture.contractLabel)).toContain('CONTRACT')
  })

  it('binds the expected owner session to each contract', () => {
    expect(CONTRACTS['DR-GW-1'].ownerSession).toBe('Gateway')
    expect(CONTRACTS['DR-GW-2'].ownerSession).toBe('Gateway')
    expect(CONTRACTS['DR-GW-3'].ownerSession).toBe('Gateway')
    expect(CONTRACTS['DR-RT-1'].ownerSession).toBe('Runtime')
    expect(CONTRACTS['DR-RT-3a'].ownerSession).toBe('Runtime')
    expect(CONTRACTS['DR-RT-3b'].ownerSession).toBe('Runtime')
  })
})

describe('Gateway file-capability schema is the supported version', () => {
  it('re-exports schema 1 and matches the canonical-workspace contract', () => {
    expect(SUPPORTED_FILE_CAPABILITY_SCHEMA).toBe(1)
    expect(drGw3.contract.supportedFileCapabilitySchema).toBe(SUPPORTED_FILE_CAPABILITY_SCHEMA)
    expect(drGw3.contract.surface.file_capability_schema).toBe(SUPPORTED_FILE_CAPABILITY_SCHEMA)
  })

  it('publishes the file-ingress scan states and error codes', () => {
    expect(drGw1.contract.serverIssuedField).toBe('file_id')
    expect(drGw1.contract.scanStates).toEqual(['scanning', 'clean', 'quarantined', 'rejected'])
    expect(drGw1.contract.errorCodes).toContain('workspace_denied')
  })
})

describe('DR-RT-3a manifest fixture matches the frontend operation types exactly', () => {
  const c = drRt3a.contract

  it('OperationDomain union members match', () => {
    expectSameSet(c.OperationDomain, Object.keys(OPERATION_DOMAIN))
  })

  it('RiskLevel union members match', () => {
    expectSameSet(c.RiskLevel, Object.keys(RISK_LEVEL))
  })

  it('OperationParam field names and type union match', () => {
    expectSameSet(Object.keys(c.OperationParam), Object.keys(OPERATION_PARAM_KEYS))
    expectSameSet(c.OperationParam.type, Object.keys(OPERATION_PARAM_TYPE))
  })

  it('OperationManifest field names match', () => {
    expectSameSet(Object.keys(c.OperationManifest), Object.keys(OPERATION_MANIFEST_KEYS))
  })

  it('OperationPhase union members match', () => {
    expectSameSet(c.OperationPhase, Object.keys(OPERATION_PHASE))
  })

  it('OperationReceipt field names and source union match', () => {
    expectSameSet(Object.keys(c.OperationReceipt), Object.keys(OPERATION_RECEIPT_KEYS))
    expectSameSet(c.OperationReceipt.source, ['reference', 'runtime'])
  })

  it('is shape-compatible with a constructed frontend OperationReceipt', () => {
    // Compile-time proof: this object must satisfy the real frontend type.
    const referenceReceipt: OperationReceipt = {
      effectId: null,
      status: 'preview',
      detail: 'reference receipt — no runtime effect',
      source: 'reference'
    }

    expect(referenceReceipt.effectId).toBeNull()
    expect(referenceReceipt.source).toBe('reference')
  })
})

describe('DR-RT-3b governance fixture matches the frontend governance types exactly', () => {
  const c = drRt3b.contract

  it('GovernancePhase union members match', () => {
    expectSameSet(c.GovernancePhase, Object.keys(GOVERNANCE_PHASE))
  })

  it('GovernanceRequest field names match', () => {
    expectSameSet(Object.keys(c.GovernanceRequest), Object.keys(GOVERNANCE_REQUEST_KEYS))
  })

  it('GovernanceReceipt field names and source union match', () => {
    expectSameSet(Object.keys(c.GovernanceReceipt), Object.keys(GOVERNANCE_RECEIPT_KEYS))
    expectSameSet(c.GovernanceReceipt.source, ['reference', 'runtime'])
  })

  it('is shape-compatible with constructed frontend governance values', () => {
    // Compile-time proof against the real frontend types.
    const request: GovernanceRequest = {
      id: 'req-1',
      action: 'crm.update',
      workspaceId: 'ws-1',
      payloadHash: 'sha256:deadbeef',
      requiresApproval: true,
      delegationId: null
    }

    const receipt: GovernanceReceipt = {
      requestId: request.id,
      phase: 'approval_required',
      effectId: null,
      receiptId: null,
      source: 'reference',
      detail: null
    }

    expect(receipt.effectId).toBeNull()
    expect(receipt.receiptId).toBeNull()
    expect(receipt.source).toBe('reference')
  })
})

describe('conformance does NOT imply any backend is verified', () => {
  it('every contract stays PENDING_INTEGRATION — no fixture is flagged verified', () => {
    for (const id of EXPECTED_IDS) {
      expect(CONTRACTS[id].status).toBe('PENDING_INTEGRATION')
      const fixture = CONTRACTS[id].fixture as Record<string, unknown>
      expect(fixture.status).toBe('PENDING_INTEGRATION')
      // No fixture may carry any "verified" signal.
      expect(JSON.stringify(fixture).toLowerCase()).not.toContain('"verified"')
    }
  })

  it('the ContractStatus type has no VERIFIED member (only PENDING_INTEGRATION is representable)', () => {
    // If a VERIFIED member were ever added to ContractStatus, this exhaustive
    // record would need a new key and fail to type-check.
    const statuses: Record<(typeof CONTRACTS)[string]['status'], true> = {
      PENDING_INTEGRATION: true
    }

    expect(Object.keys(statuses)).toEqual(['PENDING_INTEGRATION'])
  })
})
