/**
 * Resolve the Electron binary Playwright's `_electron.launch` should exec.
 *
 * Pure + dependency-injected so it is unit-testable without a real filesystem
 * or a real Electron install. `fixtures.ts` wraps this with the real
 * `fs.existsSync` / `which`.
 *
 * Resolution order:
 *   1. `<repoRoot>/node_modules/electron/dist/electron.exe` — the Windows binary
 *      (prefer it so Playwright gets the real executable, never a shell/.cmd
 *      shim, which fails launch with "The system cannot find the path specified").
 *   2. `<repoRoot>/node_modules/electron/dist/electron` — the POSIX binary.
 *   3. `which electron` on PATH (nix devshell / global install).
 */

export interface ElectronPathDeps {
  /** Absolute repo root that holds the hoisted `node_modules`. */
  repoRoot: string
  /** Join path segments (inject `node:path`.join). */
  join: (...segments: string[]) => string
  /** True when a path exists on disk (inject `fs.existsSync`). */
  exists: (candidate: string) => boolean
  /** Resolve `electron` on PATH, or null when absent (wraps POSIX `which`). */
  whichElectron: () => string | null
}

export function resolveElectronBinary(deps: ElectronPathDeps): string {
  const localElectron = deps.join(deps.repoRoot, 'node_modules', 'electron', 'dist', 'electron')
  const localElectronExe = `${localElectron}.exe`

  if (deps.exists(localElectronExe)) {
    return localElectronExe
  }

  if (deps.exists(localElectron)) {
    return localElectron
  }

  const onPath = deps.whichElectron()

  if (onPath && onPath.trim()) {
    return onPath.trim()
  }

  throw new Error('Electron binary not found. Run "npm install" from the repo root to install devDependencies.')
}
