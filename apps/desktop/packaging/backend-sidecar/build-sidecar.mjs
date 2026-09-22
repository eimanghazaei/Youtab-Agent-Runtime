#!/usr/bin/env node
// Reproducible build of the self-contained Youtab backend sidecar.
//
// Freezes the existing `tui_gateway.entry` backend into a PyInstaller `onedir`
// bundle that runs with NO repository, venv, uv, Python or developer PATH — the
// binary the packaged Electron app resolves from `process.resourcesPath`.
//
// This script is the reproducible build specification: it pins the PyInstaller
// version and the exact invocation, then emits a manifest (per-file SHA-256 +
// sizes + total bundle size) and an SBOM (bundled Python dependency inventory).
// It commits only source; the produced binaries/manifests are build outputs and
// are git-ignored.
//
// Usage (from repo root, with the backend venv already `uv sync`-ed):
//   node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs
//
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, rmSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'

const PYINSTALLER_VERSION = '6.22.3' // pinned — do not float
const REPO_ROOT = resolve(process.cwd())
const VENV_PY = process.platform === 'win32'
  ? join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')
  : join(REPO_ROOT, '.venv', 'bin', 'python')
const ENTRY = join(REPO_ROOT, 'tui_gateway', 'entry.py')
const OUT_DIR = join(REPO_ROOT, 'apps', 'desktop', 'build', 'backend-sidecar')
const DIST = join(OUT_DIR, 'dist')
const WORK = join(OUT_DIR, 'build')
const NAME = 'youtab-backend'

function sh(bin, args) {
  return execFileSync(bin, args, { cwd: REPO_ROOT, stdio: 'pipe', encoding: 'utf8', maxBuffer: 1 << 28 })
}
const sha256 = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
function walk(d, acc = []) {
  for (const n of readdirSync(d)) {
    const p = join(d, n)
    statSync(p).isDirectory() ? walk(p, acc) : acc.push(p)
  }
  return acc
}

if (!existsSync(VENV_PY)) {
  console.error(`[sidecar] backend venv missing at ${VENV_PY}. Run: uv sync`)
  process.exit(2)
}
mkdirSync(OUT_DIR, { recursive: true })
if (existsSync(DIST)) rmSync(DIST, { recursive: true, force: true })

console.log(`[sidecar] pinning PyInstaller==${PYINSTALLER_VERSION}`)
sh('uv', ['pip', 'install', `pyinstaller==${PYINSTALLER_VERSION}`])

console.log('[sidecar] freezing tui_gateway backend (onedir)')
sh(VENV_PY, [
  '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', NAME,
  '--distpath', DIST, '--workpath', WORK, '--specpath', WORK,
  '--collect-submodules', 'tui_gateway',
  '--collect-submodules', 'youtab_runtime',
  '--collect-submodules', 'gateway',
  '--collect-data', 'tui_gateway',
  ENTRY,
])

const bundleRoot = join(DIST, NAME)
const exe = join(bundleRoot, process.platform === 'win32' ? `${NAME}.exe` : NAME)
if (!existsSync(exe)) {
  console.error('[sidecar] freeze produced no executable — failing closed')
  process.exit(3)
}
const files = walk(bundleRoot).sort()
let total = 0
const manifestFiles = files.map((p) => {
  const size = statSync(p).size
  total += size
  return { path: relative(bundleRoot, p).split('\\').join('/'), bytes: size, sha256: sha256(p) }
})
const manifest = {
  schema: 'youtab.backend_sidecar_manifest/v1',
  name: NAME,
  entry: 'tui_gateway.entry',
  pyinstaller: PYINSTALLER_VERSION,
  platform: process.platform,
  arch: process.arch,
  generated_utc: new Date().toISOString(),
  executable: relative(bundleRoot, exe).split('\\').join('/'),
  file_count: manifestFiles.length,
  total_bytes: total,
  files: manifestFiles,
}
writeFileSync(join(OUT_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2))

// SBOM: bundled Python dependency inventory (name+version) from the venv.
let sbom = { schema: 'youtab.backend_sidecar_sbom/v1', generated_utc: new Date().toISOString(), packages: [] }
try {
  const list = JSON.parse(sh('uv', ['pip', 'list', '--format', 'json']))
  sbom.packages = list.map((p) => ({ name: p.name, version: p.version }))
} catch (e) {
  sbom.error = String(e)
}
writeFileSync(join(OUT_DIR, 'sbom.json'), JSON.stringify(sbom, null, 2))

console.log(`[sidecar] OK — ${manifest.file_count} files, ${(total / 1048576).toFixed(1)} MiB total`)
console.log(`[sidecar] exe: ${exe}`)
console.log(`[sidecar] manifest: ${join(OUT_DIR, 'manifest.json')}`)
console.log(`[sidecar] sbom: ${join(OUT_DIR, 'sbom.json')} (${sbom.packages.length} packages)`)
