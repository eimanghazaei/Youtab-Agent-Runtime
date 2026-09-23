/**
 * Tests for electron/sidecar-resolve.ts (packaged vs dev path resolution).
 * Run: npx vitest run --project electron electron/sidecar-resolve.test.ts
 */
import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { resolveSidecarPaths, sidecarExeName } from './sidecar-resolve'

test('sidecarExeName is .exe on win32, bare elsewhere', () => {
  assert.equal(sidecarExeName('win32'), 'youtab-backend.exe')
  assert.equal(sidecarExeName('linux'), 'youtab-backend')
  assert.equal(sidecarExeName('darwin'), 'youtab-backend')
})

test('packaged mode resolves under process.resourcesPath/backend-sidecar', () => {
  const r = resolveSidecarPaths({ isPackaged: true, resourcesPath: '/opt/app/resources', platform: 'linux' })
  assert.ok(r)
  assert.equal(r!.mode, 'packaged')
  assert.equal(r!.bundleDir, path.join('/opt/app/resources', 'backend-sidecar'))
  assert.equal(r!.executable, path.join('/opt/app/resources', 'backend-sidecar', 'youtab-backend'))
})

test('packaged mode on win32 uses the .exe name', () => {
  const r = resolveSidecarPaths({ isPackaged: true, resourcesPath: 'C:\\app\\resources', platform: 'win32' })
  assert.ok(r!.executable.endsWith('youtab-backend.exe'))
})

test('packaged mode without resourcesPath returns null (caller falls through)', () => {
  assert.equal(resolveSidecarPaths({ isPackaged: true, resourcesPath: null }), null)
})

test('dev mode resolves under the repo build output', () => {
  const r = resolveSidecarPaths({ isPackaged: false, repoRoot: '/repo', platform: 'linux' })
  assert.ok(r)
  assert.equal(r!.mode, 'dev')
  assert.equal(
    r!.bundleDir,
    path.join('/repo', 'apps', 'desktop', 'build', 'backend-sidecar', 'dist', 'youtab-backend')
  )
})

test('dev mode without repoRoot returns null', () => {
  assert.equal(resolveSidecarPaths({ isPackaged: false, repoRoot: null }), null)
})
