import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AccountSyncSettings } from './account-sync-settings'

vi.mock('@/i18n', () => ({ useI18n: () => ({ locale: 'en' }) }))
vi.mock('@/themes/context', () => ({ useTheme: () => ({ mode: 'dark', setMode: vi.fn() }) }))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))
vi.mock('../open-session', () => ({ openSession: vi.fn() }))
vi.mock('@/store/profile', async () => ({ $activeGatewayProfile: (await import('nanostores')).atom('default') }))

const original = Object.getOwnPropertyDescriptor(window, 'youtabDesktop')
afterEach(() => {
  cleanup()

  if (original) {Object.defineProperty(window, 'youtabDesktop', original)}
  else {Reflect.deleteProperty(window, 'youtabDesktop')}
})

function bridge(enabled = false) {
  let changed = () => {}
  const state = { enabled, pending: 0, sharedSessionIds: [], chats: [] }

  const accountSync = {
    status: vi.fn().mockResolvedValue(state),
    consent: vi.fn().mockResolvedValue({ ...state, enabled: true }),
    run: vi.fn().mockResolvedValue(state), share: vi.fn(), remove: vi.fn(),
    onSessionChanged: (listener: () => void) => { changed = listener;

 return () => {} }
  }

  Object.defineProperty(window, 'youtabDesktop', { configurable: true, value: {
    accountSync, api: vi.fn().mockResolvedValue({ sessions: [{ id: 'synthetic', title: 'Local chat' }] })
  } })

  return { accountSync, changed: () => changed() }
}

describe('account sync settings', () => {
  it('does not upload on opening settings and requires consent and a selected chat', async () => {
    const { accountSync } = bridge()
    render(<AccountSyncSettings />)
    const toggle = await screen.findByRole('switch')
    expect(accountSync.share).not.toHaveBeenCalled()
    expect(accountSync.run).not.toHaveBeenCalled()
    fireEvent.click(toggle)
    await waitFor(() => expect(accountSync.consent).toHaveBeenCalledWith('default', true))
    expect((await screen.findByRole('button', { name: 'Share chat' })).hasAttribute('disabled')).toBe(true)
  })

  it('clears old account data and rejects a stale sync acknowledgement after logout', async () => {
    const { accountSync, changed } = bridge(true)
    const oldState = { enabled: true, pending: 0, sharedSessionIds: [], chats: [{ id: 'old', title: 'Old account chat', messages: [] }] }
    accountSync.status.mockResolvedValueOnce(oldState)

    let finish: (value: typeof oldState) => void = () => {}
    accountSync.run.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    render(<AccountSyncSettings />)
    await screen.findByText('Old account chat')
    fireEvent.click(screen.getByRole('button', { name: 'Sync now' }))
    accountSync.status.mockRejectedValue(new Error('SIGNED_OUT'))
    await act(async () => { changed(); finish(oldState) })
    await screen.findByText(/Account sync is unavailable/)
    expect(screen.queryByText('Old account chat')).toBeNull()
  })
})
