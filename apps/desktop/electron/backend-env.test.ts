import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  buildDesktopBackendPath,
  normalizeYoutabHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES
} from './backend-env'

test('desktop backend PATH adds Youtab-managed bins and missing POSIX sane entries', () => {
  const result = buildDesktopBackendPath({
    youtabHome: '/Users/test/.youtab-agent-runtime',
    venvRoot: '/Users/test/.youtab-agent-runtime/youtab-agent-runtime/venv',
    currentPath: '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  const entries = result.split(':')
  assert.equal(entries[0], '/Users/test/.youtab-agent-runtime/node/bin')
  assert.equal(entries[1], '/Users/test/.youtab-agent-runtime/youtab-agent-runtime/venv/bin')
  assert.ok(entries.includes('/opt/homebrew/bin'), 'Apple Silicon Homebrew bin is added')
  assert.ok(entries.includes('/opt/homebrew/sbin'), 'Apple Silicon Homebrew sbin is added')
  assert.ok(entries.includes('/usr/local/sbin'), 'missing standard sbin is added')

  for (const expected of POSIX_SANE_PATH_ENTRIES) {
    assert.ok(entries.includes(expected), `${expected} should be present`)
  }
})

test('desktop backend PATH preserves first occurrence and avoids duplicates', () => {
  const result = buildDesktopBackendPath({
    youtabHome: '/Users/test/.youtab-agent-runtime',
    venvRoot: '/Users/test/.youtab-agent-runtime/youtab-agent-runtime/venv',
    currentPath: '/opt/homebrew/bin:/usr/bin:/opt/homebrew/bin:/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  const entries = result.split(':')
  assert.equal(entries.filter(entry => entry === '/opt/homebrew/bin').length, 1)
  assert.ok(
    entries.indexOf('/opt/homebrew/bin') < entries.indexOf('/opt/homebrew/sbin'),
    'existing Homebrew bin keeps its precedence over appended missing sane entries'
  )
})

test('buildDesktopBackendEnv extends PYTHONPATH and backend PATH together', () => {
  const env = buildDesktopBackendEnv({
    youtabHome: '/Users/test/.youtab-agent-runtime',
    pythonPathEntries: ['/repo/youtab-agent-runtime'],
    venvRoot: '/Users/test/.youtab-agent-runtime/youtab-agent-runtime/venv',
    currentEnv: {
      PATH: '/usr/bin:/bin',
      PYTHONPATH: '/existing/pythonpath'
    },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(env.PYTHONPATH, '/repo/youtab-agent-runtime:/existing/pythonpath')
  assert.ok(
    env.PATH.startsWith(
      '/Users/test/.youtab-agent-runtime/node/bin:/Users/test/.youtab-agent-runtime/youtab-agent-runtime/venv/bin:'
    )
  )
  assert.ok(env.PATH.includes('/opt/homebrew/bin'))
})

test('buildDesktopBackendEnv forces PYTHONUTF8 unless the user set it explicitly', () => {
  const defaulted = buildDesktopBackendEnv({
    youtabHome: '/Users/test/.youtab-agent-runtime',
    currentEnv: { PATH: '/usr/bin' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(defaulted.PYTHONUTF8, '1')

  const optedOut = buildDesktopBackendEnv({
    youtabHome: '/Users/test/.youtab-agent-runtime',
    currentEnv: { PATH: '/usr/bin', PYTHONUTF8: '0' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(optedOut.PYTHONUTF8, '0')
})

test('normalizeYoutabHomeRoot maps profile homes back to the global Youtab root', () => {
  assert.equal(
    normalizeYoutabHomeRoot('/Users/test/.youtab-agent-runtime/profiles/oracle', { pathModule: path.posix }),
    '/Users/test/.youtab-agent-runtime'
  )
  assert.equal(
    normalizeYoutabHomeRoot('C:\\Users\\test\\AppData\\Local\\youtab\\profiles\\oracle', { pathModule: path.win32 }),
    'C:\\Users\\test\\AppData\\Local\\youtab'
  )
  assert.equal(
    normalizeYoutabHomeRoot('/Users/test/.youtab-agent-runtime', { pathModule: path.posix }),
    '/Users/test/.youtab-agent-runtime'
  )
})

test('Windows PATH casing and delimiter are preserved without POSIX sane entries', () => {
  const env = buildDesktopBackendEnv({
    youtabHome: 'C:\\Users\\test\\AppData\\Local\\youtab',
    pythonPathEntries: ['C:\\repo\\youtab-agent-runtime'],
    venvRoot: 'C:\\Users\\test\\AppData\\Local\\youtab\\youtab-agent-runtime\\venv',
    currentEnv: {
      Path: 'C:\\Windows\\System32;C:\\Windows',
      PYTHONPATH: 'C:\\existing\\pythonpath'
    },
    platform: 'win32',
    pathModule: path.win32
  })

  assert.equal(pathEnvKey({ Path: 'x' }, 'win32'), 'Path')
  assert.equal(env.PATH, undefined)
  assert.ok(env.Path.startsWith('C:\\Users\\test\\AppData\\Local\\youtab\\node\\bin;'))
  assert.ok(env.Path.includes('\\venv\\Scripts;'))
  assert.ok(env.Path.includes(';C:\\Windows\\System32;C:\\Windows'))
  assert.equal(env.Path.includes('/opt/homebrew/bin'), false)
})

test('appendUniquePathEntries drops empty entries and keeps first occurrence', () => {
  assert.equal(appendUniquePathEntries([':/a::/b', ['/a', '/c']], { delimiter: ':' }), '/a:/b:/c')
})
