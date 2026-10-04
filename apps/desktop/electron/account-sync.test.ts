import { describe, expect, it } from 'vitest'

import { AccountSync, type SyncJournal, type SyncRecord, transcript } from './account-sync'

const scope = { tenant_id: 'tenant', user_id: 'user', workspace_id: 'private' }
const payload = { title: 'Example', messages: [{ role: 'user' as const, content: 'Synthetic text' }] }

function fixture() {
  const documents = new Map<string, SyncJournal>()
  const records = new Map<string, SyncRecord>()
  let cursor = 0
  let requests = 0
  let offline = false
  let activeScope = scope

  const client = (journal = documents) => new AccountSync({
    load: async key => structuredClone(journal.get(key) ?? null),
    save: async (key, value) => { journal.set(key, structuredClone(value)) },
    request: async (route, method, body: any) => {
      requests++

      if (offline) {throw new Error('offline')}

      if (route === '/scope') {return activeScope}

      if (method === 'GET') {
        const since = Number(new URL(route, 'https://test.invalid').searchParams.get('since'))
        const page = [...records.values()].filter(v => v.cursor > since).sort((a, b) => a.cursor - b.cursor)

        return { records: structuredClone(page), cursor: page.at(-1)?.cursor ?? since, has_more: false }
      }

      const prior = records.get(body.object_id)

      if ((prior?.revision ?? 0) !== body.expected_revision) {throw new Error('409')}
      cursor = Math.max(cursor, ...[...records.values()].map(v => v.cursor))
      const next = { object_id: body.object_id, kind: body.kind, deleted: body.deleted, payload: body.payload, revision: (prior?.revision ?? 0) + 1, cursor: ++cursor }
      records.set(next.object_id, structuredClone(next))

      return next
    }
  })

  return { client, documents, records, requests: () => requests, offline: (v: boolean) => {offline = v}, switchAccount: () => {activeScope = { ...scope, user_id: 'other' }} }
}

describe('account sync journal', () => {
  it('cancels a queued offline edit when local content reverts to the server copy', async () => {
    const f = fixture(); const client = f.client()
    await client.setConsent(true); await client.queue('chat', 'chat', payload); await client.synchronize()
    await client.queue('chat', 'chat', { ...payload, title: 'unsent edit' })
    f.offline(true)
    await expect(client.synchronize()).rejects.toThrow('offline')
    f.offline(false)
    await client.queue('chat', 'chat', payload)
    expect((await client.synchronize()).outbox).toHaveLength(0)
    expect(f.records.get('chat')?.payload).toEqual(payload)
    expect(f.records.get('chat')?.revision).toBe(1)
  })
  it('reloads the durable checkpoint after a failed save rather than losing queued data', async () => {
    const journal = new Map<string, SyncJournal>(); let fail = false

    const client = new AccountSync({ request: async () => scope,
      load: async key => structuredClone(journal.get(key) ?? null),
      save: async (key, value) => {if (fail) {throw new Error('STORE_FULL');} journal.set(key, structuredClone(value))} })

    await client.setConsent(true); await client.queue('first', 'chat', payload)
    fail = true
    await expect(client.queue('second', 'chat', payload)).rejects.toThrow('STORE_FULL')
    fail = false
    expect((await client.connect()).outbox.map(v => v.object_id)).toEqual(['first'])
  })
  it('requires explicit consent before any transcript leaves the device', async () => {
    const f = fixture(); const client = f.client()
    await expect(client.queue('chat', 'chat', payload)).rejects.toThrow('CONSENT')
    await expect(client.synchronize()).rejects.toThrow('CONSENT')
    expect(f.records.size).toBe(0)
  })
  it('shares one account across two device journals', async () => {
    const f = fixture(); const one = f.client(); const two = f.client(new Map())
    await one.setConsent(true); await one.queue('chat', 'chat', payload); await one.synchronize()
    await two.setConsent(true)
    expect((await two.synchronize()).records.chat.payload).toEqual(payload)
  })
  it('preserves the durable outbox after offline failure and retries once', async () => {
    const f = fixture(); const one = f.client()
    await one.setConsent(true); await one.queue('chat', 'chat', payload)
    f.offline(true)
    await expect(one.synchronize()).rejects.toThrow('offline')
    expect([...f.documents.values()][0].outbox).toHaveLength(1)
    f.offline(false)
    const restarted = f.client()
    expect((await restarted.synchronize()).outbox).toHaveLength(0)
    expect(f.records.get('chat')?.revision).toBe(1)
  })
  it('keeps both conflicting edits without overwriting remote text', async () => {
    const f = fixture(); const one = f.client()
    await one.setConsent(true); await one.queue('chat', 'chat', payload); await one.synchronize()
    await one.queue('chat', 'chat', { ...payload, title: 'local edit' })
    f.records.set('chat', { ...f.records.get('chat')!, revision: 2, cursor: 2, payload: { ...payload, title: 'other device' } })
    const result = await one.synchronize()
    expect(result.records.chat.payload).toEqual({ ...payload, title: 'other device' })
    expect(Object.values(result.records).some(v => (v.payload as any)?.title === 'local edit')).toBe(true)
  })
  it('remote deletion suppresses a stale offline edit instead of resurrecting it', async () => {
    const f = fixture(); const one = f.client()
    await one.setConsent(true); await one.queue('chat', 'chat', payload); await one.synchronize()
    await one.queue('chat', 'chat', { ...payload, title: 'delayed edit' })
    f.records.set('chat', { ...f.records.get('chat')!, revision: 2, cursor: 2, deleted: true, payload: null })
    const result = await one.synchronize()
    expect(result.outbox).toHaveLength(0)
    expect(result.records.chat.deleted).toBe(true)
    await expect(one.queue('chat', 'chat', payload)).rejects.toThrow('DELETED')
  })
  it('does not inherit consent or another account records after switching account', async () => {
    const f = fixture(); const one = f.client()
    await one.setConsent(true); await one.queue('chat', 'chat', payload)
    f.switchAccount(); one.stop()
    const journal = await one.connect()
    expect(journal.consent).toBe(false); expect(journal.outbox).toHaveLength(0); expect(journal.records).toEqual({})
  })
  it('drops tool execution, system prompts, local paths, and credential metadata', () => {
    expect(transcript({ session: { title: 'Example', cwd: 'private path', api_key: 'private' }, messages: [
      { role: 'system', content: 'private instructions' },
      { role: 'user', content: 'Synthetic text', local_path: 'private' },
      { role: 'tool', content: 'private tool result' }
    ] })).toEqual(payload)
  })
  it('cancels in-flight identity acquisition on sign-out', async () => {
    let resolve: (v: unknown) => void = () => undefined
    const client = new AccountSync({ request: () => new Promise(r => {resolve = r}), load: async () => null, save: async () => {throw new Error('must not save')} })
    const pending = client.connect(); client.stop(); resolve(scope)
    await expect(pending).rejects.toThrow('SESSION_CHANGED')
  })
})
