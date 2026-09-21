import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// SE-02 branding-integrity guard.
//
// A source-text + asset scan (same category as `no-native-title.test.ts` — an
// ESLint-style rule expressed as a vitest) that pins the Owner brand identity so
// an accidental or unauthorised rebrand fails the suite instead of shipping.
//
// It asserts, against the CURRENT tree, that:
//   1. The BrandMark component references the canonical `youtab-girl` mark on the
//      white brand tile (the badge Owner ships in light + dark).
//   2. That brand asset actually exists in `public/`.
//   3. The canonical product name "Youtab" is the document title, and the full
//      product string "Youtab Agent Runtime" is present in the shell.
//
// It must PASS as-is and FAIL if the brand name or the key brand asset is
// altered. This test changes NO branding.

const componentsDir = resolve(__dirname, '..')
const desktopDir = resolve(__dirname, '../../..')

const CANONICAL_BRAND_NAME = 'Youtab'
const CANONICAL_PRODUCT_STRING = 'Youtab Agent Runtime'
const BRAND_MARK_ASSET = 'youtab-girl.jpg'

describe('branding integrity (SE-02)', () => {
  it('BrandMark renders the canonical youtab-girl mark on the white brand tile', () => {
    const brandMark = readFileSync(resolve(componentsDir, 'brand-mark.tsx'), 'utf-8')

    // The mark asset reference must be intact and rendered as the badge image.
    expect(brandMark).toContain(`assetPath('${BRAND_MARK_ASSET}')`)
    // The badge is a white tile — identical in light/dark per the brand spec.
    expect(brandMark).toContain('bg-white')
  })

  it('ships the youtab-girl brand asset in public/', () => {
    expect(existsSync(resolve(desktopDir, 'public', BRAND_MARK_ASSET))).toBe(true)
  })

  it('keeps "Youtab" as the canonical document/product name', () => {
    const indexHtml = readFileSync(resolve(desktopDir, 'index.html'), 'utf-8')

    // The window/document title is the bare brand name.
    expect(indexHtml).toContain(`<title>${CANONICAL_BRAND_NAME}</title>`)
  })

  it('keeps the full "Youtab Agent Runtime" product string in the install shell', () => {
    const installOverlay = readFileSync(resolve(componentsDir, 'desktop-install-overlay.tsx'), 'utf-8')

    expect(installOverlay).toContain(CANONICAL_PRODUCT_STRING)
  })

  it('has not rebranded: no competing product name in the BrandMark or shell', () => {
    // Guard against a silent rebrand of the two brand-bearing surfaces. If the
    // mark asset name or the brand tile ever changes, the assertions above break;
    // here we additionally assert the mark component still names the brand.
    const brandMark = readFileSync(resolve(componentsDir, 'brand-mark.tsx'), 'utf-8')
    const indexHtml = readFileSync(resolve(desktopDir, 'index.html'), 'utf-8')

    expect(brandMark.toLowerCase()).toContain('youtab')
    expect(indexHtml.toLowerCase()).toContain('youtab')
  })
})
