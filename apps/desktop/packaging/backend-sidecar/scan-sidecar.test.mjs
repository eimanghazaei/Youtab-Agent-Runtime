// Positive/negative self-tests for the A2 bundle security scanner.
// Builds synthetic bundle fixtures (no PyInstaller freeze needed) and asserts
// each rule fires on the bad case and stays silent on the clean case.
//
// Run: npx vitest run --project electron packaging/backend-sidecar/scan-sidecar.test.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, test } from 'vitest'

import { EICAR, blockingCount, scanBundle } from './scan-sidecar.mjs'

const dirs = []
function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), 'scan-fx-'))
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

const rules = f => f.findings.map(x => x.rule)

test('NEGATIVE: a clean bundle produces zero blocking findings', () => {
  const root = fixture({
    'youtab-backend.exe': 'MZ\u0000binary-ish',
    'cacert.pem': '-----BEGIN CERTIFICATE-----\npublic ca bundle\n-----END CERTIFICATE-----',
    'README.txt': 'Uses a .venv and worktrees in dev docs — bare tokens must not trip the scanner.',
    '_internal/lib.pyc': '\u0000\u0000compiled'
  })
  const r = scanBundle(root)
  assert.equal(blockingCount(r.findings), 0, JSON.stringify(r.findings))
})

test('POSITIVE: EICAR test signature is a critical finding', () => {
  const r = scanBundle(fixture({ 'sample.txt': `harmless\n${EICAR}\n` }))
  assert.ok(rules(r).includes('eicar'))
  assert.ok(r.findings.some(x => x.severity === 'critical'))
})

test('POSITIVE: a real private key by content is flagged', () => {
  const r = scanBundle(fixture({ 'leaked.txt': '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----' }))
  assert.ok(rules(r).includes('secret-material'))
  assert.ok(blockingCount(r.findings) > 0)
})

test('POSITIVE: cloud/API tokens are flagged', () => {
  const r = scanBundle(fixture({ 'a.env-sample': 'ghp_0123456789abcdefghijklmnopqrstuvwx' }))
  assert.ok(rules(r).includes('secret-material'))
})

test('POSITIVE: developer machine-local absolute paths are flagged', () => {
  const r = scanBundle(fixture({ 'trace.txt': 'File "C:\\\\Users\\\\eiman\\\\worktrees\\\\rt-px-dep\\\\x.py"' }))
  assert.ok(rules(r).includes('machine-local-path'))
})

test('POSITIVE: forbidden filenames (.env, .log, key stores) are flagged', () => {
  const r = scanBundle(fixture({ '.env': 'X=1', 'run.log': 'boot', 'keys.pfx': 'store' }))
  const names = r.findings.filter(x => x.rule === 'forbidden-filename').map(x => x.file)
  assert.ok(names.includes('.env'))
  assert.ok(names.includes('run.log'))
  assert.ok(names.includes('keys.pfx'))
})

test('POSITIVE: forbidden AI/Claude attribution is flagged', () => {
  // Build the trigger from parts so the literal attribution never appears
  // verbatim in the source tree (mirrors the split EICAR constant).
  const attribution = 'Co-Authored' + '-By: ' + 'Claude Opus (noreply@' + 'anthropic' + '.com)'
  const r = scanBundle(fixture({ 'notice.txt': attribution }))
  assert.ok(rules(r).includes('forbidden-attribution'))
})

test('POSITIVE: case-insensitive attribution host substring is flagged', () => {
  const r = scanBundle(fixture({ 'notice.txt': 'reference: HTTPS://docs.AnThRoPiC.CoM/legal' }))
  assert.ok(r.findings.some(x => x.rule === 'forbidden-attribution' && x.detail === 'anthropic.com'))
})

test('POSITIVE: in-bundle file and directory symlinks remain valid without duplicate traversal', () => {
  const root = fixture({ 'nested/key.txt': '-----BEGIN OPENSSH PRIVATE KEY-----\nabc' })
  try {
    symlinkSync(join(root, 'nested', 'key.txt'), join(root, 'key-alias.txt'))
    symlinkSync(join(root, 'nested'), join(root, 'nested-alias'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return // Windows without symlink privilege
    throw error
  }
  const r = scanBundle(root)
  assert.equal(r.scanned_files, 1)
  assert.equal(r.findings.filter(x => x.rule === 'secret-material').length, 1)
  assert.equal(r.findings.filter(x => x.rule === 'unsafe-symlink').length, 0)
})

test('POSITIVE: a symlink cannot redirect content scanning outside the bundle', () => {
  const outside = fixture({ 'secret.txt': 'ordinary content' })
  const root = fixture({ 'safe.txt': 'ordinary content' })
  try {
    symlinkSync(join(outside, 'secret.txt'), join(root, 'redirect.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return // Windows without symlink privilege
    throw error
  }
  const r = scanBundle(root)
  assert.ok(r.findings.some(x => x.rule === 'unsafe-bundle-tree'))
  assert.ok(blockingCount(r.findings) > 0)
})

test('POSITIVE: dangling and cyclic symlinks are blocking', () => {
  const root = fixture({ 'safe.txt': 'ordinary content' })
  try {
    symlinkSync(join(root, 'missing.txt'), join(root, 'dangling.txt'))
    symlinkSync(join(root, 'cycle-b.txt'), join(root, 'cycle-a.txt'))
    symlinkSync(join(root, 'cycle-a.txt'), join(root, 'cycle-b.txt'))
  } catch (error) {
    if (error.code === 'EPERM') return // Windows without symlink privilege
    throw error
  }
  const r = scanBundle(root)
  assert.ok(r.findings.some(x => x.rule === 'unsafe-bundle-tree'))
  assert.ok(blockingCount(r.findings) > 0)
})

test('POSITIVE: directory symlink back to bundle root is blocking', () => {
  const root = fixture({ 'nested/safe.txt': 'ordinary content' })
  try {
    symlinkSync(root, join(root, 'nested', 'back'), 'dir')
  } catch (error) {
    if (error.code === 'EPERM') return
    throw error
  }
  const r = scanBundle(root)
  assert.ok(r.findings.some(x => x.rule === 'unsafe-bundle-tree'))
  assert.ok(blockingCount(r.findings) > 0)
})

test('NEGATIVE: public CA bundle .pem is NOT flagged by extension', () => {
  const r = scanBundle(fixture({ 'certifi/cacert.pem': '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----' }))
  assert.equal(r.findings.filter(x => x.rule === 'forbidden-filename').length, 0)
  assert.equal(r.findings.filter(x => x.rule === 'secret-material').length, 0)
})
