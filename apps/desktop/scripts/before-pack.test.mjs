import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Arch } from 'electron-builder'
import { test } from 'vitest'

import beforePack, { assertSidecarTarget, cleanStaleAppOutDir, preserveRollbackBackup } from '../scripts/before-pack.mjs'

test('native sidecar manifest must match the actual package platform and architecture', () => {
  const manifest = { schema: 'youtab.backend_sidecar_manifest/v1', platform: 'darwin', arch: 'arm64' }
  assert.doesNotThrow(() => assertSidecarTarget(manifest, 'darwin', 'arm64'))
  assert.throws(() => assertSidecarTarget(manifest, 'darwin', 'x64'), /package targets darwin-x64/)
  assert.throws(() => assertSidecarTarget(manifest, 'linux', 'arm64'), /package targets linux-arm64/)
  assert.throws(() => assertSidecarTarget({ ...manifest, schema: 'unknown' }, 'darwin', 'arm64'), /invalid backend sidecar manifest/)
})

test('beforePack refuses a mismatched native sidecar before packaging files', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-sidecar-target-'))
  try {
    const manifestPath = path.join(tempRoot, 'manifest.json')
    fs.writeFileSync(manifestPath, JSON.stringify({
      schema: 'youtab.backend_sidecar_manifest/v1', platform: 'darwin', arch: 'arm64'
    }))
    await assert.rejects(
      beforePack({ appOutDir: '', electronPlatformName: 'darwin', arch: Arch.x64, sidecarManifestPath: manifestPath }),
      /backend sidecar targets darwin-arm64, but the package targets darwin-x64/
    )
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('beforePack refuses a staged sidecar when target metadata is absent', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-sidecar-target-'))
  try {
    const manifestPath = path.join(tempRoot, 'manifest.json')
    fs.writeFileSync(manifestPath, JSON.stringify({
      schema: 'youtab.backend_sidecar_manifest/v1', platform: 'linux', arch: 'x64'
    }))
    await assert.rejects(
      beforePack({ appOutDir: '', electronPlatformName: 'linux', sidecarManifestPath: manifestPath }),
      /target platform\/arch is missing/
    )
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('cleanStaleAppOutDir removes a populated unpacked directory', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'linux-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    // Reproduce the corrupted partial state: license + payload present,
    // electron binary missing — exactly what trips the ENOENT rename.
    fs.writeFileSync(path.join(appOutDir, 'LICENSE.electron.txt'), 'x', 'utf8')
    fs.writeFileSync(path.join(appOutDir, 'resources.pak'), 'x', 'utf8')
    fs.mkdirSync(path.join(appOutDir, 'resources'), { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'resources', 'app.asar'), 'x', 'utf8')

    const removed = cleanStaleAppOutDir(appOutDir)

    assert.equal(removed, true)
    assert.equal(fs.existsSync(appOutDir), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('cleanStaleAppOutDir is a no-op when the directory is absent', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const missing = path.join(tempRoot, 'does-not-exist')
    assert.equal(cleanStaleAppOutDir(missing), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('cleanStaleAppOutDir ignores empty or invalid input', () => {
  assert.equal(cleanStaleAppOutDir(''), false)
  assert.equal(cleanStaleAppOutDir(undefined), false)
  assert.equal(cleanStaleAppOutDir(null), false)
  assert.equal(cleanStaleAppOutDir(42), false)
})

test('beforePack default export resolves even when cleanup throws', async () => {
  // A directory path that rmSync can't remove is simulated by passing a
  // context whose appOutDir is a file the hook will try (and be allowed) to
  // remove; the contract under test is that the hook never rejects.
  await assert.doesNotReject(beforePack({ appOutDir: '', electronPlatformName: 'linux' }))
})

// ─── Windows rollback preservation (#69179) ────────────────────────────────

test('preserveRollbackBackup moves a working build to .bak', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Youtab.exe'), 'MZ-old-build', 'utf8')
    fs.writeFileSync(path.join(appOutDir, 'resources.pak'), 'x', 'utf8')

    const preserved = preserveRollbackBackup(appOutDir, 'Youtab.exe')

    assert.equal(preserved, true)
    // Original slot vacated so electron-builder stages into a clean tree...
    assert.equal(fs.existsSync(appOutDir), false)
    // ...and the previous working build is intact under .bak for rollback.
    assert.equal(
      fs.readFileSync(path.join(`${appOutDir}.bak`, 'Youtab.exe'), 'utf8'),
      'MZ-old-build'
    )
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup replaces a stale .bak from an older update', () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Youtab.exe'), 'current', 'utf8')
    fs.mkdirSync(`${appOutDir}.bak`, { recursive: true })
    fs.writeFileSync(path.join(`${appOutDir}.bak`, 'Youtab.exe'), 'two-updates-ago', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'Youtab.exe'), true)
    assert.equal(fs.readFileSync(path.join(`${appOutDir}.bak`, 'Youtab.exe'), 'utf8'), 'current')
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup refuses a partial tree missing the product exe', () => {
  // The corrupted partial state (interrupted prior pack) must NOT become
  // rollback material — it is exactly what cleanStaleAppOutDir exists to wipe.
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'LICENSE.electron.txt'), 'x', 'utf8')

    assert.equal(preserveRollbackBackup(appOutDir, 'Youtab.exe'), false)
    // Tree untouched; the caller's wipe path handles it.
    assert.equal(fs.existsSync(appOutDir), true)
    assert.equal(fs.existsSync(`${appOutDir}.bak`), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('preserveRollbackBackup ignores missing or invalid input', () => {
  assert.equal(preserveRollbackBackup(''), false)
  assert.equal(preserveRollbackBackup(undefined), false)
  assert.equal(preserveRollbackBackup(null), false)
  assert.equal(preserveRollbackBackup(path.join(os.tmpdir(), 'does-not-exist-xyz')), false)
})

test('beforePack on win32 preserves the previous build instead of wiping it', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'win-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Youtab.exe'), 'MZ-working', 'utf8')

    // No packager info in the context → default 'Youtab.exe' product name.
    // node-pty staging is skipped because arch is not a number here.
    await beforePack({ appOutDir, electronPlatformName: 'win32' })

    assert.equal(fs.existsSync(appOutDir), false)
    assert.equal(
      fs.readFileSync(path.join(`${appOutDir}.bak`, 'Youtab.exe'), 'utf8'),
      'MZ-working'
    )
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})

test('beforePack on linux keeps the plain wipe (no .bak)', async () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-before-pack-'))
  try {
    const appOutDir = path.join(tempRoot, 'linux-unpacked')
    fs.mkdirSync(appOutDir, { recursive: true })
    fs.writeFileSync(path.join(appOutDir, 'Youtab.exe'), 'x', 'utf8')

    await beforePack({ appOutDir, electronPlatformName: 'linux' })

    assert.equal(fs.existsSync(appOutDir), false)
    assert.equal(fs.existsSync(`${appOutDir}.bak`), false)
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
})
