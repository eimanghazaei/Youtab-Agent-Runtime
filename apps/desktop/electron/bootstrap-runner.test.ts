import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildPinArgs,
  buildPosixPinArgs,
  cachedScriptPath,
  classifyPilotReleaseUpdate,
  fetchApprovedPilotRelease,
  hasExistingGitCheckout,
  hasPendingRuntimeInstall,
  installedAgentInstallScript,
  installRefForStamp,
  isPackagedWindowsCustomerMode,
  isPinnedCommit,
  pendingRuntimeInstallMarker,
  pilotReleaseBaseUrl,
  resolveInstallScript,
  resolveMarkerPinnedCommit,
  runBootstrap,
  trustedPilotReleaseBaseUrl
} from './bootstrap-runner'

const SCRIPT_NAME = process.platform === 'win32' ? 'install.ps1' : 'install.sh'
const ZERO_COMMIT = '0000000000000000000000000000000000000000'
const RELEASE_BASE = `https://api.youtab.io/pilot-runtime-${'a'.repeat(32)}/releases`
const RELEASE_SHA = 'b'.repeat(40)
const RELEASE_HASH = 'c'.repeat(64)

test('packaged Windows remains customer mode and detects the sibling install transaction marker', () => {
  assert.equal(isPackagedWindowsCustomerMode(true, true), true)
  assert.equal(isPackagedWindowsCustomerMode(true, false), false)
  assert.equal(isPackagedWindowsCustomerMode(false, true), false)

  const home = mkTmpHome()
  const activeRoot = path.join(home, 'youtab-agent-runtime')
  const marker = path.join(home, '.youtab-runtime-install-pending')

  try {
    assert.equal(pendingRuntimeInstallMarker(activeRoot), marker)
    assert.equal(hasPendingRuntimeInstall(activeRoot), false)
    fs.writeFileSync(marker, 'pending')
    assert.equal(hasPendingRuntimeInstall(activeRoot), true)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

function releaseFixture() {
  return {
    version: '0.19.2',
    release_sequence: 2,
    source_sha: RELEASE_SHA,
    artifact_url: `${RELEASE_BASE}/${RELEASE_SHA}/youtab-runtime-${RELEASE_SHA}.zip`,
    manifest_url: `${RELEASE_BASE}/${RELEASE_SHA}/manifest.json`,
    sha256: RELEASE_HASH,
    created_at: '2026-10-01T10:00:00Z',
    platform: 'windows',
    architecture: 'x64',
    format: 'zip'
  }
}

test('pilot release discovery verifies latest and exact immutable manifest on the fixed origin', async () => {
  const release = releaseFixture()
  const read = async url => {
    if (url === `${RELEASE_BASE}/latest.json`) {
      return { ...release, manifest_url: `${RELEASE_BASE}/${RELEASE_SHA}/manifest.json` }
    }
    assert.equal(url, `${RELEASE_BASE}/${RELEASE_SHA}/manifest.json`)
    return release
  }
  assert.equal((await fetchApprovedPilotRelease(RELEASE_BASE, read)).source_sha, RELEASE_SHA)
  assert.equal(
    classifyPilotReleaseUpdate(
      { pinnedCommit: 'd'.repeat(40), releaseSequence: 1, artifactSha256: 'e'.repeat(64) },
      release
    ),
    'available'
  )
  assert.equal(
    classifyPilotReleaseUpdate(
      { pinnedCommit: RELEASE_SHA, releaseSequence: 2, artifactSha256: RELEASE_HASH },
      release
    ),
    'current'
  )
})

test('pilot release discovery rejects cross-origin, path escape, and mutable identity conflicts', async () => {
  assert.throws(() => pilotReleaseBaseUrl(`https://evil.example/pilot-runtime-${'a'.repeat(32)}/releases`), /approved/)
  assert.throws(() => pilotReleaseBaseUrl('https://api.youtab.io/runtime/releases'), /approved/)
  assert.equal(trustedPilotReleaseBaseUrl(RELEASE_BASE, RELEASE_BASE), RELEASE_BASE)
  assert.throws(
    () => trustedPilotReleaseBaseUrl(RELEASE_BASE, `https://api.youtab.io/pilot-runtime-${'d'.repeat(32)}/releases`),
    /does not match/
  )
  const release = releaseFixture()
  await assert.rejects(
    fetchApprovedPilotRelease(RELEASE_BASE, async () => ({
      ...release,
      manifest_url: 'https://evil.example/manifest.json'
    })),
    /escapes/
  )
  await assert.rejects(
    fetchApprovedPilotRelease(RELEASE_BASE, async url =>
      url.endsWith('latest.json')
        ? { ...release, manifest_url: `${RELEASE_BASE}/${RELEASE_SHA}/manifest.json` }
        : { ...release, sha256: 'f'.repeat(64) }
    ),
    /disagree/
  )
  assert.throws(
    () =>
      classifyPilotReleaseUpdate(
        { pinnedCommit: RELEASE_SHA, releaseSequence: 3, artifactSha256: RELEASE_HASH },
        release
      ),
    /downgrade/
  )
  assert.throws(
    () =>
      classifyPilotReleaseUpdate(
        { pinnedCommit: 'd'.repeat(40), releaseSequence: 2, artifactSha256: RELEASE_HASH },
        release
      ),
    /conflicts/
  )
  assert.throws(() => classifyPilotReleaseUpdate({ pinnedCommit: RELEASE_SHA }, release), /repair is required/)
})

test('packaged customer recovery cannot download or fall back to a GitHub install script', async () => {
  let downloads = 0
  await assert.rejects(
    resolveInstallScript({
      installStamp: { commit: RELEASE_SHA },
      sourceRepoRoot: null,
      youtabHome: path.join(os.tmpdir(), 'youtab-customer-no-fetch'),
      emit: () => {},
      customerMode: true,
      _download: async () => { downloads++ }
    }),
    /staged Youtab Setup/
  )
  assert.equal(downloads, 0)
})

function mkTmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-bootstrap-test-'))
}

test('runBootstrap bails immediately when the signal is already aborted', async () => {
  const controller = new AbortController()
  controller.abort()

  const events = []

  const result = await runBootstrap({
    installStamp: null,
    activeRoot: '/tmp/youtab-runner-test',
    sourceRepoRoot: null,
    youtabHome: '/tmp/youtab-runner-test',
    logRoot: '/tmp/youtab-runner-test',
    onEvent: ev => events.push(ev),
    abortSignal: controller.signal
  })

  // Cancelled before any install script is spawned.
  assert.deepEqual(result, { ok: false, cancelled: true })
  assert.ok(
    events.some(ev => ev.type === 'failed' && /cancelled/i.test(ev.error)),
    'should emit a cancelled failure event'
  )
})

test('installedAgentInstallScript resolves the installer in the agent checkout', () => {
  const home = mkTmpHome()

  try {
    assert.equal(installedAgentInstallScript(home), null, 'absent before the checkout exists')

    const scriptsDir = path.join(home, 'youtab-agent-runtime', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const scriptPath = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(scriptPath, '#!/bin/sh\necho hi\n')

    assert.equal(installedAgentInstallScript(home), scriptPath)
    assert.equal(installedAgentInstallScript(null), null, 'null home -> null')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('existing checkout detection requires git metadata', () => {
  const home = mkTmpHome()

  try {
    const activeRoot = path.join(home, 'youtab-agent-runtime')
    assert.equal(hasExistingGitCheckout(activeRoot), false)

    fs.mkdirSync(path.join(activeRoot, '.git'), { recursive: true })
    assert.equal(hasExistingGitCheckout(activeRoot), true)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('fresh bootstrap args include the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp), ['-Commit', installStamp.commit, '-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/youtab-agent-runtime',
      youtabHome: '/tmp/youtab'
    }),
    [
      '--dir',
      '/tmp/youtab-agent-runtime',
      '--youtab-home',
      '/tmp/youtab',
      '--branch',
      'main',
      '--commit',
      installStamp.commit
    ]
  )
})

test('existing-checkout bootstrap args keep branch but skip the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp, { pinCommit: false }), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/youtab-agent-runtime',
      youtabHome: '/tmp/youtab',
      pinCommit: false
    }),
    ['--dir', '/tmp/youtab-agent-runtime', '--youtab-home', '/tmp/youtab', '--branch', 'main']
  )
})

test('fallback install stamps use an unpinned branch ref', () => {
  const stamp = { commit: ZERO_COMMIT, branch: 'main' }

  assert.equal(isPinnedCommit(ZERO_COMMIT), false)
  assert.deepEqual(installRefForStamp(stamp), {
    ref: 'main',
    cacheKey: 'fallback-main',
    pinned: false
  })
  // Must NOT pass -Commit / --commit for the all-zero placeholder.
  assert.deepEqual(buildPinArgs(stamp), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp: stamp,
      activeRoot: '/tmp/youtab',
      youtabHome: '/tmp/home'
    }),
    ['--dir', '/tmp/youtab', '--youtab-home', '/tmp/home', '--branch', 'main']
  )
})

test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {
  const realHead = 'c'.repeat(40)
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    realHead
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: 'd'.repeat(40), branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    'd'.repeat(40),
    'packaged real pin wins over checkout HEAD'
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/missing', {
      resolveHead: () => null
    }),
    null
  )
})

test('resolveInstallScript downloads fallback stamps by branch instead of zero commit', async () => {
  const home = mkTmpHome()

  try {
    const logs = []
    const refs = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main' },
      sourceRepoRoot: null,
      youtabHome: home,
      emit: ev => logs.push(ev),
      _download: async (ref, destPath) => {
        refs.push(ref)
        fs.mkdirSync(path.dirname(destPath), { recursive: true })
        fs.writeFileSync(destPath, '#!/bin/sh\necho fallback branch\n')

        return destPath
      }
    })

    assert.deepEqual(refs, ['main'])
    assert.equal(result.source, 'download')
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'fallback-main'))
    assert.ok(
      logs.some(ev => /fallback, unpinned/.test(ev.line || '')),
      'emits an unpinned fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript prefers a cached script without touching the network', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    fs.writeFileSync(cached, '#!/bin/sh\necho cached\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      youtabHome: home,
      emit: ev => logs.push(ev)
    })

    assert.equal(result.source, 'cache')
    assert.equal(result.path, cached)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript falls back to the installed agent checkout on a 404', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // Seed the installed agent checkout so the fallback has something to resolve.
    const scriptsDir = path.join(home, 'youtab-agent-runtime', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const installed = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(installed, '#!/bin/sh\necho fallback\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      youtabHome: home,
      emit: ev => logs.push(ev),
      // Simulate GitHub returning a 404 for the pinned commit.
      _download: async () => {
        throw new Error('Failed to download install.sh: HTTP 404')
      }
    })

    assert.equal(result.source, 'installed-agent')
    // It should have copied the installer into the bootstrap cache.
    assert.equal(result.path, cachedScriptPath(home, commit))
    assert.ok(fs.existsSync(result.path), 'fallback script copied into cache')
    assert.ok(
      logs.some(ev => /falling back to installed agent/.test(ev.line || '')),
      'emits a fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript rethrows when the 404 fallback is unavailable', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // No installed agent checkout seeded -> nothing to fall back to.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        youtabHome: home,
        emit: () => {},
        _download: async () => {
          throw new Error('Failed to download install.sh: HTTP 404')
        }
      }),
      /HTTP 404|Failed to download/
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
