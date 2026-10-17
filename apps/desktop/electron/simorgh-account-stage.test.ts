import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isTrustedSimorghRendererUrl, stageSimorghAccountTurn } from './simorgh-account-stage'

test('accepts only the application renderer document for account handoff', () => {
  const renderer = 'file:///C:/Youtab/resources/app/dist/index.html'
  assert.equal(isTrustedSimorghRendererUrl(`${renderer}?win=session#/chat`, renderer), true)
  assert.equal(isTrustedSimorghRendererUrl('file:///C:/Users/Public/other.html', renderer), false)
  assert.equal(isTrustedSimorghRendererUrl('https://other.example/', renderer), false)
  assert.equal(isTrustedSimorghRendererUrl('http://127.0.0.1:5173/?win=session', 'http://127.0.0.1:5173/'), true)
  assert.equal(isTrustedSimorghRendererUrl('http://127.0.0.1:5174/', 'http://127.0.0.1:5173/'), false)
})

test('stages a bearer without returning it and ignores unrelated gateway events', async () => {
  let socket: any

  class FakeSocket {
    listeners: Record<string, ((event?: any) => void)[]> = {}
    sent = ''
    closed = false
    constructor(_url: string) { socket = this }
    addEventListener(name: string, listener: (event?: any) => void) {
      ;(this.listeners[name] ||= []).push(listener)
    }
    send(value: string) { this.sent = value }
    close() { this.closed = true }
    emit(name: string, event?: any) {
      for (const listener of this.listeners[name] || []) { listener(event) }
    }
  }

  const promise = stageSimorghAccountTurn('ws://127.0.0.1/api/ws', {
    session_id: 'owned-session', prompt_sha256: 'a'.repeat(64), account_bearer: 'synthetic-account'
  }, FakeSocket)

  socket.emit('open')
  const sent = JSON.parse(socket.sent)
  assert.equal(sent.method, 'simorgh.account.stage')
  assert.equal(sent.params.account_bearer, 'synthetic-account')
  socket.emit('message', { data: '{"method":"event"}' })
  socket.emit('message', { data: JSON.stringify({ id: sent.id, result: { nonce: 'b'.repeat(32) } }) })
  assert.equal(await promise, 'b'.repeat(32))
  assert.equal(socket.closed, true)
})

test('rejects an error without echoing credential content', async () => {
  let socket: any

  class FakeSocket {
    listeners: Record<string, ((event?: any) => void)[]> = {}
    constructor(_url: string) { socket = this }
    addEventListener(name: string, listener: (event?: any) => void) {
      ;(this.listeners[name] ||= []).push(listener)
    }
    send(_value: string) {}
    close() {}
    emit(name: string, event?: any) {
      for (const listener of this.listeners[name] || []) { listener(event) }
    }
  }

  const promise = stageSimorghAccountTurn('ws://127.0.0.1/api/ws', {
    session_id: 's', prompt_sha256: 'a'.repeat(64), account_bearer: 'synthetic-account'
  }, FakeSocket)

  socket.emit('open')
  socket.emit('message', { data: '{"id":"simorgh-account-stage","error":{"message":"synthetic-account"}}' })
  await assert.rejects(promise, error => {
    assert.equal(String(error).includes('synthetic-account'), false)

    return true
  })
})
