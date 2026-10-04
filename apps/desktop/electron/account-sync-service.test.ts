import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { accountSyncService, syncSessionIdentity } from './account-sync-service'

const roots: string[] = []
afterEach(async () => {
  vi.unstubAllGlobals()

  for (const root of roots.splice(0)) {
    expect(path.dirname(root)).toBe(path.resolve(os.tmpdir()))
    expect(path.basename(root).startsWith('youtab-sync-service-')).toBe(true)
    await fs.rm(root, { recursive: true, force: true })
  }
})

async function fixture() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'youtab-sync-service-'))
  roots.push(directory)
  const key = randomBytes(32)
  const imported: unknown[] = []
  const records = new Map<string, any>()
  let cursor = 0

  const transport = vi.fn(async (url: string, init: RequestInit) => {
    expect(url.startsWith('https://api.youtab.io/v1/account-sync/')).toBe(true)
    expect(init.redirect).toBe('error')
    expect(init.headers).toMatchObject({ Authorization: 'Bearer synthetic-sync-token' })
    const route = new URL(url)
    let data: unknown

    if (route.pathname.endsWith('/scope')) {data = { tenant_id: 'tenant', user_id: 'same-user', workspace_id: 'private' }}
    else if (init.method === 'POST') {
      const body = JSON.parse(init.body as string)
      const previous = records.get(body.object_id)

      if (body.payload?.chunk_refs?.some((id: string) => !records.has(id) || records.get(id).deleted)) {return new Response('{}', { status: 422 })}

      if (body.deleted && previous?.payload?.is_part && [...records.values()].some(record => !record.deleted && record.payload?.chunk_refs?.includes(body.object_id))) {return new Response('{}', { status: 409 })}

      if ((previous?.revision ?? 0) !== body.expected_revision) {return new Response('{}', { status: 409 })}
      data = { ...body, revision: (previous?.revision ?? 0) + 1, cursor: ++cursor }
      records.set(body.object_id, data)
    } else {
      const since = Number(route.searchParams.get('since'))
      const page = [...records.values()].filter(r => r.cursor > since).sort((a, b) => a.cursor - b.cursor)
      data = { records: page, cursor: page.at(-1)?.cursor ?? since, has_more: false }
    }

    return new Response(JSON.stringify(data))
  })

  vi.stubGlobal('fetch', transport)

  const dependencies = {
    directory, baseUrl: 'https://api.youtab.io', profile: 'default',
    token: async () => 'synthetic-sync-token',
    encrypt: (text: string) => {
      const nonce = randomBytes(12); const cipher = createCipheriv('aes-256-gcm', key, nonce)

      return { nonce: nonce.toString('base64'), ciphertext: Buffer.concat([cipher.update(text), cipher.final()]).toString('base64'), tag: cipher.getAuthTag().toString('base64') }
    },
    decrypt: (raw: any) => {
      const cipher = createDecipheriv('aes-256-gcm', key, Buffer.from(raw.nonce, 'base64'))
      cipher.setAuthTag(Buffer.from(raw.tag, 'base64'))

      return Buffer.concat([cipher.update(Buffer.from(raw.ciphertext, 'base64')), cipher.final()]).toString()
    },
    exportSession: async (_id: string) => ({ title: 'Synthetic chat', messages: [{ role: 'user', content: 'Synthetic private transcript' }, { role: 'tool', content: 'excluded secret' }] }),
    importSession: async (session: unknown) => { imported.push(session) }
    , listNewSessions: async (_since: number) => ({ sessions: [] as { id: string; started_at: number }[], limited: false })
  }

  return { dependencies, records, imported, transport }
}

describe('main process account sync service', () => {
  it('keeps a compressed conversation attached to its original account identity', async () => {
    const f = await fixture()
    let row = { id: 'original-root', started_at: 100, _lineage_root_id: undefined as string | undefined }
    let content = 'old history'

    const service = accountSyncService({ ...f.dependencies,
      listNewSessions: async () => ({ sessions: [{ id: syncSessionIdentity(row), started_at: row.started_at }], limited: false }),
      exportSession: async id => {expect(id).toBe('original-root');

 return { started_at: 100, messages: [{ role: 'user', content }] }}
    })

    await service.consent(true); await service.sync()
    row = { ...row, id: 'compression-tip', _lineage_root_id: 'original-root' }
    content = 'complete compressed history'
    await service.sync()
    expect(f.records.size).toBe(1)
    expect(Object.keys((await service.status()).localLinks ?? {})).toEqual(['original-root'])
  })
  it('coalesces offline long-chat edits after their parts and reclaims superseded parts safely', async () => {
    const f = await fixture()
    const messages = Array.from({ length: 5001 }, (_, index) => ({ role: 'user', content: `message ${index}` }))
    let offline = true
    const transport = f.transport
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      if (offline && init.method === 'POST') {throw new Error('offline')}

      return transport(url, init)
    })
    const service = accountSyncService({ ...f.dependencies, exportSession: async () => ({ title: 'Long', messages }) })
    await service.consent(true)
    await expect(service.share('long')).rejects.toThrow('offline')
    messages[5000] = { role: 'user', content: 'offline revised text' }
    await expect(service.share('long')).rejects.toThrow('offline')
    offline = false
    await service.sync()
    let roots = [...f.records.values()].filter(record => !record.deleted && !record.payload.is_part)
    expect(roots).toHaveLength(1)
    expect([...f.records.values()].filter(record => !record.deleted)).toHaveLength(3)
    const size = f.records.size
    await service.share('long')
    expect(f.records.size).toBe(size)
    messages[5000] = { role: 'user', content: 'new append' }
    await service.share('long')
    expect([...f.records.values()].filter(record => !record.deleted)).toHaveLength(3)
    roots = [...f.records.values()].filter(record => !record.deleted && !record.payload.is_part)
    messages[5000] = { role: 'user', content: 'late local edit before deletion' }
    await service.remove(roots[0].object_id)
    expect([...f.records.values()].filter(record => !record.deleted)).toHaveLength(0)
  }, 15000)
  it('reports oversized history while uploading healthy chats and preserving every local original', async () => {
    const f = await fixture()

    const service = accountSyncService({ ...f.dependencies,
      listNewSessions: async () => ({ sessions: [{ id: 'large', started_at: 10 }, { id: 'healthy', started_at: 20 }], limited: false }),
      exportSession: async id => id === 'large'
        ? { title: 'Large history', messages: Array.from({ length: 100001 }, () => ({ role: 'user', content: 'old text' })) }
        : { title: 'Healthy', messages: [{ role: 'user', content: 'preserved' }] }
    })

    await service.consent(true)
    const state = await service.sync()
    expect(state.migrationFailures).toEqual({ large: 'history-limit' })
    expect(f.records.size).toBe(1)
    expect(state.localLinks).toHaveProperty('healthy')
    expect(f.imported).toHaveLength(0)
  })
  it('transfers long history in bounded parts and reconstructs it on a second device', async () => {
    const f = await fixture()
    const messages = Array.from({ length: 5001 }, (_, index) => ({ role: 'user' as const, content: `old message ${index}`, timestamp: 100 + index }))
    const one = accountSyncService({ ...f.dependencies, exportSession: async () => ({ title: 'Complete history', started_at: 100, messages }) })
    await one.consent(true); await one.share('long-history')
    expect(f.records.size).toBe(3)
    const root = [...f.records.values()].find(record => !record.payload.is_part)
    expect(root.payload.chunk_refs).toHaveLength(2)
    const second = await fs.mkdtemp(path.join(os.tmpdir(), 'youtab-sync-service-')); roots.push(second)
    const two = accountSyncService({ ...f.dependencies, directory: second })
    await two.consent(true); await two.continueChat(root.object_id)
    expect(f.imported[0]).toMatchObject({ title: 'Complete history', started_at: 100, messages })
  })
  it('preserves identical repeated history batches without duplicate part references', async () => {
    const f = await fixture()
    const messages = Array.from({ length: 6000 }, () => ({ role: 'user', content: 'repeated original text' }))
    const service = accountSyncService({ ...f.dependencies, exportSession: async () => ({ messages }) })
    await service.consent(true); await service.share('repeated')
    const root = [...f.records.values()].find(record => !record.payload.is_part)
    expect(new Set(root.payload.chunk_refs).size).toBe(2)
    await service.continueChat(root.object_id)
    expect(f.imported[0]).toMatchObject({ messages })
  })
  it('stores an encrypted durable journal and resumes after restarting', async () => {
    const f = await fixture(); const service = accountSyncService(f.dependencies)
    await service.consent(true); await service.share('local-chat')
    const files = await fs.readdir(f.dependencies.directory)
    expect(files).toHaveLength(1)
    expect(await fs.readFile(path.join(f.dependencies.directory, files[0]), 'utf8')).not.toContain('Synthetic private transcript')
    const restored = await accountSyncService(f.dependencies).status()
    expect(restored.consent).toBe(true)
    expect(Object.values(restored.records)).toHaveLength(1)
  })
  it('imports only inert chat text on a second device and binds subsequent edits', async () => {
    const f = await fixture(); const one = accountSyncService(f.dependencies)
    await one.consent(true); await one.share('local-chat')
    const second = await fs.mkdtemp(path.join(os.tmpdir(), 'youtab-sync-service-')); roots.push(second)
    const two = accountSyncService({ ...f.dependencies, directory: second })
    await two.consent(true)
    const cloudId = [...f.records.keys()][0]
    const result = await two.continueChat(cloudId)
    expect(f.imported).toEqual([{ id: result.localId, source: 'account-sync', title: 'Synthetic chat', messages: [{ role: 'user', content: 'Synthetic private transcript' }] }])
    expect((await two.status()).localLinks?.[result.localId]).toBe(cloudId)
  })
  it('does not upload a local export that completes after logout', async () => {
    const f = await fixture();

 let finish: (value: unknown) => void = () => {}
    let started = false
    const service = accountSyncService({ ...f.dependencies, exportSession: () => new Promise(resolve => { started = true; finish = resolve }) })
    await service.consent(true)
    const pending = service.share('local-chat')
    await vi.waitFor(() => expect(started).toBe(true))
    service.stop()
    finish({ title: 'Old account', messages: [] })
    await expect(pending).rejects.toThrow('SESSION_CHANGED')
    expect(f.records.size).toBe(0)
  })
  it('rejects oversized network responses without persisting account data', async () => {
    const f = await fixture()
    vi.stubGlobal('fetch', async () => new Response(' '.repeat(2 * 1024 * 1024 + 1)))
    await expect(accountSyncService(f.dependencies).status()).rejects.toThrow('RESPONSE_LIMIT')
    expect(await fs.readdir(f.dependencies.directory)).toEqual([])
  })
  it('adds old and new chats after consent and preserves links across restart', async () => {
    const f = await fixture()

    const service = accountSyncService({ ...f.dependencies, listNewSessions: async () => ({ sessions: [
      { id: 'old', started_at: Date.now() / 1000 - 3600 }, { id: 'new', started_at: Date.now() / 1000 + 1 }
    ], limited: false }) })

    await service.status()
    expect(f.records.size).toBe(0)
    await service.consent(true)
    await service.sync()
    expect(f.records.size).toBe(2)
    expect(Object.keys((await service.status()).localLinks ?? {})).toEqual(['old', 'new'])
    const restarted = accountSyncService(f.dependencies)
    expect((await restarted.status()).autoNewChats).toBe(true)
  })
  it('continues discovery beyond its first batch without deleting earlier chats', async () => {
    const f = await fixture()

    const list = vi.fn(async (_since: number, offset: number) => ({
      sessions: [{ id: offset === 0 ? 'oldest' : 'later', started_at: 100 }],
      limited: offset === 0, nextOffset: offset === 0 ? 1000 : 0
    }))

    const service = accountSyncService({ ...f.dependencies, listNewSessions: list,
      exportSession: async id => ({ title: id, started_at: 100, last_active: 200, messages: [{ role: 'user', content: id, timestamp: 101 }] }) })

    await service.consent(true); await service.sync(); await service.sync()
    expect(list.mock.calls.map(call => call[1])).toEqual([0, 1000])
    expect(f.records.size).toBe(2)
    expect([...f.records.values()].every(record => !record.deleted)).toBe(true)
    expect([...f.records.values()][0].payload).toMatchObject({ started_at: 100, last_active: 200, messages: [{ timestamp: 101 }] })
    await service.consent(false)
    expect((await service.status()).localLinks).toHaveProperty('oldest')
    expect(f.records.size).toBe(2)
  })
  it('keeps shared-chat sync and deletion working when automatic discovery fails', async () => {
    const f = await fixture()
    const service = accountSyncService({ ...f.dependencies, listNewSessions: async () => {throw new Error('inventory limit')} })
    await service.consent(true); await service.share('local-chat'); await service.autoNewChats(true)
    const state = await service.sync()
    expect(state.discoveryLimited).toBe(true)
    const id = [...f.records.keys()][0]
    await service.remove(id)
    expect(f.records.get(id).deleted).toBe(true)
  })
  it('sends deletion before a quota-rejected upload so the account can recover', async () => {
    const f = await fixture()
    const normal = f.transport
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        const mutation = JSON.parse(init.body as string)

        if (!mutation.deleted && [...f.records.values()].some(v => !v.deleted && v.object_id !== mutation.object_id)) {return new Response('{}', { status: 429 })}
      }

      return normal(url, init)
    })
    const service = accountSyncService(f.dependencies)
    await service.consent(true); await service.share('first')
    const first = [...f.records.keys()][0]
    await expect(service.share('second')).rejects.toThrow('429')
    await service.remove(first)
    expect(f.records.get(first).deleted).toBe(true)
    expect([...f.records.values()].filter(v => !v.deleted)).toHaveLength(1)
    expect((await service.status()).outbox).toHaveLength(0)
  })
})
