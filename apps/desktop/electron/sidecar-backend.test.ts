/**
 * Tests for electron/sidecar-backend.ts (packaged sidecar → backend descriptor).
 * Run: npx vitest run --project electron electron/sidecar-backend.test.ts
 */
import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { canUseDeveloperSourceOverride, resolvePackagedSidecarBackend } from './sidecar-backend'
import { TRUSTED_SIDECAR_ROOT_DIGEST } from './sidecar-trusted-digest'

const DIGEST = 'a'.repeat(64)
const ARGS = ['serve', '--host', '127.0.0.1', '--port', '0']

test('dev mode (not packaged) returns null → existing source/venv chain', () => {
  assert.equal(resolvePackagedSidecarBackend(ARGS, { isPackaged: false }), null)
})

test('a source-root override cannot bypass the bundled sidecar in a packaged app', () => {
  const validRoot = () => true
  assert.equal(canUseDeveloperSourceOverride(true, '/trusted-looking/source', validRoot), false)
  assert.equal(canUseDeveloperSourceOverride(false, '/trusted-looking/source', validRoot), true)
  assert.equal(canUseDeveloperSourceOverride(false, undefined, validRoot), false)
})

test('packaged + verified bundle → command backend with serve args and no argv secret', () => {
  const b = resolvePackagedSidecarBackend(ARGS, {
    isPackaged: true,
    resourcesPath: '/app/resources',
    platform: 'linux',
    fileExists: () => true,
    trustedDigest: DIGEST,
    computeDigest: () => DIGEST
  })

  assert.ok(b)
  assert.equal(b!.kind, 'command')
  assert.equal((b as any).command, path.join('/app/resources', 'backend-sidecar', 'youtab-backend'))
  assert.deepEqual((b as any).args, ARGS)
  assert.equal((b as any).sidecar, true)
  assert.equal((b as any).bootstrap, false)
  // secret is added by the existing spawn via env, never argv:
  assert.ok(!(b as any).args.some((a: string) => /secret|token/i.test(a)))
})

test('packaged + tampered bundle → sidecar-refused (fail closed)', () => {
  const b = resolvePackagedSidecarBackend(ARGS, {
    isPackaged: true,
    resourcesPath: '/app/resources',
    platform: 'linux',
    fileExists: () => true,
    trustedDigest: DIGEST,
    computeDigest: () => 'b'.repeat(64)
  })

  assert.equal(b!.kind, 'sidecar-refused')
  assert.equal((b as any).sidecarRefusal.reason, 'digest-mismatch')
  assert.equal((b as any).command, null)
})

test('packaged sidecar ignores an environment-supplied replacement digest', () => {
  const replacement = 'b'.repeat(64)

  const b = resolvePackagedSidecarBackend(ARGS, {
    isPackaged: true,
    resourcesPath: '/app/resources',
    platform: 'linux',
    fileExists: () => true,
    env: { YOUTAB_AGENT_SIDECAR_TRUSTED_DIGEST: replacement },
    computeDigest: () => replacement
  })

  assert.equal(b!.kind, 'sidecar-refused')
  assert.equal((b as any).sidecarRefusal.reason, 'digest-mismatch')
  assert.equal((b as any).sidecarRefusal.expected, TRUSTED_SIDECAR_ROOT_DIGEST)
})

test('packaged + missing bundle but anchor pinned → sidecar-refused (release must ship it)', () => {
  const b = resolvePackagedSidecarBackend(ARGS, {
    isPackaged: true,
    resourcesPath: '/app/resources',
    platform: 'linux',
    fileExists: () => false,
    trustedDigest: DIGEST
  })

  assert.equal(b!.kind, 'sidecar-refused')
  assert.equal((b as any).sidecarRefusal.reason, 'missing-sidecar')
})

test('packaged + missing bundle and no anchor → null (dev-packaged build without a sidecar)', () => {
  const b = resolvePackagedSidecarBackend(ARGS, {
    isPackaged: true,
    resourcesPath: '/app/resources',
    platform: 'linux',
    fileExists: () => false,
    trustedDigest: null
  })

  assert.equal(b, null)
})

test('packaged without a resourcesPath but anchor pinned → refuse (cannot locate bundle)', () => {
  const b = resolvePackagedSidecarBackend(ARGS, { isPackaged: true, resourcesPath: null, trustedDigest: DIGEST })
  assert.equal(b!.kind, 'sidecar-refused')
  assert.equal((b as any).sidecarRefusal.reason, 'missing-sidecar')
})
