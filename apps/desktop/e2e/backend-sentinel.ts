/**
 * Fail-fast guard for packaged-app specs: once the app is ready, pin the pid of
 * the bundled backend it spawned and watch it. If that backend exits while the
 * test still expects it to run, the spec fails naming the pid and attaching the
 * backend's own lifecycle/stdio evidence, instead of later reporting a
 * misleading durability or UI failure.
 */
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

interface BackendProcess {
  pid: number
  path: string
}

function listBundledBackends(sidecarRoot: string): BackendProcess[] {
  if (process.platform !== 'win32') {return []}

  try {
    const out = execFileSync(
      'powershell',
      ['-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='youtab-backend.exe'\" | ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"],
      { encoding: 'utf8' },
    )

    const root = sidecarRoot.toLowerCase()

    return out.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
      const [pid, exe] = line.split('|')

      return { pid: Number(pid), path: exe || '' }
    }).filter(p => Number.isInteger(p.pid) && p.path.toLowerCase().startsWith(root))
  } catch {
    return []
  }
}

function isAlive(pid: number): boolean {
  if (process.platform === 'win32') {
    try {
      const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/NH', '/FO', 'CSV'], { encoding: 'utf8' })

      return out.includes(`"${pid}"`)
    } catch {
      return true // unknown: never report a false death
    }
  }

  try {
    process.kill(pid, 0)

    return true
  } catch {
    return false
  }
}

function tail(file: string, lines = 60): string {
  try {
    return fs.readFileSync(file, 'utf8').split(/\r?\n/).slice(-lines).join('\n')
  } catch {
    return `(missing: ${file})`
  }
}

export class BackendSentinel {
  private timer: ReturnType<typeof setInterval> | null = null
  private deathAt: string | null = null

  private constructor(readonly pid: number, private readonly youtabHome: string) {}

  static async attach(youtabHome: string, packagedBinary: string, timeoutMs = 30_000): Promise<BackendSentinel> {
    const sidecarRoot = path.join(path.dirname(packagedBinary), 'resources', 'backend-sidecar') + path.sep
    const deadline = Date.now() + timeoutMs

    for (;;) {
      const found = listBundledBackends(sidecarRoot)

      if (found.length === 1) {
        const sentinel = new BackendSentinel(found[0].pid, youtabHome)
        sentinel.timer = setInterval(() => sentinel.check(), 1000)

        return sentinel
      }

      if (Date.now() > deadline) {
        throw new Error(`expected exactly one bundled backend under ${sidecarRoot}, found ${JSON.stringify(found)}`)
      }

      await new Promise(resolve => setTimeout(resolve, 500))
    }
  }

  private check(): void {
    if (!this.deathAt && !isAlive(this.pid)) {
      this.deathAt = new Date().toISOString()
    }
  }

  /** Throw with the backend's own evidence if it has exited. */
  assertAlive(label: string): void {
    this.check()

    if (!this.deathAt) {return}
    const logs = path.join(this.youtabHome, 'logs')
    throw new Error(
      `bundled backend pid=${this.pid} exited unexpectedly (observed by ${this.deathAt}; checkpoint: ${label})\n` +
      `--- backend-lifecycle.log ---\n${tail(path.join(logs, 'backend-lifecycle.log'))}\n` +
      `--- backend-stdio-${this.pid}.log ---\n${tail(path.join(logs, `backend-stdio-${this.pid}.log`))}\n` +
      `--- desktop.log ---\n${tail(path.join(logs, 'desktop.log'), 30)}`,
    )
  }

  stop(): void {
    if (this.timer) {clearInterval(this.timer)}
    this.timer = null
  }
}
