/**
 * Enterprise + Governance full-page routes — mounted purely through the
 * contribution registry, exactly as a plugin would. Each page pairs a
 * ROUTES_AREA contribution (the full-page render at its `data.path`) with a
 * SIDEBAR_NAV_AREA data contribution (the sidebar nav row that navigates to
 * it). No core AppView/AppRouteId/APP_ROUTES/shell-switch edit is involved —
 * this stays additive and low-risk.
 */

import { EnterprisePanel } from '@/app/enterprise/enterprise-panel'
import { GovernancePanel } from '@/app/governance/governance-panel'
import { type RouteContribution, ROUTES_AREA, SIDEBAR_NAV_AREA, type SidebarNavContribution } from '@/app/routes'
import { registry } from '@/contrib/registry'

const ENTERPRISE_ROUTE = '/enterprise'
const GOVERNANCE_ROUTE = '/governance'

let registered = false

/** Register the Enterprise + Governance pages and their sidebar nav rows.
 *  Idempotent: same-id re-registration replaces in place, so calling twice is
 *  harmless. */
export function registerEnterpriseGovernanceRoutes(): void {
  if (registered) {
    return
  }

  registered = true

  registry.registerMany([
    {
      id: 'enterprise',
      area: ROUTES_AREA,
      title: 'Enterprise',
      data: { path: ENTERPRISE_ROUTE } satisfies RouteContribution,
      render: () => <EnterprisePanel />
    },
    {
      id: 'enterprise',
      area: SIDEBAR_NAV_AREA,
      data: { codicon: 'organization', label: 'Enterprise', path: ENTERPRISE_ROUTE } satisfies SidebarNavContribution
    },
    {
      id: 'governance',
      area: ROUTES_AREA,
      title: 'Governance',
      data: { path: GOVERNANCE_ROUTE } satisfies RouteContribution,
      render: () => <GovernancePanel />
    },
    {
      id: 'governance',
      area: SIDEBAR_NAV_AREA,
      data: { codicon: 'law', label: 'Governance', path: GOVERNANCE_ROUTE } satisfies SidebarNavContribution
    }
  ])
}
