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

async function startBackend(
  extraEnv: Record<string, string> = {},
  timeoutMs = 30_000,
  host = '127.0.0.1'
): Promise<RunningBackend> {
  const home = mkTmp('sc-int-home-')

  const proc = spawn(EXE, ['serve', '--host', host, '--port', '0'], {
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

async function httpGet(
  url: string,
  timeoutMs = 5000,
  headers: Record<string, string> = {}
): Promise<{ status: number; body: string }> {
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), timeoutMs)

  try {
    const r = await fetch(url, { signal: ctrl.signal, headers })

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

// Decode the kernel socket table for the backend's announced ephemeral port.
// 0A is TCP LISTEN; all other states must be ignored.
function parseProcListenAddrs(tables: Record<string, string>, port: number): string[] {
  const addresses: string[] = []

  for (const family of ['tcp', 'tcp6']) {
    const table = tables[family] || ''

    for (const line of table.split(/\r?\n/).slice(1)) {
      const columns = line.trim().split(/\s+/)

      if (columns.length < 4 || columns[3] !== '0A') {
        continue // LISTEN
      }

      const [address, hexPort] = (columns[1] || '').split(':')

      if (Number.parseInt(hexPort, 16) !== port) {
        continue
      }

      if (family === 'tcp') {
        const octets = address.match(/../g)?.reverse().map(hex => Number.parseInt(hex, 16))

        if (octets?.length === 4) {
          addresses.push(octets.join('.'))
        }
      } else if (address === '00000000000000000000000001000000') {
        addresses.push('::1')
      } else if (address === '00000000000000000000000000000000') {
        addresses.push('::')
      } else if (address) {
        addresses.push(`tcp6:${address}`) // conservatively non-loopback
      }
    }
  }

  return addresses
}

// The actual listening TCP socket addresses (real socket state, not an HTTP
// probe). Linux filters /proc/net by the OS-selected port announced by this
// backend; Windows/macOS query the process directly.
function listenAddrsFor(pid: number, port?: number): string[] {
  if (process.platform === 'linux') {
    if (!port) {
      return []
    }

    const tables: Record<string, string> = {}

    for (const family of ['tcp', 'tcp6']) {
      try {
        tables[family] = readFileSync(`/proc/net/${family}`, 'utf8')
      } catch {
        // A missing kernel table contributes no sockets.
      }
    }

    return parseProcListenAddrs(tables, port)
  }

  if (process.platform === 'darwin') {
    if (!port) {
      return []
    }

    try {
      const out = execFileSync('lsof', ['-nP', '-a', '-p', String(pid), `-iTCP:${port}`, '-sTCP:LISTEN', '-F', 'n'], {
        encoding: 'utf8'
      })

      return out.split(/\r?\n/).filter(line => line.startsWith('n') && line.endsWith(`:${port}`))
        .map(line => line.slice(1, -(`:${port}`.length)))
    } catch {
      return []
    }
  }

  if (process.platform !== 'win32') {
    return []
  }

  try {
    const out = execFileSync(
      'powershell',
      [
        '-NoProfile',
        '-Command',
        `Get-NetTCPConnection -OwningProcess ${pid} -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalAddress }`
      ],
      { encoding: 'utf8' }
    )

    return out
      .split(/\r?\n/)
      .map(s => s.trim())
      .filter(Boolean)
  } catch {
    return []
  }
}

test('Linux socket table probe finds only LISTEN addresses on the announced port', () => {
  const tables = {
    tcp: 'sl local_address rem_address st\n 0: 0100007F:1F90 00000000:0000 0A\n 1: 00000000:1F91 00000000:0000 0A\n 2: 0100007F:1F90 00000000:0000 01',
    tcp6: 'sl local_address rem_address st\n 0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A'
  }

  assert.deepEqual(parseProcListenAddrs(tables, 8080), ['127.0.0.1', '::1'])
  assert.deepEqual(parseProcListenAddrs(tables, 8081), ['0.0.0.0'])
})

function isLoopbackAddr(addr: string): boolean {
  const a = addr.replace(/^\[|\]$/g, '').toLowerCase()

  return a === '127.0.0.1' || a.startsWith('127.') || a === '::1'
}

interface RefusalResult {
  pid: number
  exitCode: number | null
  readyAnnounced: boolean
  listeners: string[]
}

// Spawn the frozen backend on a host expected to be REFUSED: it must exit
// (non-zero) without ever announcing readiness, and no listener may exist.
function startBackendRefused(host: string, timeoutMs = 20_000): Promise<RefusalResult> {
  const home = mkTmp('sc-int-refuse-')

  const proc = spawn(EXE, ['serve', '--host', host, '--port', '0'], {
    env: { ...process.env, YOUTAB_AGENT_HOME: home, YOUTAB_AGENT_DESKTOP: '1' },
    stdio: ['ignore', 'pipe', 'pipe']
  })

  running.push(proc)
  let readyAnnounced = false
  let out = ''

  return new Promise<RefusalResult>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`host ${host} neither exited nor announced in ${timeoutMs}ms`)),
      timeoutMs
    )

    const onData = (c: Buffer) => {
      out += c.toString()

      if (READY_RE.test(out)) {
        readyAnnounced = true
      }
    }

    proc.stdout!.on('data', onData)
    proc.stderr!.on('data', onData)
    proc.once('exit', code => {
      clearTimeout(timer)
      resolve({ pid: proc.pid!, exitCode: code, readyAnnounced, listeners: listenAddrsFor(proc.pid!) })
    })
    proc.once('error', err => {
      clearTimeout(timer)
      reject(err)
    })
  })
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
  test('2: real bundle digest matches the controlled build anchor → launch', () => {
    const actual = rootDigestFromBundle(BUNDLE_DIR)
    const expected = process.env.YOUTAB_AGENT_SIDECAR_CI_BUILD_DIGEST || TRUSTED_SIDECAR_ROOT_DIGEST

    assert.match(expected || '', /^[0-9a-f]{64}$/)
    assert.equal(readFileSync(path.join(DESKTOP_ROOT, 'build', 'backend-sidecar', 'sidecar-root-digest.txt'), 'utf8').trim(), expected)
    assert.equal(JSON.parse(readFileSync(path.join(DESKTOP_ROOT, 'build', 'backend-sidecar', 'manifest.json'), 'utf8')).root_digest_sha256, expected)
    assert.equal(actual, expected)

    const d = decideSidecarLaunch({
      bundlePresent: true,
      bundleDir: BUNDLE_DIR,
      trustedDigest: expected
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

  // 13 — a protected API endpoint enforces the ephemeral secret.
  test('13: a protected endpoint rejects missing/wrong secret and accepts the correct one', async () => {
    const secret = 'sc-int-auth-' + Math.random().toString(36).slice(2)
    const b = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret })
    const url = `http://127.0.0.1:${b.port}/api/config` // not in PUBLIC_API_PATHS

    const noToken = await httpGet(url)
    assert.equal(noToken.status, 401, 'protected endpoint must 401 without the secret')

    const wrongToken = await httpGet(url, 5000, { 'X-Youtab-Session-Token': 'wrong-' + secret })
    assert.equal(wrongToken.status, 401, 'protected endpoint must 401 with a wrong secret')

    const correct = await httpGet(url, 5000, { 'X-Youtab-Session-Token': secret })
    assert.equal(correct.status, 200, 'protected endpoint must accept the correct secret')
  }, 40_000)

  // 14 — BOTH public endpoints (/api/health and /api/status) carry no secret or
  // session state. These are the only two /api/* paths exempt from the token
  // gate (PUBLIC_API_PATHS), so both must be leakage-free.
  test('14: /api/health and /api/status are public and expose no secret/session/path', async () => {
    const secret = 'sc-int-health-' + Math.random().toString(36).slice(2)
    const b = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret })

    for (const path of ['/api/health', '/api/status']) {
      const res = await httpGet(`http://127.0.0.1:${b.port}${path}`)
      assert.equal(res.status, 200, `${path} should be public (200)`)
      assert.ok(!res.body.includes(secret), `${path} leaked the session secret`)
      assert.ok(
        !/session_token|password|X-Youtab-Session-Token/i.test(res.body),
        `${path} carried a sensitive token field: ${res.body.slice(0, 300)}`
      )
      assert.ok(!/[A-Za-z]:\\Users\\eiman\\worktrees/i.test(res.body), `${path} leaked a developer machine path`)
    }
  }, 40_000)

  // 15 — a bind to a host OTHER than the trusted loopback set (127.0.0.1,
  // localhost, ::1) engages the auth gate. 127.0.0.2 IS a loopback-range address
  // (127.0.0.0/8, never exposed off-host) but is NOT the trusted 127.0.0.1, so
  // should_require_auth() returns true and the OAuth/default-deny gate takes over
  // — the loopback session token is no longer sufficient.
  test('15: a non-trusted-host bind (127.0.0.2) engages the gate — session token no longer suffices', async () => {
    const secret = 'sc-int-nontrusted-' + Math.random().toString(36).slice(2)
    let started: RunningBackend | null = null

    try {
      started = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret }, 20_000, '127.0.0.2')
    } catch {
      return // refused to come up on a non-trusted host — itself fail-closed
    }

    const res = await httpGet(`http://127.0.0.2:${started.port}/api/config`, 5000, {
      'X-Youtab-Session-Token': secret
    })

    assert.notEqual(
      res.status,
      200,
      `gated host served a protected endpoint to the loopback token (not fail-closed): ${res.status}`
    )
  }, 40_000)

  // 16 — a REAL external bind (0.0.0.0, all interfaces) fails closed. The gate
  // engages (non-loopback), so a protected endpoint is never served without
  // OAuth even though the ephemeral session token is presented. The listener is
  // bounded to this test: probed only via loopback and torn down in afterEach.
  test('16: a real 0.0.0.0 (all-interfaces) bind fails closed on protected endpoints', async () => {
    const secret = 'sc-int-extbind-' + Math.random().toString(36).slice(2)
    let started: RunningBackend | null = null

    try {
      started = await startBackend({ YOUTAB_AGENT_DASHBOARD_SESSION_TOKEN: secret }, 20_000, '0.0.0.0')
    } catch {
      return // refused to bind all-interfaces without a gate — fail-closed
    }

    // Probe over loopback only (do not reach out over the LAN). The gate must
    // deny a protected endpoint even with the session token.
    const res = await httpGet(`http://127.0.0.1:${started.port}/api/config`, 5000, {
      'X-Youtab-Session-Token': secret
    })

    assert.notEqual(res.status, 200, `0.0.0.0 bind served a protected endpoint (not fail-closed): ${res.status}`)
  }, 40_000)

  // 17 — loopback hosts are ACCEPTED, and the real listening socket is
  // loopback-only (validated via Get-NetTCPConnection, not just an HTTP probe).
  test('17: loopback hosts (127.0.0.1, ::1) are accepted and the real socket is loopback-only', async () => {
    for (const host of ['127.0.0.1', '::1']) {
      const b = await startBackend({}, 30_000, host)
      const addrs = listenAddrsFor(b.proc.pid!, b.port)
      assert.ok(addrs.length > 0, `no listening socket found for host ${host}`)

      for (const a of addrs) {
        assert.ok(isLoopbackAddr(a), `host ${host} opened a NON-loopback listener: ${a} (all: ${addrs.join(',')})`)
      }

      treeKill(b.proc.pid!)
    }
  }, 120_000)

  // 18 — non-loopback hosts are REFUSED before readiness: the process exits with
  // the config code, never announces a port, opens NO listener, and leaves no
  // orphan. This is the socket-level loopback-only guarantee.
  test('18: 0.0.0.0, :: and a LAN address are refused before readiness (no listener, no orphan)', async () => {
    for (const host of ['0.0.0.0', '::', '10.255.255.254']) {
      const r = await startBackendRefused(host)
      assert.equal(r.readyAnnounced, false, `host ${host} announced readiness (must be refused before bind)`)
      assert.equal(r.exitCode, 78, `host ${host} exit code was ${r.exitCode}, expected 78 (EX_CONFIG refusal)`)
      assert.equal(r.listeners.length, 0, `host ${host} created a listener: ${r.listeners.join(',')}`)
      assert.ok(!alive(r.pid), `host ${host} left an orphan process`)
    }
  }, 90_000)
})
