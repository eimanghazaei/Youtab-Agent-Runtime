import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export function resolveApiKeyPath(rawValue) {
  const value = String(rawValue || '').trim()
  if (!value) return { keyPath: '', cleanup: () => {} }
  if (fs.existsSync(value)) return { keyPath: value, cleanup: () => {} }
  if (!value.includes('BEGIN PRIVATE KEY') || !value.includes('END PRIVATE KEY')) {
    throw new Error('APPLE_API_KEY must be a file path or inline .p8 key content')
  }

  // Inline release credentials must never enter a predictable shared temp file.
  // mkdtemp creates an owned, private directory; wx refuses existing files and
  // symlinks instead of overwriting or following them.
  const tempRoot = path.resolve(os.tmpdir())
  const directory = fs.mkdtempSync(path.join(tempRoot, 'youtab-notary-'))
  const owned = fs.lstatSync(directory)
  const keyPath = path.join(directory, 'AuthKey.p8')
  const cleanup = () => {
    if (path.dirname(directory) !== tempRoot || !path.basename(directory).startsWith('youtab-notary-')) {
      throw new Error('Refusing cleanup outside the owned notarization directory')
    }
    let current
    try {
      current = fs.lstatSync(directory)
    } catch (error) {
      if (error.code === 'ENOENT') return
      throw error
    }
    if (!current.isDirectory() || current.isSymbolicLink() || current.dev !== owned.dev || current.ino !== owned.ino) {
      throw new Error('Refusing cleanup of a replaced notarization directory')
    }
    fs.rmSync(directory, { recursive: true, force: true })
  }

  try {
    fs.chmodSync(directory, 0o700)
    fs.writeFileSync(keyPath, value, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
  } catch (error) {
    cleanup()
    throw error
  }
  return { keyPath, cleanup }
}
