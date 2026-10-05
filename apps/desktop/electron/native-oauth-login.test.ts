/**
 * Tests for electron/native-oauth-login.ts — the loopback-listener
 * orchestration of the RFC 8252 native login, with all I/O injected (fake
 * http server, fake openExternal, fake token POST) so no real socket or
 * browser is needed.
 *
 * Run with: node --test electron/native-oauth-login.test.ts
 */

import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { runNativeLogin } from './native-oauth-login'

// A fake http.Server: captures the request handler, lets the test drive a
// synthetic browser callback, and records listen/close lifecycle.
function makeFakeServerFactory(port = 51234) {
  const state: any = { handler: null, listening: false, closed: false, openedUrl: null }

  const createServer: any = (handler: any) => {
    state.handler = handler
    const server: any = new EventEmitter()

    server.listen = (_port: number, _host: string, cb: () => void) => {
      state.listening = true
      cb()
    }

    server.address = () => ({ address: '127.0.0.1', family: 'IPv4', port })

    server.close = () => {
      state.closed = true
    }

    state.server = server

    return server
  }

  // Drive a synthetic browser hit to the loopback callback.
  state.hitCallback = (query: string, method = 'GET', path = '/callback') => {
    const res: any = { writeHead: () => undefined, end: () => undefined }
    state.handler({ url: `${path}?${query}`, method }, res)
  }

  return { createServer, state }
}

test('runNativeLogin completes the loopback round trip and returns tokens', async () => {
  const { createServer, state } = makeFakeServerFactory()
  let capturedAuthorizeUrl = ''
  let tokenPostBody: any = null

  const promise = runNativeLogin(
    'https://gw.example.com',
    {
      openExternal: async url => {
        capturedAuthorizeUrl = url
      },
      postJson: async (_url, body) => {
        tokenPostBody = body

        return {
          access_token: 'AT-native',
          refresh_token: 'RT-native',
          token_type: 'Bearer',
          expires_at: 1893456000,
          provider: 'youtab',
          user_id: 'u-9'
        }
      },
      createServer,
      timeoutMs: 5_000
    },
    { provider: 'youtab' }
  )

  // Give the listen callback a tick to open the browser + capture the URL.
  await new Promise(r => setTimeout(r, 5))

  // The authorize URL must carry OUR challenge + loopback redirect + state.
  const authorize = new URL(capturedAuthorizeUrl)
  assert.equal(authorize.pathname, '/auth/native/authorize')
  const challenge = authorize.searchParams.get('code_challenge')
  const stateParam = authorize.searchParams.get('state')
  assert.ok(challenge && challenge.length > 0)
  assert.match(authorize.searchParams.get('redirect_uri') || '', /^http:\/\/127\.0\.0\.1:\d+\/callback$/)

  // Synthetic browser redirect back with the matching state + a code.
  state.hitCallback(`code=gw-code-1&state=${encodeURIComponent(stateParam!)}`)

  const tokens = await promise
  assert.equal(tokens.accessToken, 'AT-native')
  assert.equal(tokens.refreshToken, 'RT-native')
  assert.equal(tokens.userId, 'u-9')
  // The token POST carried the code + a verifier whose hash is the challenge.
  assert.equal(tokenPostBody.code, 'gw-code-1')
  assert.ok(tokenPostBody.code_verifier && tokenPostBody.code_verifier.length >= 43)
  // Listener was cleaned up.
  assert.equal(state.closed, true)
})

test('runNativeLogin ignores forged callbacks and still accepts the legitimate callback', async () => {
  const { createServer, state } = makeFakeServerFactory()
  let authorizeUrl = ''
  const tokenPosts: unknown[] = []
  const logs: string[] = []
  let settled = false

  const promise = runNativeLogin('https://gw.example.com', {
    openExternal: async url => { authorizeUrl = url },
    postJson: async (_url, body) => {
      tokenPosts.push(body)

      return { access_token: 'AT-legitimate', refresh_token: 'RT-legitimate', user_id: 'u-9' }
    },
    createServer,
    rememberLog: line => { logs.push(line) },
    timeoutMs: 5_000
  })

  // Attach both outcomes so a regression cannot create an unhandled rejection.
  void promise.then(() => { settled = true }, () => { settled = true })

  await new Promise(r => setTimeout(r, 5))
  const matchingState = new URL(authorizeUrl).searchParams.get('state')!
  const injected = encodeURIComponent('callback-canary\n\u001b[31mforged-log')

  const foreignCallbacks = [
    { query: `error=${injected}&error_description=${injected}` },
    { query: `error=access_denied&state=wrong&error_description=${injected}` },
    { query: 'code=evil&state=wrong' },
    { query: `code=evil&state=${matchingState}&state=wrong` },
    { query: `error=access_denied&state=${matchingState}`, path: '/favicon.ico' },
    { query: `code=evil&state=${matchingState}`, method: 'POST' },
    { query: `error=access_denied&state=${matchingState}`, method: 'POST' }
  ]

  for (const callback of foreignCallbacks) {
    state.hitCallback(callback.query, callback.method, callback.path)
    await new Promise(r => setTimeout(r, 0))
    assert.equal(settled, false, `foreign callback settled login: ${callback.query}`)
    assert.equal(state.closed, false)
    assert.equal(tokenPosts.length, 0)
  }

  state.hitCallback(`code=legitimate-code&state=${matchingState}`)
  const tokens = await promise
  assert.equal(tokens.accessToken, 'AT-legitimate')
  assert.equal(tokens.userId, 'u-9')
  assert.equal(tokenPosts.length, 1)
  assert.equal((tokenPosts[0] as { code: string }).code, 'legitimate-code')
  assert.equal(logs.some(line => line.includes('callback-canary') || line.includes('\u001b')), false)
  assert.equal(state.closed, true)
})

test('runNativeLogin surfaces only a stable error for a matching-state provider denial', async () => {
  const { createServer, state } = makeFakeServerFactory()
  let authorizeUrl = ''
  let tokenPostCalled = false
  const logs: string[] = []

  const promise = runNativeLogin('https://gw.example.com', {
    openExternal: async url => { authorizeUrl = url },
    postJson: async () => {
      tokenPostCalled = true

      return {}
    },
    createServer,
    rememberLog: line => { logs.push(line) },
    timeoutMs: 5_000
  })

  await new Promise(r => setTimeout(r, 5))
  const matchingState = new URL(authorizeUrl).searchParams.get('state')!
  const description = encodeURIComponent('denial-canary\n\u001b[31mprivate-description')
  state.hitCallback(`error=access_denied&state=${matchingState}&error_description=${description}`)

  await assert.rejects(promise, { message: 'Gateway rejected native login: access_denied' })
  assert.equal(tokenPostCalled, false)
  assert.equal(state.closed, true)
  assert.equal(logs.some(line => line.includes('denial-canary') || line.includes('\u001b')), false)
})

test('runNativeLogin times out when no callback arrives', async () => {
  const { createServer } = makeFakeServerFactory()

  await assert.rejects(
    runNativeLogin('https://gw.example.com', {
      openExternal: async () => undefined,
      postJson: async () => ({}),
      createServer,
      timeoutMs: 20
    }),
    /timed out/i
  )
})

test('runNativeLogin fails if the browser cannot be opened', async () => {
  const { createServer } = makeFakeServerFactory()

  await assert.rejects(
    runNativeLogin('https://gw.example.com', {
      openExternal: async () => {
        throw new Error('no browser')
      },
      postJson: async () => ({}),
      createServer,
      timeoutMs: 5_000
    }),
    /could not open the system browser/i
  )
})
