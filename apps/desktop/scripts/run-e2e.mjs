#!/usr/bin/env node
// Deterministic, repository-managed environment for the Electron E2E suite.
//
// The dev-mode E2E fixtures spawn the real Python backend two ways:
//   • the desktop backend  → `python -m youtab_agent_cli.main serve`
//     (YOUTAB_AGENT_DESKTOP_PYTHON picks the interpreter)
//   • the real-session gateway → `uv run --active python -m tui_gateway.entry`
//     (uses the active VIRTUAL_ENV)
// Both need the project's Python dependencies (fastapi, uvicorn, openai,
// python-dotenv, …). Rather than install anything ad-hoc into a system Python,
// this runner points BOTH at the lockfile-derived virtual environment that
// `build-sidecar.mjs` provisions with `uv sync --frozen` (from uv.lock) — so the
// exact same pinned dependency set that ships frozen in the sidecar is what the
// E2E backend runs. It sets VIRTUAL_ENV, YOUTAB_AGENT_DESKTOP_PYTHON and PATH
// explicitly, then execs Playwright and propagates its real exit code.
//
// Prerequisites (both are existing Lane-3 build prerequisites, not new deps):
//   • the sidecar build venv exists — run
//       node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs
//     (its `uv sync --frozen` step creates build/backend-sidecar/.buildvenv)
//   • `uv` is on PATH (same tool build-sidecar.mjs already requires)
//
// Usage (from apps/desktop):  node scripts/run-e2e.mjs [playwright args…]
//   e.g.  node scripts/run-e2e.mjs e2e/ --reporter=line --workers=1
import { spawnSync, execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { delimiter, join, resolve, dirname } from 'node:path'

const DESKTOP_ROOT = resolve(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..')
const VENV = join(DESKTOP_ROOT, 'build', 'backend-sidecar', '.buildvenv')
const VENV_SCRIPTS = join(VENV, process.platform === 'win32' ? 'Scripts' : 'bin')
const VENV_PY = join(VENV_SCRIPTS, process.platform === 'win32' ? 'python.exe' : 'python')

if (!existsSync(VENV_PY)) {
  console.error(`[run-e2e] lockfile-derived venv missing: ${VENV_PY}`)
  console.error('[run-e2e] build the sidecar first (its `uv sync --frozen` creates it):')
  console.error('[run-e2e]   node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs')
  process.exit(2)
}

// `uv` must be resolvable for the real-session gateway (`uv run --active`).
let uvDir = null
try {
  const uvPath = execFileSync(process.platform === 'win32' ? 'where' : 'which', ['uv'], { encoding: 'utf8' })
    .split(/\r?\n/)[0]
    .trim()
  if (uvPath) uvDir = dirname(uvPath)
} catch {
  console.error('[run-e2e] `uv` is not on PATH — required for the real-session gateway (uv run --active).')
  process.exit(2)
}

// Verify the venv actually carries the backend + provider deps up front, so a
// missing dep is a clear error here rather than a 90s backend-boot timeout.
try {
  execFileSync(VENV_PY, ['-c', 'import youtab_agent_cli, fastapi, uvicorn, openai, dotenv, tui_gateway'], {
    stdio: 'pipe',
    env: { ...process.env, PYTHONPATH: resolve(DESKTOP_ROOT, '..', '..') }
  })
} catch (e) {
  console.error('[run-e2e] the lockfile venv is missing backend/provider deps — re-run build-sidecar.mjs (uv sync --frozen).')
  console.error(String(e.stderr || e.message || e).slice(0, 400))
  process.exit(2)
}

const env = {
  ...process.env,
  VIRTUAL_ENV: VENV,
  YOUTAB_AGENT_DESKTOP_PYTHON: VENV_PY,
  PATH: [VENV_SCRIPTS, uvDir, process.env.PATH].filter(Boolean).join(delimiter)
}

console.log(`[run-e2e] VIRTUAL_ENV=${VENV}`)
console.log(`[run-e2e] YOUTAB_AGENT_DESKTOP_PYTHON=${VENV_PY}`)
console.log(`[run-e2e] uv dir on PATH=${uvDir}`)

const args = process.argv.slice(2)
const pwArgs = args.length ? args : ['e2e/', '--reporter=line']
const result = spawnSync('npx', ['playwright', 'test', ...pwArgs], {
  cwd: DESKTOP_ROOT,
  env,
  stdio: 'inherit',
  shell: process.platform === 'win32'
})

process.exit(result.status == null ? 1 : result.status)
