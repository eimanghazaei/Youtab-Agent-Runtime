/**
 * Tests for electron/sidecar-ready.ts (gateway.ready stdio handshake).
 * Run: npx vitest run --project electron electron/sidecar-ready.test.ts
 */
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import {
  MIN_GATEWAY_READY_TIMEOUT_MS,
  parseGatewayReadyLine,
  resolveGatewayReadyTimeoutMs,
  waitForGatewayReady
} from './sidecar-ready'

function fakeChild() {
  const child: any = new EventEmitter()
  child.stdout = new EventEmitter()
  return child
}

const READY = JSON.stringify({
  jsonrpc: '2.0',
  method: 'event',
  params: { type: 'gateway.ready', payload: { skin: 'youtab', change_events: true } }
})

test('parseGatewayReadyLine recognizes the real emission and extracts payload', () => {
  const r = parseGatewayReadyLine(READY)
  assert.ok(r)
  assert.equal(r!.type, 'gateway.ready')
  assert.deepEqual(r!.payload, { skin: 'youtab', change_events: true })
})

test('parseGatewayReadyLine ignores other events and non-JSON noise', () => {
  assert.equal(parseGatewayReadyLine('booting…'), null)
  assert.equal(parseGatewayReadyLine(JSON.stringify({ method: 'event', params: { type: 'other' } })), null)
  assert.equal(parseGatewayReadyLine('{ broken'), null)
})

test('waitForGatewayReady resolves when the event arrives (even split across chunks)', async () => {
  const child = fakeChild()
  const p = waitForGatewayReady(child, 1000)
  child.stdout.emit('data', READY.slice(0, 20))
  child.stdout.emit('data', READY.slice(20) + '\n')
  const r = await p
  assert.equal(r.type, 'gateway.ready')
})

test('waitForGatewayReady rejects when the child exits first', async () => {
  const child = fakeChild()
  const p = waitForGatewayReady(child, 1000)
  child.emit('exit', 1, null)
  await assert.rejects(p, /exited before gateway.ready/)
})

test('waitForGatewayReady rejects on timeout', async () => {
  const child = fakeChild()
  await assert.rejects(waitForGatewayReady(child, 10), /Timed out/)
})

test('ready timeout override is clamped to a sane floor', () => {
  assert.equal(
    resolveGatewayReadyTimeoutMs({ YOUTAB_AGENT_SIDECAR_READY_TIMEOUT_MS: '1' } as any),
    MIN_GATEWAY_READY_TIMEOUT_MS
  )
  assert.equal(resolveGatewayReadyTimeoutMs({ YOUTAB_AGENT_SIDECAR_READY_TIMEOUT_MS: '120000' } as any), 120000)
})
