import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { BACKEND_STDIO_LOG_PREFIX, openBackendStdioLog } from './backend-stdio-log'

test('backend stdio log records spawn, raw output and exit synchronously by pid', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-stdio-log-'))

  try {
    const log = openBackendStdioLog(dir, 4242)
    log.event('spawned pid=4242')
    log.output('stdout', 'YOUTAB_AGENT_BACKEND_READY port=1\n')
    log.output('stderr', Buffer.from('Traceback (most recent call last)'))
    log.event('exited pid=4242 code=1 signal=null')

    const text = fs.readFileSync(path.join(dir, `${BACKEND_STDIO_LOG_PREFIX}4242.log`), 'utf8')
    assert.match(text, /\[event\] spawned pid=4242/)
    assert.match(text, /\[stdout\] YOUTAB_AGENT_BACKEND_READY port=1/)
    assert.match(text, /\[stderr\] Traceback \(most recent call last\)\n/)
    assert.match(text, /\[event\] exited pid=4242 code=1/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('backend stdio logs are bounded to the newest files', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'youtab-stdio-log-'))

  try {
    for (let pid = 1; pid <= 5; pid++) {
      openBackendStdioLog(dir, pid, 3).event(`spawned pid=${pid}`)
      const file = path.join(dir, `${BACKEND_STDIO_LOG_PREFIX}${pid}.log`)
      fs.utimesSync(file, pid, pid)
    }

    const remaining = fs.readdirSync(dir).filter(name => name.startsWith(BACKEND_STDIO_LOG_PREFIX)).sort()
    assert.deepEqual(remaining, [`${BACKEND_STDIO_LOG_PREFIX}3.log`, `${BACKEND_STDIO_LOG_PREFIX}4.log`, `${BACKEND_STDIO_LOG_PREFIX}5.log`])
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
