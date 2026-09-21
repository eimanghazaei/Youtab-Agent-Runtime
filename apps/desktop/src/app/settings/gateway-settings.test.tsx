import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ProfileInfo } from '@/types/youtab'

// Create the atom the mock exposes inside vi.hoisted so it exists before the
// (hoisted) static import of GatewaySettings runs its '@/store/profile' mock
// factory — the factory dereferences `profiles` at eval time.
const { getConnectionConfig, profiles } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores
  return { getConnectionConfig: vi.fn(), profiles: atom<ProfileInfo[]>([]) }
})

vi.mock('@/store/profile', () => ({
  $profiles: profiles,
  refreshActiveProfile: vi.fn()
}))

// Static import so the heavy GatewaySettings module graph is transformed at
// collection, not lazily inside the timed test body (see skills/index.test.tsx).
// Keeps the test to render+assert under the official (unchanged) 15s timeout.
import { GatewaySettings } from './gateway-settings'

const localConnection = {
  cloudOrg: '',
  envOverride: false,
  mode: 'local',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

beforeEach(() => {
  profiles.set([
    {
      has_env: false,
      is_default: true,
      model: null,
      name: 'default',
      path: '/tmp/youtab',
      provider: null,
      skill_count: 0
    },
    {
      has_env: false,
      is_default: false,
      model: null,
      name: 'work',
      path: '/tmp/youtab/profiles/work',
      provider: null,
      skill_count: 0
    }
  ])
  getConnectionConfig.mockResolvedValue(localConnection)
  Object.defineProperty(window, 'youtabDesktop', {
    configurable: true,
    value: { getConnectionConfig }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GatewaySettings', () => {
  it('labels local mode as default inheritance for a named profile', async () => {
    render(<GatewaySettings />)
    expect(await screen.findByText('Local gateway')).toBeTruthy()
    expect(
      screen.getByText('Start a private Youtab backend on localhost. This is the default and works offline.')
    ).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'work' }))

    await waitFor(() => expect(getConnectionConfig).toHaveBeenLastCalledWith('work'))
    expect(await screen.findByText('Use default gateway')).toBeTruthy()
    expect(screen.getByText("Remove this profile's override and use the default connection.")).toBeTruthy()
    expect(
      screen.queryByText('Start a private Youtab backend on localhost. This is the default and works offline.')
    ).toBeNull()
  })
})
