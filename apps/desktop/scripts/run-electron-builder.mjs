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
// extraResources → <resourcesPath>/backend-sidecar so the packaged app runs the
// backend without a system Python/uv.
//
// TWO EXPLICIT MODES (fail-closed by default):
//   • RELEASE / qualification (default): the sidecar bundle, manifest.json,
//     sbom.json and sidecar-root-digest.txt MUST be present, and the bundle's
//     recomputed root digest MUST equal the committed digest AND the embedded
//     Electron trust anchor. Any gap aborts the build — a production installer
//     can never silently ship a shell-only package.
//   • DEVELOPER (opt-in only): pass --allow-no-sidecar (or set
//     YOUTAB_AGENT_DESKTOP_ALLOW_NO_SIDECAR=1) to package without the sidecar,
//     e.g. for a UI-only local build. Never used by dist:*.
//
// NOTE — this gate proves INTEGRITY (the shipped bytes match the committed
// digest and the code's trust anchor). It does NOT assert PROVENANCE /
// authenticity: there is no code-signature over the digest here, so it does not
// prove the digest itself came from the controlled build. Signing is out of
// scope and not claimed.
const passthrough = process.argv.slice(2)
const allowNoSidecar =
  passthrough.includes("--allow-no-sidecar") || process.env.YOUTAB_AGENT_DESKTOP_ALLOW_NO_SIDECAR === "1"
const sidecarPassthrough = passthrough.filter((a) => a !== "--allow-no-sidecar")

const sidecarOutDir = path.resolve("build/backend-sidecar")
const sidecarBundle = path.join(sidecarOutDir, "dist/youtab-backend")
const sidecarExe = path.join(sidecarBundle, process.platform === "win32" ? "youtab-backend.exe" : "youtab-backend")

function abort(msg) {
  console.error(`[run-electron-builder] RELEASE PACKAGING ABORTED (fail-closed): ${msg}`)
  console.error("[run-electron-builder] build the sidecar first (node apps/desktop/packaging/backend-sidecar/build-sidecar.mjs),")
  console.error("[run-electron-builder] or pass --allow-no-sidecar for an explicit developer (shell-only) package.")
  process.exit(3)
}

async function enforceSidecar() {
  const required = [
    [sidecarExe, "frozen backend executable"],
    [path.join(sidecarOutDir, "manifest.json"), "manifest.json"],
    [path.join(sidecarOutDir, "sbom.json"), "sbom.json"],
    [path.join(sidecarOutDir, "sidecar-root-digest.txt"), "sidecar-root-digest.txt"]
  ]
  for (const [p, label] of required) {
    if (!fs.existsSync(p)) abort(`missing ${label} at ${p}`)
  }

  // Recompute the bundle digest and require it to equal BOTH the committed
  // digest file AND the embedded Electron trust anchor.
  const { rootDigestFromBundle, parseTrustedDigest } = await import(
    path.resolve("packaging/backend-sidecar/root-digest.mjs")
  )
  const committed = parseTrustedDigest(fs.readFileSync(path.join(sidecarOutDir, "sidecar-root-digest.txt"), "utf8"))
  if (!committed) abort("sidecar-root-digest.txt is malformed")

  const actual = rootDigestFromBundle(sidecarBundle)
  if (actual !== committed) abort(`recomputed root digest ${actual} != committed ${committed}`)

  const anchorSrc = fs.readFileSync(path.resolve("electron/sidecar-trusted-digest.ts"), "utf8")
  const anchorMatch = anchorSrc.match(/TRUSTED_SIDECAR_ROOT_DIGEST[^'"]*['"]([0-9a-f]{64})['"]/)
  const anchor = anchorMatch ? anchorMatch[1] : null
  if (!anchor) abort("Electron trust anchor TRUSTED_SIDECAR_ROOT_DIGEST is not pinned (null)")
  if (anchor !== actual) abort(`Electron trust anchor ${anchor} != bundle digest ${actual}`)

  console.log(`[run-electron-builder] sidecar integrity OK — root digest ${actual} matches committed + trust anchor`)
  args.push(
    "-c.extraResources.2.from=build/backend-sidecar/dist/youtab-backend",
    "-c.extraResources.2.to=backend-sidecar"
  )
}

if (allowNoSidecar) {
  console.warn("[run-electron-builder] --allow-no-sidecar: DEVELOPER package WITHOUT a bundled backend (shell-only).")
} else {
  await enforceSidecar()
}

args.push(...sidecarPassthrough)

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  stdio: "inherit",
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
