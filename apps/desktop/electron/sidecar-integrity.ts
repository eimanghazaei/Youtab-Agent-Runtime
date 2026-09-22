// A2 trusted-binding gate for the packaged backend sidecar.
//
// At spawn time the main process recomputes the shipped bundle's root digest
// (canonical algorithm, shared with build-sidecar.mjs / verify-sidecar.mjs) and
// decides whether it is safe to launch. Fail-closed: a shipped-but-unverifiable
// or tampered bundle is REFUSED — it is never launched, and never silently
// downgraded to some other runtime.
//
// The digest computation is injectable so the decision logic is unit-testable
// without a real PyInstaller freeze; production uses `rootDigestFromBundle`.
import { rootDigestFromBundle } from '../packaging/backend-sidecar/root-digest.mjs'

export type SidecarLaunchAction = 'launch' | 'refuse' | 'skip'

export interface SidecarLaunchDecision {
  action: SidecarLaunchAction
  reason:
    | 'verified'
    | 'no-bundle'
    | 'no-trusted-digest'
    | 'digest-mismatch'
    | 'compute-error'
  expected?: string | null
  actual?: string | null
  detail?: string
}

export interface DecideSidecarLaunchInput {
  /** Is a sidecar bundle actually shipped/present at the resolved path? */
  bundlePresent: boolean
  /** Bundle directory to hash when present. */
  bundleDir?: string | null
  /** The committed trust anchor (may be null when not yet pinned). */
  trustedDigest?: string | null
  /** Injectable digest computation (defaults to the real bundle walk). */
  computeDigest?: (dir: string) => string
}

/**
 * Decide whether the packaged sidecar may launch.
 *
 *   no bundle present            → skip   (nothing shipped; caller uses its
 *                                          normal runtime-resolution chain)
 *   bundle present, no anchor    → refuse (unverifiable trusted code)
 *   bundle present, mismatch     → refuse (tampered / drifted)
 *   bundle present, digest error → refuse (cannot prove integrity)
 *   bundle present, match        → launch
 */
export function decideSidecarLaunch({
  bundlePresent,
  bundleDir,
  trustedDigest,
  computeDigest = rootDigestFromBundle
}: DecideSidecarLaunchInput): SidecarLaunchDecision {
  if (!bundlePresent || !bundleDir) {
    return { action: 'skip', reason: 'no-bundle' }
  }

  const expected = trustedDigest && /^[0-9a-f]{64}$/.test(trustedDigest) ? trustedDigest : null

  if (!expected) {
    return {
      action: 'refuse',
      reason: 'no-trusted-digest',
      expected: null,
      detail: 'a sidecar bundle is shipped but the trusted root digest is not pinned; refusing to run unverified code'
    }
  }

  let actual: string

  try {
    actual = computeDigest(bundleDir)
  } catch (error) {
    return {
      action: 'refuse',
      reason: 'compute-error',
      expected,
      actual: null,
      detail: `failed to recompute sidecar root digest: ${error instanceof Error ? error.message : String(error)}`
    }
  }

  if (actual !== expected) {
    return {
      action: 'refuse',
      reason: 'digest-mismatch',
      expected,
      actual,
      detail: `sidecar root digest mismatch: expected ${expected} but recomputed ${actual}`
    }
  }

  return { action: 'launch', reason: 'verified', expected, actual }
}
