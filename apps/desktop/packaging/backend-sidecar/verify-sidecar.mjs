#!/usr/bin/env node
// A2 offline integrity verifier (CI-style gate). Recomputes the frozen
// sidecar's root digest from disk with the canonical algorithm and compares it
// to the committed trusted value in `sidecar-root-digest.txt`. Fails closed
// (non-zero exit) on any mismatch, missing bundle, or missing/garbled trusted
// digest — the same fail-closed contract the Electron main process enforces at
// spawn time, runnable in CI without launching Electron.
//
// Usage: node apps/desktop/packaging/backend-sidecar/verify-sidecar.mjs
//
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { parseTrustedDigest, rootDigestFromBundle } from './root-digest.mjs'

/**
 * Pure verification: returns { ok, reason, expected, actual }.
 *   ok:false + reason='no-bundle'      — bundle dir absent
 *   ok:false + reason='no-trusted'     — trusted digest missing/garbled
 *   ok:false + reason='mismatch'       — recomputed != trusted
 *   ok:true                            — recomputed == trusted
 */
export function verifyBundleDigest({ bundleDir, trustedDigestText, computeDigest = rootDigestFromBundle, bundleExists }) {
  const exists = typeof bundleExists === 'function' ? bundleExists(bundleDir) : existsSync(bundleDir)

  if (!exists) {
    return { ok: false, reason: 'no-bundle', expected: null, actual: null }
  }

  const expected = parseTrustedDigest(trustedDigestText)

  if (!expected) {
    return { ok: false, reason: 'no-trusted', expected: null, actual: null }
  }

  const actual = computeDigest(bundleDir)

  return actual === expected
    ? { ok: true, reason: 'match', expected, actual }
    : { ok: false, reason: 'mismatch', expected, actual }
}

function main() {
  const REPO_ROOT = resolve(process.cwd())
  const OUT_DIR = join(REPO_ROOT, 'apps', 'desktop', 'build', 'backend-sidecar')
  const bundleDir = join(OUT_DIR, 'dist', 'youtab-backend')
  const digestFile = join(OUT_DIR, 'sidecar-root-digest.txt')
  const trustedDigestText = existsSync(digestFile) ? readFileSync(digestFile, 'utf8') : ''

  const result = verifyBundleDigest({ bundleDir, trustedDigestText })

  if (result.ok) {
    console.log(`[verify] OK — root_digest matches trusted value: ${result.actual}`)
    process.exit(0)
  }

  const detail =
    result.reason === 'mismatch'
      ? `expected ${result.expected} but recomputed ${result.actual}`
      : result.reason === 'no-trusted'
        ? `missing or malformed ${digestFile}`
        : `no bundle at ${bundleDir} — run build-sidecar.mjs first`
  console.error(`[verify] FAIL (${result.reason}): ${detail}`)
  process.exit(result.reason === 'no-bundle' ? 2 : 1)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
