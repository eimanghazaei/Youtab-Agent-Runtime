import { isValidElement } from 'react'
import { describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { registerEnterpriseGovernanceRoutes } from './enterprise-governance-routes'
import { contributedRoutes, SIDEBAR_NAV_AREA, type SidebarNavContribution } from './routes'

describe('enterprise + governance routes', () => {
  it('registers both full-page routes with working render fns', () => {
    registerEnterpriseGovernanceRoutes()

    const routes = contributedRoutes()
    const enterprise = routes.find(r => r.path === '/enterprise')
    const governance = routes.find(r => r.path === '/governance')

    expect(enterprise).toBeDefined()
    expect(enterprise?.title).toBe('Enterprise')
    expect(isValidElement(enterprise?.render())).toBe(true)

    expect(governance).toBeDefined()
    expect(governance?.title).toBe('Governance')
    expect(isValidElement(governance?.render())).toBe(true)
  })

  it('registers both sidebar nav rows with the right paths and labels', () => {
    registerEnterpriseGovernanceRoutes()

    const nav = registry.getArea(SIDEBAR_NAV_AREA).map(c => c.data as SidebarNavContribution)

    const enterprise = nav.find(d => d.path === '/enterprise')
    const governance = nav.find(d => d.path === '/governance')

    expect(enterprise).toMatchObject({ label: 'Enterprise', path: '/enterprise' })
    expect(enterprise?.codicon).toBeTruthy()

    expect(governance).toMatchObject({ label: 'Governance', path: '/governance' })
    expect(governance?.codicon).toBeTruthy()
  })
})
