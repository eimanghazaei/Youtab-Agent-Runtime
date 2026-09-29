/**
 * native-oauth.ts
 *
 * Pure, electron-free helpers for the desktop's RFC 8252 (OAuth 2.0 for Native
 * Apps) login to a gated Youtab gateway: system-browser + loopback redirect +
 * PKCE, with tokens returned to the app (never browser session cookies).
 *
 * Kept standalone (no `import 'electron'`) so it unit-tests with `node --test`
 * — same pattern as connection-config.ts. main.ts owns the electron-coupled
 * parts (the actual http.Server loopback listener, shell.openExternal, and
 * safeStorage keychain writes) and calls these helpers for the pure logic.
 *
 * Why the gateway brokers the flow (not a direct desktop→IDP client): the
 * upstream IDP (Youtab Portal) issues a per-gateway-instance client_id and only
 * accepts a redirect_uri on the gateway's own origin, so a desktop loopback
 * redirect can't be a direct Portal client. Instead the gateway exposes
 * /auth/native/{authorize,token,refresh}: it is the authorization server to
 * the desktop and an OAuth client to Portal. The desktop still gets the full
 * RFC 8252 experience — its own PKCE pair, its own loopback redirect, tokens
 * it stores itself.
 *
 * Capability detection: the gateway advertises supported flows on the public
 * /api/status `auth_flows` array. `native_pkce` present ⇒ use this flow;
 * absent (older gateway) ⇒ the caller falls back to the embedded-webview
 * cookie flow. This is the "observable ladder / compatibility fallback tied to
 * an identified older runtime" the desktop guide requires.
 */

import { createHash, randomBytes } from 'node:crypto'

// The gateway status field that lists supported auth flows. See
// youtab_agent_cli/web_server.py status handler.
const NATIVE_FLOW_ID = 'native_pkce'

export interface NativePkcePair {
  verifier: string
  challenge: string
  method: 'S256'
}

export interface NativeTokenSet {
  accessToken: string
  refreshToken: string
  inferenceAccessToken?: string
  /** Local profiles whose inference credential follows this Gateway session. */
  profiles?: string[]
  /** Encrypted retry inventory after an incomplete local sign-out. */
  logoutPending?: boolean
  /** Rotated single-use refresh authority saved before profile bearers are updated. */
  rotationPending?: boolean
  expiresAt: number
  provider: string
  userId: string
}

/** A renderer-supplied profile may only confirm the main process's selected profile. */
export function resolveNativeProviderProfile(requested: unknown, selected: string | null, sticky = 'default'): string {
  const profile = selected || sticky
  if (requested != null && requested !== profile) {
    throw new Error('Native provider profile does not match the selected Desktop profile')
  }
  return profile
}

/** Keep unreadable saved sessions in wake validation so they fail closed. */
export function nativeProfileSessionKeys(
  keys: string[],
  load: (baseUrl: string) => NativeTokenSet | null
): string[] {
  return keys.filter(baseUrl => {
    const tokens = load(baseUrl)
    return !tokens || Boolean(tokens.profiles?.length)
  })
}

/** Retry saved cleanup briefly; corrupt sessions still require a new login or logout. */
export function shouldRetryNativeResume(
  keys: string[],
  load: (baseUrl: string) => NativeTokenSet | null,
  attempts: number
): boolean {
  return attempts < 3 && keys.every(baseUrl => {
    const tokens = load(baseUrl)
    return tokens !== null
  })
}

/** Clear the old account before installing a different account's profile token. */
export async function applyNativeLoginProfiles(
  previous: NativeTokenSet | null,
  next: NativeTokenSet,
  selected: string,
  writeProfile: (profile: string, token: string | null) => Promise<void>,
  clearPrevious: () => void
): Promise<string[]> {
  if (!next.userId || !next.inferenceAccessToken) {
    throw new Error('Gateway native session omitted account identity or inference credential')
  }
  if (!previous || previous.userId !== next.userId) {
    const priorProfiles = [...new Set(previous?.profiles || [])]
    const priorToken = previous && unexpiredInferenceToken(previous, Math.floor(Date.now() / 1000))
    const attempted: string[] = []
    try {
      for (const profile of priorProfiles) {
        attempted.push(profile)
        await writeProfile(profile, null)
      }
    } catch (error) {
      for (const profile of attempted.reverse()) {
        try { await writeProfile(profile, priorToken || null) } catch { /* Keep restoring siblings. */ }
      }
      throw error
    }
    if (previous) {
      try { clearPrevious() }
      catch (error) {
        for (const profile of priorProfiles.reverse()) {
          try { await writeProfile(profile, priorToken || null) } catch { /* Keep restoring siblings. */ }
        }
        throw error
      }
    }
    try {
      await writeProfile(selected, next.inferenceAccessToken)
    } catch (error) {
      try { await writeProfile(selected, null) } catch { /* Preserve the original write error. */ }
      throw error
    }
    return [selected]
  }
  const profiles = [...new Set([...(previous.profiles || []), selected])]
  const updated: string[] = []
  try {
    for (const profile of profiles) {
      updated.push(profile)
      await writeProfile(profile, next.inferenceAccessToken)
    }
  } catch (error) {
    const priorToken = unexpiredInferenceToken(previous, Math.floor(Date.now() / 1000))
    for (const profile of updated.reverse()) {
      try { await writeProfile(profile, priorToken) } catch { /* Original error remains actionable. */ }
    }
    throw error
  }
  return profiles
}

/** One mutation boundary for the saved session, profile bearers and refresh timer. */
export function createNativeSessionLifecycle(deps: {
  load: (baseUrl: string) => NativeTokenSet | null
  store: (baseUrl: string, tokens: NativeTokenSet) => void
  clear: (baseUrl: string) => void
  writeProfile: (profile: string, token: string | null) => Promise<void>
  pauseRefresh: (baseUrl: string) => void
  waitForRefresh: (baseUrl: string) => Promise<void>
  discardPending: (baseUrl: string) => void
  relinkPending?: (baseUrl: string, oldName: string, newName: string | null) => void
  savedSessionState?: (baseUrl: string) => 'absent' | 'readable' | 'unreadable'
  scheduleRelinkRetry?: (retry: () => Promise<void>, delayMs: number) => void
  logRelinkFailure?: () => void
  onRelinkExhausted?: (baseUrl: string, profiles: string[]) => void
  scheduleRefresh: (baseUrl: string, tokens: NativeTokenSet) => void
  revoke: (baseUrl: string, refreshToken: string) => Promise<void>
  logRevocationFailure: () => void
}) {
  const mutating = new Set<string>()
  const pendingRelinks = new Map<string, Array<{ oldName: string; newName: string | null }>>()
  const relinking = new Map<string, Promise<void>>()
  const relinkFailures = new Map<string, number>()

  function currentProfiles(baseUrl: string, profiles: string[] | undefined): string[] {
    let current = profiles || []
    for (const { oldName, newName } of pendingRelinks.get(baseUrl) || []) {
      current = current.map(name => name === oldName ? newName : name).filter((name): name is string => name !== null)
    }
    return [...new Set(current)]
  }

  async function withMutation(baseUrl: string, action: () => Promise<void>) {
    if (mutating.has(baseUrl)) { throw new Error('Native session update already in progress') }
    mutating.add(baseUrl)
    deps.pauseRefresh(baseUrl)
    try {
      await deps.waitForRefresh(baseUrl)
      await action()
    } finally {
      mutating.delete(baseUrl)
      const current = deps.load(baseUrl)
      if (current && !current.logoutPending) { deps.scheduleRefresh(baseUrl, current) }
    }
  }

  async function login(baseUrl: string, selected: string, tokens: NativeTokenSet) {
    await withMutation(baseUrl, async () => {
      deps.discardPending(baseUrl)
      const saved = deps.load(baseUrl)
      const previous = saved ? { ...saved, profiles: currentProfiles(baseUrl, saved.profiles) } : null
      const selectedProfiles = currentProfiles(baseUrl, [selected])
      if (!selectedProfiles.length) { throw new Error('Selected profile was deleted. Choose another profile.') }
      tokens.profiles = await applyNativeLoginProfiles(previous, tokens, selectedProfiles[0], deps.writeProfile, () => deps.clear(baseUrl))
      try {
        deps.store(baseUrl, tokens)
        pendingRelinks.delete(baseUrl)
        relinkFailures.delete(baseUrl)
      } catch (error) {
        if (previous && previous.userId === tokens.userId && !previous.logoutPending) {
          // The old encrypted session is still authoritative when its rewrite
          // fails. Restore all linked bearers instead of signing out siblings.
          const oldBearer = unexpiredInferenceToken(previous, Math.floor(Date.now() / 1000))
          for (const profile of tokens.profiles) {
            const restore = previous.profiles?.includes(profile) ? oldBearer : null
            try { await deps.writeProfile(profile, restore) } catch { /* Preserve the store error. */ }
          }
          throw error
        }
        // A failed encrypted-store commit must not leave a new bearer with an
        // old (or absent) refresh authority. Fail closed, including siblings.
        try { deps.clear(baseUrl) } catch { /* Continue clearing profile bearers. */ }
        for (const profile of tokens.profiles) {
          try { await deps.writeProfile(profile, null) } catch { /* Surface commit failure. */ }
        }
        throw error
      }
    })
  }

  /** Gateway Connections sign-in replaces account auth without linking a local provider profile. */
  async function replaceGatewaySession(baseUrl: string, tokens: NativeTokenSet) {
    await withMutation(baseUrl, async () => {
      if (!tokens.refreshToken) {
        throw new Error('Gateway native session omitted refresh credential')
      }
      deps.discardPending(baseUrl)
      const saved = deps.load(baseUrl)
      const previous = saved ? { ...saved, profiles: currentProfiles(baseUrl, saved.profiles) } : null
      const profiles = [...new Set(previous?.profiles || [])]
      const sameAccount = Boolean(previous && !previous.logoutPending && previous.userId && previous.userId === tokens.userId)
      if (sameAccount && profiles.length && !tokens.inferenceAccessToken) {
        throw new Error('Gateway native session omitted inference credential for linked profiles')
      }
      const attempted: string[] = []
      try {
        for (const profile of profiles) {
          attempted.push(profile)
          await deps.writeProfile(profile, sameAccount ? tokens.inferenceAccessToken! : null)
        }
        deps.store(baseUrl, { ...tokens, profiles: sameAccount ? profiles : [] })
        pendingRelinks.delete(baseUrl)
        relinkFailures.delete(baseUrl)
      } catch (error) {
        const priorToken = previous && unexpiredInferenceToken(previous, Math.floor(Date.now() / 1000))
        for (const profile of attempted.reverse()) {
          try { await deps.writeProfile(profile, priorToken || null) } catch { /* Preserve the original failure. */ }
        }
        throw error
      }
    })
  }

  async function logout(baseUrl: string, selected: string | null) {
    await withMutation(baseUrl, async () => {
      deps.discardPending(baseUrl)
      const saved = deps.load(baseUrl)
      const tokens = saved ? { ...saved, profiles: currentProfiles(baseUrl, saved.profiles) } : null
      // Persist a fail-closed inventory before touching profile files. Failed
      // clears remain retryable across restarts without refreshing bearers.
      if (tokens && !tokens.logoutPending) { deps.store(baseUrl, { ...tokens, logoutPending: true }) }
      await clearNativeProfileCredentials(tokens, selected, () => deps.clear(baseUrl), deps.writeProfile)
      pendingRelinks.delete(baseUrl)
      relinkFailures.delete(baseUrl)
      if (tokens?.refreshToken) {
        try { await deps.revoke(baseUrl, tokens.refreshToken) }
        catch { deps.logRevocationFailure() }
      }
    })
  }

  function flushProfileRelinks(baseUrl: string): Promise<void> {
    const active = relinking.get(baseUrl)
    if (active) { return active }
    const operation = (async () => {
      const queue = pendingRelinks.get(baseUrl)
      while (queue?.length) {
        const { oldName, newName } = queue[0]
        await withMutation(baseUrl, async () => {
          // A rotated one-use refresh may be waiting after a failed write.
          // Retarget it before the encrypted-store write can fail.
          deps.relinkPending?.(baseUrl, oldName, newName)
          const sessionState = deps.savedSessionState?.(baseUrl)
          if (sessionState === 'unreadable') {
            throw new Error('Saved native session is unreadable')
          }
          const tokens = deps.load(baseUrl)
          if (!tokens && sessionState === 'readable') {
            throw new Error('Saved native session became unreadable')
          }
          if (tokens?.profiles?.includes(oldName)) {
            const profiles = [...new Set(tokens.profiles.map(name => name === oldName ? newName : name).filter((name): name is string => name !== null))]
            deps.store(baseUrl, { ...tokens, profiles })
          }
        })
        queue.shift()
      }
      pendingRelinks.delete(baseUrl)
      relinkFailures.delete(baseUrl)
    })()
    const handled = operation.catch(() => {
      try { deps.logRelinkFailure?.() } catch { /* Keep the confirmed API result. */ }
      const failures = (relinkFailures.get(baseUrl) || 0) + 1
      relinkFailures.set(baseUrl, failures)
      if (failures < 3) {
        try { deps.scheduleRelinkRetry?.(() => flushProfileRelinks(baseUrl), 30_000) }
        catch { /* The pending relink still blocks native refresh. */ }
      } else if (failures === 3) {
        try {
          // The encrypted store can still hold the old name (or be unreadable)
          // when its rewrite failed. Report the affected retargeted profiles
          // from the confirmed rename queue, not the stale saved inventory.
          const profiles = currentProfiles(
            baseUrl, (pendingRelinks.get(baseUrl) || []).map(({ oldName }) => oldName)
          )
          deps.onRelinkExhausted?.(baseUrl, profiles)
        } catch { /* Keep the confirmed API result. */ }
      }
    }).finally(() => {
      if (relinking.get(baseUrl) === handled) {
        relinking.delete(baseUrl)
        if (!relinkFailures.has(baseUrl) && pendingRelinks.get(baseUrl)?.length) {
          void flushProfileRelinks(baseUrl)
        }
      }
    })
    relinking.set(baseUrl, handled)
    return handled
  }

  function relinkProfile(baseUrl: string, oldName: string, newName: string | null): Promise<void> {
    const queue = pendingRelinks.get(baseUrl) || []
    queue.push({ oldName, newName })
    pendingRelinks.set(baseUrl, queue)
    return flushProfileRelinks(baseUrl)
  }

  return { isMutating: (baseUrl: string) => mutating.has(baseUrl) || pendingRelinks.has(baseUrl), login, logout, relinkProfile, replaceGatewaySession }
}

/** Stop transient native refresh retries after a bounded failure streak. */
export function createNativeRefreshRetryCounter(limit = 3) {
  const failures = new Map<string, number>()
  return {
    failed(baseUrl: string): boolean {
      const count = (failures.get(baseUrl) || 0) + 1
      failures.set(baseUrl, count)
      return count < limit
    },
    reset(baseUrl: string) { failures.delete(baseUrl) }
  }
}

/** Read only successful profile mutations; the backend owns canonical names. */
export function nativeProfileMutationFromApiResult(
  request: { body?: { new_name?: unknown }; method?: string; path?: string } | null,
  result: { name?: unknown; ok?: boolean } | null,
  backendMode: string
): { oldName: string; newName: string | null } | null {
  // A remote backend's profile inventory does not rename/delete Desktop's
  // local profile directories or their linked inference credentials.
  if (backendMode !== 'local' || result?.ok !== true) { return null }
  const method = String(request?.method || '').toUpperCase()
  if (method !== 'PATCH' && method !== 'DELETE') { return null }
  const match = String(request?.path || '').match(/^\/api\/profiles\/([^/?#]+)(?:[?#].*)?$/)
  if (!match) { return null }
  let oldName: string
  try { oldName = decodeURIComponent(match[1]).trim().toLowerCase() }
  catch { return null }
  const valid = (name: string) => name !== 'default' && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)
  if (!valid(oldName)) { return null }
  if (method === 'DELETE') { return { oldName, newName: null } }
  const newName = String(result.name || request?.body?.new_name || '').trim().toLowerCase()
  return valid(newName) ? { oldName, newName } : null
}

/** Settings sign-out clears the cookie partition even if native cleanup fails. */
export async function clearGatewaySessionCredentials(
  hasNativeSession: boolean,
  logoutNative: () => Promise<void>,
  clearOauthCookie: () => Promise<void>,
  afterNativeChange: () => void
): Promise<void> {
  try {
    if (hasNativeSession) { await logoutNative() }
  } finally {
    try { await clearOauthCookie() }
    finally { if (hasNativeSession) { afterNativeChange() } }
  }
}

/** Clear every linked bearer, retaining the saved inventory on any failure. */
export async function clearNativeProfileCredentials(
  tokens: NativeTokenSet | null,
  selected: string | null,
  clearSession: () => void,
  writeProfile: (profile: string, token: string | null) => Promise<void>
): Promise<void> {
  let firstError: unknown = null
  for (const profile of new Set([...(tokens?.profiles || []), ...(selected ? [selected] : [])])) {
    try { await writeProfile(profile, null) }
    catch (error) { firstError ??= error }
  }
  if (firstError) { throw firstError }
  clearSession()
}

/** base64url without `=` padding (RFC 7636 §4). */
function b64url(raw: Buffer): string {
  return raw.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Generate a PKCE verifier/challenge pair (S256). The verifier is 32 random
 * bytes base64url-encoded (43 chars, within RFC 7636's 43–128 range).
 */
export function generatePkcePair(randomImpl: (n: number) => Buffer = randomBytes): NativePkcePair {
  const verifier = b64url(randomImpl(32))
  const challenge = b64url(createHash('sha256').update(verifier, 'ascii').digest())

  return { verifier, challenge, method: 'S256' }
}

/** A high-entropy CSRF `state` value for the loopback round trip. */
export function generateState(randomImpl: (n: number) => Buffer = randomBytes): string {
  return b64url(randomImpl(24))
}

/**
 * True if a gateway `/api/status` body advertises the native PKCE flow.
 * Tolerant of the field being absent (older gateway) or malformed.
 */
export function statusSupportsNativeFlow(statusBody: any): boolean {
  const flows = statusBody && statusBody.auth_flows

  return Array.isArray(flows) && flows.includes(NATIVE_FLOW_ID)
}

/**
 * Decide the login strategy for a gated gateway from its status body.
 * Returns 'native' when the gateway can do RFC 8252 AND we're not forced to
 * the legacy path; 'embedded' otherwise (older gateway ⇒ webview fallback).
 *
 * `forceEmbedded` lets a user/setting or an env override pin the legacy flow
 * (e.g. a corporate proxy that blocks loopback). Precedence written down here,
 * in one place, as a pure function — per the desktop "observable ladder" rule.
 */
export function resolveLoginStrategy(statusBody: any, opts: { forceEmbedded?: boolean } = {}): 'native' | 'embedded' {
  if (opts.forceEmbedded) {
    return 'embedded'
  }

  return statusSupportsNativeFlow(statusBody) ? 'native' : 'embedded'
}

/**
 * Build the gateway `/auth/native/authorize` URL the system browser opens.
 * `redirectUri` is the desktop's loopback callback (127.0.0.1:<port>/...).
 * `provider` is optional — omitted lets the gateway pick when it has exactly
 * one session provider (the common hosted case).
 */
export function buildNativeAuthorizeUrl(
  baseUrl: string,
  params: { challenge: string; redirectUri: string; state: string; provider?: string }
): string {
  const parsed = new URL(baseUrl)
  const prefix = parsed.pathname.replace(/\/+$/, '')

  const q = new URLSearchParams({
    code_challenge: params.challenge,
    code_challenge_method: 'S256',
    redirect_uri: params.redirectUri,
    state: params.state
  })

  if (params.provider) {
    q.set('provider', params.provider)
  }

  return `${parsed.protocol}//${parsed.host}${prefix}/auth/native/authorize?${q.toString()}`
}

/** The `/auth/native/token` endpoint URL for a gateway base URL. */
export function nativeTokenUrl(baseUrl: string): string {
  const parsed = new URL(baseUrl)
  const prefix = parsed.pathname.replace(/\/+$/, '')

  return `${parsed.protocol}//${parsed.host}${prefix}/auth/native/token`
}

/** The `/auth/native/refresh` endpoint URL for a gateway base URL. */
export function nativeRefreshUrl(baseUrl: string): string {
  const parsed = new URL(baseUrl)
  const prefix = parsed.pathname.replace(/\/+$/, '')

  return `${parsed.protocol}//${parsed.host}${prefix}/auth/native/refresh`
}

/**
 * Parse the loopback redirect the gateway sends the browser to. Returns the
 * `code` + `state`, or throws with the gateway's `error` if the flow failed.
 * `expectedState` MUST match (CSRF defense — RFC 6749 §10.12); a mismatch
 * throws rather than proceeding.
 */
export function parseLoopbackCallback(requestUrl: string, expectedState: string): { code: string } {
  // requestUrl is the path+query the loopback server received, e.g.
  // "/callback?code=...&state=...". Resolve against a dummy origin to parse.
  const parsed = new URL(requestUrl, 'http://127.0.0.1')
  const error = parsed.searchParams.get('error')

  if (error) {
    const desc = parsed.searchParams.get('error_description') || ''
    throw new Error(`Gateway rejected native login: ${error}${desc ? ` (${desc})` : ''}`)
  }

  const code = parsed.searchParams.get('code') || ''
  const state = parsed.searchParams.get('state') || ''

  if (!code) {
    throw new Error('Loopback callback missing authorization code')
  }

  if (!expectedState || state !== expectedState) {
    // Never redeem a code that arrived with a mismatched state — it may be a
    // forged callback trying to inject an attacker's code.
    throw new Error('Loopback callback state mismatch (possible CSRF)')
  }

  return { code }
}

/**
 * Normalize a `/auth/native/token` (or refresh) JSON response into a
 * NativeTokenSet, validating the shape. Throws on a missing/short access
 * token so a malformed response fails loudly rather than storing junk.
 */
export function parseTokenResponse(body: any): NativeTokenSet {
  const accessToken = String(body?.access_token || body?.accessToken || '')

  if (!accessToken) {
    throw new Error('Gateway token response missing access_token')
  }

  const expiresAt = Number(body?.expires_at ?? body?.expiresAt)

  return {
    accessToken,
    refreshToken: String(body?.refresh_token || body?.refreshToken || ''),
    inferenceAccessToken: String(body?.inference_access_token || body?.inferenceAccessToken || ''),
    profiles: Array.isArray(body?.profiles)
      ? body.profiles.filter((profile: unknown): profile is string => typeof profile === 'string')
      : [],
    logoutPending: body?.logoutPending === true,
    rotationPending: body?.rotationPending === true,
    expiresAt: Number.isFinite(expiresAt) ? expiresAt : 0,
    provider: String(body?.provider || ''),
    userId: String(body?.user_id || body?.userId || '')
  }
}

/** Keep the HTTP status available to the native refresh rejection policy. */
export function nativeHttpResponseError(statusCode: number, detail: string): Error & { statusCode: number } {
  return Object.assign(new Error(`${statusCode}: ${detail}`), { statusCode })
}

/**
 * True when a stored token set is at/near expiry and should be refreshed
 * before use. `skewSeconds` refreshes slightly early to avoid a race where
 * the token expires in flight (mirrors the server's 60s cookie floor).
 */
export function tokenNeedsRefresh(
  tokens: Pick<NativeTokenSet, 'expiresAt'>,
  nowSeconds: number,
  skewSeconds = 60
): boolean {
  if (!tokens || !Number.isFinite(tokens.expiresAt) || tokens.expiresAt <= 0) {
    // Unknown expiry ⇒ treat as needing refresh so we validate before use.
    return true
  }

  return nowSeconds >= tokens.expiresAt - skewSeconds
}

/** Deadline for replacing the profile token before Runtime's lifetime margin. */
export function inferenceRefreshDelayMs(tokens: NativeTokenSet, nowSeconds: number): number {
  if (!tokens.inferenceAccessToken) { return 0 }
  try {
    const payload = JSON.parse(Buffer.from(tokens.inferenceAccessToken.split('.')[1], 'base64url').toString('utf8'))
    const expires = Number(payload.exp)
    const issued = Number(payload.iat)
    if (!Number.isFinite(expires) || expires <= nowSeconds) { return 0 }
    const lifetime = Number.isFinite(issued) && issued < expires ? expires - issued : expires - nowSeconds
    const margin = Math.min(Math.max(1, Math.floor(lifetime / 10)), 30, Math.max(0, lifetime - 1))
    const accountDeadline = tokens.expiresAt > 0 ? tokens.expiresAt - 90 : expires
    return Math.max(0, Math.floor((Math.min(expires - margin, accountDeadline) - nowSeconds) * 1000))
  } catch {
    return 0 // Malformed token must be refreshed, not treated as durable.
  }
}

function unexpiredInferenceToken(tokens: NativeTokenSet, nowSeconds: number): string | null {
  try {
    const token = tokens.inferenceAccessToken || ''
    const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
    return Number(payload.exp) > nowSeconds ? token : null
  } catch {
    return null
  }
}

/** Serialize refresh and durably retain a rotated response until profile writes succeed. */
export function createNativeRefreshCoordinator(deps: {
  load: (baseUrl: string) => NativeTokenSet | null
  exchange: (baseUrl: string, tokens: NativeTokenSet) => Promise<NativeTokenSet>
  writeProfile: (profile: string, token: string | null) => Promise<void>
  commit: (baseUrl: string, tokens: NativeTokenSet) => void
  clear: (baseUrl: string) => void
  revoke?: (baseUrl: string, refreshToken: string) => Promise<void>
  validateProfiles?: (profiles: string[]) => void
  now: () => number
}) {
  const inFlight = new Map<string, Promise<string | null>>()
  const pending = new Map<string, NativeTokenSet>()

  function discard(baseUrl: string) {
    pending.delete(baseUrl)
  }

  function relinkPending(baseUrl: string, oldName: string, newName: string | null) {
    const tokens = pending.get(baseUrl)
    if (!tokens?.profiles?.includes(oldName)) { return }
    tokens.profiles = [...new Set(tokens.profiles.map(name => name === oldName ? newName : name).filter((name): name is string => name !== null))]
  }

  async function wait(baseUrl: string): Promise<void> {
    try {
      await inFlight.get(baseUrl)
    } catch {
      // Login/logout still needs to proceed after a transient refresh error.
    }
  }

  async function clearTerminalSession(baseUrl: string, tokens: NativeTokenSet): Promise<never> {
    try {
      // Persist the cleanup inventory before touching any child. An interrupted
      // clear cannot make the rejected refresh authority usable after restart.
      if (!tokens.logoutPending) { deps.commit(baseUrl, { ...tokens, logoutPending: true }) }
      let firstError: unknown = null
      for (const profile of new Set(tokens.profiles || [])) {
        try { await deps.writeProfile(profile, null) }
        catch (error) { firstError ??= error }
      }
      if (firstError) { throw firstError }
      if (tokens.refreshToken) {
        try { await deps.revoke?.(baseUrl, tokens.refreshToken) }
        catch { /* Local cleanup remains authoritative if remote revocation fails. */ }
      }
      deps.clear(baseUrl)
      pending.delete(baseUrl)
    } catch (error) {
      // Even an interrupted clear follows a confirmed terminal rejection.
      // Keep all REST/WS callers off the cookie fallback while cleanup retries.
      if (error instanceof Error) { throw Object.assign(error, { needsOauthLogin: true }) }
      throw Object.assign(new Error('Native session cleanup incomplete.'), { needsOauthLogin: true, cause: error })
    }
    throw Object.assign(new Error('Native session expired. Sign in again in Settings → Gateway.'), { needsOauthLogin: true })
  }

  function ensure(baseUrl: string, force = false): Promise<string | null> {
    const active = inFlight.get(baseUrl)
    if (active) { return active }

    const operation = (async () => {
      const tokens = deps.load(baseUrl)
      if (!tokens) { return null }
      if (tokens.logoutPending) { return clearTerminalSession(baseUrl, tokens) }
      const now = deps.now()
      const profileTokenNeedsRefresh = Boolean(tokens.profiles?.length)
        && inferenceRefreshDelayMs(tokens, now) <= 0
      // Also fail closed for a still-valid account token after a CLI rename.
      deps.validateProfiles?.(tokens.profiles || [])
      if (!force && !tokens.rotationPending && !pending.has(baseUrl) && !tokenNeedsRefresh(tokens, now)
          && !profileTokenNeedsRefresh) { return tokens.accessToken }

      if (!tokens.refreshToken) {
        return clearTerminalSession(baseUrl, tokens)
      }

      const savedRotation = pending.get(baseUrl) || (tokens.rotationPending ? tokens : null)
      // A restart can outlive the saved inference bearer. Refresh using the
      // *new* saved refresh authority before replaying profile writes.
      const staleRotation = savedRotation && (tokenNeedsRefresh(savedRotation, now)
        || (savedRotation.profiles?.length && inferenceRefreshDelayMs(savedRotation, now) <= 0))
      let rotated = staleRotation ? null : savedRotation
      if (!rotated) {
        try {
          rotated = await deps.exchange(baseUrl, tokens)
        } catch (error: any) {
          if (error?.statusCode !== 401 && error?.statusCode !== 403) { throw error }
          return clearTerminalSession(baseUrl, tokens)
        }
        rotated.profiles = tokens.profiles
        if (!rotated.refreshToken) {
          throw new Error('Gateway refresh omitted refresh credential')
        }
        if (rotated.profiles?.length && !rotated.inferenceAccessToken) {
          throw new Error('Gateway refresh omitted inference credential')
        }
        // The Gateway may already have consumed the old, single-use refresh
        // token. Save the new authority in the existing encrypted session store
        // before the fallible profile writes; the marker makes restart replay
        // mandatory and prevents an incomplete rotation from being accepted.
        rotated = { ...rotated, rotationPending: true }
        deps.commit(baseUrl, rotated)
        pending.set(baseUrl, rotated)
      }
      const updated: string[] = []
      try {
        for (const profile of rotated.profiles || []) {
          updated.push(profile)
          await deps.writeProfile(profile, rotated.inferenceAccessToken!)
        }
        deps.commit(baseUrl, { ...rotated, rotationPending: false })
      } catch (error) {
        // A timed-out child may have persisted its write before rejecting.
        // Restore every attempted profile, including that child.
        const previous = unexpiredInferenceToken(tokens, deps.now())
        for (const profile of updated.reverse()) {
          try { await deps.writeProfile(profile, previous) } catch { /* Keep restoring siblings. */ }
        }
        throw error
      }
      pending.delete(baseUrl)
      return rotated.accessToken
    })()

    inFlight.set(baseUrl, operation)
    void operation.finally(() => {
      if (inFlight.get(baseUrl) === operation) { inFlight.delete(baseUrl) }
    }).catch(() => undefined)
    return operation
  }

  return { discard, ensure, relinkPending, wait }
}

/** Renderer reconnect is released only after every profile session is revalidated. */
export async function revalidateNativeSessionsBeforeResume(
  baseUrls: string[],
  load: (baseUrl: string) => NativeTokenSet | null,
  ensure: (baseUrl: string) => Promise<string | null>,
  reconnect: () => void
): Promise<void> {
  for (const baseUrl of baseUrls) {
    const tokens = load(baseUrl)
    if (tokens?.logoutPending) {
      // Resume may be the first opportunity to retry an interrupted clear.
      // The tombstone still blocks reconnect even if cleanup succeeds.
      try { await ensure(baseUrl) } catch { /* Keep the saved inventory for another retry. */ }
      throw Object.assign(new Error('Native session unavailable. Sign in again in Settings → Gateway.'), { needsOauthLogin: true })
    }
    if (!tokens?.profiles?.length) {
      throw Object.assign(new Error('Native session unavailable. Sign in again in Settings → Gateway.'), { needsOauthLogin: true })
    }
    if (!await ensure(baseUrl)) {
      throw Object.assign(new Error('Native session expired. Sign in again in Settings → Gateway.'), { needsOauthLogin: true })
    }
  }
  reconnect()
}

/** A terminal native rejection must not turn into a cookie fallback. */
export function cookieFallbackAfterNativeError(error: unknown): null {
  if (typeof error === 'object' && error !== null && (error as { needsOauthLogin?: unknown }).needsOauthLogin === true) {
    throw error
  }
  return null
}

/** Keep the auth marker intact across Electron's error-stripping IPC boundary. */
export async function nativeResumeIpcResult(revalidate: () => Promise<void>) {
  try {
    await revalidate()
    return { ok: true as const }
  } catch (error) {
    if (typeof error === 'object' && error !== null && (error as { needsOauthLogin?: unknown }).needsOauthLogin === true) {
      return {
        error: error instanceof Error ? error.message : 'Native session unavailable. Sign in again in Settings → Gateway.',
        needsOauthLogin: true as const,
        ok: false as const
      }
    }
    throw error
  }
}

export { NATIVE_FLOW_ID }
