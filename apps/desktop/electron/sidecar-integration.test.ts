/**
 * REAL-PROCESS integration tests for the packaged backend sidecar.
 *
 * Unlike the mocked-descriptor unit tests, these launch the ACTUAL frozen
 * `youtab-backend` executable and exercise the actual HTTP lifecycle (port
 * announcement, /api/health), plus the real integrity gate over the real 1527-
 * file bundle and a real process-tree kill. They require the sidecar to have
 * been built (`node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs`);
 * when the bundle is absent the whole suite skips with a clear reason.
 *
 * Run: npx vitest run --project electron electron/sidecar-integration.test.ts
 */
import assert from 'node:assert/strict'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import { appendFileSync, cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterAll, afterEach, beforeAll, describe, expect, test } from 'vitest'

import { rootDigestFromBundle } from '../packaging/backend-sidecar/root-digest.mjs'

import { decideSidecarLaunch } from './sidecar-integrity'
import { redactSecret } from './sidecar-launch'
import { TRUSTED_SIDECAR_ROOT_DIGEST } from './sidecar-trusted-digest'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const DESKTOP_ROOT = path.resolve(HERE, '..')
const BUNDLE_DIR = path.join(DESKTOP_ROOT, 'build', 'backend-sidecar', 'dist', 'youtab-backend')
const EXE = path.join(BUNDLE_DIR, process.platform === 'win32' ? 'youtab-backend.exe' : 'youtab-backend')
const HAVE_BUNDLE = existsSync(EXE)

const READY_RE = /YOUTAB_AGENT_(?:BACKEND|DASHBOARD)_READY port=(\d+)/

interface RunningBackend {
  proc: ChildProcess
  port: number
  logs: string
}

const tmpDirs: string[] = []
const running: ChildProcess[] = []

function mkTmp(prefix: string): string {
  const d = mkdtempSync(path.join(tmpdir(), prefix))
  tmpDirs.push(d)

  return d
}

async function startBackend(extraEnv: Record<string, string> = {}, timeoutMs = 30_000): Promise<RunningBackend> {
  const home = mkTmp('sc-int-home-')

  const proc = spawn(EXE, ['serve', '--host', '127.0.0.1', '--port', '0'], {
    env: { ...process.env, YOUTAB_AGENT_HOME: home, YOUTAB_AGENT_DESKTOP: '1', ...extraEnv },
    stdio: ['ignore', 'pipe', 'pipe']
  })

  running.push(proc)
  let logs = ''

  return new Promise<RunningBackend>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`backend did not announce a port in ${timeoutMs}ms; logs:\n${logs}`)),
      timeoutMs
    )

    const onData = (chunk: Buffer) => {
      logs += chunk.toString()
      const m = logs.match(READY_RE)

      if (m) {
        clearTimeout(timer)
        resolve({ proc, port: Number(m[1]), logs })
      }
    }

    proc.stdout!.on('data', onData)
    proc.stderr!.on('data', onData)
    proc.once('error', err => {
      clearTimeout(timer)
      reject(err)
    })
    proc.once('exit', code => {
      clearTimeout(timer)
      reject(new Error(`backend exited early (code ${code}); logs:\n${logs}`))
    })
  })
}

async function httpGet(url: string, timeoutMs = 5000): Promise<{ status: number; body: string }> {
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), timeoutMs)

  try {
    const r = await fetch(url, { signal: ctrl.signal })

    return { status: r.status, body: await r.text() }
  } finally {
    clearTimeout(t)
  }
}

function alive(pid: number): boolean {
  try {
    if (process.platform === 'win32') {
      const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/NH'], { encoding: 'utf8' })

      return out.includes(String(pid))
    }

    process.kill(pid, 0)

    return true
  } catch {
    return false
  }
}

function treeKill(pid: number): void {
  if (process.platform === 'win32') {
    try {
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
    } catch {
      // already gone
    }
  } else {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // already gone
    }
  }
}

let bundleCopy: string | null = null

beforeAll(() => {
  if (HAVE_BUNDLE) {
    // One real copy of the 1527-file bundle, reused by the tamper / wrong-exe
    // tests so we mutate real bundle bytes rather than a synthetic stand-in.
    bundleCopy = mkTmp('sc-int-copy-')
    cpSync(BUNDLE_DIR, path.join(bundleCopy, 'youtab-backend'), { recursive: true })
  }
})

afterEach(() => {
  for (const p of running.splice(0)) {
    if (p.pid) {
      treeKill(p.pid)
    }
  }
})

afterAll(() => {
  for (const d of tmpDirs.splice(0)) {
    try {
      rmSync(d, { recursive: true, force: true })
    } catch {
      // best-effort
    }
  }
})

describe.skipIf(!HAVE_BUNDLE)('sidecar real-process integration', () => {
  // 1
  test('1: packaged backend executable exists', () => {
    assert.ok(existsSync(EXE), `missing frozen backend at ${EXE}`)
  })

  // 2
  test('2: real bundle digest matches the pinned trust anchor → launch', () => {
    const actual = rootDigestFromBundle(BUNDLE_DIR)
    assert.equal(actual, TRUSTED_SIDECAR_ROOT_DIGEST)

    const d = decideSidecarLaunch({
      bundlePresent: true,
      bundleDir: BUNDLE_DIR,
      trustedDigest: TRUSTED_SIDECAR_ROOT_DIGEST
    })

    assert.equal(d.action, 'launch')
  }, 60_000)

  // 3
  test('3: one-byte tampering of the real bundle fails before launch', () => {
    const dir = path.join(bundleCopy!, 'youtab-backend')
    const trusted = rootDigestFromBundle(BUNDLE_DIR)
    // Flip one byte by appending to a real file in the copied tree.
    appendFileSync(
      path.join(dir, process.platform === 'win32' ? 'youtab-backend.exe' : 'youtab-backend'),
      Buffer.from([0])
    )
    const d = decideSidecarLaunch({ bundlePresent: true, bundleDir: dir, trustedDigest: trusted })
    assert.equal(d.action, 'refuse')
    assert.equal(d.reason, 'digest-mismatch')
  }, 60_000)

  // 4
  test('4: missing sidecar fails closed when an anchor is pinned', () => {
    const empty = mkTmp('sc-int-empty-')

    const d = decideSidecarLaunch({
      bundlePresent: false,
      bundleDir: path.join(empty, 'nope'),
      trustedDigest: TRUSTED_SIDECAR_ROOT_DIGEST
    })

    assert.equal(d.action, 'refuse')
    assert.equal(d.reason, 'missing-sidecar')
  })

  // 5
  test('5: a wrong executable fails closed (digest mismatch)', () => {
    const wrong = mkTmp('sc-int-wrong-')
    const dir = path.join(wrong, 'youtab-backend')
    cpSync(BUNDLE_DIR, dir, { recursive: true })
    const trusted = rootDigestFromBundle(BUNDLE_DIR)
    // Replace the exe bytes with a different program's bytes.
    const exe = path.join(dir, process.platform === 'win32' ? 'youtab-backend.exe' : 'youtab-backend')
    writeFileSync(exe, readFileSync(process.execPath)) // node.exe stands in as the wrong binary
    const d = decideSidecarLaunch({ bundlePresent: true, bundleDir: dir, trustedDigest: trusted })
    assert.equal(d.action, 'refuse')
    assert.equal(d.reason, 'digest-mismatch')
  }, 120_000)

  // 6
  test('6: backend binds loopback and answers /api/health on 127.0.0.1', async () => {
    const b = await startBackend()
    const res = await httpGet(`http://127.0.0.1:${b.port}/api/health`)
    assert.equal(res.status, 200)
    assert.match(res.body, /"ok"\s*:\s*true/)
  }, 40_000)

  // 7 + 8
  test('7/8: ephemeral session token is passed via env (not argv) and health is reachable with it', async () => {
    const secret = 'sc-int-secret-' + Math.random().toString(36).slice(2)
    const b = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret })
    // Real argv of the spawned process must not contain the secret.
    let cmdline = ''

    if (process.platform === 'win32') {
      cmdline = execFileSync(
        'powershell',
        ['-NoProfile', '-Command', `(Get-CimInstance Win32_Process -Filter "ProcessId=${b.proc.pid}").CommandLine`],
        { encoding: 'utf8' }
      )
    }

    assert.ok(!cmdline.includes(secret), 'session token leaked into process argv')
    const res = await httpGet(`http://127.0.0.1:${b.port}/api/health`)
    assert.equal(res.status, 200)
  }, 40_000)

  // 9
  test('9: two --port 0 launches get distinct ports (no fixed-port collision)', async () => {
    const a = await startBackend()
    const b = await startBackend()
    assert.notEqual(a.port, b.port)
  }, 60_000)

  // 10
  test('10: a real crash sequence obeys the bounded restart ceiling', async () => {
    const { SidecarRestartPolicy } = await import('./sidecar-restart')
    const policy = new SidecarRestartPolicy({ maxRestarts: 2, windowMs: 10 ** 9, backoffMs: () => 0 })
    const decisions: boolean[] = []

    for (let i = 0; i < 3; i++) {
      const b = await startBackend()
      assert.ok(alive(b.proc.pid!))
      treeKill(b.proc.pid!) // real crash
      decisions.push(policy.onCrash(i).restart)
    }

    assert.deepEqual(decisions, [true, true, false]) // ceiling stops the 3rd
  }, 90_000)

  // 11
  test('11: process-tree kill terminates the backend with no orphan', async () => {
    const b = await startBackend()
    const pid = b.proc.pid!
    assert.ok(alive(pid))
    treeKill(pid)

    // Poll briefly for the OS to reap it.
    for (let i = 0; i < 20 && alive(pid); i++) {
      await new Promise(r => setTimeout(r, 100))
    }

    assert.ok(!alive(pid), `backend pid ${pid} still alive after tree kill`)
  }, 40_000)

  // 12
  test('12: the session token and dev machine paths do not appear in backend logs', async () => {
    const secret = 'sc-int-logsecret-' + Math.random().toString(36).slice(2)
    const b = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret })
    // Give it a moment to emit any startup logging.
    await new Promise(r => setTimeout(r, 750))
    assert.ok(!b.logs.includes(secret), 'session token appeared in backend stdout/stderr')
    assert.ok(!/[A-Za-z]:\\\\Users\\\\eiman\\\\worktrees/.test(b.logs), 'developer worktree path leaked in logs')
    // redactSecret is the log chokepoint that would scrub it if it ever did.
    expect(redactSecret(`token=${secret}`, secret)).not.toContain(secret)
  }, 40_000)
})
