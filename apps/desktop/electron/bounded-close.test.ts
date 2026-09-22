/**
 * Regression tests for electron/bounded-close.ts (E2E teardown safeguard).
 * Run: npx vitest run --project electron electron/bounded-close.test.ts
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { boundedClose } from './bounded-close'

test('graceful close that settles in time → closed, no kill', async () => {
  const killed: number[] = []

  const outcome = await boundedClose({
    close: () => Promise.resolve(),
    pid: 123,
    timeoutMs: 1000,
    killTree: pid => killed.push(pid)
  })

  assert.equal(outcome, 'closed')
  assert.deepEqual(killed, [])
})

test('close that never settles → owned pid is tree-killed', async () => {
  const killed: number[] = []

  const outcome = await boundedClose({
    close: () => new Promise(() => {}), // never resolves
    pid: 4321,
    timeoutMs: 10,
    killTree: pid => killed.push(pid)
  })

  assert.equal(outcome, 'killed')
  assert.deepEqual(killed, [4321], 'only the owned pid is terminated')
})

test('a rejecting close is swallowed and counts as closed (no kill)', async () => {
  const killed: number[] = []

  const outcome = await boundedClose({
    close: () => Promise.reject(new Error('close failed')),
    pid: 999,
    timeoutMs: 1000,
    killTree: pid => killed.push(pid)
  })

  assert.equal(outcome, 'closed')
  assert.deepEqual(killed, [])
})

test('timeout without a known owned pid kills nothing', async () => {
  const killed: number[] = []

  const outcome = await boundedClose({
    close: () => new Promise(() => {}),
    pid: undefined,
    timeoutMs: 10,
    killTree: pid => killed.push(pid)
  })

  assert.equal(outcome, 'no-pid-timeout')
  assert.deepEqual(killed, [], 'never force-kills when the owned pid is unknown')
})

test('kill targets exactly the passed pid (ownership-verified, no name-based sweep)', async () => {
  const killed: number[] = []
  await boundedClose({
    close: () => new Promise(() => {}),
    pid: 55_512,
    timeoutMs: 10,
    killTree: pid => killed.push(pid)
  })
  assert.deepEqual(killed, [55_512])
})
