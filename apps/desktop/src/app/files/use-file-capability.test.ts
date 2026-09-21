import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $gatewayState } from '@/store/session'
import type { StatusResponse } from '@/types/youtab'

import { useFileCapability } from './use-file-capability'

const getStatus = vi.hoisted(() => vi.fn())
vi.mock(import('@/youtab'), async importOriginal => ({ ...(await importOriginal()), getStatus: () => getStatus() }))

function status(features?: Record<string, unknown>): StatusResponse {
  return { gateway_running: true, ...(features ? { features } : {}) } as unknown as StatusResponse
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children)
}

describe('useFileCapability — real runtime signal, not a static flag', () => {
  beforeEach(() => {
    getStatus.mockReset()
    $gatewayState.set('idle')
  })

  afterEach(() => {
    $gatewayState.set('idle')
  })

  it('is loading while the gateway is connecting (idle)', () => {
    $gatewayState.set('idle')
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    expect(result.current.state).toBe('loading')
    expect(getStatus).not.toHaveBeenCalled()
  })

  it('is unavailable (not a probe) when the gateway is not connected', () => {
    $gatewayState.set('closed')
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    expect(result.current.state).toBe('unavailable')
    expect(result.current.reason).toContain('not connected')
    expect(getStatus).not.toHaveBeenCalled()
  })

  it('is unavailable (from the REAL status response) when the gateway does not advertise the capability', async () => {
    $gatewayState.set('open')
    getStatus.mockResolvedValue(status()) // no `features` → not advertised
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.state).toBe('unavailable'))
    expect(getStatus).toHaveBeenCalled()
    expect(result.current.reason).toContain('does not advertise')
  })

  it('is available ONLY when the live status advertises file_ingress + workspace_authority', async () => {
    $gatewayState.set('open')
    getStatus.mockResolvedValue(status({ file_ingress: true, workspace_authority: true }))
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.state).toBe('available'))
    expect(result.current.reason).toBeNull()
  })

  it('is unavailable when only one of the two capability flags is advertised', async () => {
    $gatewayState.set('open')
    getStatus.mockResolvedValue(status({ file_ingress: true }))
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.state).toBe('unavailable'))
  })

  it('is error (truthful) when the real status probe fails', async () => {
    $gatewayState.set('open')
    getStatus.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useFileCapability(), { wrapper: wrapper() })
    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(result.current.reason).toContain('gateway status')
  })
})
