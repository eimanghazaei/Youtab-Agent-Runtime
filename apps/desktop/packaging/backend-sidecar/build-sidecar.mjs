#!/usr/bin/env node
// Reproducible, self-provisioning build of the self-contained Youtab backend
// sidecar. Does NOT depend on PyInstaller being preinstalled in the active venv:
// it creates an ISOLATED build environment from the committed lockfile, pins the
// build tool, freezes `tui_gateway.entry` into a PyInstaller `onedir` bundle, and
// emits a per-file manifest (+ root digest) and an SBOM. Source-only; the build
// outputs under apps/desktop/build/ are git-ignored.
//
// Usage (from repo root; needs `uv` on PATH — nothing else):
//   node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs
//
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'

import { manifestEntriesFromBundle, rootDigestFromEntries } from './root-digest.mjs'

const PYINSTALLER_VERSION = '6.22.3' // pinned — do not float
// Pin the interpreter for the isolated build venv. The project supports
// >=3.11,<3.14; we build on 3.12 explicitly so the frozen bundle does not drift
// with whatever python the ambient `uv` happens to default to (an unpinned
// `uv venv` produced 3.12.10 under uv 0.12.17 but 3.11.9 under uv 0.8.17, which
// changed the root digest). Override with SIDECAR_BUILD_PYTHON if a release
// must target a different supported minor.
const SIDECAR_PYTHON = process.env.SIDECAR_BUILD_PYTHON || '3.12'
const REPO_ROOT = resolve(process.cwd())
// Freeze the SAME backend entrypoint Electron runs in dev (`youtab_agent_cli.main`)
// so the packaged executable serves the existing HTTP loopback gateway
// (`serve --host 127.0.0.1 --port 0` → port announcement + /api/health). A thin
// launcher preserves the youtab_agent_cli package context when frozen.
const ENTRY = join(REPO_ROOT, 'apps', 'desktop', 'packaging', 'backend-sidecar', 'sidecar_main.py')
const OUT_DIR = join(REPO_ROOT, 'apps', 'desktop', 'build', 'backend-sidecar')
const BUILD_VENV = join(OUT_DIR, '.buildvenv') // isolated; NOT the dev .venv
const DIST = join(OUT_DIR, 'dist')
const WORK = join(OUT_DIR, 'build')
const NAME = 'youtab-backend'
const VENV_PY = join(BUILD_VENV, process.platform === 'win32' ? 'Scripts' : 'bin', process.platform === 'win32' ? 'python.exe' : 'python')

function sh(bin, args, env) {
  return execFileSync(bin, args, { cwd: REPO_ROOT, stdio: 'pipe', encoding: 'utf8', maxBuffer: 1 << 28, env: { ...process.env, ...env } })
}
const sha256 = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
const trim = (s) => String(s).trim()

mkdirSync(OUT_DIR, { recursive: true })
if (existsSync(DIST)) rmSync(DIST, { recursive: true, force: true })
if (existsSync(BUILD_VENV)) rmSync(BUILD_VENV, { recursive: true, force: true })

const uvVersion = trim(sh('uv', ['--version']))
console.log(`[sidecar] uv: ${uvVersion}`)

// 1) isolated build venv from the committed lockfile (no dev-venv dependency)
console.log(`[sidecar] creating isolated build venv (python ${SIDECAR_PYTHON})`)
sh('uv', ['venv', '--python', SIDECAR_PYTHON, BUILD_VENV])
const pyVersion = trim(sh(VENV_PY, ['--version']))
console.log(`[sidecar] python: ${pyVersion}`)
// application + runtime deps from the frozen lockfile, into the isolated venv
console.log('[sidecar] uv sync --frozen (app deps from lockfile) into isolated venv')
sh('uv', ['sync', '--frozen'], { UV_PROJECT_ENVIRONMENT: BUILD_VENV, VIRTUAL_ENV: BUILD_VENV })
// pinned build tool, isolated
sh('uv', ['pip', 'install', `pyinstaller==${PYINSTALLER_VERSION}`], { VIRTUAL_ENV: BUILD_VENV })
const piVersion = trim(sh(VENV_PY, ['-m', 'PyInstaller', '--version']))
console.log(`[sidecar] pyinstaller: ${piVersion}`)
if (piVersion !== PYINSTALLER_VERSION) { console.error(`[sidecar] pyinstaller version drift: ${piVersion} != ${PYINSTALLER_VERSION}`); process.exit(4) }

// 2) freeze (onedir). Collect the whole first-party backend import graph plus
//    the dynamically-imported server stack (uvicorn workers, provider plugins)
//    that PyInstaller's static analysis would otherwise miss.
console.log('[sidecar] freezing youtab_agent_cli backend (serve; onedir)')
sh(VENV_PY, [
  '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', NAME,
  '--distpath', DIST, '--workpath', WORK, '--specpath', WORK,
  '--collect-submodules', 'youtab_agent_cli',
  '--collect-submodules', 'tui_gateway',
  '--collect-submodules', 'youtab_runtime',
  '--collect-submodules', 'gateway',
  '--collect-data', 'youtab_agent_cli', '--collect-data', 'tui_gateway',
  '--collect-all', 'uvicorn', '--collect-all', 'fastapi', '--collect-all', 'starlette',
  ENTRY,
], { VIRTUAL_ENV: BUILD_VENV })

const bundleRoot = join(DIST, NAME)
const exe = join(bundleRoot, process.platform === 'win32' ? `${NAME}.exe` : NAME)
if (!existsSync(exe)) { console.error('[sidecar] no executable produced — fail closed'); process.exit(3) }

// 3) per-file manifest + deterministic root digest (canonical algorithm shared
//    with the offline verifier and the Electron trusted-binding check).
const manifestFiles = manifestEntriesFromBundle(bundleRoot)
const total = manifestFiles.reduce((n, f) => n + f.bytes, 0)
const rootDigest = rootDigestFromEntries(manifestFiles)
const manifest = {
  // `entry` is the real frozen module: youtab_agent_cli.main (the same `serve`
  // backend Electron runs in dev), launched via the sidecar_main.py wrapper.
  schema: 'youtab.backend_sidecar_manifest/v1', name: NAME,
  entry: 'youtab_agent_cli.main', entry_launcher: 'apps/desktop/packaging/backend-sidecar/sidecar_main.py',
  build: { python: pyVersion, uv: uvVersion, pyinstaller: piVersion, lockfile: 'uv.lock', frozen: true },
  platform: process.platform, arch: process.arch, generated_utc: new Date().toISOString(),
  executable: relative(bundleRoot, exe).split('\\').join('/'), file_count: manifestFiles.length,
  total_bytes: total, root_digest_sha256: rootDigest, files: manifestFiles,
}
writeFileSync(join(OUT_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2))

// 4) SBOM (bundled dependency inventory from the isolated build venv)
let sbom = { schema: 'youtab.backend_sidecar_sbom/v1', generated_utc: new Date().toISOString(), python: pyVersion, packages: [] }
try { sbom.packages = JSON.parse(sh('uv', ['pip', 'list', '--format', 'json'], { VIRTUAL_ENV: BUILD_VENV })).map((p) => ({ name: p.name, version: p.version })) } catch (e) { sbom.error = String(e) }
writeFileSync(join(OUT_DIR, 'sbom.json'), JSON.stringify(sbom, null, 2))

// 5) the root digest as a standalone trusted-binding input (A2 embeds this in Electron)
writeFileSync(join(OUT_DIR, 'sidecar-root-digest.txt'), rootDigest + '\n')

const manifestSha = sha256(join(OUT_DIR, 'manifest.json'))
const sbomSha = sha256(join(OUT_DIR, 'sbom.json'))
console.log(`[sidecar] OK — ${manifest.file_count} files, ${(total / 1048576).toFixed(1)} MiB`)
console.log(`[sidecar] python=${pyVersion} uv=${uvVersion} pyinstaller=${piVersion}`)
console.log(`[sidecar] root_digest=${rootDigest}`)
console.log(`[sidecar] manifest.json sha256=${manifestSha}`)
console.log(`[sidecar] sbom.json sha256=${sbomSha}`)
