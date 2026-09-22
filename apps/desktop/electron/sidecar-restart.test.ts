/**
 * Tests for electron/sidecar-restart.ts (bounded restart policy).
 * Run: npx vitest run --project electron electron/sidecar-restart.test.ts
 */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { SidecarRestartPolicy } from './sidecar-restart'

test('restarts up to the budget, then latches into budget-exhausted', () => {
  const p = new SidecarRestartPolicy({ maxRestarts: 3, windowMs: 60_000, backoffMs: () => 0 })
  assert.equal(p.onCrash(0).reason, 'restart')
  assert.equal(p.onCrash(1).reason, 'restart')
  assert.equal(p.onCrash(2).reason, 'restart')
  const fourth = p.onCrash(3)
  assert.equal(fourth.restart, false)
  assert.equal(fourth.reason, 'budget-exhausted')
})

test('crashes outside the rolling window do not exhaust the budget', () => {
  const p = new SidecarRestartPolicy({ maxRestarts: 2, windowMs: 1000, backoffMs: () => 0 })
  assert.equal(p.onCrash(0).restart, true)
  assert.equal(p.onCrash(500).restart, true)
  // 2000ms later the first two crashes have aged out of the window.
  const later = p.onCrash(2000)
  assert.equal(later.restart, true)
  assert.equal(later.attempt, 1)
})

test('intentionalStop suppresses restart (app quit / superseded)', () => {
  const p = new SidecarRestartPolicy({ maxRestarts: 3 })
  p.intentionalStop()
  const d = p.onCrash(0)
  assert.equal(d.restart, false)
  assert.equal(d.reason, 'shutting-down')
})

test('reset clears the budget after a healthy run', () => {
  const p = new SidecarRestartPolicy({ maxRestarts: 1, windowMs: 60_000, backoffMs: () => 0 })
  assert.equal(p.onCrash(0).restart, true)
  assert.equal(p.onCrash(1).restart, false)
  p.reset()
  assert.equal(p.onCrash(2).restart, true)
})

test('default backoff grows and is capped', () => {
  const p = new SidecarRestartPolicy({ maxRestarts: 10, windowMs: 10 ** 9 })
  assert.equal(p.onCrash(0).delayMs, 500)
  assert.equal(p.onCrash(0).delayMs, 1000)
  assert.equal(p.onCrash(0).delayMs, 2000)

  for (let i = 0; i < 10; i++) {
    p.onCrash(0)
  }

  assert.ok(p['backoffMs'](20) <= 10_000)
})
