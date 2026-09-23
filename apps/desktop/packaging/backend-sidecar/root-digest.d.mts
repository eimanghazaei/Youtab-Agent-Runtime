// Type declarations for root-digest.mjs so the Electron TS program can import
// the canonical algorithm without `allowJs`.
export interface SidecarManifestEntry {
  path: string
  bytes: number
  sha256: string
}

export function sha256File(p: string): string
export function sha256String(s: string): string
export function walkFiles(dir: string, acc?: string[]): string[]
export function toRelPosix(bundleRoot: string, absPath: string): string
export function manifestEntriesFromBundle(bundleRoot: string): SidecarManifestEntry[]
export function rootDigestFromEntries(entries: SidecarManifestEntry[]): string
export function rootDigestFromBundle(bundleRoot: string): string
export function parseTrustedDigest(text: string | null | undefined): string | null
