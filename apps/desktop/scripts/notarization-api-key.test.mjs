import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, test, vi } from 'vitest'
import { execFile } from 'node:child_process'
import { resolveApiKeyPath } from './notarization-api-key.mjs'
import notarize from './notarize.mjs'

vi.mock('node:child_process', () => ({ execFile: vi.fn() }))

// Deliberately invalid as an Apple key; tests never contact Apple or execute tools.
const inlineKey = '-----BEGIN PRIVATE KEY-----\nsynthetic-notary-test-only\n-----END PRIVATE KEY-----'
let root

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-notary-test-'))
  vi.spyOn(os, 'tmpdir').mockReturnValue(root)
  vi.stubEnv('APPLE_NOTARY_PROFILE', '')
  vi.stubEnv('APPLE_API_KEY', inlineKey)
  vi.stubEnv('APPLE_API_KEY_ID', 'synthetic-key-id')
  vi.stubEnv('APPLE_API_ISSUER', 'synthetic-issuer')
  execFile.mockReset()
  execFile.mockImplementation((_command, _args, callback) => callback(null, '', ''))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllEnvs()
  assert.equal(path.dirname(root), path.resolve(os.tmpdir()))
  assert.ok(path.basename(root).startsWith('youtab-notary-test-'))
  fs.rmSync(root, { recursive: true, force: true })
})

test('inline keys use distinct private directories and exclusive owner-only creation', () => {
  const write = vi.spyOn(fs, 'writeFileSync')
  const first = resolveApiKeyPath(inlineKey)
  const second = resolveApiKeyPath(inlineKey)
  try {
    assert.notEqual(path.dirname(first.keyPath), path.dirname(second.keyPath))
    assert.equal(path.dirname(path.dirname(first.keyPath)), root)
    assert.equal(fs.readFileSync(first.keyPath, 'utf8'), inlineKey)
    assert.equal(write.mock.calls[0][2].flag, 'wx')
    assert.equal(write.mock.calls[0][2].mode, 0o600)
  } finally {
    first.cleanup()
    second.cleanup()
  }
  first.cleanup() // Cleanup is idempotent after its owned directory is gone.
  assert.deepEqual(fs.readdirSync(root), [])
})

test.skipIf(process.platform === 'win32')('POSIX directory and key modes are 0700 and 0600', () => {
  const { keyPath, cleanup } = resolveApiKeyPath(inlineKey)
  try {
    assert.equal(fs.statSync(path.dirname(keyPath)).mode & 0o777, 0o700)
    assert.equal(fs.statSync(keyPath).mode & 0o777, 0o600)
  } finally {
    cleanup()
  }
})

test('existing key-file paths are retained and never removed', () => {
  const keyPath = path.join(root, 'existing-key.p8')
  fs.writeFileSync(keyPath, inlineKey, { mode: 0o600 })
  const resolved = resolveApiKeyPath(`  ${keyPath}  `)
  assert.equal(resolved.keyPath, keyPath)
  resolved.cleanup()
  assert.equal(fs.readFileSync(keyPath, 'utf8'), inlineKey)
  assert.deepEqual(fs.readdirSync(root), ['existing-key.p8'])
})

test('empty or invalid values do not create temporary credentials', () => {
  const empty = resolveApiKeyPath(' ')
  assert.equal(empty.keyPath, '')
  empty.cleanup()
  assert.throws(() => resolveApiKeyPath('missing-key.p8'), /file path or inline/)
  assert.deepEqual(fs.readdirSync(root), [])
})

test('exclusive creation refuses a colliding hard link without writing its external target', () => {
  const outside = path.join(root, 'external-file')
  fs.writeFileSync(outside, 'external sentinel')
  const originalWrite = fs.writeFileSync
  vi.spyOn(fs, 'writeFileSync').mockImplementation((file, value, options) => {
    fs.linkSync(outside, file)
    return originalWrite(file, value, options)
  })
  assert.throws(() => resolveApiKeyPath(inlineKey), { code: 'EEXIST' })
  assert.equal(fs.readFileSync(outside, 'utf8'), 'external sentinel')
  assert.deepEqual(fs.readdirSync(root), ['external-file'])
})

test.skipIf(process.platform === 'win32')('exclusive creation refuses a precreated symlink', () => {
  const outside = path.join(root, 'external-file')
  fs.writeFileSync(outside, 'external sentinel')
  const originalWrite = fs.writeFileSync
  vi.spyOn(fs, 'writeFileSync').mockImplementation((file, value, options) => {
    fs.symlinkSync(outside, file)
    return originalWrite(file, value, options)
  })
  assert.throws(() => resolveApiKeyPath(inlineKey), { code: 'EEXIST' })
  assert.equal(fs.readFileSync(outside, 'utf8'), 'external sentinel')
  assert.deepEqual(fs.readdirSync(root), ['external-file'])
})

test('failed writes clean only the owned directory even after a partial write', () => {
  const outside = path.join(root, 'external-file')
  fs.writeFileSync(outside, 'external sentinel')
  const originalWrite = fs.writeFileSync
  vi.spyOn(fs, 'writeFileSync').mockImplementation((file, value, options) => {
    originalWrite(file, value, options)
    throw new Error('synthetic write failure')
  })
  assert.throws(() => resolveApiKeyPath(inlineKey), /synthetic write failure/)
  assert.equal(fs.readFileSync(outside, 'utf8'), 'external sentinel')
  assert.deepEqual(fs.readdirSync(root), ['external-file'])
})

test('cleanup refuses a replaced directory and retains the replacement contents', () => {
  const { keyPath, cleanup } = resolveApiKeyPath(inlineKey)
  const directory = path.dirname(keyPath)
  const moved = `${directory}-moved`
  fs.renameSync(directory, moved)
  fs.mkdirSync(directory)
  const replacement = path.join(directory, 'external-file')
  fs.writeFileSync(replacement, 'external sentinel')
  assert.throws(cleanup, /replaced notarization directory/)
  assert.equal(fs.readFileSync(replacement, 'utf8'), 'external sentinel')
  assert.equal(fs.readFileSync(path.join(moved, 'AuthKey.p8'), 'utf8'), inlineKey)
})

test('notarize hook preserves command arguments and cleans credentials on command failure', async () => {
  const appPath = path.join(root, 'Synthetic.app')
  fs.mkdirSync(appPath)
  let keyPath
  execFile.mockImplementation((command, args, callback) => {
    if (command === 'xcrun') {
      keyPath = args[args.indexOf('--key') + 1]
      assert.equal(fs.readFileSync(keyPath, 'utf8'), inlineKey)
      assert.ok(args.includes('--key-id') && args.includes('--issuer') && args.includes('--wait'))
      callback(new Error('synthetic notary failure'), '', '')
      return
    }
    callback(null, '', '')
  })
  await assert.rejects(
    notarize({ electronPlatformName: 'darwin', appOutDir: root, packager: { appInfo: { productFilename: 'Synthetic' } } }),
    /synthetic notary failure/
  )
  assert.ok(keyPath)
  assert.equal(fs.existsSync(path.dirname(keyPath)), false)
  assert.deepEqual(execFile.mock.calls.map(([command]) => command), ['ditto', 'xcrun'])
  assert.deepEqual(fs.readdirSync(root), ['Synthetic.app'])
})

test('keychain profile retains precedence without creating an inline key', async () => {
  fs.mkdirSync(path.join(root, 'Synthetic.app'))
  vi.stubEnv('APPLE_NOTARY_PROFILE', 'synthetic-profile')
  await notarize({ electronPlatformName: 'darwin', appOutDir: root, packager: { appInfo: { productFilename: 'Synthetic' } } })
  assert.ok(execFile.mock.calls[1][1].includes('--keychain-profile'))
  assert.ok(!execFile.mock.calls[1][1].includes('--key'))
  assert.deepEqual(fs.readdirSync(root), ['Synthetic.app'])
})

test('artifact entrypoint cleans inline credentials on mocked notary failure', async () => {
  const artifact = path.join(root, 'Synthetic.dmg')
  fs.writeFileSync(artifact, 'synthetic artifact')
  const originalArgv = process.argv
  process.argv = [originalArgv[0], 'notarize-artifact.mjs', artifact]
  const exit = vi.spyOn(process, 'exit').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  let keyPath
  execFile.mockImplementation((_command, args, callback) => {
    keyPath = args[args.indexOf('--key') + 1]
    assert.equal(fs.readFileSync(keyPath, 'utf8'), inlineKey)
    callback(new Error('synthetic notary failure'), '', '')
  })
  try {
    await import('./notarize-artifact.mjs')
    await vi.waitFor(() => assert.equal(exit.mock.calls[0]?.[0], 1))
    assert.equal(fs.existsSync(path.dirname(keyPath)), false)
    assert.deepEqual(fs.readdirSync(root), ['Synthetic.dmg'])
  } finally {
    process.argv = originalArgv
  }
})
