// Determinism/tamper tests for the canonical sidecar root-digest algorithm.
// Run: npx vitest run --project electron packaging/backend-sidecar/root-digest.test.mjs
import assert from 'node:assert/strict'
import { cpSync, mkdtempSync, mkdirSync, writeFileSync, rmSync, renameSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, join, relative } from 'node:path'
import { afterEach, test } from 'vitest'

import { collectBundleTree, manifestEntriesFromBundle, parseTrustedDigest, rootDigestFromBundle, rootDigestFromEntries, sha256String, validateBundleTree } from './root-digest.mjs'

const dirs = []
function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), 'rd-fx-'))
  dirs.push(root)
  for (const [rel, content] of Object.entries(files)) {
    const p = join(root, rel)
    mkdirSync(join(p, '..'), { recursive: true })
    writeFileSync(p, content)
  }
  return root
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop(), { recursive: true, force: true })
})

test('digest is deterministic across two identical trees', () => {
  // Binary-ish content built at runtime so the source stays pure ASCII text.
  const files = { 'a.txt': 'alpha', 'sub/b.txt': 'beta', 'sub/c.bin': String.fromCharCode(0, 1, 2) }
  const d1 = rootDigestFromBundle(fixture(files))
  const d2 = rootDigestFromBundle(fixture(files))
  assert.match(d1, /^[0-9a-f]{64}$/)
  assert.equal(d1, d2)
})

test('regular-file-only digest retains the original line format', () => {
  const root = fixture({ 'a.txt': 'alpha' })
  assert.equal(rootDigestFromBundle(root), sha256String(`${sha256String('alpha')} a.txt\n`))
})

test('digest changes when any file content changes (tamper detection)', () => {
  const base = rootDigestFromBundle(fixture({ 'a.txt': 'alpha', 'b.txt': 'beta' }))
  const tampered = rootDigestFromBundle(fixture({ 'a.txt': 'alpha', 'b.txt': 'BETA' }))
  assert.notEqual(base, tampered)
})

test('digest changes when a file is added or removed', () => {
  const two = rootDigestFromBundle(fixture({ 'a.txt': 'x', 'b.txt': 'y' }))
  const three = rootDigestFromBundle(fixture({ 'a.txt': 'x', 'b.txt': 'y', 'c.txt': 'z' }))
  assert.notEqual(two, three)
})

test('digest is independent of filesystem enumeration order (sorted)', () => {
  const forward = rootDigestFromBundle(fixture({ 'a.txt': '1', 'm.txt': '2', 'z.txt': '3' }))
  const reverse = rootDigestFromBundle(fixture({ 'z.txt': '3', 'm.txt': '2', 'a.txt': '1' }))
  assert.equal(forward, reverse)
})

test('rootDigestFromEntries matches rootDigestFromBundle for the same tree', () => {
  const root = fixture({ 'a.txt': 'alpha', 'sub/b.txt': 'beta' })
  assert.equal(rootDigestFromEntries(manifestEntriesFromBundle(root)), rootDigestFromBundle(root))
})

test('parseTrustedDigest accepts 64-hex, rejects junk', () => {
  const good = 'a'.repeat(64)
  assert.equal(parseTrustedDigest(`${good}\n`), good)
  assert.equal(parseTrustedDigest('not-a-digest'), null)
  assert.equal(parseTrustedDigest(''), null)
  assert.equal(parseTrustedDigest(null), null)
})

test('in-bundle symlink target is bound into digest without following directory aliases', () => {
  const root = fixture({ 'a.txt': 'same', 'b.txt': 'same', 'dir/c.txt': 'content' })
  try {
    symlinkSync('a.txt', join(root, 'alias.txt'))
    symlinkSync('dir', join(root, 'dir-alias'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  const before = rootDigestFromBundle(root)
  const entries = manifestEntriesFromBundle(root)
  assert.equal(entries.filter(entry => entry.type === 'symlink').length, 2)
  assert.equal(entries.length, 5)
  rmSync(join(root, 'alias.txt'))
  symlinkSync('b.txt', join(root, 'alias.txt'))
  assert.notEqual(rootDigestFromBundle(root), before)
})

test('digest rejects escaping and cyclic directory symlinks', () => {
  const outside = fixture({ 'file.txt': 'outside' })
  const root = fixture({ 'nested/file.txt': 'inside' })
  try {
    symlinkSync(relative(root, outside), join(root, 'escape'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  assert.throws(() => rootDigestFromBundle(root), /not portable within root/)
  rmSync(join(root, 'escape'))
  symlinkSync('..', join(root, 'nested', 'back'), 'dir')
  assert.throws(() => rootDigestFromBundle(root), /cyclic bundle directory symlink/)
})

test('digest rejects a directory alias cycle across siblings', () => {
  const root = fixture({ 'a/one.txt': 'one', 'b/two.txt': 'two' })
  try {
    symlinkSync('../b', join(root, 'a', 'to-b'), 'dir')
    symlinkSync('../a', join(root, 'b', 'to-a'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  assert.throws(() => rootDigestFromBundle(root), /cyclic bundle directory symlink/)
})

test('tree validation rejects a symlink replaced by an unscanned regular file', () => {
  const root = fixture({ 'target.txt': 'safe' })
  try {
    symlinkSync('target.txt', join(root, 'alias.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  const tree = collectBundleTree(root)
  rmSync(join(root, 'alias.txt'))
  writeFileSync(join(root, 'alias.txt'), 'new bytes')
  assert.throws(() => validateBundleTree(tree, root), /bundle tree changed/)
})

test('digest separates a symlink from a regular file with matching metadata bytes', () => {
  const root = fixture({ 'target.txt': 'safe', 'alias.txt': 'symlink\0target.txt' })
  const fileDigest = rootDigestFromBundle(root)
  rmSync(join(root, 'alias.txt'))
  try {
    symlinkSync('target.txt', join(root, 'alias.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  assert.notEqual(rootDigestFromBundle(root), fileDigest)
})

test('relative symlinks keep the same digest after moving the whole bundle', () => {
  const root = fixture({ 'lib/data.txt': 'data', 'lib/sub/other.txt': 'other' })
  try {
    symlinkSync('lib/data.txt', join(root, 'data-alias.txt'))
    symlinkSync('lib', join(root, 'lib-alias'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  const before = rootDigestFromBundle(root)
  const holder = mkdtempSync(join(tmpdir(), 'rd-move-'))
  dirs.push(holder)
  const moved = join(holder, 'relocated-bundle')
  renameSync(root, moved)
  assert.equal(rootDigestFromBundle(moved), before)
})

test('relative symlinks keep the same digest in a verbatim installer copy', () => {
  const root = fixture({ 'lib/data.txt': 'data' })
  try {
    symlinkSync('lib/data.txt', join(root, 'data-alias.txt'))
    symlinkSync('lib', join(root, 'lib-alias'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  const copied = mkdtempSync(join(tmpdir(), 'rd-copy-'))
  dirs.push(copied)
  const dest = join(copied, 'bundle')
  cpSync(root, dest, { recursive: true, verbatimSymlinks: true })
  assert.equal(rootDigestFromBundle(dest), rootDigestFromBundle(root))
})

test('absolute symlink resolving inside the build tree is rejected before packaging', () => {
  const root = fixture({ 'target.txt': 'safe' })
  try {
    symlinkSync(join(root, 'target.txt'), join(root, 'absolute-alias.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  assert.throws(() => rootDigestFromBundle(root), /symlink text is not portable/)
})

test('relative symlink that leaves and reenters the build root is rejected', () => {
  const root = fixture({ 'target.txt': 'safe' })
  try {
    symlinkSync(`../${basename(root)}/target.txt`, join(root, 'nonportable.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  assert.throws(() => rootDigestFromBundle(root), /symlink text is not portable/)
})
