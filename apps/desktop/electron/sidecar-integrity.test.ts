/**
 * Tests for electron/sidecar-integrity.ts (A2 trusted-binding gate).
 * Run: npx vitest run --project electron electron/sidecar-integrity.test.ts
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { decideSidecarLaunch } from './sidecar-integrity'

const DIGEST = 'a'.repeat(64)
const OTHER = 'b'.repeat(64)

test('no bundle present and no anchor → skip (dev; fall through to normal chain)', () => {
  const d = decideSidecarLaunch({ bundlePresent: false })
  assert.equal(d.action, 'skip')
  assert.equal(d.reason, 'no-bundle')
})

test('no bundle present but anchor pinned → refuse (release must ship the sidecar)', () => {
  const d = decideSidecarLaunch({ bundlePresent: false, trustedDigest: DIGEST })
  assert.equal(d.action, 'refuse')
  assert.equal(d.reason, 'missing-sidecar')
  assert.equal(d.expected, DIGEST)
})

test('bundle present but no trusted anchor → refuse (fail closed)', () => {
  const d = decideSidecarLaunch({
    bundlePresent: true,
    bundleDir: '/b',
    trustedDigest: null,
    computeDigest: () => DIGEST
  })

  assert.equal(d.action, 'refuse')
  assert.equal(d.reason, 'no-trusted-digest')
})

test('matching digest → launch', () => {
  const d = decideSidecarLaunch({
    bundlePresent: true,
    bundleDir: '/b',
    trustedDigest: DIGEST,
    computeDigest: () => DIGEST
  })

  assert.equal(d.action, 'launch')
  assert.equal(d.actual, DIGEST)
})

test('mismatching digest → refuse (tamper)', () => {
  const d = decideSidecarLaunch({
    bundlePresent: true,
    bundleDir: '/b',
    trustedDigest: DIGEST,
    computeDigest: () => OTHER
  })

  assert.equal(d.action, 'refuse')
  assert.equal(d.reason, 'digest-mismatch')
  assert.equal(d.expected, DIGEST)
  assert.equal(d.actual, OTHER)
})

test('digest computation throwing → refuse (cannot prove integrity)', () => {
  const d = decideSidecarLaunch({
    bundlePresent: true,
    bundleDir: '/b',
    trustedDigest: DIGEST,
    computeDigest: () => {
      throw new Error('walk failed')
    }
  })

  assert.equal(d.action, 'refuse')
  assert.equal(d.reason, 'compute-error')
})

test('malformed trusted anchor is treated as absent → refuse', () => {
  const d = decideSidecarLaunch({
    bundlePresent: true,
    bundleDir: '/b',
    trustedDigest: 'xyz',
    computeDigest: () => DIGEST
  })

  assert.equal(d.action, 'refuse')
  assert.equal(d.reason, 'no-trusted-digest')
})
