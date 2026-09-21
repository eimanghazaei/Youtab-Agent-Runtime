import { afterEach, describe, expect, it, vi } from 'vitest'

import type { YoutabFolderGrant } from '@/global'

import {
  FolderGrantsUnavailableError,
  isFolderGrantsAvailable,
  listFolderGrants,
  readInGrant,
  requestFolderGrant,
  revokeFolderGrant,
  writeInGrant
} from './folder-grants'

function makeGrant(overrides: Partial<YoutabFolderGrant> = {}): YoutabFolderGrant {
  return {
    grantId: 'g1',
    safeLabel: 'Projects',
    permission: 'read-only',
    status: 'active',
    workspaceId: 'ws-1',
    createdAt: '2026-09-21T00:00:00.000Z',
    ...overrides
  }
}

function stubBridge(folderGrants: unknown) {
  Object.defineProperty(window, 'youtabDesktop', {
    configurable: true,
    value: folderGrants ? { folderGrants } : {}
  })
}

describe('folder-grants consumer', () => {
  afterEach(() => {
    Object.defineProperty(window, 'youtabDesktop', { configurable: true, value: undefined })
    vi.restoreAllMocks()
  })

  it('reports availability from the presence of the bridge', () => {
    stubBridge(null)
    expect(isFolderGrantsAvailable()).toBe(false)

    stubBridge({ request: vi.fn(), list: vi.fn(), revoke: vi.fn(), read: vi.fn(), write: vi.fn() })
    expect(isFolderGrantsAvailable()).toBe(true)
  })

  it('requests a read-only grant through the real bridge', async () => {
    const grant = makeGrant()
    const request = vi.fn(async () => grant)
    stubBridge({ request, list: vi.fn(), revoke: vi.fn(), read: vi.fn(), write: vi.fn() })

    await expect(requestFolderGrant({ readOnly: true })).resolves.toEqual(grant)
    expect(request).toHaveBeenCalledWith({ readOnly: true })
  })

  it('defaults to read-only when no option is passed', async () => {
    const request = vi.fn(async () => makeGrant())
    stubBridge({ request, list: vi.fn(), revoke: vi.fn(), read: vi.fn(), write: vi.fn() })

    await requestFolderGrant()
    expect(request).toHaveBeenCalledWith({ readOnly: true })
  })

  it('lists, revokes, reads and writes through the bridge', async () => {
    const list = vi.fn(async () => [makeGrant()])
    const revoke = vi.fn(async () => ({ revoked: true as const }))
    const read = vi.fn(async () => ({ bytes: new ArrayBuffer(4) }))
    const write = vi.fn(async () => ({ written: true as const, receiptId: 'r1' }))
    stubBridge({ request: vi.fn(), list, revoke, read, write })

    await expect(listFolderGrants()).resolves.toHaveLength(1)
    await expect(revokeFolderGrant('g1')).resolves.toEqual({ revoked: true })
    await readInGrant('g1', 'a.txt')
    await writeInGrant('g1', 'a.txt', new ArrayBuffer(2), { requireApproval: true })

    expect(revoke).toHaveBeenCalledWith('g1')
    expect(read).toHaveBeenCalledWith('g1', 'a.txt')
    expect(write).toHaveBeenCalledWith('g1', 'a.txt', expect.any(ArrayBuffer), { requireApproval: true })
  })

  it('throws a typed unavailable error when the bridge is absent — never a fake grant', async () => {
    stubBridge(null)

    await expect(requestFolderGrant()).rejects.toBeInstanceOf(FolderGrantsUnavailableError)
    await expect(listFolderGrants()).rejects.toBeInstanceOf(FolderGrantsUnavailableError)
    await expect(revokeFolderGrant('g1')).rejects.toBeInstanceOf(FolderGrantsUnavailableError)
  })
})
