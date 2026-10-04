/** Account-bound sync journal. The main process owns encryption and transport. */
import { createHash, randomUUID } from 'node:crypto'
import { isDeepStrictEqual } from 'node:util'

export interface SyncScope { tenant_id: string; user_id: string; workspace_id: string }
export type SyncLanguage = 'en' | 'nl' | 'fa' | 'zh' | 'zh-hant' | 'ja' | 'ar'
export type SyncPreferences = { language: SyncLanguage; appearance: 'light' | 'dark' | 'system' }
export type SyncMessage = { role: 'user' | 'assistant'; content: string; timestamp?: number }
export type SyncTranscript = { title: string; messages: SyncMessage[]; started_at?: number; last_active?: number; chunk_refs?: string[]; is_part?: true }
export type SyncPayload = SyncTranscript | SyncPreferences
export interface SyncRecord {
  object_id: string; kind: 'chat' | 'preferences'; revision: number; cursor: number; deleted: boolean; payload: SyncPayload | null
}
export interface SyncMutation { object_id: string; kind: SyncRecord['kind']; expected_revision: number; deleted: boolean; payload: SyncPayload | null }
export interface SyncJournal {
  version: 1; scope: SyncScope; consent: boolean; cursor: number
  records: Record<string, SyncRecord>; outbox: SyncMutation[]
  localLinks?: Record<string, string>
  localHashes?: Record<string, string>
  autoNewChats?: boolean
  autoSince?: number
  discoveryOffset?: number
  discoveryLimited?: boolean
  migrationFailures?: Record<string, 'export' | 'history-limit'>
  ownedParts?: string[]
}
export interface SyncDependencies {
  request: (route: string, method: 'GET' | 'POST', body: unknown, signal: AbortSignal) => Promise<unknown>
  load: (key: string) => Promise<SyncJournal | null>
  save: (key: string, journal: SyncJournal) => Promise<void>
}

export function scopeKey(scope: SyncScope): string {
  const parts = [scope.tenant_id, scope.user_id, scope.workspace_id]

  if (!parts.every(v => typeof v === 'string' && v.length > 0 && v.length <= 128)) {throw new Error('INVALID_SYNC_SCOPE')}

  return createHash('sha256').update(JSON.stringify(parts)).digest('hex')
}

function plain(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {throw new Error('INVALID_SYNC_DOCUMENT')}

  return value as Record<string, unknown>
}

export function transcript(value: unknown): SyncPayload {
  const root = plain(value)
  const session = plain(root.session ?? root)

  if (!Array.isArray(root.messages)) {throw new Error('INVALID_SYNC_TRANSCRIPT')}
  const messages: SyncMessage[] = []

  const timestamp = (value: unknown): number | undefined => {
    if (value == null) {return undefined}

    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 253402300799) {throw new Error('INVALID_SYNC_TIMESTAMP')}

    return value
  }

  for (const raw of root.messages) {
    const message = plain(raw)

    if (message.role !== 'user' && message.role !== 'assistant') {continue}

    if (message.content == null) {continue}

    const content = Array.isArray(message.content)
      ? message.content.filter(part => part && typeof part === 'object' && part.type === 'text' && typeof part.text === 'string').map(part => part.text).join('\n')
      : message.content

    if (typeof content !== 'string' || content.length > 262144) {throw new Error('INVALID_SYNC_MESSAGE')}

    if (content) {
      const time = timestamp(message.timestamp)
      messages.push({ role: message.role, content, ...(time === undefined ? {} : { timestamp: time }) })
    }
  }

  const title = typeof session.title === 'string' ? session.title : ''

  if (title.length > 1024 || messages.length > 100000) {throw new Error('INVALID_SYNC_TRANSCRIPT')}
  const result: SyncTranscript = { title, messages }

  for (const field of ['started_at', 'last_active'] as const) {
    const time = timestamp(session[field])

    if (time !== undefined) {result[field] = time}
  }

  if (Buffer.byteLength(JSON.stringify(result)) > 16 * 1024 * 1024) {throw new Error('SYNC_TRANSCRIPT_TOO_LARGE')}

  return result
}

/** Parts are inert text records; the root is published after every part. */
export function joinedTranscript(payload: unknown, records: Record<string, SyncRecord>): SyncTranscript {
  const root = plain(payload)
  const result = transcript(root) as SyncTranscript

  if (root.is_part) {throw new Error('SYNC_HISTORY_PART_IS_NOT_CHAT')}

  if (root.chunk_refs !== undefined) {
    if (!Array.isArray(root.chunk_refs) || root.chunk_refs.length > 100) {throw new Error('INVALID_SYNC_PARTS')}

    for (const id of root.chunk_refs) {
      const part = records[id]

      if (!part || part.deleted || part.kind !== 'chat' || !part.payload || !('is_part' in part.payload) || part.payload.is_part !== true) {throw new Error('SYNC_HISTORY_INCOMPLETE')}
      result.messages.push(...part.payload.messages)
    }
  }

  return transcript(result) as SyncTranscript
}

function wireTranscript(payload: unknown): SyncTranscript {
  const root = plain(payload)
  const result = transcript(root) as SyncTranscript

  if (root.is_part !== undefined) {
    if (root.is_part !== true || root.chunk_refs !== undefined) {throw new Error('INVALID_SYNC_PART')}
    result.is_part = true
  }

  if (root.chunk_refs !== undefined) {
    if (!Array.isArray(root.chunk_refs) || root.chunk_refs.length > 100
      || new Set(root.chunk_refs).size !== root.chunk_refs.length
      || !root.chunk_refs.every(id => typeof id === 'string' && /^part_[0-9a-f]{32}$/.test(id))) {throw new Error('INVALID_SYNC_PARTS')}

    result.chunk_refs = root.chunk_refs
  }

  if (result.messages.length > 2000 || Buffer.byteLength(JSON.stringify(result)) > 1024 * 1024) {throw new Error('SYNC_WIRE_HISTORY_LIMIT')}

  return result
}

function record(value: unknown): SyncRecord {
  const root = plain(value)

  if (typeof root.object_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(root.object_id) || ['__proto__', 'constructor', 'prototype'].includes(root.object_id) || !['chat', 'preferences'].includes(root.kind as string)
    || !Number.isSafeInteger(root.revision) || (root.revision as number) < 1 || !Number.isSafeInteger(root.cursor) || (root.cursor as number) < 1 || typeof root.deleted !== 'boolean') {throw new Error('INVALID_SYNC_RECORD')}

  if (root.deleted) {
    if (root.payload !== null) {throw new Error('INVALID_SYNC_TOMBSTONE')}
  } else if (root.kind === 'chat') {
    const payload = plain(root.payload)
    const clean = wireTranscript(payload)

    if (!isDeepStrictEqual(clean, root.payload)) {throw new Error('INVALID_SYNC_PAYLOAD')}
  } else {
    const payload = plain(root.payload)

    if (Object.keys(payload).some(k => !['language', 'appearance'].includes(k))
      || (payload.language != null && !['en', 'nl', 'fa', 'zh', 'zh-hant', 'ja', 'ar'].includes(payload.language as string))
      || (payload.appearance != null && !['light', 'dark', 'system'].includes(payload.appearance as string))) {throw new Error('INVALID_SYNC_PREFERENCES')}
  }

  return root as unknown as SyncRecord
}

function equal(a: unknown, b: unknown) { return isDeepStrictEqual(a, b) }

export class AccountSync {
  private state: SyncJournal | null = null
  private abort = new AbortController()
  private generation = 0
  private busy = false
  constructor(private readonly deps: SyncDependencies) {}

  stop() {
    this.abort.abort()
    this.abort = new AbortController()
    this.generation++
    this.state = null
  }
  private live(generation: number) {
    if (this.generation !== generation || this.abort.signal.aborted) {throw new Error('SYNC_SESSION_CHANGED')}
  }
  async connect(): Promise<SyncJournal> {
    const generation = this.generation
    const scope = await this.deps.request('/scope', 'GET', undefined, this.abort.signal) as SyncScope
    this.live(generation)
    const key = scopeKey(scope)

    if (this.state && scopeKey(this.state.scope) !== key) {
      this.stop()
      throw new Error('SYNC_SESSION_CHANGED')
    }

    if (!this.state || scopeKey(this.state.scope) !== key) {
      const stored = await this.deps.load(key)
      this.live(generation)

      if (stored && (stored.version !== 1 || scopeKey(stored.scope) !== key)) {throw new Error('SYNC_JOURNAL_SCOPE_MISMATCH')}
      this.state = stored ?? { version: 1, scope, consent: false, cursor: 0, records: {}, outbox: [] }
    }

    return structuredClone(this.state)
  }
  async setConsent(enabled: boolean) {
    if (this.busy) {throw new Error('SYNC_BUSY')}
    await this.connect()
    this.state!.consent = enabled
    // Account sync includes existing and future chats. Disabling sync never
    // deletes either the local history or the account's copy.
    this.state!.autoNewChats = enabled
    this.state!.autoSince = 0
    await this.persist()
  }
  async setAutoNewChats(enabled: boolean) {
    if (this.busy) {throw new Error('SYNC_BUSY')}
    const generation = this.generation
    await this.connect()
    this.live(generation)

    if (!this.state!.consent) {throw new Error('SYNC_CONSENT_REQUIRED')}

    if (enabled) {this.state!.autoSince = 0}
    this.state!.autoNewChats = enabled
    await this.persist()
  }
  async checkpointDiscovery(offset: number, failures: SyncJournal['migrationFailures']) {
    if (!Number.isSafeInteger(offset) || offset < 0) {throw new Error('INVALID_SYNC_DISCOVERY_OFFSET')}
    this.state!.discoveryOffset = offset
    this.state!.migrationFailures = failures
    await this.persist()
  }
  private async persist() {
    const generation = this.generation
    const snapshot = structuredClone(this.state!)

    try {await this.deps.save(scopeKey(snapshot.scope), snapshot)}
    catch (error) {
      // Reload the last durable checkpoint after a failed save. Never advance
      // a cursor in memory past data that could not be persisted.
      if (generation === this.generation) {this.state = null}
      throw error
    }
  }
  async queue(objectId: string, kind: SyncRecord['kind'], payload: SyncPayload | null, deleted = false) {
    if (this.busy) {throw new Error('SYNC_BUSY')}
    const generation = this.generation
    await this.connect()
    this.live(generation)
    const state = this.state!

    if (!state.consent) {throw new Error('SYNC_CONSENT_REQUIRED')}

    if (!/^[A-Za-z0-9_-]{1,128}$/.test(objectId)) {throw new Error('INVALID_SYNC_OBJECT')}
    const existing = state.records[objectId]

    if (deleted && existing?.kind === 'chat' && existing.payload && 'chunk_refs' in existing.payload) {
      state.ownedParts = [...new Set([...(state.ownedParts ?? []), ...(existing.payload.chunk_refs ?? [])])]
    }

    if (existing?.deleted) {throw new Error('SYNC_RECORD_DELETED')}

    const index = state.outbox.findIndex(v => v.object_id === objectId)

    if (!deleted && existing && equal(existing.payload, payload)) {
      // A local revert cancels a still-pending edit; it is not a no-op while
      // that edit remains queued for a later network retry.
      if (index >= 0) {state.outbox.splice(index, 1); await this.persist()}

      return
    }

    const mutation = { object_id: objectId, kind, expected_revision: existing?.revision ?? 0, deleted, payload: deleted ? null : payload }
    record({ ...mutation, revision: 1, cursor: 1 })

    if (index < 0) {state.outbox.push(mutation)} else {state.outbox[index] = mutation}
    await this.persist()
  }
  async shareSession(localId: string, payload: SyncPayload) {
    if (this.busy) {throw new Error('SYNC_BUSY')}
    const generation = this.generation
    await this.connect()
    this.live(generation)

    if (!/^[A-Za-z0-9_-]{1,128}$/.test(localId) || ['__proto__', 'constructor', 'prototype'].includes(localId)) {throw new Error('INVALID_LOCAL_SESSION')}
    const state = this.state!

    if (!state.consent) {throw new Error('SYNC_CONSENT_REQUIRED')}
    const full = transcript(payload) as SyncTranscript
    const links = state.localLinks ?? {}
    let cloudId = Object.hasOwn(links, localId) ? links[localId] : `chat_${randomUUID().replaceAll('-', '')}`
    const current = state.records[cloudId]
    const baseline = state.localHashes?.[localId]

    const currentTranscript = current && !current.deleted ? joinedTranscript(current.payload, state.records) : null

    if (baseline && currentTranscript
      && createHash('sha256').update(JSON.stringify(currentTranscript)).digest('hex') !== baseline
      && !equal(currentTranscript, payload)) {
      cloudId = `conflict_${randomUUID().replaceAll('-', '')}`
    }

    if (currentTranscript && equal(currentTranscript, full)) {
      await this.queue(cloudId, 'chat', current!.payload)
      ;(state.localHashes ??= {})[localId] = createHash('sha256').update(JSON.stringify(full)).digest('hex')
      await this.persist()

      return
    }

    let batch: SyncMessage[] = []
    let size = 0
    const batches: SyncMessage[][] = []

    for (const message of full.messages) {
      const bytes = Buffer.byteLength(JSON.stringify(message)) + 1

      if (batch.length && (batch.length >= 2000 || size + bytes > 900000)) {batches.push(batch); batch = []; size = 0}
      batch.push(message); size += bytes
    }

    batches.push(batch)
    const root: SyncTranscript = { ...full, messages: batches[0] }
    const parts: { id: string; payload: SyncTranscript }[] = []

    if (batches.length > 1) {
      root.chunk_refs = []

      for (const messages of batches.slice(1)) {
        // Reuse only unpublished local parts. Reusing acknowledged server
        // parts would let another device's cleanup invalidate an offline edit.
        const reusable = state.outbox.find(part => !part.deleted
          && !root.chunk_refs!.includes(part.object_id)
          && part.payload && 'is_part' in part.payload && part.payload.is_part && equal(part.payload.messages, messages))

        if (reusable) {root.chunk_refs.push(reusable.object_id);

 continue}

        const id = `part_${randomUUID().replaceAll('-', '')}`

        parts.push({ id, payload: { title: '', messages, is_part: true } })
        root.chunk_refs.push(id)
      }
    }

    // JSON escaping can make one valid local message exceed a wire record.
    // Validate the entire upload before changing links or queuing any parts.
    wireTranscript(root)

    for (const part of parts) {wireTranscript(part.payload)}

    ;(state.localLinks ??= {})[localId] = cloudId

    if (current?.payload && 'chunk_refs' in current.payload) {
      state.ownedParts = [...new Set([...(state.ownedParts ?? []), ...(current.payload.chunk_refs ?? [])])]
    }

    for (const part of parts) {
      ;(state.ownedParts ??= []).push(part.id)
      await this.queue(part.id, 'chat', part.payload)
      this.live(generation)
    }

    await this.queue(cloudId, 'chat', root)
    this.live(generation)
    ;(state.localHashes ??= {})[localId] = createHash('sha256').update(JSON.stringify(payload)).digest('hex')
    await this.persist()
  }
  async bindImportedSession(localId: string, cloudId: string, revision: number) {
    const generation = this.generation
    await this.connect()
    this.live(generation)
    const state = this.state!
    const current = state.records[cloudId]

    if (!state.consent || !current || current.deleted || current.kind !== 'chat' || current.revision !== revision) {
      throw new Error('SYNC_IMPORT_CHANGED')
    }

    if (!/^[A-Za-z0-9_-]{1,128}$/.test(localId) || ['__proto__', 'constructor', 'prototype'].includes(localId)) {
      throw new Error('INVALID_LOCAL_SESSION')
    }

    ;(state.localLinks ??= {})[localId] = cloudId
    ;(state.localHashes ??= {})[localId] = createHash('sha256').update(JSON.stringify(joinedTranscript(current.payload, state.records))).digest('hex')
    await this.persist()
  }
  async synchronize(): Promise<SyncJournal> {
    if (this.busy) {throw new Error('SYNC_BUSY')}
    this.busy = true
    const generation = this.generation

    try {
      await this.connect()
      this.live(generation)
      const state = this.state!

      if (!state.consent) {throw new Error('SYNC_CONSENT_REQUIRED')}
      // Pull first, preserving divergent local edits as explicit conflict copies.
      let more = true
      let pages = 0

      while (more) {
        if (++pages > 100) {throw new Error('SYNC_PAGE_LIMIT')}
        const raw = plain(await this.deps.request(`/changes?since=${state.cursor}&limit=100`, 'GET', undefined, this.abort.signal))
        this.live(generation)

        if (!Array.isArray(raw.records) || raw.records.length > 100 || typeof raw.has_more !== 'boolean' || !Number.isSafeInteger(raw.cursor) || (raw.cursor as number) < state.cursor) {throw new Error('INVALID_SYNC_PAGE')}
        let cursor = state.cursor
        const incomingRecords = raw.records.map(record)

        for (const incoming of incomingRecords) {
          if (incoming.cursor <= cursor || incoming.cursor > (raw.cursor as number)) {throw new Error('INVALID_SYNC_ORDER')}
          cursor = incoming.cursor
        }

        if (raw.cursor !== cursor || (raw.has_more && raw.records.length === 0)) {throw new Error('INVALID_SYNC_CURSOR')}

        for (const incoming of incomingRecords) {
          const pendingIndex = state.outbox.findIndex(v => v.object_id === incoming.object_id)
          const pending = state.outbox[pendingIndex]

          if (pending && incoming.revision !== pending.expected_revision) {
            state.outbox.splice(pendingIndex, 1)

            if (pending.deleted && !incoming.deleted) {
              state.outbox.push({ ...pending, expected_revision: incoming.revision })
            }

            // Tombstones dominate delayed offline writes. A conflicting edit
            // survives under a new identity; the server copy stays untouched.
            if (!incoming.deleted && !pending.deleted && !equal(incoming.payload, pending.payload)) {
              const conflictId = `conflict_${randomUUID().replaceAll('-', '')}`
              state.outbox.push({ ...pending, object_id: conflictId, expected_revision: 0 })

              for (const [localId, id] of Object.entries(state.localLinks ?? {})) {
                if (id === incoming.object_id) {state.localLinks![localId] = conflictId}
              }
            }
          }

          state.records[incoming.object_id] = incoming
        }

        state.cursor = cursor
        more = raw.has_more
        await this.persist()
      }

      // Explicit deletions free quota and must remain available when an upload
      // is blocked by the account's storage limit. Queue entries are coalesced
      // per object, so this does not reorder mutations of the same identity.
      const references = () => new Set([...Object.values(state.records), ...state.outbox]
        .filter(value => !value.deleted && value.payload && 'chunk_refs' in value.payload)
        .flatMap(value => (value.payload as SyncTranscript).chunk_refs ?? []))

      const needed = references()
      state.outbox = state.outbox.filter(value => !(value.payload && 'is_part' in value.payload && value.payload.is_part && !needed.has(value.object_id)))
      const rank = (value: SyncMutation) => value.deleted ? 0 : value.payload && 'is_part' in value.payload && value.payload.is_part ? 1 : 2
      state.outbox.sort((one, two) => rank(one) - rank(two))

      while (state.outbox.length) {
        const pending = state.outbox[0]
        const response = record(await this.deps.request('/records', 'POST', pending, this.abort.signal))
        this.live(generation)

        if (response.object_id !== pending.object_id || response.kind !== pending.kind || response.deleted !== pending.deleted || !equal(response.payload, pending.payload)) {throw new Error('SYNC_ACK_MISMATCH')}

        if ((!pending.deleted && response.revision !== pending.expected_revision + 1) || (pending.deleted && response.revision < pending.expected_revision + 1)) {throw new Error('SYNC_ACK_REVISION_MISMATCH')}
        state.records[response.object_id] = response
        state.outbox.shift()
        // Do not advance the pull cursor here: other devices may have written
        // records between our last pull and this acknowledgement.
        await this.persist()
      }

      // Reclaim only parts created/retired by this device. Another device's
      // in-flight upload is never garbage-collected. Gateway also checks refs.
      const liveParts = references()

      for (const id of state.ownedParts ?? []) {
        const part = state.records[id]

        if (!part || part.deleted || liveParts.has(id)) {continue}
        const deletion: SyncMutation = { object_id: id, kind: 'chat', expected_revision: part.revision, deleted: true, payload: null }
        let response: SyncRecord

        try {response = record(await this.deps.request('/records', 'POST', deletion, this.abort.signal))}
        catch (error) {
          this.live(generation)

          if (String(error).includes('409')) {continue}
          throw error
        }

        this.live(generation)

        if (response.object_id !== id || response.kind !== 'chat' || !response.deleted || response.payload !== null
          || response.revision < part.revision + 1) {throw new Error('SYNC_PART_DELETE_ACK_MISMATCH')}

        state.records[id] = response
        await this.persist()
      }

      state.ownedParts = (state.ownedParts ?? []).filter(id => state.records[id] && !state.records[id].deleted)
      await this.persist()

      return structuredClone(state)
    } finally {this.busy = false}
  }
}
