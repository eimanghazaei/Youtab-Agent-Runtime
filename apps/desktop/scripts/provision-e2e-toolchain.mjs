// Reproducibly provision the pinned E2E/packaging toolchain WITHOUT touching any
// production dependency pin:
//
//   1. `uv` — the pinned build tool used by the sidecar freeze
//      (build-sidecar.mjs) and the real-session E2E gateway. Provisioned from
//      PyPI (an authorized source) into an isolated venv under the desktop
//      build dir; version + sha256 are asserted against the pins below.
//   2. Electron — the devDependency binary (electron-builder ships it, but
//      `npm ci` skips the postinstall download in CI); fetched via the
//      package's own install.js and the version asserted against package.json.
//
// Usage:  node apps/desktop/scripts/provision-e2e-toolchain.mjs
// Prints an `E2E_UV_BIN=<path>` line the harness/CI can eval to put uv on PATH.
// Exits non-zero on any version/hash mismatch — a missing/wrong tool must fail
// the gate loudly, never silently degrade.
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')

// ── Pins (must match RUNTIME_LANE1_VERIFIER_CHECKLIST + build-sidecar.mjs) ──
const UV_VERSION = '0.8.17'
// sha256 of the win-x64 uv.exe shipped in the uv 0.8.17 PyPI wheel. Recorded on
// first provision; assert-only (a platform without a recorded hash warns).
const UV_SHA256_BY_PLATFORM = {
  win32: '7cfa09dd56e0ab2977cba654cf51eb025e43234d0a5b3c242fb8d06d180bc14b',
}

function sh(bin, args, opts = {}) {
  return execFileSync(bin, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts }).trim()
}

function sha256(file) {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex')
}

function provisionUv() {
  const isWin = process.platform === 'win32'
  const venvDir = path.join(DESKTOP_ROOT, 'build', 'e2e-toolchain', 'uv-venv')
  const uvBin = path.join(venvDir, isWin ? 'Scripts' : 'bin', isWin ? 'uv.exe' : 'uv')
  const py = process.env.E2E_BASE_PYTHON || (isWin ? 'python' : 'python3')

  if (!fs.existsSync(uvBin)) {
    fs.mkdirSync(path.dirname(venvDir), { recursive: true })
    sh(py, ['-m', 'venv', venvDir])
    const venvPy = path.join(venvDir, isWin ? 'Scripts' : 'bin', isWin ? 'python.exe' : 'python')
    sh(venvPy, ['-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', `uv==${UV_VERSION}`])
  }

  const reported = sh(uvBin, ['--version']) // e.g. "uv 0.8.17 (…)"
  if (!reported.startsWith(`uv ${UV_VERSION}`)) {
    throw new Error(`uv version mismatch: expected ${UV_VERSION}, got "${reported}"`)
  }
  const expectedHash = UV_SHA256_BY_PLATFORM[process.platform]
  const actualHash = sha256(uvBin)
  if (expectedHash && actualHash !== expectedHash) {
    throw new Error(`uv sha256 mismatch on ${process.platform}: expected ${expectedHash}, got ${actualHash}`)
  }
  console.log(`[provision] uv ${UV_VERSION} at ${uvBin} (sha256 ${actualHash}${expectedHash ? ' ✓' : ' — no pinned hash for this platform'})`)
  return uvBin
}

function provisionElectron() {
  const pkg = JSON.parse(fs.readFileSync(path.join(DESKTOP_ROOT, 'package.json'), 'utf8'))
  const pinned = String(pkg.devDependencies?.electron || '').replace(/^[^\d]*/, '')
  const electronPkgDir = path.join(REPO_ROOT, 'node_modules', 'electron')
  const distExe = path.join(electronPkgDir, 'dist', process.platform === 'win32' ? 'electron.exe' : 'electron')

  if (!fs.existsSync(distExe)) {
    // The electron package's own installer downloads the pinned binary.
    sh('node', [path.join(electronPkgDir, 'install.js')], { cwd: REPO_ROOT })
  }
  if (!fs.existsSync(distExe)) {
    throw new Error(`electron binary missing after install.js: ${distExe}`)
  }
  const pathTxt = path.join(electronPkgDir, 'path.txt')
  const reportedVersion = fs.existsSync(path.join(electronPkgDir, 'package.json'))
    ? JSON.parse(fs.readFileSync(path.join(electronPkgDir, 'package.json'), 'utf8')).version
    : ''
  if (pinned && reportedVersion && !reportedVersion.startsWith(pinned)) {
    throw new Error(`electron version mismatch: package.json pins ${pinned}, installed ${reportedVersion}`)
  }
  const size = fs.statSync(distExe).size
  console.log(`[provision] electron ${reportedVersion || '(unknown)'} binary at ${distExe} (${size} bytes; path.txt=${fs.existsSync(pathTxt)})`)
  return distExe
}

try {
  const uvBin = provisionUv()
  provisionElectron()
  // A line CI/harness can eval to prepend uv to PATH.
  console.log(`E2E_UV_BIN=${uvBin}`)
  process.exit(0)
} catch (err) {
  console.error(`[provision] FAILED: ${err instanceof Error ? err.message : String(err)}`)
  process.exit(1)
}
