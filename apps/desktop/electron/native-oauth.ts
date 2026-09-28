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
  expiresAt: number
  provider: string
  userId: string
}

/** A renderer-supplied profile may only confirm the main process's selected profile. */
export function resolveNativeProviderProfile(requested: unknown, selected: string | null): string {
  const profile = selected || 'default'
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
    expiresAt: Number.isFinite(expiresAt) ? expiresAt : 0,
    provider: String(body?.provider || ''),
    userId: String(body?.user_id || body?.userId || '')
  }
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

/** Serialize refresh and keep a rotated response in memory until every profile write succeeds. */
export function createNativeRefreshCoordinator(deps: {
  load: (baseUrl: string) => NativeTokenSet | null
  exchange: (baseUrl: string, tokens: NativeTokenSet) => Promise<NativeTokenSet>
  writeProfile: (profile: string, token: string | null) => Promise<void>
  commit: (baseUrl: string, tokens: NativeTokenSet) => void
  clear: (baseUrl: string) => void
  now: () => number
}) {
  const inFlight = new Map<string, Promise<string | null>>()
  const pending = new Map<string, NativeTokenSet>()

  function discard(baseUrl: string) {
    pending.delete(baseUrl)
  }

  async function wait(baseUrl: string): Promise<void> {
    try {
      await inFlight.get(baseUrl)
    } catch {
      // Login/logout still needs to proceed after a transient refresh error.
    }
  }

  function ensure(baseUrl: string, force = false): Promise<string | null> {
    const active = inFlight.get(baseUrl)
    if (active) { return active }

    const operation = (async () => {
      const tokens = deps.load(baseUrl)
      if (!tokens) { return null }
      const now = deps.now()
      const profileTokenNeedsRefresh = Boolean(tokens.profiles?.length)
        && inferenceRefreshDelayMs(tokens, now) <= 0
      if (!force && !pending.has(baseUrl) && !tokenNeedsRefresh(tokens, now)
          && !profileTokenNeedsRefresh) { return tokens.accessToken }

      if (!tokens.refreshToken) {
        for (const profile of tokens.profiles || []) { await deps.writeProfile(profile, null) }
        deps.clear(baseUrl)
        return null
      }

      let rotated = pending.get(baseUrl)
      if (!rotated) {
        try {
          rotated = await deps.exchange(baseUrl, tokens)
        } catch (error: any) {
          if (error?.statusCode !== 401) { throw error }
          for (const profile of tokens.profiles || []) { await deps.writeProfile(profile, null) }
          deps.clear(baseUrl)
          return null
        }
        rotated.profiles = tokens.profiles
        if (rotated.profiles?.length && !rotated.inferenceAccessToken) {
          throw new Error('Gateway refresh omitted inference credential')
        }
        pending.set(baseUrl, rotated)
      }
      const updated: string[] = []
      try {
        for (const profile of rotated.profiles || []) {
          await deps.writeProfile(profile, rotated.inferenceAccessToken!)
          updated.push(profile)
        }
        deps.commit(baseUrl, rotated)
      } catch (error) {
        // A multi-profile write is not atomic; restore completed profiles to
        // the previous usable credential (or clear an expired credential).
        const previous = unexpiredInferenceToken(tokens, deps.now())
        for (const profile of updated.reverse()) { await deps.writeProfile(profile, previous) }
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

  return { discard, ensure, wait }
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
    if (!tokens?.profiles?.length) { throw new Error('Native session revalidation pending') }
    if (!await ensure(baseUrl)) { throw new Error('Native session revalidation pending') }
  }
  reconnect()
}

export { NATIVE_FLOW_ID }
