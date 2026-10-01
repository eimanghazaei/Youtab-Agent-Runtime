import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as notifications from '@/store/notifications'
import type { OAuthProvider } from '@/types/youtab'

import {
  $desktopOnboarding,
  type DesktopOnboardingState,
  type OnboardingContext,
  type OnboardingFlow,
  refreshOnboarding,
  requestDesktopOnboarding,
  retryOnboardingProviderDiscovery,
  saveOnboardingLocalEndpoint,
  startProviderOAuth,
  submitOnboardingCode
} from './onboarding'

function provider(id: string, name = id): OAuthProvider {
  return {
    cli_command: `youtab login ${id}`,
    docs_url: `https://example.com/${id}`,
    flow: 'pkce',
    id,
    name,
    status: { logged_in: false }
  }
}

function baseState(overrides: Partial<DesktopOnboardingState> = {}): DesktopOnboardingState {
  return {
    configured: false,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false,
    ...overrides
  }
}

function installApiMock(api: (request: { path: string }) => Promise<unknown>) {
  Object.defineProperty(window, 'youtabDesktop', {
    configurable: true,
    value: { api }
  })
}

it('uses the advertised native_pkce capability through the existing desktop login IPC', async () => {
  const login = vi.fn().mockResolvedValue({ ok: true, connected: true, baseUrl: 'https://api.youtab.io' })
  Object.defineProperty(window, 'youtabDesktop', {
    configurable: true,
    value: {
      api: vi.fn().mockRejectedValue(new Error('model catalog unavailable')),
      oauthLoginConnectionConfig: login
    }
  })
  const selected = { ...provider('gateway'), flow: 'native_pkce' as const, native_base_url: 'https://api.youtab.io' }
  await startProviderOAuth(selected, {
    requestGateway: async method => {
      if (method === 'reload.env') {
        return {} as never
      }
      if (method === 'setup.status') {
        return { provider_configured: true } as never
      }
      if (method === 'setup.runtime_check') {
        return { ok: true } as never
      }
      throw new Error(`unexpected gateway method: ${method}`)
    }
  })
  expect(login).toHaveBeenCalledWith('https://api.youtab.io', { nativeCapability: true, profile: null })
  expect($desktopOnboarding.get().configured).toBe(true)
})

it.each([
  ['404', () => Promise.reject(new Error('HTTP 404'))],
  ['malformed response', () => Promise.resolve({ providers: null })],
  ['temporary failure', () => Promise.reject(new Error('Connection timed out'))]
])('keeps Youtab sign-in unavailable on provider discovery %s and recovers on retry', async (_case, firstResult) => {
  const nativeLogin = vi.fn()

  const api = vi
    .fn()
    .mockImplementationOnce(firstResult)
    .mockResolvedValueOnce({
      providers: [{ ...provider('youtab'), flow: 'native_pkce', native_base_url: 'https://api.youtab.io' }]
    })

  Object.defineProperty(window, 'youtabDesktop', {
    configurable: true,
    value: { api, oauthLoginConnectionConfig: nativeLogin }
  })
  $desktopOnboarding.set(baseState({ manual: true, requested: true }))

  await refreshOnboarding({ requestGateway: vi.fn() })

  expect($desktopOnboarding.get().flow).toMatchObject({
    status: 'error',
    message: expect.stringContaining('retry')
  })
  expect($desktopOnboarding.get().mode).toBe('oauth')
  expect($desktopOnboarding.get().providers).toEqual([])
  expect(api).toHaveBeenCalledTimes(1)
  expect(nativeLogin).not.toHaveBeenCalled()

  await retryOnboardingProviderDiscovery()

  expect($desktopOnboarding.get().flow.status).toBe('idle')
  expect($desktopOnboarding.get().providers?.[0]).toMatchObject({ id: 'youtab', flow: 'native_pkce' })
  expect(api).toHaveBeenCalledTimes(2)
})

it.each(['awaiting_user', 'polling'] as const)('preserves a newer %s flow when catalog discovery fails', async status => {
  let rejectCatalog!: (error: Error) => void
  installApiMock(() => new Promise((_resolve, reject) => { rejectCatalog = reject }))
  $desktopOnboarding.set(baseState())
  const pending = retryOnboardingProviderDiscovery()
  const selected = provider('example')
  const start = { flow: status === 'polling' ? 'device_code' : 'pkce', session_id: 'fixture-session' }
  const flow = { status, provider: selected, start, code: '', copied: false } as OnboardingFlow
  $desktopOnboarding.set({ ...$desktopOnboarding.get(), flow })
  rejectCatalog(new Error('Connection timed out'))
  await pending

  expect($desktopOnboarding.get().flow).toBe(flow)
  expect($desktopOnboarding.get().flow).toMatchObject({ start: { session_id: 'fixture-session' } })
})

function emptyOpenRouterGateway(): OnboardingContext['requestGateway'] {
  return async method => {
    if (method === 'setup.status') {
      return { provider_configured: true } as never
    }

    if (method === 'setup.runtime_check') {
      return { error: 'No usable credentials found for openrouter.', ok: false, provider: 'openrouter' } as never
    }

    throw new Error(`unexpected gateway method: ${method}`)
  }
}

function keylessCustomGateway(): OnboardingContext['requestGateway'] {
  return async method => {
    if (method === 'setup.status') {
      return { provider_configured: true } as never
    }

    if (method === 'setup.runtime_check') {
      return { ok: true, provider: 'custom' } as never
    }

    throw new Error(`unexpected gateway method: ${method}`)
  }
}

function onboardingContext(requestGateway: OnboardingContext['requestGateway']): OnboardingContext {
  return { requestGateway }
}

function fallbackTimeoutGateway(): OnboardingContext['requestGateway'] {
  return async method => {
    if (method === 'setup.status' || method === 'setup.runtime_check') {
      throw new Error(`request timed out: ${method}`)
    }

    throw new Error(`unexpected gateway method: ${method}`)
  }
}

describe('refreshOnboarding', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
  })

  afterEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
    vi.restoreAllMocks()
  })

  it('refreshes OAuth providers again when onboarding was explicitly requested', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return { providers: [provider('fresh')] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    $desktopOnboarding.set(baseState({ providers: [provider('cached')] }))
    requestDesktopOnboarding('Need provider setup')

    const ready = await refreshOnboarding(onboardingContext(emptyOpenRouterGateway()))

    expect(ready).toBe(false)
    expect(api).toHaveBeenCalledTimes(1)
    expect($desktopOnboarding.get().providers?.map(p => p.id)).toEqual(['fresh'])
    expect($desktopOnboarding.get().reason).toContain('No usable credentials found for openrouter.')
    expect($desktopOnboarding.get().reason).toContain('setup.status reports configured credentials')
  })

  it('keeps cached providers when onboarding was not re-requested', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return { providers: [provider('fresh')] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    $desktopOnboarding.set(baseState({ providers: [provider('cached')] }))

    const ready = await refreshOnboarding(onboardingContext(emptyOpenRouterGateway()))

    expect(ready).toBe(false)
    expect(api).not.toHaveBeenCalled()
    expect($desktopOnboarding.get().providers?.map(p => p.id)).toEqual(['cached'])
  })

  it('does not downgrade configured=true on fallback-only readiness failures', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return { providers: [provider('fresh')] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    // Simulate a returning user: cache is set and store is configured.
    window.localStorage.setItem('youtab-desktop-onboarded-v1', '1')
    $desktopOnboarding.set(
      baseState({
        configured: true,
        providers: [provider('cached')],
        reason: null,
        requested: false
      })
    )

    const ready = await refreshOnboarding(onboardingContext(fallbackTimeoutGateway()))

    expect(ready).toBe(false)
    expect(api).not.toHaveBeenCalled()
    expect($desktopOnboarding.get().configured).toBe(true)
    expect($desktopOnboarding.get().reason).toBeNull()
    // The cache must survive the refresh — proving we didn't downgrade.
    expect(window.localStorage.getItem('youtab-desktop-onboarded-v1')).toBe('1')
  })

  it('shows a non-blocking notification when preserving configured on fallback', async () => {
    const notifySpy = vi.spyOn(notifications, 'notify')

    installApiMock(vi.fn())
    $desktopOnboarding.set(
      baseState({
        configured: true,
        providers: [provider('cached')],
        reason: null,
        requested: false
      })
    )

    await refreshOnboarding(onboardingContext(fallbackTimeoutGateway()))

    expect(notifySpy).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'runtime-not-ready',
        kind: 'error'
      })
    )
    expect($desktopOnboarding.get().configured).toBe(true)
  })

  it('enters setup when the selected OpenRouter credential is genuinely empty', async () => {
    installApiMock(vi.fn())
    window.localStorage.setItem('youtab-desktop-onboarded-v1', '1')
    $desktopOnboarding.set(
      baseState({
        configured: true,
        providers: [provider('cached')],
        reason: null,
        requested: false
      })
    )

    const ready = await refreshOnboarding(onboardingContext(emptyOpenRouterGateway()))

    expect(ready).toBe(false)
    expect($desktopOnboarding.get().configured).toBe(false)
    expect($desktopOnboarding.get().reason).toContain('No usable credentials found for openrouter.')
    expect(window.localStorage.getItem('youtab-desktop-onboarded-v1')).toBeNull()
  })

  it('keeps a keyless custom runtime out of setup', async () => {
    const api = vi.fn()

    installApiMock(api)
    $desktopOnboarding.set(baseState({ configured: false, reason: 'stale setup error', requested: true }))

    const ready = await refreshOnboarding(onboardingContext(keylessCustomGateway()))

    expect(ready).toBe(true)
    expect(api).not.toHaveBeenCalled()
    expect($desktopOnboarding.get()).toMatchObject({
      configured: true,
      reason: null,
      requested: false
    })
  })

  it('does not preserve configured when onboarding was explicitly requested', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return { providers: [provider('fresh')] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    $desktopOnboarding.set(
      baseState({
        configured: true,
        providers: [provider('cached')],
        reason: null,
        requested: true
      })
    )

    const ready = await refreshOnboarding(onboardingContext(fallbackTimeoutGateway()))

    expect(ready).toBe(false)
    // requested overrides preservation — should downgrade.
    expect($desktopOnboarding.get().configured).toBe(false)
    expect(api).toHaveBeenCalledTimes(1)
  })

  it('still surfaces onboarding when fallback failure happens before configured state', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return { providers: [provider('fresh')] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    $desktopOnboarding.set(baseState({ configured: false, providers: null, requested: true }))

    const ready = await refreshOnboarding(onboardingContext(fallbackTimeoutGateway()))

    expect(ready).toBe(false)
    expect(api).toHaveBeenCalledTimes(1)
    expect($desktopOnboarding.get().configured).toBe(false)
    expect($desktopOnboarding.get().reason).toContain('request timed out')
  })

  it('deduplicates concurrent provider refresh calls', async () => {
    let resolveProviders!: (value: { providers: OAuthProvider[] }) => void

    const providersPromise = new Promise<{ providers: OAuthProvider[] }>(resolve => {
      resolveProviders = value => {
        resolve(value)
      }
    })

    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/providers/oauth') {
        return providersPromise
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    $desktopOnboarding.set(baseState({ requested: true }))

    const first = refreshOnboarding(onboardingContext(emptyOpenRouterGateway()))
    const second = refreshOnboarding(onboardingContext(emptyOpenRouterGateway()))

    await vi.waitFor(() => expect(api).toHaveBeenCalledTimes(1))

    resolveProviders({ providers: [provider('shared')] })
    await Promise.all([first, second])

    expect($desktopOnboarding.get().providers?.map(p => p.id)).toEqual(['shared'])
  })
})

describe('OAuth onboarding', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
  })

  afterEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
    vi.restoreAllMocks()
  })

  it('clears stale readiness errors after OAuth succeeds and model confirmation is shown', async () => {
    const model = 'anthropic/claude-opus-4.8'
    const calls: { body?: unknown; path: string }[] = []

    installApiMock(async ({ body, path }: { body?: unknown; path: string }) => {
      calls.push({ body, path })

      if (path === '/api/providers/oauth/youtab/submit') {
        return { ok: true, status: 'approved' }
      }

      if (path.startsWith('/api/model/options')) {
        return {
          providers: [
            {
              name: 'Youtab Portal',
              slug: 'youtab',
              models: [model]
            }
          ]
        }
      }

      if (path.startsWith('/api/model/recommended-default?')) {
        return { provider: 'youtab', model, free_tier: false }
      }

      if (path === '/api/model/set') {
        return { ok: true, provider: 'youtab', model, gateway_tools: [] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    const requestGateway: OnboardingContext['requestGateway'] = async (method, params) => {
      if (method === 'reload.env') {
        return {} as never
      }

      if (method === 'setup.status') {
        return { provider_configured: true } as never
      }

      if (method === 'setup.runtime_check') {
        expect(params).toEqual({ provider: 'youtab' })

        return { ok: true } as never
      }

      throw new Error(`unexpected gateway method: ${method}`)
    }

    $desktopOnboarding.set(
      baseState({
        flow: {
          status: 'awaiting_user',
          provider: provider('youtab', 'Youtab Portal'),
          start: {
            auth_url: 'https://portal.example/auth',
            expires_in: 600,
            flow: 'pkce',
            session_id: 'portal-session'
          },
          code: 'fresh-code'
        },
        reason:
          'No access token found for Youtab Portal login. setup.status reports configured credentials, but runtime resolution still failed.',
        requested: true
      })
    )

    await submitOnboardingCode(onboardingContext(requestGateway))

    const state = $desktopOnboarding.get()
    expect(state.reason).toBeNull()
    expect(state.flow.status).toBe('confirming_model')

    if (state.flow.status === 'confirming_model') {
      expect(state.flow.label).toBe('Youtab Portal')
      expect(state.flow.currentModel).toBe(model)
    }

    expect(calls.some(c => c.path === '/api/model/set')).toBe(true)

    const optionsIndex = calls.findIndex(c => c.path.startsWith('/api/model/options'))
    const recommendedIndex = calls.findIndex(c => c.path.startsWith('/api/model/recommended-default'))
    const setIndex = calls.findIndex(c => c.path === '/api/model/set')

    expect(optionsIndex).toBeGreaterThanOrEqual(0)
    expect(recommendedIndex).toBeGreaterThan(optionsIndex)
    expect(setIndex).toBeGreaterThan(recommendedIndex)
  })
})

describe('saveOnboardingLocalEndpoint', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
  })

  afterEach(() => {
    window.localStorage.clear()
    $desktopOnboarding.set(baseState())
    vi.restoreAllMocks()
  })

  function readyGateway(): OnboardingContext['requestGateway'] {
    return async method => {
      if (method === 'reload.env') {
        return {} as never
      }

      if (method === 'setup.status') {
        return { provider_configured: true } as never
      }

      if (method === 'setup.runtime_check') {
        return { ok: true } as never
      }

      throw new Error(`unexpected gateway method: ${method}`)
    }
  }

  it('errors when the endpoint advertises no models (nothing to route to)', async () => {
    const calls: string[] = []
    installApiMock(async ({ path }: { path: string }) => {
      calls.push(path)

      if (path === '/api/providers/validate') {
        return { ok: true, reachable: true, message: '', models: [] }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    const result = await saveOnboardingLocalEndpoint('http://127.0.0.1:8000/v1', '', {
      requestGateway: readyGateway()
    })

    expect(result.ok).toBe(false)
    expect(result.message).toContain('no models')
    // Must not attempt to persist an assignment without a model.
    expect(calls).not.toContain('/api/model/set')
  })

  it('auto-discovers the model and persists provider=custom + base_url, then finishes', async () => {
    const calls: { body?: unknown; path: string }[] = []

    const api = vi.fn(async ({ body, path }: { body?: unknown; path: string }) => {
      calls.push({ body, path })

      if (path === '/api/providers/validate') {
        return { ok: true, reachable: true, message: '', models: ['llama-3.1-8b', 'qwen2.5-7b'] }
      }

      if (path === '/api/model/set') {
        return { ok: true, provider: 'custom', model: 'llama-3.1-8b', base_url: 'http://127.0.0.1:8000/v1' }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)
    const onCompleted = vi.fn()

    const result = await saveOnboardingLocalEndpoint('http://127.0.0.1:8000/v1', '', {
      onCompleted,
      requestGateway: readyGateway()
    })

    expect(result.ok).toBe(true)

    const assign = calls.find(c => c.path === '/api/model/set')
    expect(assign?.body).toMatchObject({
      scope: 'main',
      provider: 'custom',
      model: 'llama-3.1-8b',
      base_url: 'http://127.0.0.1:8000/v1'
    })

    expect(onCompleted).toHaveBeenCalledTimes(1)
    expect($desktopOnboarding.get().configured).toBe(true)
  })

  it('forwards the API key to the probe and persists it for auth-gated endpoints', async () => {
    const calls: { body?: unknown; path: string }[] = []

    const api = vi.fn(async ({ body, path }: { body?: unknown; path: string }) => {
      calls.push({ body, path })

      if (path === '/api/providers/validate') {
        return { ok: true, reachable: true, message: '', models: ['gpt-oss-120b'] }
      }

      if (path === '/api/model/set') {
        return { ok: true, provider: 'custom', model: 'gpt-oss-120b', base_url: 'https://text.example.com/v1' }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    installApiMock(api)

    const result = await saveOnboardingLocalEndpoint('https://text.example.com/v1', 'sk-secret', {
      requestGateway: readyGateway()
    })

    expect(result.ok).toBe(true)

    // The probe must receive the key so an auth-gated /v1/models enumerates.
    const probe = calls.find(c => c.path === '/api/providers/validate')
    expect(probe?.body).toMatchObject({
      key: 'OPENAI_BASE_URL',
      value: 'https://text.example.com/v1',
      api_key: 'sk-secret'
    })

    // And the key must be persisted alongside the endpoint for runtime auth.
    const assign = calls.find(c => c.path === '/api/model/set')
    expect(assign?.body).toMatchObject({
      scope: 'main',
      provider: 'custom',
      model: 'gpt-oss-120b',
      base_url: 'https://text.example.com/v1',
      api_key: 'sk-secret'
    })
  })

  it('reports the runtime reason when resolution still fails after saving', async () => {
    installApiMock(async ({ path }: { path: string }) => {
      if (path === '/api/providers/validate') {
        return { ok: true, reachable: true, message: '', models: ['llama-3.1-8b'] }
      }

      if (path === '/api/model/set') {
        return { ok: true }
      }

      throw new Error(`unexpected api path: ${path}`)
    })

    const failingGateway: OnboardingContext['requestGateway'] = async method => {
      if (method === 'reload.env') {
        return {} as never
      }

      if (method === 'setup.status') {
        return { provider_configured: false } as never
      }

      if (method === 'setup.runtime_check') {
        return { ok: false, error: 'No provider can serve the selected model.' } as never
      }

      throw new Error(`unexpected gateway method: ${method}`)
    }

    const result = await saveOnboardingLocalEndpoint('http://127.0.0.1:8000/v1', '', {
      requestGateway: failingGateway
    })

    expect(result.ok).toBe(false)
    expect(result.message).toContain('No provider can serve the selected model.')
    expect($desktopOnboarding.get().configured).not.toBe(true)
  })
})
