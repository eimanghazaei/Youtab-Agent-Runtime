// Sibling-contract manifest — the machine-readable index of the exact contracts
// the sibling Gateway / Runtime sessions must satisfy.
//
// TRUTHFULNESS RULE (hard): every contract here is a DRAFT_CONSUMER_EXPECTATION
// with `status: 'UNBOUND'` and STAYS that way. This module cannot flag any
// fixture VERIFIED or BOUND — a sibling backend becomes real only when its exact
// SHA is integrated and independently reviewed, which is out of scope for this
// Frontend-only conformance surface. No signature/verification is ever claimed.
// Loading/parsing a fixture proves nothing about a backend; it only lets the
// Frontend prove its own consumers are shape-compatible with the published shape.

import drGw1FileIngress from './fixtures/dr-gw-1.file-ingress.json'
import drGw2RefTextRevalidation from './fixtures/dr-gw-2.reftext-revalidation.json'
import drGw3CanonicalWorkspace from './fixtures/dr-gw-3.canonical-workspace.json'
import drRt1FolderGrantBridge from './fixtures/dr-rt-1.folder-grant-bridge.json'
import drRt3aEnterpriseOperation from './fixtures/dr-rt-3a.enterprise-operation.json'
import drRt3bGovernance from './fixtures/dr-rt-3b.governance.json'

/**
 * The only status a contract fixture may carry in this Frontend-only surface.
 * There is deliberately no `VERIFIED`/`BOUND` member: nothing here can mark a
 * sibling backend verified. `UNBOUND` = not yet bound to a real provider SHA.
 */
export type ContractStatus = 'UNBOUND'

/**
 * Every fixture is a Frontend CONSUMER EXPECTATION, never a canonical provider
 * contract. It carries no authority and no signature/verification claim.
 */
export type ContractClassification = 'DRAFT_CONSUMER_EXPECTATION'

/** Which sibling session owns satisfying the contract at its exact SHA. */
export type ContractOwnerSession = 'Gateway' | 'Runtime'

/** The published, versioned file-capability schema the Frontend supports. */
export const SUPPORTED_FILE_CAPABILITY_SCHEMA = 1 as const

/** A single published contract entry. `fixture` is the raw parsed JSON. */
export interface ContractEntry {
  id: string
  title: string
  /** Always `DRAFT_CONSUMER_EXPECTATION` — a Frontend expectation, not a contract. */
  classification: ContractClassification
  /** Always `UNBOUND` until a real provider SHA is integrated + reviewed. */
  status: ContractStatus
  ownerSession: ContractOwnerSession
  /** The repo expected to export the real schema (documentation only). */
  expectedProviderRepo: string
  /** The consumer-expectation schema version. */
  schemaVersion: number
  /** The exact provider SHA once bound — `null` while UNBOUND. */
  pendingSiblingSha: string | null
  /** No cryptographic signature/verification is claimed here — always `null`. */
  signature: null
  fixture: unknown
}

/** Shape every fixture JSON shares — used only to read the labelling fields. */
interface RawFixture {
  id: string
  title: string
  status: string
  classification?: string
  ownerSession: string
  expectedProviderRepo?: string
  schemaVersion?: number
  pendingSiblingSha?: string | null
}

function toEntry(fixture: RawFixture): ContractEntry {
  // status + classification + signature are pinned locally — the JSON value is
  // read for documentation but never trusted to upgrade a contract past UNBOUND
  // / DRAFT_CONSUMER_EXPECTATION or to assert any verification.
  return {
    id: fixture.id,
    title: fixture.title,
    classification: 'DRAFT_CONSUMER_EXPECTATION',
    status: 'UNBOUND',
    ownerSession: fixture.ownerSession as ContractOwnerSession,
    expectedProviderRepo: fixture.expectedProviderRepo ?? 'eimanghazaei/Youtab-Agent-Runtime',
    schemaVersion: fixture.schemaVersion ?? 1,
    pendingSiblingSha: fixture.pendingSiblingSha ?? null,
    signature: null,
    fixture
  }
}

/**
 * The typed contract index, keyed by contract id. Every entry is
 * `status: 'UNBOUND'` / `classification: 'DRAFT_CONSUMER_EXPECTATION'` by construction.
 */
export const CONTRACTS: Record<string, ContractEntry> = {
  'DR-GW-1': toEntry(drGw1FileIngress as RawFixture),
  'DR-GW-2': toEntry(drGw2RefTextRevalidation as RawFixture),
  'DR-GW-3': toEntry(drGw3CanonicalWorkspace as RawFixture),
  'DR-RT-1': toEntry(drRt1FolderGrantBridge as RawFixture),
  'DR-RT-3a': toEntry(drRt3aEnterpriseOperation as RawFixture),
  'DR-RT-3b': toEntry(drRt3bGovernance as RawFixture)
}
