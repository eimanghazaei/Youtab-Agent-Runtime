import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { $gatewayState } from '@/store/session'

import { RuntimePluginsSettings } from './runtime-plugins-settings'

const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({ useGatewayRequest: () => ({ gateway: { request } }) }))

function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <RuntimePluginsSettings />
  </QueryClientProvider>)
}

beforeEach(() => { request.mockReset(); $gatewayState.set('open') })
afterEach(() => { cleanup(); $gatewayState.set('closed') })

it('shows bundled Runtime integrations independently of Desktop extension records', async () => {
  request.mockResolvedValue({ plugins: [{ name: 'example-web', version: '1', description: 'Web integration', source: 'bundled', status: 'not enabled' }] })
  mount()
  expect(await screen.findByText('example-web')).toBeTruthy()
  expect(request).toHaveBeenCalledWith('plugins.manage', { action: 'list' })
  expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false')
  expect(request).toHaveBeenCalledTimes(1)
})

it('only enables a plugin after an explicit user click through the existing backend contract', async () => {
  request.mockResolvedValue({ plugins: [{ key: 'web/example-web', name: 'example-web', version: '1', description: 'Web integration', source: 'bundled', status: 'not enabled' }] })
  mount()
  fireEvent.click(await screen.findByRole('switch'))
  await waitFor(() => expect(request).toHaveBeenCalledWith('plugins.manage', { action: 'toggle', name: 'web/example-web', enable: true }))
})

it('shows an actionable error instead of presenting a failed lookup as an empty catalogue', async () => {
  request.mockRejectedValue(new Error('synthetic unavailable'))
  mount()
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
})
