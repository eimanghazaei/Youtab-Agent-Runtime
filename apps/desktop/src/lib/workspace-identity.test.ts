import { describe, expect, it } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { resolveCanonicalWorkspaceId, workspaceIdMatches } from './workspace-identity'

describe('canonical workspace identity (fail-closed, no client substitution)', () => {
  it('never substitutes the gateway profile as the workspace id', () => {
    $activeGatewayProfile.set('my-profile')
    expect(resolveCanonicalWorkspaceId()).toBeNull()
    $activeGatewayProfile.set('another-profile')
    expect(resolveCanonicalWorkspaceId()).toBeNull()
  })

  it('a profile change is NOT treated as a workspace authority change', () => {
    $activeGatewayProfile.set('p1')
    const a = resolveCanonicalWorkspaceId()
    $activeGatewayProfile.set('p2')
    const b = resolveCanonicalWorkspaceId()
    expect(a).toBeNull()
    expect(b).toBeNull()
  })

  it('rejects any workspace match fail-closed while no authority exists', () => {
    expect(workspaceIdMatches('ws-1')).toBe(false)
    expect(workspaceIdMatches('')).toBe(false)
    expect(workspaceIdMatches(null)).toBe(false)
    expect(workspaceIdMatches(undefined)).toBe(false)
  })
})
