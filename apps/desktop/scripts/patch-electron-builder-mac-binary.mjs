import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

if (process.platform !== 'darwin') {
  process.exit(0)
}

const desktopRoot = path.resolve(import.meta.dirname, '..')
const repoRoot = path.resolve(desktopRoot, '..', '..')
const electronMacPath = path.join(repoRoot, 'node_modules', 'app-builder-lib', 'out', 'electron', 'electronMac.js')

const marker = 'youtab-macos-electron-binary-fallback'
const needle = `    await Promise.all([
        doRename(path.join(contentsPath, "MacOS"), electronBranding.productName, appPlist.CFBundleExecutable),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSE")),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSES.chromium.html")),
    ]);`
const replacement = `    // ${marker}: electron-builder 26.8.x can sometimes copy
    // Electron.app without its main MacOS/Electron binary before this rename.
    // Restore it from the installed Electron runtime so local desktop installs
    // do not fail with ENOENT during macOS arm64 packaging.
    const macosDir = path.join(contentsPath, "MacOS");
    const bundledElectronBinary = path.join(macosDir, electronBranding.productName);
    if (!fs.existsSync(bundledElectronBinary)) {
        const candidates = [
            path.join(packager.info.framework.distMacOsAppName, "Contents", "MacOS", electronBranding.productName),
            // npm may nest the workspace-only electron devDep under
            // apps/desktop/node_modules (process.cwd() during pack), or hoist
            // it to the repo root. Try the workspace-local install first, then
            // the root hoist, so the fallback works under either layout.
            path.join(process.cwd(), "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
            path.join(process.cwd(), "..", "..", "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
        ];
        const sourceBinary = candidates.find(candidate => fs.existsSync(candidate));
        if (sourceBinary == null) {
            throw new Error("Electron binary missing from packaged app and Electron runtime: " + bundledElectronBinary);
        }
        await (0, promises_1.copyFile)(sourceBinary, bundledElectronBinary);
        await (0, promises_1.chmod)(bundledElectronBinary, 0o755);
    }
    await Promise.all([
        doRename(macosDir, electronBranding.productName, appPlist.CFBundleExecutable),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSE")),
        (0, builder_util_1.unlinkIfExists)(path.join(appOutDir, "LICENSES.chromium.html")),
    ]);`

let source
let sourceMode
let sourceFd

try {
  sourceFd = fs.openSync(electronMacPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW)
  const stat = fs.fstatSync(sourceFd)
  if (!stat.isFile()) throw new Error(`Not a regular file: ${electronMacPath}`)
  sourceMode = stat.mode & 0o777
  source = fs.readFileSync(sourceFd, 'utf8')
} catch (error) {
  if (error?.code !== 'ENOENT') throw error
  console.warn(`[patch-electron-builder] skipped: ${electronMacPath} not found`)
  process.exit(0)
} finally {
  if (sourceFd !== undefined) fs.closeSync(sourceFd)
}
if (source.includes(marker)) {
  console.log('[patch-electron-builder] macOS Electron binary fallback already applied')
  process.exit(0)
}

if (!source.includes(needle)) {
  console.warn('[patch-electron-builder] skipped: expected electronMac.js shape not found')
  process.exit(0)
}

const stagedPath = `${electronMacPath}.youtab-${process.pid}-${crypto.randomUUID()}.tmp`
try {
  fs.writeFileSync(stagedPath, source.replace(needle, replacement), { flag: 'wx', mode: sourceMode })
  fs.renameSync(stagedPath, electronMacPath)
} finally {
  try { fs.unlinkSync(stagedPath) } catch { /* renamed or never created */ }
}
console.log('[patch-electron-builder] applied macOS Electron binary fallback')
