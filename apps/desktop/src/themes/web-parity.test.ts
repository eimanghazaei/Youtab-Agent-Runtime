import { describe, expect, it } from 'vitest'

import { modePref } from './context'
import { DEFAULT_TYPOGRAPHY, youtabTheme } from './presets'

// Visual parity with the Youtab Web Platform (web/ dashboard). The desktop
// default skin must render the web "Youtab Teal" (LENS_0) palette in its dark
// (default) mode, use the web system-font stacks, and default to dark mode so a
// fresh install opens in the teal look. See web/src/index.css +
// web/src/themes/presets.ts.
describe('desktop ↔ web visual parity: default youtab skin', () => {
  it('dark palette matches the web Youtab Teal (LENS_0) seeds', () => {
    const d = youtabTheme.darkColors!
    expect(d.background).toBe('#041C1C') // web --background-base
    expect(d.foreground).toBe('#FFE6CB') // web --midground-base (cream)
    expect(d.primary).toBe('#FFE6CB') // web --color-primary (= midground)
    expect(d.primaryForeground).toBe('#041C1C') // web --color-primary-foreground
    expect(d.ring).toBe('#FFE6CB')
    expect(d.midground).toBe('#FFE6CB')
    expect(d.destructive).toBe('#FB2C36') // web --color-destructive
    // borders/inputs are cream at 15% like the web tokens
    expect(d.border).toContain('#FFE6CB')
    expect(d.input).toContain('#FFE6CB')
  })

  it('uses the Web Platform font stacks and does not network-load a mono face', () => {
    // Web --theme-font-sans / --theme-font-mono lead the stack.
    expect(DEFAULT_TYPOGRAPHY.fontSans.startsWith('system-ui')).toBe(true)
    expect(DEFAULT_TYPOGRAPHY.fontMono.startsWith('ui-monospace')).toBe(true)
    expect(youtabTheme.typography?.fontUrl).toBeUndefined()
  })

  it('defaults to dark mode so a fresh install opens in the teal look', () => {
    // No persisted value → normalizeMode fallback resolves to 'dark'.
    expect(modePref.resolve('__parity_probe_no_such_profile__')).toBe('dark')
  })
})
