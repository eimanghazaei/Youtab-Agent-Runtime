import { describe, expect, it } from 'vitest'

import { caduceus, logo, LOGO_WIDTH } from '../banner.js'
import { DARK_THEME, LIGHT_THEME } from '../theme.js'

describe('Youtab Code banner', () => {
  it('uses the compact replacement title and Ocean Blue column', () => {
    expect(
      logo(DARK_THEME.color)
        .map(([, text]) => text)
        .join('\n')
    ).toContain('Youtab Code')
    expect(LOGO_WIDTH).toBeLessThan(66)
    expect(caduceus(DARK_THEME.color)).toHaveLength(28)
    expect(caduceus(DARK_THEME.color).every(([color]) => color === '#0096C7')).toBe(true)
    expect(DARK_THEME.color.border).toBe('#0096C7')
    expect(LIGHT_THEME.color.border).toBe('#0077B6')
  })
  it('replaces a retired custom-skin logo instead of rendering it again', () => {
    const block = String.fromCodePoint(0x2588)
    const corner = String.fromCodePoint(0x2557)

    const previous =
      block.repeat(2) +
      corner +
      '  ' +
      block.repeat(2) +
      corner +
      block.repeat(7) +
      corner +
      block.repeat(6) +
      corner +
      ' ' +
      block.repeat(3) +
      corner

    const title = logo(DARK_THEME.color, previous)
      .map(([, text]) => text)
      .join('\n')

    expect(title).toContain('Youtab Code')
    expect(title).not.toContain(previous)
    expect(caduceus(DARK_THEME.color, previous)).toEqual(caduceus(DARK_THEME.color))
  })
})
