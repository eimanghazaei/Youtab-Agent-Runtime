import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { gitFor, readBoundedSample, repoStatus, resolveRenamePath, reviewList } from './git-review-ops'

const tempDirs: string[] = []

test('readBoundedSample continues after short reads and stops at the cap', async () => {
  const source = Buffer.from('one\ntwo\nthree')

  const positions: number[] = []

  const handle = {
    async read(target: Buffer, offset: number, length: number, position: number) {
      positions.push(position)

      const bytesRead = source.copy(target, offset, position, position + Math.min(length, 2))

      return { bytesRead }
    }
  }

  assert.equal((await readBoundedSample(handle, 100)).toString(), source.toString())
  assert.deepEqual(positions, [0, 2, 4, 6, 8, 10, 12, 13])
  assert.equal((await readBoundedSample(handle, 4)).toString(), 'one\nt')
})

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-desktop-git-status-'))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['config', 'user.email', 'youtab-test@example.com'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'Youtab Test'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'tracked\n')
  execFileSync('git', ['add', 'tracked.txt'], { cwd: dir })
  execFileSync('git', ['commit', '-qm', 'initial'], { cwd: dir })

  return dir
}

test('resolveRenamePath: plain path is unchanged', () => {
  assert.equal(resolveRenamePath('src/a.ts'), 'src/a.ts')
})

test('gitFor accepts an internally resolved git binary path containing spaces', () => {
  assert.doesNotThrow(() => gitFor(process.cwd(), 'C:\\Program Files\\Git\\cmd\\git.exe'))
})

test('gitFor runs git through a spaced binary path', async () => {
  if (process.platform !== 'win32') {
    return
  }

  const gitBin = path.join(process.env.ProgramFiles || String.raw`C:\Program Files`, 'Git', 'cmd', 'git.exe')

  if (!fs.existsSync(gitBin)) {
    return
  }

  const repo = makeRepo()

  fs.writeFileSync(path.join(repo, 'changed.txt'), 'review me\n')

  const status = await gitFor(repo, gitBin).status()

  assert.equal(status.not_added.includes('changed.txt'), true)
})

test('resolveRenamePath: simple rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('old.ts => new.ts'), 'new.ts')
})

test('resolveRenamePath: brace rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('src/{old => new}/file.ts'), 'src/new/file.ts')
})

test('resolveRenamePath: brace rename collapsing a segment', () => {
  assert.equal(resolveRenamePath('src/{lib => }/file.ts'), 'src/file.ts')
})

test('repoStatus reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'generated', 'deep')

  fs.mkdirSync(nested, { recursive: true })
  fs.writeFileSync(path.join(nested, 'large-output.txt'), 'generated\n')

  const status = await repoStatus(dir, 'git')

  assert.ok(status)
  assert.equal(status.untracked, 1)
  assert.equal(status.changed, 1)
  assert.deepEqual(
    status.files.map(file => file.path),
    ['generated/']
  )
})

test('reviewList counts only bounded regular untracked files', async () => {
  const dir = makeRepo()
  fs.writeFileSync(path.join(dir, 'short.txt'), 'one\ntwo')
  fs.writeFileSync(path.join(dir, 'oversized.txt'), Buffer.alloc(1024 * 1024 + 1, 65))

  if (process.platform !== 'win32') {
    fs.symlinkSync(path.join(dir, 'short.txt'), path.join(dir, 'link.txt'))
  }

  const result = await reviewList(dir, 'unstaged', null, 'git')
  const files = new Map(result.files.map(file => [file.path, file] as const))

  assert.equal(files.get('short.txt')?.added, 2)
  assert.equal(files.get('oversized.txt')?.added, 0)

  if (process.platform !== 'win32') {
    assert.equal(files.get('link.txt')?.added, 0)
  }
})
