import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { channelIndex, channelPreferenceStamp, selectedReleaseChannel, stageSetup, verifiedSetupRelease } from './release-delivery'

const base = `https://api.youtab.io/pilot-runtime-${'b'.repeat(32)}/releases`
const sha = 'a'.repeat(40)
const bytes = Buffer.alloc(512, 1)
bytes[0] = 0x4d
bytes[1] = 0x5a
bytes.writeUInt32LE(0x80, 0x3c)
bytes.writeUInt32LE(0x4550, 0x80)
bytes.writeUInt16LE(0x8664, 0x84)
bytes.writeUInt16LE(2, 0x96)
bytes.writeUInt16LE(0x20b, 0x98)
const manifest = { source_sha: sha, release_sequence: 4, platform: 'windows', architecture: 'x64', format: 'zip', setup_source_sha: sha, updater_protocol: 1, setup_url: `${base}/${sha}/Youtab-Setup-${sha}.exe`, setup_sha256: createHash('sha256').update(bytes).digest('hex'), setup_size: bytes.length }
const latest = { ...manifest, manifest_url: `${base}/${sha}/manifest.json` }

test('Pilot and Stable discover separate pointers for the same immutable Setup', () => {
  assert.notEqual(channelIndex(base, 'pilot'), channelIndex(base, 'stable'))
  assert.equal(verifiedSetupRelease(base, latest, manifest, 3).sha256, manifest.setup_sha256)
})
test('a later explicit Setup channel selection supersedes the saved About choice', () => {
  const installed = { pinnedCommit: sha, releaseSequence: 4, releaseChannel: 'pilot', channelSelectedAt: 'initial' }
  const config = { channel: 'pilot', channelMarker: channelPreferenceStamp(installed) }
  assert.equal(selectedReleaseChannel(config, installed), 'pilot')
  const switched = { ...installed, releaseChannel: 'stable', channelSelectedAt: 'next-choice' }
  assert.equal(selectedReleaseChannel(config, switched), 'stable')
  const aboutChoice = { channel: 'pilot', channelMarker: channelPreferenceStamp(switched) }
  assert.equal(selectedReleaseChannel(aboutChoice, switched), 'pilot')
  assert.equal(selectedReleaseChannel(aboutChoice, { ...switched, channelSelectedAt: 'choose-stable-again' }), 'stable')
})

for (const change of [
  { setup_source_sha: 'c'.repeat(40) }, { updater_protocol: 9 },
  { setup_url: 'https://evil.example/setup.exe' }, { setup_size: 0 },
  { setup_sha256: 'bad' }, { release_sequence: 2 }, { architecture: 'arm64' },
]) {
  test(`metadata fails closed: ${Object.keys(change)[0]}`, () => {
    const changed = { ...manifest, ...change }
    assert.throws(() => verifiedSetupRelease(base, { ...changed, manifest_url: latest.manifest_url }, changed, 3))
  })
}

test('mutable and immutable metadata must agree', () => {
  assert.throws(() => verifiedSetupRelease(base, { ...latest, setup_sha256: 'c'.repeat(64) }, manifest, 3))
})

test('verified sibling staging preserves an existing Setup; tampering leaves no file', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'youtab-setup-test-'))

  try {
    const old = path.join(root, 'youtab-setup.exe')
    await fs.writeFile(old, 'old helper')
    const release = verifiedSetupRelease(base, latest, manifest, 3)
    const staged = await stageSetup(release, root, async () => bytes)
    assert.deepEqual(await fs.readFile(staged), bytes)
    assert.equal(await fs.readFile(old, 'utf8'), 'old helper')
    const count = (await fs.readdir(root)).length
    await assert.rejects(stageSetup(release, root, async () => Buffer.alloc(100)), /INTEGRITY/)
    await assert.rejects(stageSetup(release, root, async () => bytes.subarray(0, 50)), /INTEGRITY/)
    assert.equal((await fs.readdir(root)).length, count)
  } finally {
    assert.equal(path.dirname(root), path.resolve(os.tmpdir()))
    assert.ok(path.basename(root).startsWith('youtab-setup-test-'))
    await fs.rm(root, { recursive: true, force: true })
  }
})
