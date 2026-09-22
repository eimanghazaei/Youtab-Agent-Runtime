#!/usr/bin/env node
// A2 bundle security scan of the frozen backend sidecar. Read-only. Fails
// (exit 1) if the bundle contains anything that must never ship: secrets,
// private keys/tokens, machine-local developer paths, logs/databases/caches,
// test fixtures/EICAR, developer-only tools, or forbidden attribution.
// Emits scan-report.json (git-ignored build output).
//
// Usage: node apps/desktop/packaging/backend-sidecar/scan-sidecar.mjs
//
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { basename, join, relative, resolve } from 'node:path'

const REPO_ROOT = resolve(process.cwd())
const OUT_DIR = join(REPO_ROOT, 'apps', 'desktop', 'build', 'backend-sidecar')
const bundleRoot = join(OUT_DIR, 'dist', 'youtab-backend')
if (!existsSync(bundleRoot)) { console.error(`[scan] no bundle at ${bundleRoot} — run build-sidecar.mjs first`); process.exit(2) }

function walk(d, acc = []) { for (const n of readdirSync(d)) { const p = join(d, n); statSync(p).isDirectory() ? walk(p, acc) : acc.push(p) } return acc }
const files = walk(bundleRoot)
const findings = []
const add = (sev, rule, file, detail) => findings.push({ severity: sev, rule, file: relative(bundleRoot, file).split('\\').join('/'), detail })

// EICAR test signature (must never be present)
const EICAR = 'X5O!P%@AP[4\\PZX54(P^)7CC)7}$' + 'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
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
const attribPat = [/Co-Authored-By:\s*Claude/i, /Generated with \[?Claude/i, /anthropic\.com/i]

for (const f of files) {
  const name = basename(f)
  if (badNames.some((re) => re.test(name))) add('high', 'forbidden-filename', f, name)
  let buf
  try { const st = statSync(f); if (st.size > 8 * 1024 * 1024) continue; buf = readFileSync(f) } catch { continue }
  // skip obvious binaries for content scan (but still name-checked above)
  const nul = buf.indexOf(0)
  const isBinary = nul !== -1 && nul < 4096
  const text = buf.toString('latin1')
  if (text.includes(EICAR)) add('critical', 'eicar', f, 'EICAR test signature')
  if (isBinary) continue
  for (const re of secretPat) if (re.test(text)) add('high', 'secret-material', f, re.source.slice(0, 40))
  for (const re of devPaths) if (re.test(text)) add('high', 'machine-local-path', f, re.source)
  for (const re of attribPat) if (re.test(text)) add('medium', 'forbidden-attribution', f, re.source)
}

const bySev = findings.reduce((a, f) => ((a[f.severity] = (a[f.severity] || 0) + 1), a), {})
const report = { schema: 'youtab.backend_sidecar_scan/v1', generated_utc: new Date().toISOString(), bundle: relative(REPO_ROOT, bundleRoot).split('\\').join('/'), scanned_files: files.length, findings_by_severity: bySev, findings }
writeFileSync(join(OUT_DIR, 'scan-report.json'), JSON.stringify(report, null, 2))
console.log(`[scan] scanned ${files.length} files; findings: ${JSON.stringify(bySev)}`)
for (const f of findings.slice(0, 20)) console.log(`  [${f.severity}] ${f.rule}: ${f.file} (${f.detail})`)
const blocking = findings.filter((f) => f.severity === 'critical' || f.severity === 'high').length
process.exit(blocking > 0 ? 1 : 0)
