// Enterprise panel container.
//
// Lists the four enterprise domains (CRM/ERP/SAP/CAD) and renders the generic
// operation surface for the selected one, backed by the deterministic reference
// provider. Not wired into global nav (that is not trivially additive here); the
// Master mounts it — see the mount-point note in the task return.

import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'

import { EnterpriseOperationSurface } from './enterprise-operation-surface'
import type { OperationDomain } from './operation-manifest'
import { getReferenceManifests } from './reference-operations'

const DOMAINS: { id: OperationDomain; label: string }[] = [
  { id: 'crm', label: 'CRM' },
  { id: 'erp', label: 'ERP' },
  { id: 'sap', label: 'SAP' },
  { id: 'cad', label: 'CAD' }
]

export interface EnterprisePanelProps {
  /** Selected workspace/org from session state, else the reference workspace. */
  workspace?: string
}

export function EnterprisePanel({ workspace = 'reference' }: EnterprisePanelProps) {
  const [domain, setDomain] = useState<OperationDomain>('crm')
  const manifests = useMemo(() => getReferenceManifests(domain), [domain])

  return (
    <div className="flex flex-col gap-3 p-2" data-testid="enterprise-panel">
      <nav aria-label="Enterprise domains" className="flex flex-wrap gap-1">
        {DOMAINS.map(entry => (
          <Button
            aria-pressed={entry.id === domain}
            data-testid={`domain-tab-${entry.id}`}
            key={entry.id}
            onClick={() => setDomain(entry.id)}
            size="sm"
            type="button"
            variant={entry.id === domain ? 'default' : 'secondary'}
          >
            {entry.label}
          </Button>
        ))}
      </nav>
      <EnterpriseOperationSurface key={domain} manifests={manifests} workspace={workspace} />
    </div>
  )
}
