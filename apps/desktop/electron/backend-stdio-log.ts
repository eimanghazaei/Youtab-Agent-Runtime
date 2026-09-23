/**
 * backend-stdio-log.ts
 *
 * Per-process, append-only record of the managed backend's raw stdout/stderr
 * plus its spawn and exit, keyed by pid. The desktop log is buffered and can
 * lose its tail when the app closes; this file is written synchronously so a
 * backend that disappears always leaves its last output and exit code on disk.
 *
 * Dependency-free (no electron import) so it is unit-testable with a temp dir.
 */
import fs from 'node:fs'
import path from 'node:path'

export const BACKEND_STDIO_LOG_PREFIX = 'backend-stdio-'
const KEEP_FILES = 10

export interface BackendStdioLog {
  file: string
  output: (stream: 'stdout' | 'stderr', chunk: unknown) => void
  event: (text: string) => void
}

function stamp(): string {
  return new Date().toISOString()
}

function pruneOld(dir: string, keep: number): void {
  try {
    const files = fs
      .readdirSync(dir)
      .filter(name => name.startsWith(BACKEND_STDIO_LOG_PREFIX) && name.endsWith('.log'))
      .map(name => ({ name, mtime: fs.statSync(path.join(dir, name)).mtimeMs }))
      .sort((a, b) => b.mtime - a.mtime)

    for (const { name } of files.slice(keep)) {
      fs.rmSync(path.join(dir, name), { force: true })
    }
  } catch {
    // Diagnostics must never break backend startup.
  }
}

export function openBackendStdioLog(logDir: string, pid: number | undefined, keep = KEEP_FILES): BackendStdioLog {
  const file = path.join(logDir, `${BACKEND_STDIO_LOG_PREFIX}${pid ?? 'unknown'}.log`)

  const append = (text: string) => {
    try {
      fs.mkdirSync(logDir, { recursive: true })
      fs.appendFileSync(file, text)
    } catch {
      // Diagnostics must never break backend startup.
    }
  }

  pruneOld(logDir, Math.max(1, keep - 1))

  return {
    file,
    output: (stream, chunk) => {
      const text = String(chunk ?? '')

      if (text) {
        append(`${stamp()} [${stream}] ${text.endsWith('\n') ? text : `${text}\n`}`)
      }
    },
    event: text => append(`${stamp()} [event] ${text}\n`)
  }
}
