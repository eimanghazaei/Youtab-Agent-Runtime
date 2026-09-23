/**
 * Tests for electron/sidecar-launch.ts (secret / loopback / userData / env).
 * Run: npx vitest run --project electron electron/sidecar-launch.test.ts
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildSidecarLaunch,
  generateEphemeralSecret,
  isLoopbackOnly,
  LOOPBACK_HOST,
  redactSecret,
  secretInArgv,
  SIDECAR_BIND_ENV,
  SIDECAR_SECRET_ENV
} from './sidecar-launch'

function launch(overrides: Partial<Parameters<typeof buildSidecarLaunch>[0]> = {}) {
  return buildSidecarLaunch({
    executable: '/res/backend-sidecar/youtab-backend',
    userDataDir: '/userdata/youtab',
    secret: 'SECRET-TOKEN-123',
    homeDir: '/home/u',
    env: {},
    platform: 'linux',
    ...overrides
  })
}

test('generateEphemeralSecret is random per call and url-safe', () => {
  let n = 0
  const rand = (len: number) => Buffer.alloc(len, n++)
  const a = generateEphemeralSecret(rand)
  const b = generateEphemeralSecret(rand)
  assert.notEqual(a, b)
  assert.match(generateEphemeralSecret(), /^[A-Za-z0-9_-]+$/)
})

test('secret travels in env, NEVER in argv', () => {
  const d = launch()
  assert.equal(d.env[SIDECAR_SECRET_ENV], 'SECRET-TOKEN-123')
  assert.equal(secretInArgv(d.args, 'SECRET-TOKEN-123'), false)
  assert.deepEqual(d.args, [])
})

test('dedicated app-owned userData home is pinned', () => {
  const d = launch()
  assert.equal(d.env.YOUTAB_AGENT_HOME, '/userdata/youtab')
})

test('USERPROFILE set on win32, HOME set elsewhere (Path.home())', () => {
  assert.equal(launch({ platform: 'win32', homeDir: 'C:\\Users\\u' }).env.USERPROFILE, 'C:\\Users\\u')
  assert.equal(launch({ platform: 'linux', homeDir: '/home/u' }).env.HOME, '/home/u')
})

test('loopback-only: bind marker is 127.0.0.1 and argv exposes no external host', () => {
  const d = launch()
  assert.equal(d.env[SIDECAR_BIND_ENV], LOOPBACK_HOST)
  assert.equal(isLoopbackOnly(d), true)
})

test('isLoopbackOnly rejects a non-loopback bind or host arg', () => {
  const d = launch()
  assert.equal(isLoopbackOnly({ ...d, env: { ...d.env, [SIDECAR_BIND_ENV]: '0.0.0.0' } }), false)
  assert.equal(isLoopbackOnly({ ...d, args: ['--host', '0.0.0.0'] }), false)
  assert.equal(isLoopbackOnly({ ...d, args: ['--host', '10.0.0.5'] }), false)
})

test('stdio is fully piped (no inherited console, enables handshake capture)', () => {
  assert.deepEqual(launch().stdio, ['pipe', 'pipe', 'pipe'])
})

test('redactSecret scrubs the raw secret and the env-assignment form from logs', () => {
  const s = 'SECRET-TOKEN-123'
  assert.ok(!redactSecret(`booting with token ${s} now`, s).includes(s))
  assert.ok(!redactSecret(`${SIDECAR_SECRET_ENV}=${s} inherited`, s).includes(s))
  assert.match(redactSecret(`x ${s}`, s), /«redacted»/)
})

test('PYTHONUTF8 defaults to 1 but respects an explicit inherited value', () => {
  assert.equal(launch({ env: {} }).env.PYTHONUTF8, '1')
  assert.equal(launch({ env: { PYTHONUTF8: '0' } }).env.PYTHONUTF8, '0')
})
