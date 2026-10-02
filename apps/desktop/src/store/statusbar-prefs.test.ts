import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('pilot statusbar control visibility', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('reveals approval and terminal on upgrade while preserving unrelated preferences', async () => {
    window.localStorage.setItem('youtab.desktop.statusbarHidden', JSON.stringify(['approval-mode', 'terminal', 'cron']))
    const store = await import('./statusbar-prefs')
    expect(store.$statusbarHiddenIds.get()).toEqual(['cron'])
  })

  it('preserves a later explicit hide across reload', async () => {
    const store = await import('./statusbar-prefs')
    store.setStatusbarItemVisible('terminal', false)
    vi.resetModules()
    const reloaded = await import('./statusbar-prefs')
    expect(reloaded.$statusbarHiddenIds.get()).toContain('terminal')
    expect(reloaded.$statusbarHiddenIds.get()).not.toContain('approval-mode')
  })
})
