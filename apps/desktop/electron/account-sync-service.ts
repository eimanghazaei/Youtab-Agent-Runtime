import { createHash, randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import fs from 'node:fs/promises'
import path from 'node:path'

import { AccountSync, joinedTranscript, scopeKey, type SyncJournal, type SyncPreferences, type SyncTranscript, transcript } from './account-sync'

interface ServiceDependencies {
  directory: string
  baseUrl: string
  profile: string
  token: () => Promise<string>
  encrypt: (text: string) => unknown
  decrypt: (encrypted: unknown) => string | null
  exportSession: (id: string) => Promise<unknown>
  importSession: (session: SyncTranscript & { id: string; source: string }) => Promise<void>
  listNewSessions: (since: number, offset: number) => Promise<{ sessions: { id: string; started_at: number }[]; limited: boolean; nextOffset?: number }>
}

export function syncSessionIdentity(session: { id: string; _lineage_root_id?: string }): string {
  const id = session._lineage_root_id ?? session.id

  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id)
    || ['__proto__', 'constructor', 'prototype'].includes(id)) {throw new Error('INVALID_LOCAL_SESSION')}

  return id
}

/** Main-process service: credentials stay here; redirects are never followed. */
export function accountSyncService(deps: ServiceDependencies) {
  if (deps.baseUrl !== 'https://api.youtab.io') {throw new Error('UNTRUSTED_ACCOUNT_SYNC_ORIGIN')}
  let job: Promise<SyncJournal> | null = null
  let generation = 0
  let discoveryLimited = false
  let tail: Promise<unknown> = Promise.resolve()

  const live = (expected: number) => {if (generation !== expected) {throw new Error('SYNC_SESSION_CHANGED')}}

  const exclusive = <T>(action: () => Promise<T>): Promise<T> => {
    const expected = generation

    const next = tail.then(async () => {
      live(expected)
      const result = await action()
      live(expected)

      return result
    })

    tail = next.catch(() => undefined)

    return next
  }

  const client = new AccountSync({
    request: async (route, method, body, signal) => {
      if (!/^\/(scope|records|changes\?since=\d+&limit=100)$/.test(route)) {throw new Error('INVALID_SYNC_ROUTE')}
      const token = await deps.token()
      signal.throwIfAborted()

      const response = await fetch(`${deps.baseUrl}/v1/account-sync${route}`, {
        method, redirect: 'error', cache: 'no-store',
        signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body)
      })

      if (!response.ok) {
        await response.body?.cancel()
        throw new Error(`ACCOUNT_SYNC_HTTP_${response.status}`)
      }

      if (!response.body) {throw new Error('ACCOUNT_SYNC_EMPTY_RESPONSE')}
      const reader = response.body.getReader()
      const chunks: Uint8Array[] = []
      let size = 0

      try {
        while (true) {
          const next = await reader.read()

          if (next.done) {break}
          size += next.value.byteLength

          if (size > 2 * 1024 * 1024) {throw new Error('ACCOUNT_SYNC_RESPONSE_LIMIT')}
          chunks.push(next.value)
        }
      } finally {await reader.cancel().catch(() => undefined)}

      return JSON.parse(Buffer.concat(chunks).toString('utf8'))
    },
    load: async key => {
      const file = path.join(deps.directory, `${key}.json`)

      try {
        const handle = await fs.open(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0))
        let bytes: Buffer

        try {
          const opened = await handle.stat()
          const info = await fs.lstat(file)

          if (!info.isFile() || info.isSymbolicLink() || info.size > 128 * 1024 * 1024 || !opened.isFile() || opened.dev !== info.dev || opened.ino !== info.ino || opened.size !== info.size) {throw new Error('UNSAFE_SYNC_STORE')}
          bytes = Buffer.alloc(info.size + 1)
          let length = 0

          while (length < bytes.length) {
            const { bytesRead } = await handle.read(bytes, length, bytes.length - length, length)

            if (!bytesRead) {break}
            length += bytesRead
          }

          if (length !== info.size) {throw new Error('UNSAFE_SYNC_STORE')}
          bytes = bytes.subarray(0, length)
        } finally {await handle.close()}

        const encrypted = JSON.parse(bytes.toString('utf8'))
        const plain = deps.decrypt(encrypted)

        if (!plain) {throw new Error('SYNC_STORE_UNREADABLE')}

        return JSON.parse(plain)
      } catch (error: any) {
        if (error.code === 'ENOENT') {return null}
        throw error
      }
    },
    save: async (key, journal) => {
      const plain = JSON.stringify(journal)

      if (Buffer.byteLength(plain) > 64 * 1024 * 1024) {throw new Error('SYNC_STORE_LIMIT')}
      const secret = JSON.stringify(deps.encrypt(plain))

      if (Buffer.byteLength(secret) > 128 * 1024 * 1024) {throw new Error('SYNC_STORE_LIMIT')}
      await fs.mkdir(deps.directory, { recursive: true })

      if ((await fs.lstat(deps.directory)).isSymbolicLink()) {throw new Error('UNSAFE_SYNC_STORE')}
      const file = path.join(deps.directory, `${key}.json`)
      const tmp = `${file}.${randomBytes(16).toString('hex')}.tmp`
      const handle = await fs.open(tmp, 'wx', 0o600)

      try {
        try {await handle.writeFile(secret); await handle.sync()} finally {await handle.close()}
        await fs.rename(tmp, file)
      } finally {await fs.unlink(tmp).catch(() => undefined)}
    }
  })

  const synchronize = async () => {
    if (job) {return job}
    const expected = generation
    job = (async () => {
      const state = await client.connect()
      live(expected)

      if (!state.consent) {throw new Error('SYNC_CONSENT_REQUIRED')}
      const failures = { ...state.migrationFailures }

      const exportTranscript = async (id: string) => {
        let exported: unknown

        try {exported = await deps.exportSession(id)}
        catch {live(expected); failures[id] = 'export';

 return null}

        live(expected)

        try {
          const payload = transcript(exported)
          delete failures[id]

          return payload
        } catch {failures[id] = 'history-limit';

 return null}
      }

      const shareTranscript = async (id: string, payload: SyncTranscript) => {
        try {await client.shareSession(id, payload)}
        catch (error) {
          live(expected)

          if (!(error instanceof Error) || error.message !== 'SYNC_WIRE_HISTORY_LIMIT') {throw error}
          // Keep the original local chat and expose its migration failure;
          // an unrepresentable message must not block other chats or retries.
          failures[id] = 'history-limit'
        }
      }

      if (state.autoNewChats) {
        const offset = state.discoveryOffset ?? 0
        const discovery = await deps.listNewSessions(0, offset).catch(() => ({ sessions: [], limited: true, nextOffset: offset }))
        live(expected)
        discoveryLimited = discovery.limited
        const sessions = discovery.sessions

        if (sessions.length > 1000) {throw new Error('SYNC_DISCOVERY_LIMIT')}

        for (const session of sessions) {
          if (Object.hasOwn(state.localLinks ?? {}, session.id) || session.id.startsWith('synced_')
            || !Number.isFinite(session.started_at) || session.started_at < 0
            || session.started_at > Date.now() / 1000 + 60) {continue}

          const payload = await exportTranscript(session.id)

          if (payload) {await shareTranscript(session.id, payload as SyncTranscript)}
        }

        await client.checkpointDiscovery(discovery.nextOffset ?? 0, failures)
      }

      for (const [id, cloudId] of Object.entries(state.localLinks ?? {})) {
        if (state.records[cloudId]?.deleted || state.outbox.some(value => value.object_id === cloudId && value.deleted)) {continue}
        // Local disappearance is not permission to delete a cloud transcript.
        // Explicit deletion comes from the account's cloud list.
        const payload = await exportTranscript(id)
        live(expected)

        if (payload) {
          const hash = createHash('sha256').update(JSON.stringify(payload)).digest('hex')

          if (hash !== state.localHashes?.[id]) {await shareTranscript(id, payload as SyncTranscript)}
        }
      }

      await client.checkpointDiscovery((await client.connect()).discoveryOffset ?? 0, failures)

      return { ...await client.synchronize(), discoveryLimited }
    })()

    try {return await job} finally {job = null}
  }

  return {
    stop: () => {generation++; client.stop()},
    status: () => exclusive(async () => ({ ...await client.connect(), discoveryLimited })),
    consent: (enabled: boolean) => exclusive(() => client.setConsent(enabled)),
    autoNewChats: (enabled: boolean) => exclusive(() => client.setAutoNewChats(enabled)),
    sync: () => exclusive(synchronize),
    preferences: (value: SyncPreferences) => exclusive(async () => {
      await client.queue('preferences', 'preferences', value)

      return synchronize()
    }),
    continueChat: (id: string) => exclusive(async () => {
      const expected = generation
      const state = await synchronize()
      live(expected)
      const record = state.records[id]

      if (!record || record.deleted || record.kind !== 'chat') {throw new Error('SYNC_CHAT_NOT_FOUND')}
      const payload = joinedTranscript(record.payload, state.records)
      // Explicit import into the backend creates inert history, never tool calls,
      // credentials, working directories or remote execution authority.
      const localId = `synced_${createHash('sha256').update(JSON.stringify([scopeKey(state.scope), id, record.revision])).digest('hex')}`
      await deps.importSession({ id: localId, source: 'account-sync', ...payload })
      live(expected)
      await client.bindImportedSession(localId, id, record.revision)

      return { localId }
    }),
    share: (id: string) => exclusive(async () => {const expected = generation;
      const exported = await deps.exportSession(id);
      live(expected);
      await client.shareSession(id, transcript(exported));

 return synchronize()}),
    remove: (id: string) => exclusive(async () => {await client.queue(id, 'chat', null, true);

 return synchronize()})
  }
}
