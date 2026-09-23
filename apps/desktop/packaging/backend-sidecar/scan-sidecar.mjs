#!/usr/bin/env node
// A2 bundle security scan of the frozen backend sidecar. Read-only. Fails
// (exit 1) if the bundle contains anything that must never ship: secrets,
// private keys/tokens, machine-local developer paths, logs/databases/caches,
// test fixtures/EICAR, developer-only tools, or forbidden attribution.
// Emits scan-report.json (git-ignored build output).
//
// The rule set and traversal are exported as a pure `scanBundle(root)` so they
// can be exercised by positive/negative self-tests over synthetic fixtures
// (no PyInstaller freeze required); the CLI wrapper below just runs it against
// the real build output and sets the process exit code.
//
// Usage: node apps/desktop/packaging/backend-sidecar/scan-sidecar.mjs
//
import { closeSync, constants, existsSync, fstatSync, lstatSync, openSync, readdirSync, readSync, realpathSync, writeFileSync } from 'node:fs'
import { basename, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

// EICAR test signature (must never be present). Split so this source file does
// not itself contain the full trigger string.
export const EICAR = 'X5O!P%@AP[4\\PZX54(P^)7CC)7}$' + 'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'

// forbidden filename patterns. NOTE: .pem/.crt are NOT flagged by extension —
// public CA bundles (e.g. certifi/cacert.pem) legitimately ship; real private
// keys are caught by the BEGIN PRIVATE KEY content rule below. Key STORES
// (.pfx/.p12) and env/log/db files are always forbidden.
const badNames = [/^\.env(\.|$)/i, /\.pfx$/i, /\.p12$/i, /_rsa$/i, /_dsa$/i, /_ed25519$/i, /\.log$/i, /\.sqlite3?$/i, /(^|\.)db$/i]
// machine-local dev paths: FULL developer-machine absolute paths only (bare
// tokens like ".venv"/"worktrees" appear in legit README/doc text).
const devPaths = [/C:\\Users\\eiman/i, /[\\/]rt-px-dep[\\/]/i, /AppData[\\/]Local[\\/]Temp[\\/]claude/i]
// secret-ish content
const secretPat = [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, /xox[baprs]-[0-9A-Za-z-]+/, /ghp_[0-9A-Za-z]{20,}/, /AKIA[0-9A-Z]{16}/, /aws_secret_access_key/i, /["']?(api[_-]?key|secret|token|password)["']?\s*[:=]\s*["'][A-Za-z0-9/_+.=-]{16,}["']/]
// forbidden attribution
const attribPat = [/Co-Authored-By:\s*Claude/i, /Generated with \[?Claude/i]
const maxScanBytes = 8 * 1024 * 1024

function walk(d, files = [], links = []) {
  for (const n of readdirSync(d)) {
    const p = join(d, n)
    const st = lstatSync(p)
    if (st.isSymbolicLink()) links.push(p)
    else if (st.isDirectory()) walk(p, files, links)
    else files.push(p)
  }

  return { files, links }
}

/**
 * Scan a bundle directory and return { scanned_files, findings, findings_by_severity }.
 * Pure (no process exit, no file writes) so it is unit-testable.
 */
export function scanBundle(bundleRoot) {
  // Visit each physical directory once. Valid in-bundle symlinks are aliases
  // of paths visited this way; following directory aliases would duplicate
  // scans or recurse forever on a link back to an ancestor.
  const rootReal = realpathSync(bundleRoot)
  const { files, links } = walk(bundleRoot)
  const findings = []
  const add = (severity, rule, file, detail) =>
    findings.push({ severity, rule, file: relative(bundleRoot, file).split('\\').join('/'), detail })

  for (const link of links) {
    const name = basename(link)
    if (badNames.some(re => re.test(name))) add('high', 'forbidden-filename', link, name)
    try {
      const target = realpathSync(link)
      const targetRel = relative(rootReal, target)
      if (targetRel === '..' || targetRel.startsWith(`..${sep}`) || isAbsolute(targetRel)) {
        add('high', 'unsafe-symlink', link, 'target escapes bundle')
      }
    } catch {
      add('high', 'unsafe-symlink', link, 'target is dangling or cyclic')
    }
  }

  for (const f of files) {
    const name = basename(f)

    if (badNames.some(re => re.test(name))) {
      add('high', 'forbidden-filename', f, name)
    }

    let buf
    let fd

    try {
      // Keep metadata and content on the same opened file. O_NOFOLLOW stops a
      // symlink from redirecting the scan outside the bundle between steps.
      fd = openSync(f, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
      const st = fstatSync(fd)

      if (!st.isFile() || st.size > maxScanBytes) {
        continue
      }

      buf = Buffer.alloc(st.size)
      let offset = 0
      while (offset < st.size) {
        const count = readSync(fd, buf, offset, st.size - offset, null)
        if (count === 0) throw new Error('file shortened during scan')
        offset += count
      }
      const after = fstatSync(fd)
      if (after.size !== st.size || after.mtimeMs !== st.mtimeMs) {
        throw new Error('file changed during scan')
      }
    } catch {
      add('high', 'scan-unreadable', f, 'file could not be scanned safely')
      continue
    } finally {
      if (fd !== undefined) closeSync(fd)
    }

    // skip obvious binaries for content scan (but still name-checked above)
    const nul = buf.indexOf(0)
    const isBinary = nul !== -1 && nul < 4096
    const text = buf.toString('latin1')

    if (text.includes(EICAR)) {
      add('critical', 'eicar', f, 'EICAR test signature')
    }

    if (isBinary) {
      continue
    }

    for (const re of secretPat) if (re.test(text)) add('high', 'secret-material', f, re.source.slice(0, 40))
    for (const re of devPaths) if (re.test(text)) add('high', 'machine-local-path', f, re.source)
    for (const re of attribPat) if (re.test(text)) add('medium', 'forbidden-attribution', f, re.source)
    if (text.toLowerCase().includes('anthropic.com')) {
      add('medium', 'forbidden-attribution', f, 'anthropic.com')
    }
  }

  const findings_by_severity = findings.reduce((a, f) => ((a[f.severity] = (a[f.severity] || 0) + 1), a), {})

  return { scanned_files: files.length, findings, findings_by_severity }
}

/** Count of blocking (critical/high) findings — the gate criterion. */
export function blockingCount(findings) {
  return findings.filter(f => f.severity === 'critical' || f.severity === 'high').length
}

// ---- CLI ------------------------------------------------------------------
function main() {
  const REPO_ROOT = resolve(process.cwd())
  const OUT_DIR = join(REPO_ROOT, 'apps', 'desktop', 'build', 'backend-sidecar')
  const bundleRoot = join(OUT_DIR, 'dist', 'youtab-backend')

  if (!existsSync(bundleRoot)) {
    console.error(`[scan] no bundle at ${bundleRoot} — run build-sidecar.mjs first`)
    process.exit(2)
  }

  const { scanned_files, findings, findings_by_severity } = scanBundle(bundleRoot)
  const report = {
    schema: 'youtab.backend_sidecar_scan/v1',
    generated_utc: new Date().toISOString(),
    bundle: relative(REPO_ROOT, bundleRoot).split('\\').join('/'),
    scanned_files,
    findings_by_severity,
    findings
  }
  writeFileSync(join(OUT_DIR, 'scan-report.json'), JSON.stringify(report, null, 2))
  console.log(`[scan] scanned ${scanned_files} files; findings: ${JSON.stringify(findings_by_severity)}`)
  for (const f of findings.slice(0, 20)) console.log(`  [${f.severity}] ${f.rule}: ${f.file} (${f.detail})`)
  process.exit(blockingCount(findings) > 0 ? 1 : 0)
}

// Only run the CLI when invoked directly (not when imported by a test).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
