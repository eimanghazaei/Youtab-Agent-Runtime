// Deterministic REFERENCE manifest provider + reference execute service.
//
// This stands in for the real Runtime operation API until DR-RT-3a lands. It is
// intentionally, transparently NOT a live vendor integration:
//   - `getReferenceManifests` returns a fixed, representative set of CRM/ERP/SAP/
//     CAD manifests (no hard-coded vendor workflow, just typed descriptions).
//   - `executeReference` computes a deterministic receipt and always stamps
//     `source: 'reference'`. It honours `requiresApproval` (returns
//     `approval_required` until `approved` is true) and NEVER returns a
//     `source: 'runtime'` receipt — no fake success as a real Runtime effect.
//
// No secret, credential, or local filesystem path is read, logged, or embedded.

import type { OperationDomain, OperationManifest, ReferenceScenarioResult } from './operation-manifest'

const REFERENCE_MANIFESTS: Record<OperationDomain, OperationManifest[]> = {
  crm: [
    {
      id: 'crm.create_lead',
      domain: 'crm',
      title: 'Create lead',
      description: 'Register a new sales lead in the CRM pipeline.',
      params: [
        { key: 'company', label: 'Company', type: 'string', required: true },
        { key: 'contactEmail', label: 'Contact email', type: 'string', required: true },
        {
          key: 'stage',
          label: 'Pipeline stage',
          type: 'enum',
          required: true,
          options: ['new', 'qualified', 'proposal']
        }
      ],
      risk: 'low',
      requiresApproval: false
    },
    {
      id: 'crm.merge_accounts',
      domain: 'crm',
      title: 'Merge accounts',
      description: 'Merge two CRM accounts into a single surviving record.',
      params: [
        { key: 'survivorId', label: 'Surviving account id', type: 'string', required: true },
        { key: 'mergedId', label: 'Account to merge', type: 'string', required: true }
      ],
      risk: 'high',
      requiresApproval: true
    }
  ],
  erp: [
    {
      id: 'erp.create_purchase_order',
      domain: 'erp',
      title: 'Create purchase order',
      description: 'Raise a purchase order against an approved supplier.',
      params: [
        { key: 'supplierId', label: 'Supplier id', type: 'string', required: true },
        { key: 'amount', label: 'Amount', type: 'number', required: true },
        { key: 'expedite', label: 'Expedite', type: 'boolean', required: false }
      ],
      risk: 'medium',
      requiresApproval: true
    }
  ],
  sap: [
    {
      id: 'sap.post_goods_receipt',
      domain: 'sap',
      title: 'Post goods receipt',
      description: 'Post a goods receipt against an inbound delivery.',
      params: [
        { key: 'deliveryId', label: 'Inbound delivery', type: 'string', required: true },
        { key: 'plant', label: 'Plant', type: 'enum', required: true, options: ['1000', '2000', '3000'] }
      ],
      risk: 'medium',
      requiresApproval: true
    }
  ],
  cad: [
    {
      id: 'cad.export_drawing',
      domain: 'cad',
      title: 'Export drawing',
      description: 'Export a released CAD drawing to a distributable format.',
      params: [
        { key: 'drawingId', label: 'Drawing id', type: 'string', required: true },
        { key: 'format', label: 'Format', type: 'enum', required: true, options: ['pdf', 'dwg', 'step'] }
      ],
      risk: 'low',
      requiresApproval: false
    }
  ]
}

/** Deterministic reference manifests for a domain. Same shape Runtime will fill. */
export function getReferenceManifests(domain: OperationDomain): OperationManifest[] {
  return REFERENCE_MANIFESTS[domain] ?? []
}

/**
 * Deterministic, order-independent effect id for a manifest + params. Pure and
 * stable so reference runs are reproducible. Never includes secrets/paths — it
 * hashes only the manifest id and the operator-supplied param values.
 */
function deterministicSimulatedRef(manifestId: string, params: Record<string, unknown>): string {
  const canonical = Object.keys(params)
    .sort()
    .map(key => `${key}=${String(params[key])}`)
    .join('&')

  let hash = 0
  const material = `${manifestId}|${canonical}`

  for (let i = 0; i < material.length; i += 1) {
    hash = (hash * 31 + material.charCodeAt(i)) | 0
  }

  return `ref-${(hash >>> 0).toString(16).padStart(8, '0')}`
}

export interface ExecuteReferenceOptions {
  /** Set once the operator has approved a `requiresApproval` manifest. */
  approved: boolean
}

/**
 * Reference execute service. Deterministic; always `source: 'reference'`.
 *
 * - If the manifest requires approval and it has not been approved, returns an
 *   `approval_required` result with a null `simulatedEffectRef` (no effect is
 *   produced).
 * - Otherwise returns an `effect_complete` result with a deterministic
 *   `ref-…` `simulatedEffectRef`.
 *
 * It never contacts a vendor or Runtime and always returns
 * `source: 'reference'` — it can NEVER produce a canonical `OperationReceipt`.
 */
export function executeReference(
  manifest: OperationManifest,
  params: Record<string, unknown>,
  options: ExecuteReferenceOptions
): Promise<ReferenceScenarioResult> {
  if (manifest.requiresApproval && !options.approved) {
    return Promise.resolve({
      simulatedEffectRef: null,
      status: 'approval_required',
      detail: 'Reference operation requires operator approval before it will run.',
      source: 'reference'
    })
  }

  return Promise.resolve({
    simulatedEffectRef: deterministicSimulatedRef(manifest.id, params),
    status: 'effect_complete',
    detail: `Reference effect for "${manifest.title}" (deterministic simulation; not a live Runtime effect).`,
    source: 'reference'
  })
}
