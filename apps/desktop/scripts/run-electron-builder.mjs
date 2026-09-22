// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

const dist = electronDistDir()
const args = []
if (dist && fs.existsSync(distBinary(dist))) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no local electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}

// Self-contained backend sidecar (Lane 3): ship the frozen onedir bundle via
// extraResources → <resourcesPath>/backend-sidecar so the packaged app can run
// the backend without a system Python/uv. The bundle is a heavy PyInstaller
// output built out of band (`node packaging/backend-sidecar/build-sidecar.mjs`)
// and git-ignored, so we APPEND the extraResources entry only when it is
// actually present — a normal `npm run dist` without the sidecar built stays
// unaffected. Appending at index 2 preserves the two static entries in
// package.json (install-stamp.json, icon.ico) instead of replacing the array.
const sidecarBundle = path.resolve("build/backend-sidecar/dist/youtab-backend")
if (fs.existsSync(sidecarBundle)) {
  console.log(`[run-electron-builder] shipping backend sidecar from ${sidecarBundle}`)
  args.push(
    "-c.extraResources.2.from=build/backend-sidecar/dist/youtab-backend",
    "-c.extraResources.2.to=backend-sidecar"
  )
} else {
  console.warn(
    "[run-electron-builder] no backend sidecar bundle at build/backend-sidecar/dist/youtab-backend; " +
      "packaging without it (run packaging/backend-sidecar/build-sidecar.mjs to include it)."
  )
}

args.push(...process.argv.slice(2))

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  stdio: "inherit",
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
