// Canonical root-digest algorithm for the self-contained backend sidecar.
//
// SINGLE SOURCE OF TRUTH. The build (`build-sidecar.mjs`), the offline verifier
// (`verify-sidecar.mjs`), and the Electron trusted-binding check
// (`electron/sidecar-integrity.ts`) all compute the bundle's root digest from
// THIS module, so a bundle frozen at build time and re-hashed at spawn time can
// never disagree because of a drifted second implementation.
//
// root_digest = sha256 over the sorted, newline-joined "sha256␠relpath" lines
// of every file in the bundle tree, with a trailing newline. Relative paths use
// forward slashes on every platform so the digest is path-separator stable.
//
// Pure and dependency-free (node builtins only) so it is trivially unit-testable
// and safe to bundle into the Electron main process.
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

/** sha256 hex of a file's bytes. */
export function sha256File(p) {
  return createHash('sha256').update(readFileSync(p)).digest('hex')
}

/** sha256 hex of a string (utf8). */
export function sha256String(s) {
  return createHash('sha256').update(s).digest('hex')
}

/** Recursively list every file (not directory) under `dir`, absolute paths. */
export function walkFiles(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)

    if (statSync(p).isDirectory()) {
      walkFiles(p, acc)
    } else {
      acc.push(p)
    }
  }

  return acc
}

/** Normalize an absolute path to a bundle-relative, forward-slash path. */
export function toRelPosix(bundleRoot, absPath) {
  return relative(bundleRoot, absPath).split('\\').join('/')
}

/**
 * Per-file manifest entries `{ path, bytes, sha256 }`, sorted by absolute path
 * (stable across runs on the same OS; forward-slash relative paths make the
 * digest identical across OSes for an identical tree).
 */
export function manifestEntriesFromBundle(bundleRoot) {
  const files = walkFiles(bundleRoot).sort()

  return files.map(p => ({
    path: toRelPosix(bundleRoot, p),
    bytes: statSync(p).size,
    sha256: sha256File(p)
  }))
}

/**
 * Compute the root digest from already-computed manifest entries. Order matters
 * and MUST be the sorted order produced by `manifestEntriesFromBundle`.
 */
export function rootDigestFromEntries(entries) {
  const lines = entries.map(f => `${f.sha256} ${f.path}`).join('\n') + '\n'

  return sha256String(lines)
}

/** Compute the root digest for a bundle directory on disk. */
export function rootDigestFromBundle(bundleRoot) {
  return rootDigestFromEntries(manifestEntriesFromBundle(bundleRoot))
}

/** Parse the committed `sidecar-root-digest.txt` value (trims trailing newline). */
export function parseTrustedDigest(text) {
  const v = String(text || '').trim()

  return /^[0-9a-f]{64}$/.test(v) ? v : null
}
