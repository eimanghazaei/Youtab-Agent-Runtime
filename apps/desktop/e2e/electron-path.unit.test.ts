import { describe, expect, it, vi } from 'vitest'

import { type ElectronPathDeps, resolveElectronBinary } from './electron-path'

const join = (...segments: string[]): string => segments.join('/')

function deps(overrides: Partial<ElectronPathDeps>): ElectronPathDeps {
  return {
    repoRoot: '/repo',
    join,
    exists: () => false,
    whichElectron: () => null,
    ...overrides
  }
}

describe('resolveElectronBinary', () => {
  it('prefers electron.exe (Windows) over the extension-less binary', () => {
    const exe = '/repo/node_modules/electron/dist/electron.exe'
    const exists = vi.fn((p: string) => p === exe || p === '/repo/node_modules/electron/dist/electron')

    expect(resolveElectronBinary(deps({ exists }))).toBe(exe)
  })

  it('falls back to the POSIX binary when no .exe is present', () => {
    const bin = '/repo/node_modules/electron/dist/electron'

    expect(resolveElectronBinary(deps({ exists: (p: string) => p === bin }))).toBe(bin)
  })

  it('falls back to a PATH-resolved electron, trimmed', () => {
    expect(resolveElectronBinary(deps({ whichElectron: () => '/usr/bin/electron\n' }))).toBe('/usr/bin/electron')
  })

  it('prefers the local .exe over a PATH result', () => {
    const exe = '/repo/node_modules/electron/dist/electron.exe'

    expect(
      resolveElectronBinary(deps({ exists: (p: string) => p === exe, whichElectron: () => '/usr/bin/electron' }))
    ).toBe(exe)
  })

  it('throws when Electron cannot be found anywhere', () => {
    expect(() => resolveElectronBinary(deps({}))).toThrow(/Electron binary not found/)
  })

  it('treats a blank PATH result as not found', () => {
    expect(() => resolveElectronBinary(deps({ whichElectron: () => '   ' }))).toThrow(/Electron binary not found/)
  })
})
