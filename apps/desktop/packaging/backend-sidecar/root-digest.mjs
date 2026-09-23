// Canonical root-digest algorithm for the self-contained backend sidecar.
//
// SINGLE SOURCE OF TRUTH. The build (`build-sidecar.mjs`), the offline verifier
// (`verify-sidecar.mjs`), and the Electron trusted-binding check
// (`electron/sidecar-integrity.ts`) all compute the bundle's root digest from
// THIS module, so a bundle frozen at build time and re-hashed at spawn time can
// never disagree because of a drifted second implementation.
//
// root_digest = sha256 over sorted, newline-joined manifest lines with a
// trailing newline. Regular files use "sha256␠relpath"; symlinks use
// "L␠sha256␠relpath" with their hash binding the resolved in-bundle target.
// Directory aliases are not recursively traversed. Relative paths use forward
// slashes on every platform so the digest is path-separator stable.
//
// Pure and dependency-free (node builtins only) so it is trivially unit-testable
// and safe to bundle into the Electron main process.
import { createHash } from 'node:crypto'
import { closeSync, constants, fstatSync, lstatSync, openSync, readlinkSync, readdirSync, readSync, realpathSync } from 'node:fs'
import { isAbsolute, join, relative, sep } from 'node:path'

function inside(root, target) {
  const rel = relative(root, target)
  return rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel)
}

function inspectLink(path, rootReal) {
  const before = lstatSync(path)
  if (!before.isSymbolicLink()) throw new Error(`bundle entry changed type: ${path}`)
  const linkText = readlinkSync(path)
  const target = realpathSync(path)
  if (!inside(rootReal, target)) throw new Error(`bundle symlink escapes root: ${path}`)
  const targetStat = lstatSync(target)
  if (!targetStat.isDirectory() && !targetStat.isFile()) {
    throw new Error(`bundle symlink has unsupported target: ${path}`)
  }
  const targetKind = targetStat.isDirectory() ? 'directory' : 'file'
  const after = lstatSync(path)
  if (!after.isSymbolicLink() || readlinkSync(path) !== linkText) {
    throw new Error(`bundle symlink changed during inspection: ${path}`)
  }
  return { path, target: relative(rootReal, target).split('\\').join('/'), targetKind, linkText }
}

/**
 * Inspect a static build artifact without following directory symlink aliases.
 * Pre/post checks detect persistent changes; Node path APIs cannot make an
 * attacker-writable, concurrently mutated tree an immutable snapshot.
 */
export function collectBundleTree(bundleRoot) {
  const rootReal = realpathSync(bundleRoot)
  const files = []
  const links = []
  const dirs = []
  const graph = new Map()
  function visit(dir) {
    const realDir = realpathSync(dir)
    if (!inside(rootReal, realDir) || !lstatSync(dir).isDirectory()) {
      throw new Error(`bundle directory changed or escaped root: ${dir}`)
    }
    const edges = []
    graph.set(realDir, edges)
    dirs.push(dir)
    for (const name of readdirSync(dir)) {
      const path = join(dir, name)
      const kind = lstatSync(path)
      if (kind.isSymbolicLink()) {
        const link = inspectLink(path, rootReal)
        links.push(link)
        if (link.targetKind === 'directory') edges.push(join(rootReal, link.target))
      } else if (kind.isDirectory()) {
        edges.push(realpathSync(path))
        visit(path)
      } else if (kind.isFile()) {
        files.push(path)
      } else {
        throw new Error(`unsupported bundle entry: ${path}`)
      }
    }
    if (!lstatSync(dir).isDirectory() || realpathSync(dir) !== realDir) {
      throw new Error(`bundle directory changed during inspection: ${dir}`)
    }
  }
  visit(bundleRoot)
  const active = new Set()
  const done = new Set()
  function checkCycles(dir) {
    if (active.has(dir)) throw new Error(`cyclic bundle directory symlink: ${dir}`)
    if (done.has(dir)) return
    active.add(dir)
    for (const next of graph.get(dir) || []) checkCycles(next)
    active.delete(dir)
    done.add(dir)
  }
  checkCycles(rootReal)
  return { files, links, dirs, rootReal }
}

/** Recheck that an inspected alias still has the same type and in-bundle target. */
export function validateBundleLinks(links, rootReal) {
  for (const link of links) {
    const current = inspectLink(link.path, rootReal)
    if (current.target !== link.target || current.targetKind !== link.targetKind || current.linkText !== link.linkText) {
      throw new Error(`bundle symlink changed during scan: ${link.path}`)
    }
  }
}

/** Detect persistent additions, removals, type flips, or retargeting during a scan. */
export function validateBundleTree(tree, bundleRoot) {
  const current = collectBundleTree(bundleRoot)
  const paths = list => list.map(path => relative(tree.rootReal, path)).sort()
  const links = list => list.map(link => [relative(tree.rootReal, link.path), link.target, link.targetKind, link.linkText])
    .sort((a, b) => a[0].localeCompare(b[0]))
  if (current.rootReal !== tree.rootReal ||
      JSON.stringify(paths(current.files)) !== JSON.stringify(paths(tree.files)) ||
      JSON.stringify(paths(current.dirs)) !== JSON.stringify(paths(tree.dirs)) ||
      JSON.stringify(links(current.links)) !== JSON.stringify(links(tree.links))) {
    throw new Error('bundle tree changed during scan')
  }
}

function hashFile(p) {
  let fd
  try {
    fd = openSync(p, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
    const before = fstatSync(fd)
    if (!before.isFile()) throw new Error(`bundle entry is not a file: ${p}`)
    const hash = createHash('sha256')
    const chunk = Buffer.allocUnsafe(64 * 1024)
    let remaining = before.size
    while (remaining > 0) {
      const n = readSync(fd, chunk, 0, Math.min(chunk.length, remaining), null)
      if (n === 0) throw new Error(`bundle file shortened during hashing: ${p}`)
      hash.update(chunk.subarray(0, n))
      remaining -= n
    }
    const after = fstatSync(fd)
    if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || !lstatSync(p).isFile()) {
      throw new Error(`bundle file changed during hashing: ${p}`)
    }
    return { bytes: before.size, sha256: hash.digest('hex') }
  } finally {
    if (fd !== undefined) closeSync(fd)
  }
}

/** sha256 hex of a file's bytes. */
export function sha256File(p) {
  return hashFile(p).sha256
}

/** sha256 hex of a string (utf8). */
export function sha256String(s) {
  return createHash('sha256').update(s).digest('hex')
}

/** List physical files and symlinks under `dir`, without following directory aliases. */
export function walkFiles(dir, acc = []) {
  const tree = collectBundleTree(dir)
  acc.push(...tree.files, ...tree.links.map(link => link.path))
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
  const tree = collectBundleTree(bundleRoot)
  const links = new Map(tree.links.map(link => [link.path, link]))
  // Preserve the original native absolute-path sort for no-symlink bundles.
  const entries = [...tree.files, ...links.keys()].sort().map(p => {
    const link = links.get(p)
    return link
      ? { path: toRelPosix(bundleRoot, p), bytes: 0, sha256: sha256String(`symlink\0${link.target}`), type: 'symlink', target: link.target }
      : { path: toRelPosix(bundleRoot, p), ...hashFile(p) }
  })
  validateBundleLinks(tree.links, tree.rootReal)
  validateBundleTree(tree, bundleRoot)
  return entries
}

/**
 * Compute the root digest from already-computed manifest entries. Order matters
 * and MUST be the sorted order produced by `manifestEntriesFromBundle`.
 */
export function rootDigestFromEntries(entries) {
  // The L prefix separates link metadata from file content. Legacy bundles
  // containing only regular files retain their original digest exactly.
  const lines = entries.map(f => f.type === 'symlink' ? `L ${f.sha256} ${f.path}` : `${f.sha256} ${f.path}`).join('\n') + '\n'

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
