import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pipeline } from 'node:stream/promises'

import { resolveReadableFileForIpc } from './hardening'

/** User-requested, byte-preserving export. Never executes the file or loads
 * large binaries into renderer memory. Existing sensitive-path rules apply. */
export async function exportFileCopy(
  sourcePath: string,
  chooseDestination: (name: string) => Promise<string | null>
): Promise<boolean> {
  const source = await resolveReadableFileForIpc(sourcePath, { purpose: 'File download' })
  const destination = await chooseDestination(path.basename(source.realPath))

  if (!destination) {
    return false
  }
  const verified = await resolveReadableFileForIpc(sourcePath, { purpose: 'File download' })

  if (verified.realPath !== source.realPath || verified.stat.ino !== source.stat.ino) {
    throw new Error('File changed while choosing a download destination')
  }

  const handle = await fs.promises.open(verified.realPath, 'r')

  try {
    const opened = await handle.stat()

    if (!opened.isFile() || opened.ino !== verified.stat.ino || opened.dev !== verified.stat.dev) {
      throw new Error('File changed before download')
    }

    const existing = await fs.promises.stat(destination).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') {
        throw error
      }

      return null
    })

    if (existing && existing.ino === opened.ino && existing.dev === opened.dev) {
      return false
    }
    const staged = path.join(path.dirname(destination), `.youtab-download-${randomUUID()}.tmp`)

    try {
      await pipeline(
        handle.createReadStream({ autoClose: false }),
        fs.createWriteStream(staged, { flags: 'wx', mode: 0o600 })
      )
      await fs.promises.rename(staged, destination)
    } finally {
      await fs.promises.rm(staged, { force: true })
    }

    return true
  } finally {
    await handle.close()
  }
}
