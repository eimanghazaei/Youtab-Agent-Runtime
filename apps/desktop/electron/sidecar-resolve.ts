// Resolve the on-disk location of the self-contained backend sidecar for both
// packaged and dev runs. Pure and injectable (no electron import) so packaged
// vs dev path resolution is unit-testable without launching Electron.
//
// Packaged: the sidecar tree is shipped via electron-builder `extraResources`
//   to `<resourcesPath>/backend-sidecar/…`. Layout:
//     <resourcesPath>/backend-sidecar/youtab-backend[.exe]   (frozen exe)
//     <resourcesPath>/backend-sidecar/…                      (onedir payload)
// Dev: the sidecar is the build-sidecar.mjs output under the repo:
//     <repoRoot>/apps/desktop/build/backend-sidecar/dist/youtab-backend/
import path from 'node:path'

export interface SidecarPaths {
  /** Directory whose tree the root digest is computed over. */
  bundleDir: string
  /** Absolute path to the frozen backend executable. */
  executable: string
  /** Mode used to resolve the paths (for logging/telemetry). */
  mode: 'packaged' | 'dev'
}

export interface ResolveSidecarInput {
  isPackaged: boolean
  /** process.resourcesPath (packaged) — may be undefined in dev. */
  resourcesPath?: string | null
  /** Repo root for dev resolution (…/apps/desktop up to the monorepo root). */
  repoRoot?: string | null
  platform?: NodeJS.Platform
}

const BUNDLE_NAME = 'youtab-backend'

/** Platform-correct executable filename for the frozen backend. */
export function sidecarExeName(platform: NodeJS.Platform = process.platform): string {
  return platform === 'win32' ? `${BUNDLE_NAME}.exe` : BUNDLE_NAME
}

/**
 * Resolve sidecar paths. Returns null when the inputs required for the chosen
 * mode are missing (e.g. packaged without a resourcesPath, or dev without a
 * repoRoot) — the caller then falls through to its normal resolution chain.
 */
export function resolveSidecarPaths({
  isPackaged,
  resourcesPath,
  repoRoot,
  platform = process.platform
}: ResolveSidecarInput): SidecarPaths | null {
  const exe = sidecarExeName(platform)

  if (isPackaged) {
    if (!resourcesPath) {
      return null
    }

    const bundleDir = path.join(resourcesPath, 'backend-sidecar')

    return { bundleDir, executable: path.join(bundleDir, exe), mode: 'packaged' }
  }

  if (!repoRoot) {
    return null
  }

  const bundleDir = path.join(repoRoot, 'apps', 'desktop', 'build', 'backend-sidecar', 'dist', BUNDLE_NAME)

  return { bundleDir, executable: path.join(bundleDir, exe), mode: 'dev' }
}
