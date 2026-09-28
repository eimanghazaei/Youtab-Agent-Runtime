/**
 * Tests for electron/native-oauth.ts — the pure RFC 8252 native-app login
 * helpers (PKCE, capability detection, URL building, loopback callback
 * parsing, token-response normalization, refresh-timing).
 *
 * Run with: node --test electron/native-oauth.test.ts
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import { test } from 'vitest'

import {
  buildNativeAuthorizeUrl,
  createNativeRefreshCoordinator,
  generatePkcePair,
  generateState,
  inferenceRefreshDelayMs,
  NATIVE_FLOW_ID,
  nativeProfileSessionKeys,
  nativeRefreshUrl,
  nativeTokenUrl,
  parseLoopbackCallback,
  parseTokenResponse,
  resolveLoginStrategy,
  resolveNativeProviderProfile,
  revalidateNativeSessionsBeforeResume,
  statusSupportsNativeFlow,
  tokenNeedsRefresh
} from './native-oauth'

test('native provider profile must match main-selected profile', () => {
  assert.equal(resolveNativeProviderProfile(null, null), 'default')
  assert.equal(resolveNativeProviderProfile('coder', 'coder'), 'coder')
  assert.throws(() => resolveNativeProviderProfile('sibling', 'coder'), /does not match/)
  assert.throws(() => resolveNativeProviderProfile('coder', null), /does not match/)
  assert.throws(() => resolveNativeProviderProfile(42, 'coder'), /does not match/)
})

test('wake validation retains unreadable saved sessions', () => {
  const keys = ['inference', 'unreadable', 'remote-only']
  const tokens = parseTokenResponse({ access_token: 'AT' })
  assert.deepEqual(nativeProfileSessionKeys(keys, key => {
    if (key === 'unreadable') { return null }
    return { ...tokens, profiles: key === 'inference' ? ['coder'] : [] }
  }), ['inference', 'unreadable'])
})

test('remote-only native session follows account TTL without inference refresh', async () => {
  const now = 2_000_000
  let exchanges = 0
  let saved = parseTokenResponse({
    access_token: 'account-access', refresh_token: 'account-refresh',
    expires_at: now + 600
  })
  const coordinator = createNativeRefreshCoordinator({
    load: () => saved,
    exchange: async () => {
      exchanges++
      return { ...saved, accessToken: 'rotated-account', expiresAt: now + 900 }
    },
    writeProfile: async () => { throw new Error('remote-only session has no profile credential') },
    commit: (_baseUrl, tokens) => { saved = tokens },
    clear: () => { throw new Error('unexpected clear') },
    now: () => now
  })
  assert.equal(await coordinator.ensure('gateway'), 'account-access')
  assert.equal(exchanges, 0)
  assert.equal(await coordinator.ensure('gateway', true), 'rotated-account')
  assert.equal(exchanges, 1)
})

function refreshFixture() {
  let now = 2_000_000
  const jwt = (iat: number, exp: number) =>
    `header.${Buffer.from(JSON.stringify({ iat, exp })).toString('base64url')}.signature`
  const oldToken = jwt(now - 800, now + 100)
  const newToken = jwt(now, now + 900)
  let saved = {
    accessToken: 'old-account', refreshToken: 'old-refresh',
    inferenceAccessToken: oldToken, profiles: ['default'],
    expiresAt: now + 100, provider: 'youtab', userId: 'user'
  }
  const rotated = {
    ...saved, accessToken: 'new-account', refreshToken: 'new-refresh',
    inferenceAccessToken: newToken, expiresAt: now + 900
  }
  const writes: Array<[string, string | null]> = []
  let exchanges = 0
  let commits = 0
  let exchange = async () => { exchanges++; return rotated }
  let write = async (profile: string, token: string | null) => { writes.push([profile, token]) }
  const coordinator = createNativeRefreshCoordinator({
    load: () => saved,
    exchange: () => exchange(),
    writeProfile: (profile, token) => write(profile, token),
    commit: (_baseUrl, tokens) => { commits++; saved = tokens as typeof saved },
    clear: () => { throw new Error('unexpected clear') },
    now: () => now
  })
  return {
    coordinator, writes, oldToken, newToken, rotated,
    get saved() { return saved },
    get exchanges() { return exchanges },
    get commits() { return commits },
    setNow: (value: number) => { now = value },
    setExchange: (fn: typeof exchange) => { exchange = fn },
    setWrite: (fn: typeof write) => { write = fn }
  }
}

test('scheduled refresh writes profile before committing rotated session', async () => {
  const f = refreshFixture()
  assert.equal(await f.coordinator.ensure('gateway', true), 'new-account')
  assert.deepEqual(f.writes, [['default', f.newToken]])
  assert.equal(f.exchanges, 1)
  assert.equal(f.commits, 1)
  assert.equal(f.saved.refreshToken, 'new-refresh')
})

test('network delay and concurrent callers share one refresh result', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  let exchanges = 0
  let release!: (value: typeof f.rotated) => void
  f.setExchange(() => { exchanges++; return new Promise(resolve => { release = resolve }) })
  const first = f.coordinator.ensure('gateway')
  const second = f.coordinator.ensure('gateway', true)
  assert.strictEqual(first, second)
  assert.equal(f.commits, 0)
  release(f.rotated)
  assert.deepEqual(await Promise.all([first, second]), ['new-account', 'new-account'])
  assert.equal(exchanges, 1)
  assert.equal(f.writes.length, 1)
})

test('one failed refresh retries without clearing still-valid profile token', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  let calls = 0
  f.setExchange(async () => {
    if (++calls === 1) { throw new Error('network unavailable') }
    return f.rotated
  })
  await assert.rejects(f.coordinator.ensure('gateway'), /network unavailable/)
  assert.equal(f.saved.inferenceAccessToken, f.oldToken)
  assert.equal(f.commits, 0)
  assert.equal(await f.coordinator.ensure('gateway'), 'new-account')
  assert.equal(calls, 2)
})

test('profile write failure keeps old saved session and retries pending rotation once', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  let attempts = 0
  f.setWrite(async (profile, token) => {
    if (++attempts === 1) { throw new Error('profile write failed') }
    f.writes.push([profile, token])
  })
  await assert.rejects(f.coordinator.ensure('gateway'), /profile write failed/)
  assert.equal(f.commits, 0)
  assert.equal(f.saved.refreshToken, 'old-refresh')
  assert.equal(await f.coordinator.ensure('gateway'), 'new-account')
  assert.equal(f.exchanges, 1)
  assert.equal(f.saved.refreshToken, 'new-refresh')
})

test('second profile failure restores the first profile and does not commit', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  f.saved.profiles.push('sibling')
  f.setWrite(async (profile, token) => {
    f.writes.push([profile, token])
    if (profile === 'sibling') { throw new Error('sibling write failed') }
  })
  await assert.rejects(f.coordinator.ensure('gateway'), /sibling write failed/)
  assert.deepEqual(f.writes, [
    ['default', f.newToken], ['sibling', f.newToken], ['default', f.oldToken]
  ])
  assert.equal(f.commits, 0)
  assert.equal(f.saved.refreshToken, 'old-refresh')
})

test('refresh errors and coordinator state do not expose credential values', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  f.setExchange(async () => { throw new Error('network unavailable') })
  await assert.rejects(f.coordinator.ensure('gateway'), error => {
    assert.doesNotMatch(String(error), /old-refresh|old-account|header\./)
    return true
  })
})

test('wake before safety window reconnects without refresh; clock skew triggers refresh', async () => {
  const f = refreshFixture()
  f.setNow(2_000_000 - 100)
  let reconnects = 0
  await revalidateNativeSessionsBeforeResume(
    ['gateway'], () => f.saved, key => f.coordinator.ensure(key), () => { reconnects++ }
  )
  assert.equal(f.exchanges, 0)
  assert.equal(reconnects, 1)
  f.setNow(2_000_000 + 80)
  await revalidateNativeSessionsBeforeResume(
    ['gateway'], () => f.saved, key => f.coordinator.ensure(key), () => { reconnects++ }
  )
  assert.equal(f.exchanges, 1)
  assert.equal(reconnects, 2)
})

test('wake after timer deadline blocks reconnect until profile write completes', async () => {
  const f = refreshFixture()
  f.setNow(2_000_080)
  let finishWrite!: () => void
  f.setWrite(() => new Promise(resolve => { finishWrite = resolve }))
  let reconnects = 0
  const wake = revalidateNativeSessionsBeforeResume(
    ['gateway'], () => f.saved, key => f.coordinator.ensure(key), () => { reconnects++ }
  )
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(reconnects, 0)
  assert.equal(f.commits, 0)
  finishWrite()
  await wake
  assert.equal(reconnects, 1)
  assert.equal(f.commits, 1)
})

test('wake after expiry fails closed when refresh fails and does not reconnect', async () => {
  const f = refreshFixture()
  f.setNow(2_000_000 + 101)
  f.setExchange(async () => { throw new Error('network unavailable') })
  let reconnects = 0
  await assert.rejects(revalidateNativeSessionsBeforeResume(
    ['gateway'], () => f.saved, key => f.coordinator.ensure(key), () => { reconnects++ }
  ), /network unavailable/)
  assert.equal(reconnects, 0)
  assert.equal(f.commits, 0)
})

test('missing session after failed wake never releases reconnect', async () => {
  let reconnects = 0
  await assert.rejects(revalidateNativeSessionsBeforeResume(
    ['gateway'], () => null, async () => null, () => { reconnects++ }
  ), /revalidation pending/)
  assert.equal(reconnects, 0)
})

test('900-second inference token refreshes before Runtime lifetime margin', () => {
  const now = 2_000_000
  const payload = Buffer.from(JSON.stringify({ iat: now, exp: now + 900 })).toString('base64url')
  const tokens = {
    accessToken: 'account', refreshToken: 'refresh',
    inferenceAccessToken: `header.${payload}.signature`,
    expiresAt: now + 3600, provider: 'youtab', userId: 'user'
  }
  assert.equal(inferenceRefreshDelayMs(tokens, now), 870_000)
  assert.equal(inferenceRefreshDelayMs(tokens, now + 875), 0)
})

// --- PKCE ---

test('generatePkcePair produces a valid S256 verifier/challenge', () => {
  const pair = generatePkcePair()

  assert.equal(pair.method, 'S256')
  // Verifier length within RFC 7636 range (43–128).
  assert.ok(pair.verifier.length >= 43 && pair.verifier.length <= 128)

  // Challenge must be the base64url SHA-256 of the verifier.
  const expected = createHash('sha256')
    .update(pair.verifier, 'ascii')
    .digest('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')

  assert.equal(pair.challenge, expected)
  // No padding / URL-unsafe chars.
  assert.doesNotMatch(pair.verifier, /[+/=]/)
  assert.doesNotMatch(pair.challenge, /[+/=]/)
})

test('generatePkcePair is unique per call', () => {
  assert.notEqual(generatePkcePair().verifier, generatePkcePair().verifier)
})

test('generateState is non-empty and URL-safe', () => {
  const s = generateState()

  assert.ok(s.length > 0)
  assert.doesNotMatch(s, /[+/=]/)
})

// --- capability detection ---

test('statusSupportsNativeFlow reads the auth_flows array', () => {
  assert.equal(statusSupportsNativeFlow({ auth_flows: ['cookie', NATIVE_FLOW_ID] }), true)
  assert.equal(statusSupportsNativeFlow({ auth_flows: ['cookie'] }), false)
  // Older gateway: no auth_flows field at all ⇒ not supported.
  assert.equal(statusSupportsNativeFlow({ auth_required: true }), false)
  assert.equal(statusSupportsNativeFlow({}), false)
  assert.equal(statusSupportsNativeFlow(null), false)
  // Malformed field shapes never throw.
  assert.equal(statusSupportsNativeFlow({ auth_flows: 'native_pkce' }), false)
})

test('resolveLoginStrategy picks native only when advertised and not forced', () => {
  const gated = { auth_required: true, auth_flows: ['cookie', 'native_pkce'] }
  const legacy = { auth_required: true, auth_flows: ['cookie'] }

  assert.equal(resolveLoginStrategy(gated), 'native')
  // Compatibility fallback: an older gateway lacking native_pkce ⇒ embedded.
  assert.equal(resolveLoginStrategy(legacy), 'embedded')
  // A user/env override can pin the legacy flow even on a capable gateway.
  assert.equal(resolveLoginStrategy(gated, { forceEmbedded: true }), 'embedded')
})

// --- URL building ---

test('buildNativeAuthorizeUrl encodes params and honours a path prefix', () => {
  const url = buildNativeAuthorizeUrl('https://gw.example.com', {
    challenge: 'CHAL',
    redirectUri: 'http://127.0.0.1:51000/callback',
    state: 'STATE',
    provider: 'youtab'
  })

  const parsed = new URL(url)

  assert.equal(parsed.origin, 'https://gw.example.com')
  assert.equal(parsed.pathname, '/auth/native/authorize')
  assert.equal(parsed.searchParams.get('code_challenge'), 'CHAL')
  assert.equal(parsed.searchParams.get('code_challenge_method'), 'S256')
  assert.equal(parsed.searchParams.get('redirect_uri'), 'http://127.0.0.1:51000/callback')
  assert.equal(parsed.searchParams.get('state'), 'STATE')
  assert.equal(parsed.searchParams.get('provider'), 'youtab')
})

test('buildNativeAuthorizeUrl omits provider when not given and preserves prefix', () => {
  const url = buildNativeAuthorizeUrl('https://gw.example.com/youtab', {
    challenge: 'C',
    redirectUri: 'http://127.0.0.1:1/cb',
    state: 'S'
  })

  const parsed = new URL(url)

  assert.equal(parsed.pathname, '/youtab/auth/native/authorize')
  assert.equal(parsed.searchParams.get('provider'), null)
})

test('nativeTokenUrl / nativeRefreshUrl build the right endpoints', () => {
  assert.equal(nativeTokenUrl('https://gw.example.com'), 'https://gw.example.com/auth/native/token')
  assert.equal(nativeRefreshUrl('https://gw.example.com/youtab'), 'https://gw.example.com/youtab/auth/native/refresh')
})

// --- loopback callback parsing ---

test('parseLoopbackCallback returns the code on a state match', () => {
  const { code } = parseLoopbackCallback('/callback?code=abc123&state=xyz', 'xyz')

  assert.equal(code, 'abc123')
})

test('parseLoopbackCallback throws on state mismatch (CSRF)', () => {
  assert.throws(() => parseLoopbackCallback('/callback?code=abc&state=attacker', 'expected'), /state mismatch/i)
})

test('parseLoopbackCallback surfaces a gateway error param', () => {
  assert.throws(
    () => parseLoopbackCallback('/callback?error=access_denied&error_description=nope', 'xyz'),
    /access_denied.*nope/i
  )
})

test('parseLoopbackCallback throws when the code is absent', () => {
  assert.throws(() => parseLoopbackCallback('/callback?state=xyz', 'xyz'), /missing authorization code/i)
})

// --- token response normalization ---

test('parseTokenResponse maps a well-formed body', () => {
  const t = parseTokenResponse({
    access_token: 'AT',
    refresh_token: 'RT',
    token_type: 'Bearer',
    expires_at: 1893456000,
    provider: 'youtab',
    user_id: 'u-1'
  })

  assert.equal(t.accessToken, 'AT')
  assert.equal(t.refreshToken, 'RT')
  assert.equal(t.expiresAt, 1893456000)
  assert.equal(t.provider, 'youtab')
  assert.equal(t.userId, 'u-1')
})

test('inference token and linked profiles survive safeStorage JSON restoration', () => {
  const issued = parseTokenResponse({
    access_token: 'account-access',
    refresh_token: 'account-refresh',
    inference_access_token: 'inference-only',
    expires_at: 1893456000,
    provider: 'youtab',
    user_id: 'u-1'
  })
  issued.profiles = ['coder']
  const restored = parseTokenResponse(JSON.parse(JSON.stringify(issued)))
  assert.equal(restored.accessToken, 'account-access')
  assert.equal(restored.refreshToken, 'account-refresh')
  assert.equal(restored.inferenceAccessToken, 'inference-only')
  assert.deepEqual(restored.profiles, ['coder'])
  assert.equal(restored.expiresAt, 1893456000)
  assert.equal(restored.userId, 'u-1')
})

test('parseTokenResponse throws on a missing access token', () => {
  assert.throws(() => parseTokenResponse({ refresh_token: 'RT' }), /missing access_token/i)
})

test('parseTokenResponse tolerates an absent refresh token / expiry', () => {
  const t = parseTokenResponse({ access_token: 'AT' })

  assert.equal(t.refreshToken, '')
  assert.equal(t.expiresAt, 0)
})

// --- refresh timing ---

test('tokenNeedsRefresh respects the skew window', () => {
  const now = 1_000_000
  // Expires comfortably in the future ⇒ no refresh.
  assert.equal(tokenNeedsRefresh({ expiresAt: now + 3600 }, now), false)
  // Within the 60s skew ⇒ refresh early.
  assert.equal(tokenNeedsRefresh({ expiresAt: now + 30 }, now), true)
  // Already expired ⇒ refresh.
  assert.equal(tokenNeedsRefresh({ expiresAt: now - 10 }, now), true)
  // Unknown expiry ⇒ refresh (validate before use).
  assert.equal(tokenNeedsRefresh({ expiresAt: 0 }, now), true)
})
