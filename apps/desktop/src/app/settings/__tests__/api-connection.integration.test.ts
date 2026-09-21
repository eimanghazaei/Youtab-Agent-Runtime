// Executable integration proof for the API-connection (custom-endpoints) path.
//
// SCOPE OF TRUTH: every Youtab consumer and handler under test is REAL — the
// pure-REST custom-endpoints client functions from `src/youtab.ts` run unmodified
// and emit their real `YoutabApiRequest`. Nothing about the Youtab side is stubbed.
//
// Only the boundary to the OUTSIDE of the frontend is substituted: the
// REFERENCE_EXTERNAL_SYSTEM (a recording double bound to `window.youtabDesktop.api`)
// records the exact request the real consumer emits (method + path + body) and
// returns realistic typed responses under the same contract the backend enforces.
// This proves the real consumer emits the correct request shape and maps responses
// truthfully; it is NOT a live vendor/server proof and does NOT substitute for a
// live backend handshake (see MCP-03 / live-external rows, which stay BLOCKED).
// Invalid configuration must fail VISIBLY (the consumer's promise rejects) — no
// fake success, no silent swallow.
//
// Label for the external boundary, used throughout: REFERENCE_EXTERNAL_SYSTEM.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { YoutabApiRequest } from '../../../global.d'
import type { CustomEndpoint, CustomEndpointUpdate } from '../../../types/youtab'
import {
  activateCustomEndpoint,
  deleteCustomEndpoint,
  getCustomEndpoints,
  saveCustomEndpoint,
  validateCustomEndpoint
} from '../../../youtab'

interface RecordedRequest {
  path: string
  method: string
  body: unknown
  timeoutMs?: number
  profile?: null | string
}

/**
 * REFERENCE_EXTERNAL_SYSTEM for the `/api/providers/custom-endpoints` routes: a
 * recording double of the external boundary only (NOT the Youtab consumers, which
 * run for real). It (a) records every real request the consumer emits and (b)
 * enforces the same contract the backend does, so an invalid payload fails closed
 * instead of returning a fabricated success. Not a live vendor/server proof.
 */
function makeCustomEndpointsReferenceService() {
  const requests: RecordedRequest[] = []
  const store = new Map<string, CustomEndpoint>()

  const toEndpoint = (id: string, body: CustomEndpointUpdate): CustomEndpoint => ({
    api_key_preview: body.api_key ? '****' : null,
    base_url: body.base_url,
    context_length: body.context_length ?? null,
    discover_models: body.discover_models ?? false,
    has_api_key: Boolean(body.api_key),
    id,
    is_current: Boolean(body.make_default),
    model: body.model,
    models: body.models ?? [],
    name: body.name,
    source: 'custom'
  })

  const listResponse = () => {
    const endpoints = [...store.values()]
    const current = endpoints.find(e => e.is_current) ?? endpoints[0]

    return {
      current: current
        ? { base_url: current.base_url, model: current.model, provider: current.name }
        : { base_url: '', model: '', provider: '' },
      endpoints,
      ok: true
    }
  }

  const api = async <T>(request: YoutabApiRequest): Promise<T> => {
    const method = request.method ?? 'GET'

    requests.push({
      path: request.path,
      method,
      body: request.body,
      timeoutMs: request.timeoutMs,
      profile: request.profile
    })

    // GET list
    if (request.path === '/api/providers/custom-endpoints' && method === 'GET') {
      return listResponse() as T
    }

    // POST save (create/edit)
    if (request.path === '/api/providers/custom-endpoints' && method === 'POST') {
      const body = request.body as CustomEndpointUpdate

      // Contract: name/base_url/model are mandatory. Fail CLOSED (backend 400).
      if (!body?.name?.trim() || !body?.base_url?.trim() || !body?.model?.trim()) {
        throw new Error('custom endpoint requires name, base_url and model')
      }

      const id = body.id ?? `ep-${store.size + 1}`

      if (body.make_default) {
        for (const e of store.values()) {
          e.is_current = false
        }
      }

      store.set(id, toEndpoint(id, body))

      return { ...listResponse(), id } as T
    }

    // POST validate (test reachability)
    if (request.path === '/api/providers/custom-endpoints/validate' && method === 'POST') {
      const body = request.body as CustomEndpointUpdate

      if (!body?.base_url?.trim()) {
        throw new Error('validate requires a base_url')
      }

      // A localhost base_url is treated as unreachable — a realistic negative.
      const reachable = !body.base_url.includes('127.0.0.1')

      return {
        message: reachable ? 'Endpoint reachable' : 'Connection refused',
        models: reachable ? ['gpt-x', 'gpt-y'] : [],
        ok: true,
        reachable
      } as T
    }

    // POST activate
    const activateMatch = /^\/api\/providers\/custom-endpoints\/([^/]+)\/activate$/.exec(request.path)

    if (activateMatch && method === 'POST') {
      const id = decodeURIComponent(activateMatch[1])
      const endpoint = store.get(id)

      if (!endpoint) {
        throw new Error(`no such endpoint: ${id}`)
      }

      for (const e of store.values()) {
        e.is_current = e.id === id
      }

      return { ok: true, provider: endpoint.name, model: endpoint.model } as T
    }

    // DELETE
    const deleteMatch = /^\/api\/providers\/custom-endpoints\/([^/]+)$/.exec(request.path)

    if (deleteMatch && method === 'DELETE') {
      const id = decodeURIComponent(deleteMatch[1])

      if (!store.delete(id)) {
        throw new Error(`no such endpoint: ${id}`)
      }

      return listResponse() as T
    }

    throw new Error(`unrouted request: ${method} ${request.path}`)
  }

  return { api, requests, store }
}

describe('API connection (custom endpoints) — real consumer integration', () => {
  let service: ReturnType<typeof makeCustomEndpointsReferenceService>

  beforeEach(() => {
    service = makeCustomEndpointsReferenceService()
    Object.defineProperty(window, 'youtabDesktop', {
      configurable: true,
      value: { api: service.api }
    })
  })

  afterEach(() => {
    Reflect.deleteProperty(window, 'youtabDesktop')
  })

  it('getCustomEndpoints issues GET /api/providers/custom-endpoints', async () => {
    const response = await getCustomEndpoints()

    expect(response.ok).toBe(true)
    expect(response.endpoints).toEqual([])
    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/providers/custom-endpoints',
      method: 'GET'
    })
  })

  it('saveCustomEndpoint POSTs the endpoint body and the backend persists it', async () => {
    const payload: CustomEndpointUpdate = {
      name: 'my-openai',
      base_url: 'https://api.example.com/v1',
      model: 'gpt-x',
      api_key: 'sk-secret',
      make_default: true
    }

    const response = await saveCustomEndpoint(payload)

    // Real request shape recorded by the reference service.
    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/providers/custom-endpoints',
      method: 'POST',
      body: payload
    })

    // Truthful success mapping — the created endpoint is echoed back.
    expect(response.id).toBeDefined()
    const saved = response.endpoints.find(e => e.id === response.id)
    expect(saved?.name).toBe('my-openai')
    expect(saved?.has_api_key).toBe(true)
    expect(saved?.is_current).toBe(true)
  })

  it('saveCustomEndpoint FAILS VISIBLY on invalid config (missing base_url)', async () => {
    const invalid: CustomEndpointUpdate = { name: 'broken', base_url: '   ', model: 'gpt-x' }

    await expect(saveCustomEndpoint(invalid)).rejects.toThrow(/requires name, base_url and model/)

    // Nothing was persisted — no fake success.
    expect(service.store.size).toBe(0)
  })

  it('validateCustomEndpoint POSTs to /validate and maps a reachable result', async () => {
    const response = await validateCustomEndpoint({
      name: 'reach',
      base_url: 'https://api.example.com/v1',
      model: 'gpt-x'
    })

    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/providers/custom-endpoints/validate',
      method: 'POST'
    })
    expect(response.reachable).toBe(true)
    expect(response.models).toEqual(['gpt-x', 'gpt-y'])
  })

  it('validateCustomEndpoint truthfully surfaces an UNREACHABLE endpoint', async () => {
    const response = await validateCustomEndpoint({
      name: 'down',
      base_url: 'http://127.0.0.1:9/v1',
      model: 'gpt-x'
    })

    // The consumer does not invent reachability — it relays the backend verdict.
    expect(response.reachable).toBe(false)
    expect(response.models).toEqual([])
    expect(response.message).toMatch(/refused/i)
  })

  it('activateCustomEndpoint POSTs to /{id}/activate and returns the active model', async () => {
    const saved = await saveCustomEndpoint({
      name: 'act',
      base_url: 'https://api.example.com/v1',
      model: 'gpt-z'
    })

    const id = saved.id as string

    const response = await activateCustomEndpoint(id)

    expect(service.requests.at(-1)).toMatchObject({
      path: `/api/providers/custom-endpoints/${encodeURIComponent(id)}/activate`,
      method: 'POST'
    })
    expect(response).toEqual({ ok: true, provider: 'act', model: 'gpt-z' })
    expect(service.store.get(id)?.is_current).toBe(true)
  })

  it('activateCustomEndpoint FAILS VISIBLY for an unknown id', async () => {
    await expect(activateCustomEndpoint('does-not-exist')).rejects.toThrow(/no such endpoint/)
  })

  it('deleteCustomEndpoint DELETEs /{id} and the backend drops it', async () => {
    const saved = await saveCustomEndpoint({
      name: 'gone',
      base_url: 'https://api.example.com/v1',
      model: 'gpt-x'
    })

    const id = saved.id as string
    expect(service.store.size).toBe(1)

    const response = await deleteCustomEndpoint(id)

    expect(service.requests.at(-1)).toMatchObject({
      path: `/api/providers/custom-endpoints/${encodeURIComponent(id)}`,
      method: 'DELETE'
    })
    expect(response.endpoints).toEqual([])
    expect(service.store.size).toBe(0)
  })

  it('deleteCustomEndpoint FAILS VISIBLY for an unknown id', async () => {
    await expect(deleteCustomEndpoint('ghost')).rejects.toThrow(/no such endpoint/)
  })
})
