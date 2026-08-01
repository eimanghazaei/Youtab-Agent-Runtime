import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { YoutabReadDirResult } from '@/global'
import type * as YoutabModule from '@/youtab'

import { discoverRuntimePlugins, watchRuntimePlugins } from './runtime-loader'

// getStatus would supply the connected backend's youtab_home — a REMOTE path in
// remote mode. The disk scanner must NOT derive the plugin root from it (#66899).
const getStatus = vi.fn(async () => ({ youtab_home: '/remote/box/.youtab-agent-runtime' }))

vi.mock('@/youtab', async importActual => ({
  ...(await importActual<typeof YoutabModule>()),
  getStatus: () => getStatus()
}))

const desktopPluginsRoot = vi.fn<() => Promise<string>>()
const readDir = vi.fn<(path: string) => Promise<YoutabReadDirResult>>()
const watchDirectory = vi.fn<(path: string) => Promise<{ id: string }>>()
const onPreviewFileChanged = vi.fn()

beforeEach(() => {
  desktopPluginsRoot.mockReset()
  readDir.mockReset()
  watchDirectory.mockReset()
  onPreviewFileChanged.mockReset()
  getStatus.mockClear()
  ;(window as unknown as { youtabDesktop: unknown }).youtabDesktop = {
    desktopPluginsRoot,
    onPreviewFileChanged,
    readDir,
    watchDirectory
  }
})

afterEach(() => {
  delete (window as unknown as { youtabDesktop?: unknown }).youtabDesktop
})

describe('scanDiskPlugins (#66899)', () => {
  it('scans the Electron-resolved local root, never the backend youtab_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.youtab-agent-runtime/desktop-plugins')
    readDir.mockResolvedValue({ entries: [] })

    await discoverRuntimePlugins()

    expect(desktopPluginsRoot).toHaveBeenCalled()
    expect(readDir).toHaveBeenCalledWith('/local/.youtab-agent-runtime/desktop-plugins')
    // The remote backend's youtab_home must never feed the local plugin scan.
    expect(getStatus).not.toHaveBeenCalled()
    expect(readDir).not.toHaveBeenCalledWith('/remote/box/.youtab-agent-runtime/desktop-plugins')
  })

  it('no-ops when the resolver yields no local root', async () => {
    desktopPluginsRoot.mockResolvedValue('')

    await discoverRuntimePlugins()

    expect(readDir).not.toHaveBeenCalled()
  })
})

describe('watchRuntimePlugins dir watch (#66899)', () => {
  it('watches the Electron-resolved local root, never the backend youtab_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.youtab-agent-runtime/desktop-plugins')
    readDir.mockResolvedValue({ entries: [] })
    watchDirectory.mockResolvedValue({ id: 'watch-1' })

    watchRuntimePlugins()
    // Drain the async scan + startDirWatch chains.
    await vi.waitFor(() => expect(watchDirectory).toHaveBeenCalled())

    expect(watchDirectory).toHaveBeenCalledWith('/local/.youtab-agent-runtime/desktop-plugins')
    expect(watchDirectory).not.toHaveBeenCalledWith('/remote/box/.youtab-agent-runtime/desktop-plugins')
    expect(getStatus).not.toHaveBeenCalled()
  })
})
