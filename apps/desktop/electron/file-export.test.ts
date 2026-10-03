import fs from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, expect, it } from 'vitest'

import { exportFileCopy } from './file-export'

let root: string
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'youtab-export-'))
})
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true })
})

it('exports text and binary files byte-for-byte without changing originals', async () => {
  for (const [name, bytes] of [
    ['report.md', Buffer.from('# Report\nسلام')],
    ['report.zip', Buffer.from([0, 255, 1, 128])]
  ] as const) {
    const source = path.join(root, name)
    const destination = path.join(root, `saved-${name}`)
    await fs.writeFile(source, bytes)
    expect(
      await exportFileCopy(source, async suggested => {
        expect(suggested).toBe(name)

        return destination
      })
    ).toBe(true)
    expect(await fs.readFile(destination)).toEqual(bytes)
    expect(await fs.readFile(source)).toEqual(bytes)
  }
})

it('cancels without creating a file and never truncates the source when chosen as destination', async () => {
  const source = path.join(root, 'report.md')
  await fs.writeFile(source, 'keep')
  expect(await exportFileCopy(source, async () => null)).toBe(false)
  expect(await exportFileCopy(source, async () => source)).toBe(false)
  expect(await fs.readFile(source, 'utf8')).toBe('keep')
})

it('rejects sensitive files before showing a save dialog', async () => {
  const source = path.join(root, '.env')
  await fs.writeFile(source, 'synthetic')
  let prompted = false
  await expect(
    exportFileCopy(source, async () => {
      prompted = true

      return path.join(root, 'copy.txt')
    })
  ).rejects.toThrow()
  expect(prompted).toBe(false)
})

it('fails closed when the source is replaced during the save dialog', async () => {
  const source = path.join(root, 'report.md')
  const destination = path.join(root, 'copy.md')
  await fs.writeFile(source, 'original')
  await expect(
    exportFileCopy(source, async () => {
      await fs.rename(source, path.join(root, 'old.md'))
      await fs.writeFile(source, 'replacement')

      return destination
    })
  ).rejects.toThrow('File changed')
  await expect(fs.stat(destination)).rejects.toThrow()
})

it('retains an existing destination on failed promotion and removes staging files', async () => {
  const source = path.join(root, 'report.md')
  const destination = path.join(root, 'existing')
  await fs.writeFile(source, 'report')
  await fs.mkdir(destination)
  await fs.writeFile(path.join(destination, 'keep.txt'), 'keep')
  await expect(exportFileCopy(source, async () => destination)).rejects.toThrow()
  expect(await fs.readFile(path.join(destination, 'keep.txt'), 'utf8')).toBe('keep')
  expect((await fs.readdir(root)).some(name => name.startsWith('.youtab-download-'))).toBe(false)
})
