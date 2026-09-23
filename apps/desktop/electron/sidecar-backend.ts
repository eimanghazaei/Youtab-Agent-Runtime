// Production wiring for the packaged self-contained backend sidecar.
//
// In a packaged app this resolves the frozen `youtab-backend` executable shipped
// under process.resourcesPath, runs the fail-closed integrity gate, and returns
// a backend descriptor the EXISTING Electron HTTP lifecycle consumes unchanged:
// the frozen exe is invoked with the same `serve --host 127.0.0.1 --port 0`
// argv the dev backend uses, announces its ephemeral port on stdout, and serves
// the loopback HTTP gateway (/api/health) — so port announcement, health/
// readiness, the ephemeral session token, connection-state and process-tree
// shutdown all keep working as-is. Packaged mode only swaps the *command* from a
// system Python to the bundled executable.
//
// Dev mode is untouched (returns null → caller's normal source/venv chain).
// Composed from the pure sidecar modules so it is unit-testable without Electron.
import { decideSidecarLaunch, type SidecarLaunchDecision } from './sidecar-integrity'
import { resolveSidecarPaths } from './sidecar-resolve'
import { TRUSTED_SIDECAR_ROOT_DIGEST } from './sidecar-trusted-digest'

export interface SidecarBackendDeps {
  isPackaged: boolean
  resourcesPath?: string | null
  repoRoot?: string | null
  platform?: NodeJS.Platform
  fileExists?: (p: string) => boolean
  /** Test injection; production defaults to the embedded trust anchor. */
  trustedDigest?: string | null
  /** Injectable digest computation (defaults to the real bundle walk). */
  computeDigest?: (dir: string) => string
  /** Retained for caller compatibility; cannot override packaged trust. */
  env?: NodeJS.ProcessEnv
}

export interface SidecarCommandBackend {
  kind: 'command'
  label: string
  command: string
  args: string[]
  bootstrap: false
  env: Record<string, string>
  shell: false
  sidecar: true
  sidecarBundleDir: string
  sidecarDigest: string | null
}

export interface SidecarRefusedBackend {
  kind: 'sidecar-refused'
  label: string
  command: null
  args: string[]
  bootstrap: true
  env: Record<string, string>
  shell: false
  sidecarRefusal: SidecarLaunchDecision
}

export type SidecarBackend = SidecarCommandBackend | SidecarRefusedBackend

export function canUseDeveloperSourceOverride(
  isPackaged: boolean,
  overrideRoot: string | undefined,
  isSourceRoot: (root: string) => boolean
): boolean {
  return !isPackaged && !!overrideRoot && isSourceRoot(overrideRoot)
}

/**
 * Resolve the packaged sidecar backend, or null to fall through to the caller's
 * normal runtime-resolution chain (dev mode, or a packaged build that ships no
 * sidecar and pins no trust anchor).
 *
 * @param backendArgs the already-built `serve …` argv (host/port/profile).
 */
export function resolvePackagedSidecarBackend(backendArgs: string[], deps: SidecarBackendDeps): SidecarBackend | null {
  // Dev mode keeps the existing source/venv resolution. Only a packaged app
  // selects the bundled executable.
  if (!deps.isPackaged) {
    return null
  }

  const paths = resolveSidecarPaths({
    isPackaged: true,
    resourcesPath: deps.resourcesPath,
    platform: deps.platform
  })

  // A packaged app must not accept an environment-supplied replacement for
  // the compiled sidecar digest. Developer tests may inject trustedDigest.
  const trustedDigest = deps.trustedDigest !== undefined ? deps.trustedDigest : TRUSTED_SIDECAR_ROOT_DIGEST

  // No resolvable path (packaged without a resourcesPath): only fail closed if
  // a trust anchor is pinned (a release build expected a sidecar); otherwise
  // fall through so nothing changes for a build that never shipped one.
  if (!paths) {
    if (trustedDigest) {
      return {
        kind: 'sidecar-refused',
        label: 'bundled Youtab backend could not be located',
        command: null,
        args: backendArgs,
        bootstrap: true,
        env: {},
        shell: false,
        sidecarRefusal: {
          action: 'refuse',
          reason: 'missing-sidecar',
          expected: trustedDigest,
          actual: null,
          detail: 'packaged app has no resourcesPath to resolve the bundled backend'
        }
      }
    }

    return null
  }

  const fileExists = deps.fileExists ?? (() => false)
  const bundlePresent = fileExists(paths.executable)

  const decision = decideSidecarLaunch({
    bundlePresent,
    bundleDir: paths.bundleDir,
    trustedDigest,
    computeDigest: deps.computeDigest
  })

  if (decision.action === 'skip') {
    return null
  }

  if (decision.action === 'refuse') {
    return {
      kind: 'sidecar-refused',
      label: `bundled Youtab backend failed integrity verification (${decision.reason})`,
      command: null,
      args: backendArgs,
      bootstrap: true,
      env: {},
      shell: false,
      sidecarRefusal: decision
    }
  }

  return {
    kind: 'command',
    label: `bundled Youtab backend at ${paths.executable}`,
    command: paths.executable,
    args: backendArgs,
    bootstrap: false,
    env: {},
    shell: false,
    sidecar: true,
    sidecarBundleDir: paths.bundleDir,
    sidecarDigest: decision.actual ?? null
  }
}
