import type { YoutabFolderGrant, YoutabFolderGrantsBridge, YoutabFolderWriteOptions } from '@/global'

/**
 * Typed consumer for the Runtime folder-grant bridge
 * (RUNTIME_FRONTEND_CONTRACTS_v1.0 §1).
 *
 * The bridge is OPTIONAL on `window.youtabDesktop` — the Runtime/Electron
 * session owns the implementation and it is absent today. This module makes
 * REAL calls when the bridge is present and throws a typed
 * `FolderGrantsUnavailableError` when it is not. It NEVER fabricates a grant or
 * a success: the caller renders a truthful "Local Runtime required" state on
 * the unavailable error.
 *
 * No absolute path ever crosses this boundary — grants expose `safeLabel` only.
 */

/** Thrown by every consumer below when the folder-grant bridge is absent. */
export class FolderGrantsUnavailableError extends Error {
  readonly code = 'folder-grants-unavailable' as const

  constructor(message = 'Local Runtime folder-grant bridge is unavailable') {
    super(message)
    this.name = 'FolderGrantsUnavailableError'
  }
}

/** True when the Runtime folder-grant bridge is wired up on this build. */
export function isFolderGrantsAvailable(): boolean {
  return typeof window !== 'undefined' && !!window.youtabDesktop?.folderGrants
}

function bridge(): YoutabFolderGrantsBridge {
  const grants = typeof window !== 'undefined' ? window.youtabDesktop?.folderGrants : undefined

  if (!grants) {
    throw new FolderGrantsUnavailableError()
  }

  return grants
}

/**
 * Request a new folder grant. MUST be called from a user gesture — the Runtime
 * shows the native folder picker + approval prompt and only then returns the
 * grant. `readOnly` defaults to true; a read-write grant is opt-in.
 */
export async function requestFolderGrant({ readOnly = true }: { readOnly?: boolean } = {}): Promise<YoutabFolderGrant> {
  return bridge().request({ readOnly })
}

export async function listFolderGrants(): Promise<YoutabFolderGrant[]> {
  return bridge().list()
}

export async function revokeFolderGrant(grantId: string): Promise<{ revoked: true }> {
  return bridge().revoke(grantId)
}

export async function readInGrant(grantId: string, relPath: string): Promise<{ bytes: ArrayBuffer }> {
  return bridge().read(grantId, relPath)
}

export async function writeInGrant(
  grantId: string,
  relPath: string,
  bytes: ArrayBuffer,
  options?: YoutabFolderWriteOptions
): Promise<{ written: true; receiptId: string }> {
  return bridge().write(grantId, relPath, bytes, options)
}
