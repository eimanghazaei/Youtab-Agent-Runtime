// Executable integration proof for the MCP path.
//
// SCOPE OF TRUTH: every Youtab consumer and handler under test is REAL — the MCP
// consumer functions from `src/youtab.ts` (list / save / test / auth / oauth-flow /
// catalog / install) and the pure `lib/mcp-tool-filter.ts` gating logic run
// unmodified and emit their real `YoutabApiRequest`. Nothing on the Youtab side is
// stubbed.
//
// Only the boundary to the OUTSIDE of the frontend is substituted: the
// REFERENCE_EXTERNAL_SYSTEM (a recording double bound to `window.youtabDesktop.api`)
// records the exact request shape and enforces the MCP backend contract, so invalid
// configuration fails VISIBLY rather than returning a fake success or a disconnected
// "enabled" state. This proves the real consumer emits the correct request and maps
// responses truthfully; it is NOT a live vendor/server proof and does NOT substitute
// for a live MCP handshake — MCP-03 and other live-external rows stay BLOCKED.
//
// Label for the external boundary, used throughout: REFERENCE_EXTERNAL_SYSTEM.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { YoutabApiRequest } from '../../../global.d'
import { isToolEnabled, toggleToolInServer } from '../../../lib/mcp-tool-filter'
import type { McpCatalogResponse, McpServerSummary } from '../../../types/youtab'
import {
  authMcpServer,
  getMcpCatalog,
  getMcpOAuthFlow,
  installMcpCatalogEntry,
  listMcpServers,
  saveMcpServers,
  testMcpServer
} from '../../../youtab'

interface RecordedRequest {
  path: string
  method: string
  body: unknown
  timeoutMs?: number
  profile?: null | string
}

/** REFERENCE_EXTERNAL_SYSTEM for the `/api/mcp/...` routes: a recording double of
 *  the external boundary only (the Youtab consumers run for real). Records the real
 *  request the consumer emits and enforces the backend contract. Not a live
 *  vendor/server proof. */
function makeMcpReferenceService() {
  const requests: RecordedRequest[] = []
  const servers = new Map<string, Record<string, unknown>>()
  const flows = new Map<string, { server_name: string; status: string }>()

  const summarize = (name: string, config: Record<string, unknown>): McpServerSummary => ({
    name,
    transport: (config.transport as string) ?? 'stdio',
    command: (config.command as string) ?? null,
    args: (config.args as string[]) ?? [],
    url: (config.url as string) ?? null,
    enabled: config.enabled !== false,
    tools: (config.tools as string[]) ?? null
  })

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
    if (request.path === '/api/mcp/servers' && method === 'GET') {
      return { servers: [...servers.entries()].map(([n, c]) => summarize(n, c)) } as T
    }

    // PUT replace whole map
    if (request.path === '/api/mcp/servers' && method === 'PUT') {
      const body = request.body as { servers?: Record<string, Record<string, unknown>> }
      const map = body?.servers

      if (!map || typeof map !== 'object') {
        throw new Error('mcp save requires a servers map')
      }

      // Contract: a stdio server needs a command, a remote needs a url. Fail closed.
      for (const [name, config] of Object.entries(map)) {
        const transport = (config.transport as string) ?? 'stdio'
        const isRemote = transport === 'http' || transport === 'sse'

        if (isRemote ? !config.url : !config.command) {
          throw new Error(`mcp server "${name}" is invalid: missing ${isRemote ? 'url' : 'command'}`)
        }
      }

      servers.clear()

      for (const [name, config] of Object.entries(map)) {
        servers.set(name, config)
      }

      return { ok: true } as T
    }

    // POST test one server
    const testMatch = /^\/api\/mcp\/servers\/([^/]+)\/test$/.exec(request.path)

    if (testMatch && method === 'POST') {
      const name = decodeURIComponent(testMatch[1])

      if (!servers.has(name)) {
        // Truthful failed probe — ok:false, not a thrown transport error.
        return { ok: false, error: `unknown server: ${name}`, tools: [] } as T
      }

      return { ok: true, tools: [{ name: 'search', description: 'web search' }], prompts: 0, resources: 1 } as T
    }

    // POST auth (start oauth flow)
    const authMatch = /^\/api\/mcp\/servers\/([^/]+)\/auth$/.exec(request.path)

    if (authMatch && method === 'POST') {
      const name = decodeURIComponent(authMatch[1])

      if (!servers.has(name)) {
        throw new Error(`cannot auth unknown server: ${name}`)
      }

      const flowId = `flow-${flows.size + 1}`
      flows.set(flowId, { server_name: name, status: 'authorization_required' })

      return {
        flow_id: flowId,
        server_name: name,
        status: 'authorization_required',
        authorization_url: `https://auth.example.com/${flowId}`,
        error: null
      } as T
    }

    // GET oauth flow status
    const flowMatch = /^\/api\/mcp\/oauth\/flows\/([^/]+)$/.exec(request.path)

    if (flowMatch && method === 'GET') {
      const flowId = decodeURIComponent(flowMatch[1])
      const flow = flows.get(flowId)

      if (!flow) {
        throw new Error(`no such oauth flow: ${flowId}`)
      }

      return {
        flow_id: flowId,
        server_name: flow.server_name,
        status: 'approved',
        authorization_url: null,
        error: null,
        tools: [{ name: 'search', description: 'web search' }]
      } as T
    }

    // GET catalog
    if (request.path === '/api/mcp/catalog' && method === 'GET') {
      const catalog: McpCatalogResponse = {
        entries: [
          {
            name: 'brave-search',
            description: 'Brave web search',
            source: 'youtab',
            transport: 'stdio',
            auth_type: 'env',
            required_env: [{ name: 'BRAVE_API_KEY', prompt: 'Brave API key', required: true }],
            command: 'npx',
            args: ['-y', '@brave/mcp'],
            url: null,
            install_url: null,
            install_ref: null,
            bootstrap: [],
            default_enabled: null,
            post_install: '',
            needs_install: false,
            installed: false,
            enabled: false
          }
        ],
        diagnostics: []
      }

      return catalog as T
    }

    // POST catalog install
    if (request.path === '/api/mcp/catalog/install' && method === 'POST') {
      const body = request.body as { name?: string; env?: Record<string, string>; enable?: boolean }

      if (!body?.name) {
        throw new Error('install requires a catalog entry name')
      }

      // Contract: brave-search requires BRAVE_API_KEY. Missing env → fail closed.
      if (body.name === 'brave-search' && !body.env?.BRAVE_API_KEY) {
        throw new Error('install brave-search requires BRAVE_API_KEY')
      }

      servers.set(body.name, { transport: 'stdio', command: 'npx', enabled: body.enable !== false })

      return { ok: true, name: body.name, action: 'installed', background: false } as T
    }

    throw new Error(`unrouted request: ${method} ${request.path}`)
  }

  return { api, requests, servers, flows }
}

describe('MCP consumers — real consumer integration', () => {
  let service: ReturnType<typeof makeMcpReferenceService>

  beforeEach(() => {
    service = makeMcpReferenceService()
    Object.defineProperty(window, 'youtabDesktop', {
      configurable: true,
      value: { api: service.api }
    })
  })

  afterEach(() => {
    Reflect.deleteProperty(window, 'youtabDesktop')
  })

  it('saveMcpServers PUTs /api/mcp/servers with the whole servers map', async () => {
    const map = { fs: { transport: 'stdio', command: 'npx', args: ['-y', 'fs-mcp'] } }

    const response = await saveMcpServers(map)

    expect(response.ok).toBe(true)
    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/mcp/servers',
      method: 'PUT',
      body: { servers: map }
    })
    expect(service.servers.has('fs')).toBe(true)
  })

  it('saveMcpServers FAILS VISIBLY when a stdio server has no command', async () => {
    await expect(saveMcpServers({ broken: { transport: 'stdio' } })).rejects.toThrow(/missing command/)

    // Nothing persisted — the invalid config did not silently "enable".
    expect(service.servers.size).toBe(0)
  })

  it('saveMcpServers FAILS VISIBLY when a remote server has no url', async () => {
    await expect(saveMcpServers({ remote: { transport: 'http' } })).rejects.toThrow(/missing url/)
  })

  it('listMcpServers GETs /api/mcp/servers and maps the summaries', async () => {
    await saveMcpServers({ fs: { transport: 'stdio', command: 'npx' } })

    const { servers } = await listMcpServers()

    expect(service.requests.at(-1)).toMatchObject({ path: '/api/mcp/servers', method: 'GET' })
    expect(servers).toHaveLength(1)
    expect(servers[0]).toMatchObject({ name: 'fs', transport: 'stdio', enabled: true })
  })

  it('testMcpServer POSTs /{name}/test with the slow timeout and maps tools', async () => {
    await saveMcpServers({ fs: { transport: 'stdio', command: 'npx' } })

    const result = await testMcpServer('fs')

    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/mcp/servers/fs/test',
      method: 'POST',
      timeoutMs: 60_000
    })
    expect(result.ok).toBe(true)
    expect(result.tools).toEqual([{ name: 'search', description: 'web search' }])
    expect(result.resources).toBe(1)
  })

  it('testMcpServer truthfully reports a FAILED probe for an unknown server', async () => {
    const result = await testMcpServer('ghost')

    // ok:false with an error string — not a fabricated success.
    expect(result.ok).toBe(false)
    expect(result.error).toMatch(/unknown server/)
    expect(result.tools).toEqual([])
  })

  it('authMcpServer POSTs /{name}/auth and returns the authorization URL', async () => {
    await saveMcpServers({ remote: { transport: 'http', url: 'https://mcp.example.com' } })

    const flow = await authMcpServer('remote')

    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/mcp/servers/remote/auth',
      method: 'POST',
      timeoutMs: 60_000
    })
    expect(flow.status).toBe('authorization_required')
    expect(flow.authorization_url).toContain('https://auth.example.com/')
    expect(flow.flow_id).toBeDefined()
  })

  it('authMcpServer FAILS VISIBLY for an unknown server', async () => {
    await expect(authMcpServer('nope')).rejects.toThrow(/cannot auth unknown server/)
  })

  it('getMcpOAuthFlow GETs the flow status and maps the approved result', async () => {
    await saveMcpServers({ remote: { transport: 'http', url: 'https://mcp.example.com' } })
    const started = await authMcpServer('remote')

    const flow = await getMcpOAuthFlow(started.flow_id)

    expect(service.requests.at(-1)).toMatchObject({
      path: `/api/mcp/oauth/flows/${encodeURIComponent(started.flow_id)}`
    })
    expect(flow.status).toBe('approved')
    expect(flow.tools).toEqual([{ name: 'search', description: 'web search' }])
  })

  it('getMcpCatalog GETs /api/mcp/catalog and maps entries', async () => {
    const catalog = await getMcpCatalog()

    expect(service.requests.at(-1)).toMatchObject({ path: '/api/mcp/catalog' })
    expect(catalog.entries).toHaveLength(1)
    expect(catalog.entries[0].name).toBe('brave-search')
    expect(catalog.entries[0].required_env[0].name).toBe('BRAVE_API_KEY')
  })

  it('installMcpCatalogEntry POSTs /catalog/install with name+env+enable', async () => {
    const result = await installMcpCatalogEntry('brave-search', { BRAVE_API_KEY: 'k' })

    expect(service.requests.at(-1)).toMatchObject({
      path: '/api/mcp/catalog/install',
      method: 'POST',
      body: { name: 'brave-search', env: { BRAVE_API_KEY: 'k' }, enable: true },
      timeoutMs: 60_000
    })
    expect(result.ok).toBe(true)
    expect(service.servers.has('brave-search')).toBe(true)
  })

  it('installMcpCatalogEntry FAILS VISIBLY when required env is missing', async () => {
    await expect(installMcpCatalogEntry('brave-search')).rejects.toThrow(/requires BRAVE_API_KEY/)

    // The disconnected "enabled: true" flag did not create a live server.
    expect(service.servers.has('brave-search')).toBe(false)
  })

  it('mcp-tool-filter gates tools by the persisted include/exclude config', () => {
    const server = { transport: 'stdio', command: 'npx' }

    // No filter → all tools enabled.
    expect(isToolEnabled(server, 'search')).toBe(true)

    // Toggling an unlisted tool creates an exclude denylist and disables it.
    const excluded = toggleToolInServer(server, 'search')
    expect(isToolEnabled(excluded, 'search')).toBe(false)
    expect(isToolEnabled(excluded, 'fetch')).toBe(true)
  })
})
