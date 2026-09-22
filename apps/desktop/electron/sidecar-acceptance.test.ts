/**
 * Ten sidecar acceptance tests (handoff item B). Each asserts one shipped
 * property end-to-end by composing the real sidecar modules and reusing the
 * existing backend-child tree-kill (no duplicated lifecycle logic).
 *
 * Run: npx vitest run --project electron electron/sidecar-acceptance.test.ts
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { stopBackendChild } from './backend-child'
import { decideSidecarLaunch } from './sidecar-integrity'
import {
  LOOPBACK_HOST,
  SIDECAR_BIND_ENV,
  SIDECAR_SECRET_ENV,
  buildSidecarLaunch,
  generateEphemeralSecret,
  isLoopbackOnly,
  redactSecret,
  secretInArgv
} from './sidecar-launch'
import { waitForGatewayReady } from './sidecar-ready'
import { resolveSidecarPaths } from './sidecar-resolve'
import { SidecarRestartPolicy } from './sidecar-restart'
import { rootDigestFromBundle } from '../packaging/backend-sidecar/root-digest.mjs'

const cleanups: Array<() => void> = []
afterEach(() => {
  while (cleanups.length) cleanups.pop()!()
})

function bundleFixture(files: Record<string, string>): string {
  const root = mkdtempSync(path.join(tmpdir(), 'sc-accept-'))
  cleanups.push(() => rmSync(root, { recursive: true, force: true }))
  for (const [rel, content] of Object.entries(files)) {
    const p = path.join(root, rel)
    mkdirSync(path.dirname(p), { recursive: true })
    writeFileSync(p, content)
  }
  return root
}

function fakeChild() {
  const child: any = new EventEmitter()
  child.stdout = new EventEmitter()
  return child
}

const READY_LINE =
  JSON.stringify({ jsonrpc: '2.0', method: 'event', params: { type: 'gateway.ready', payload: { skin: 'youtab' } } }) +
  '\n'

// 1 — shipped via extraResources → resolved via process.resourcesPath (packaged).
test('acceptance 1: packaged sidecar resolves under process.resourcesPath (extraResources target)', () => {
  const r = resolveSidecarPaths({ isPackaged: true, resourcesPath: '/app/resources', platform: 'linux' })
  assert.equal(r!.mode, 'packaged')
  assert.equal(r!.bundleDir, path.join('/app/resources', 'backend-sidecar'))
  assert.ok(r!.executable.startsWith(path.join('/app/resources', 'backend-sidecar')))
})

// 2 — dev path resolution (not resourcesPath).
test('acceptance 2: dev sidecar resolves under the repo build output (not resourcesPath)', () => {
  const r = resolveSidecarPaths({ isPackaged: false, repoRoot: '/repo', platform: 'linux' })
  assert.equal(r!.mode, 'dev')
  assert.ok(r!.bundleDir.includes(path.join('apps', 'desktop', 'build', 'backend-sidecar')))
})

// 3 — ephemeral per-launch secret over the loopback stdio channel.
test('acceptance 3: an ephemeral per-launch secret is handed to the sidecar via env', () => {
  const s1 = generateEphemeralSecret()
  const s2 = generateEphemeralSecret()
  assert.notEqual(s1, s2)
  const d = buildSidecarLaunch({
    executable: '/x',
    userDataDir: '/u',
    secret: s1,
    homeDir: '/h',
    env: {},
    platform: 'linux'
  })
  assert.equal(d.env[SIDECAR_SECRET_ENV], s1)
})

// 4 — health check on gateway.ready JSON-RPC.
test('acceptance 4: readiness is the gateway.ready JSON-RPC event on stdout', async () => {
  const child = fakeChild()
  const p = waitForGatewayReady(child, 1000)
  child.stdout.emit('data', READY_LINE)
  assert.equal((await p).type, 'gateway.ready')
})

// 5 — restart-on-crash, bounded.
test('acceptance 5: crashes restart up to a bounded budget then stop', () => {
  const pol = new SidecarRestartPolicy({ maxRestarts: 2, windowMs: 10 ** 9, backoffMs: () => 0 })
  assert.equal(pol.onCrash(0).restart, true)
  assert.equal(pol.onCrash(1).restart, true)
  assert.equal(pol.onCrash(2).restart, false)
})

// 6 — full process-tree shutdown on app quit (reuses backend-child tree-kill).
test('acceptance 6: shutdown tree-kills the sidecar process group on win32', () => {
  const killed: number[] = []
  stopBackendChild({ pid: 4321, kill: () => {} }, { isWindows: true, forceKillProcessTree: pid => killed.push(pid) })
  assert.deepEqual(killed, [4321])
})

// 7 — digest-binding refusal (tampered bundle).
test('acceptance 7: a tampered bundle is refused (digest binding)', () => {
  const clean = bundleFixture({ 'youtab-backend': 'exe', 'lib/a.py': 'print(1)' })
  const trusted = rootDigestFromBundle(clean)
  const tampered = bundleFixture({ 'youtab-backend': 'exe', 'lib/a.py': 'print(2)  # evil' })
  const decision = decideSidecarLaunch({ bundlePresent: true, bundleDir: tampered, trustedDigest: trusted })
  assert.equal(decision.action, 'refuse')
  assert.equal(decision.reason, 'digest-mismatch')
  // And the identical clean tree launches.
  assert.equal(decideSidecarLaunch({ bundlePresent: true, bundleDir: clean, trustedDigest: trusted }).action, 'launch')
})

// 8 — no secret in argv or logs.
test('acceptance 8: the secret never appears in argv and is redacted from logs', () => {
  const secret = generateEphemeralSecret()
  const d = buildSidecarLaunch({
    executable: '/x',
    userDataDir: '/u',
    secret,
    homeDir: '/h',
    env: {},
    platform: 'linux'
  })
  assert.equal(secretInArgv(d.args, secret), false)
  assert.ok(!redactSecret(`spawn ${d.command} ${SIDECAR_SECRET_ENV}=${secret}`, secret).includes(secret))
})

// 9 — loopback bind only.
test('acceptance 9: the launch descriptor is loopback-only (no external bind)', () => {
  const d = buildSidecarLaunch({
    executable: '/x',
    userDataDir: '/u',
    secret: 's',
    homeDir: '/h',
    env: {},
    platform: 'linux'
  })
  assert.equal(d.env[SIDECAR_BIND_ENV], LOOPBACK_HOST)
  assert.equal(isLoopbackOnly(d), true)
})

// 10 — USERPROFILE present for Path.home().
test('acceptance 10: USERPROFILE (win) / HOME (posix) is set so Python Path.home() resolves', () => {
  const win = buildSidecarLaunch({
    executable: '/x',
    userDataDir: '/u',
    secret: 's',
    homeDir: 'C:\\Users\\u',
    env: {},
    platform: 'win32'
  })
  assert.equal(win.env.USERPROFILE, 'C:\\Users\\u')
  const posix = buildSidecarLaunch({
    executable: '/x',
    userDataDir: '/u',
    secret: 's',
    homeDir: '/home/u',
    env: {},
    platform: 'linux'
  })
  assert.equal(posix.env.HOME, '/home/u')
})
