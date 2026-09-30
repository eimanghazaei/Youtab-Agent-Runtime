import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { closeSecondaryGateways, openGatewayForProfile } from '@/store/gateway'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $gatewayState } from '@/store/session'

import { takeGatewaySurvivor } from './gateway-hmr-survivor'
import { useGatewayBoot } from './use-gateway-boot'

// End-to-end-ish repro of the "remote VPS → stuck on CONNECTING, no Settings"
// bug that drives the REAL useGatewayBoot hook + REAL YoutabGateway through a
// fake WebSocket we fully control. No Docker / no real port: from the desktop's
// point of view a "remote VPS" is just a WebSocket that opens once and later
// refuses to reopen, so that is exactly (and only) what we fake.
//
// The previous test (gateway-connecting-overlay.test.tsx) hand-set the stores
// and asserted the overlays; this one proves the HOOK actually PRODUCES that
// stuck store combo — closing the "inferred by reading code" gap on the
// post-boot reconnect loop.

type Listener = (ev: unknown) => void
let connectionApplied: null | (() => void) = null
let powerResume: null | ((event?: { authChanged?: boolean; authBaseUrl?: string; nativeRecovery?: { kind: 'auth' | 'transport'; baseUrl: string; profiles?: string[] } }) => void) = null

// Minimal WebSocket stand-in implementing only what json-rpc-gateway.connect()
// touches: readyState, add/removeEventListener('open'|'error'|'close'), close().
class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  // Flipped by the test: 'open' = next socket connects; 'fail' = next socket
  // errors (a dead remote). Mirrors a VPS going away after the first connect.
  static mode: 'open' | 'fail' | 'hold' = 'open'
  static instances: FakeWebSocket[] = []

  readyState = 0
  private listeners: Record<string, Set<Listener>> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
    if (FakeWebSocket.mode === 'hold') { return }
    const willOpen = FakeWebSocket.mode === 'open'
    // Resolve on the next microtask/macrotask so connect()'s promise wiring is
    // in place before open/error fires (matches real async socket handshake).
    setTimeout(() => {
      if (willOpen) {
        this.open()
      } else {
        this.readyState = FakeWebSocket.CLOSED
        this.emit('error', {})
      }
    }, 0)
  }

  addEventListener(type: string, fn: Listener) {
    ;(this.listeners[type] ??= new Set()).add(fn)
  }

  removeEventListener(type: string, fn: Listener) {
    this.listeners[type]?.delete(fn)
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open', {})
  }

  // Force-drop an open socket, as a sleeping laptop / restarted remote would.
  drop() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  private emit(type: string, ev: unknown) {
    for (const fn of this.listeners[type] ?? []) {
      fn(ev)
    }
  }
}

function fakeDesktop(authMode: 'oauth' | 'token' = 'token', mode?: 'local' | 'remote') {
  const conn = {
    authMode,
    baseUrl: 'https://vps.example.com',
    mode,
    profile: 'default',
    token: 't',
    wsUrl: 'wss://vps.example.com/api/ws?token=t'
  }

  return {
    revalidateConnection: vi.fn(async () => ({ ok: true, rebuilt: false })),
    getConnection: vi.fn(async () => conn),
    getGatewayWsUrl: vi.fn(async () => conn.wsUrl),
    getBootProgress: vi.fn(async () => ({
      error: null,
      fakeMode: false,
      message: '',
      phase: 'init',
      progress: 0,
      running: true,
      timestamp: Date.now()
    })),
    onBootProgress: vi.fn(() => () => undefined),
    onBackendExit: vi.fn(() => () => undefined),
    onConnectionApplied: vi.fn(callback => {
      connectionApplied = callback

      return () => {
        connectionApplied = null
      }
    }),
    onPowerResume: vi.fn(callback => {
      powerResume = callback
      return () => { powerResume = null }
    }),
    onWindowStateChanged: vi.fn(() => () => undefined),
    touchBackend: vi.fn(async () => undefined),
    profile: { get: vi.fn(async () => ({ profile: 'default' })) }
  }
}

function Harness({
  beforeConnectionSwitch = () => undefined,
  refreshSessions
}: { beforeConnectionSwitch?: () => void; refreshSessions?: () => Promise<void> } = {}) {
  useGatewayBoot({
    beforeConnectionSwitch,
    handleGatewayEvent: () => undefined,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshYoutabConfig: async () => undefined,
    refreshSessions: refreshSessions ?? (async () => undefined)
  })

  return null
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  clearNotifications()
  // Drop any parked gateway left by a prior file/case (globalThis slot).
  const leftover = takeGatewaySurvivor()

  if (leftover) {
    try {
      leftover.gateway.close()
    } catch {
      // ignore
    }
  }

  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  connectionApplied = null
  powerResume = null
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop()
  $gatewayState.set('idle')
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: '',
    phase: 'init',
    progress: 0,
    running: true,
    timestamp: Date.now(),
    visible: true
  })
})

afterEach(() => {
  cleanup()
  closeSecondaryGateways()
  // Vitest keeps import.meta.hot truthy, so the boot effect's cleanup parks an
  // open gateway instead of tearing it down (the real HMR path). Drain + close
  // that survivor so the next test boots a fresh socket instead of adoptBoot().
  const survivor = takeGatewaySurvivor()

  if (survivor) {
    try {
      survivor.gateway.close()
    } catch {
      // ignore
    }
  }

  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { youtabDesktop?: unknown }).youtabDesktop
})

// Let pending microtasks (awaits) AND the queued 0ms socket open/error fire.
async function flushAsync() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

// Drive the exponential backoff forward by its full cap so the next scheduled
// reconnect attempt actually runs (1s,2s,4s,8s,15s,15s…). Returns after the
// attempt's async work settles.
async function advanceBackoff() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })
}

describe('useGatewayBoot remote reconnect loop (real hook, fake socket)', () => {
  it('does not reconnect a dropped socket until main-process revalidation succeeds', async () => {
    const desktop = fakeDesktop()
    let allowed = false
    desktop.revalidateConnection = vi.fn(async () => {
      if (!allowed) { throw new Error('Inference refresh pending') }
      return { ok: true, rebuilt: false }
    })
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop
    render(<Harness />)
    await flushAsync()
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => FakeWebSocket.instances[0].drop())
    await advanceBackoff()
    expect(desktop.revalidateConnection).toHaveBeenCalled()
    expect(FakeWebSocket.instances).toHaveLength(1)

    allowed = true
    await advanceBackoff()
    await flushAsync()
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('surfaces the serialized native sign-in result without a generic reconnect error', async () => {
    const desktop = fakeDesktop()
    let signedIn = false
    const ipcResult = JSON.parse(JSON.stringify({
      error: 'Native session unavailable. Sign in again in Settings → Gateway.',
      needsOauthLogin: true,
      ok: false
    }))
    desktop.revalidateConnection = vi.fn(async () =>
      signedIn ? { ok: true, rebuilt: false } : ipcResult
    )
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop

    render(<Harness />)
    await flushAsync()
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => FakeWebSocket.instances[0].drop())
    await advanceBackoff()
    expect($desktopBoot.get().error).toMatch(/Gateway sign-in required/i)
    expect(FakeWebSocket.instances).toHaveLength(1)
    const revalidations = desktop.revalidateConnection.mock.calls.length

    for (let i = 0; i < 8; i += 1) { await advanceBackoff() }
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(revalidations)
    expect($desktopBoot.get().error).toMatch(/Gateway sign-in required/i)
    expect($desktopBoot.get().error).not.toMatch(/connection lost/i)

    act(() => powerResume?.())
    await flushAsync()
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(revalidations)

    signedIn = true
    act(() => powerResume?.({ authChanged: true }))
    await flushAsync()
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('re-mints a remote OAuth socket only when its session authority changes', async () => {
    const desktop = fakeDesktop('oauth', 'remote')
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop
    render(<Harness />)
    await flushAsync()
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => powerResume?.({ authChanged: true, authBaseUrl: 'https://provider.example.com' }))
    await flushAsync()
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
    expect(FakeWebSocket.instances).toHaveLength(1)

    act(() => powerResume?.({ authChanged: true, authBaseUrl: 'https://vps.example.com' }))
    await flushAsync()
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(desktop.revalidateConnection).toHaveBeenCalled()
    expect($gatewayState.get()).toBe('open')
  })

  it('keeps a local provider socket open after native provider auth changes', async () => {
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop('token', 'local')
    render(<Harness />)
    await flushAsync()

    act(() => powerResume?.({ authChanged: true, authBaseUrl: 'https://vps.example.com' }))
    await flushAsync()
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
  })

  it('re-mints open background OAuth sockets for the changed authority', async () => {
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop('oauth', 'remote')
    render(<Harness />)
    await flushAsync()
    const backgroundOpen = openGatewayForProfile('background')
    await flushAsync()
    await act(async () => backgroundOpen)
    expect(FakeWebSocket.instances).toHaveLength(2)

    act(() => powerResume?.({ authChanged: true, authBaseUrl: 'https://vps.example.com' }))
    await flushAsync()
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances[1].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances).toHaveLength(4)
    expect(FakeWebSocket.instances[2].readyState).toBe(FakeWebSocket.OPEN)
    expect(FakeWebSocket.instances[3].readyState).toBe(FakeWebSocket.OPEN)
  })

  it('shows sign-in recovery when native profile reconciliation exhausts retries', async () => {
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop('oauth')
    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    act(() => powerResume?.({ nativeRecovery: { kind: 'auth', baseUrl: 'https://vps.example.com' } }))
    expect($desktopBoot.get().error).toMatch(/Gateway sign-in required/i)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('moves exhausted native refresh transport failure into the existing reconnect recovery', async () => {
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop('oauth')
    render(<Harness />)
    await flushAsync()
    FakeWebSocket.mode = 'fail'

    act(() => powerResume?.({ nativeRecovery: { kind: 'transport', baseUrl: 'https://vps.example.com' } }))
    await flushAsync()
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)

    for (let i = 0; i < 7; i += 1) { await advanceBackoff() }
    expect($desktopBoot.get().error).toMatch(/lost connection/i)
  })

  it.each([
    ['a background Gateway', 'oauth', 'https://provider.example.com'],
    ['a provider-only session at the same URL', 'token', 'https://vps.example.com']
  ] as const)('ignores native recovery for %s', async (_label, authMode, baseUrl) => {
    const desktop = fakeDesktop(authMode)
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop
    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    for (const kind of ['auth', 'transport'] as const) {
      act(() => powerResume?.({ nativeRecovery: { kind, baseUrl } }))
    }
    await flushAsync()

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
    expect($desktopBoot.get().error).toBeNull()
    expect(desktop.revalidateConnection).not.toHaveBeenCalled()
  })

  it('prompts local provider recovery for the linked live profile without closing its backend', async () => {
    const desktop = fakeDesktop('token', 'local')
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop
    render(<Harness />)
    await flushAsync()

    act(() => powerResume?.({ nativeRecovery: {
      kind: 'transport', baseUrl: 'https://api.youtab.io', profiles: ['sibling']
    } }))
    expect($notifications.get()).toHaveLength(0)

    act(() => powerResume?.({ nativeRecovery: {
      kind: 'transport', baseUrl: 'https://api.youtab.io', profiles: ['default']
    } }))
    expect($notifications.get()[0]).toMatchObject({
      id: 'native-provider-recovery:default',
      kind: 'warning',
      action: { label: 'Open Accounts' }
    })
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
    act(() => $notifications.get()[0].action?.onClick())
    expect(window.location.hash).toBe('#/settings?tab=providers&pview=accounts')
  })

  it('routes exhausted relink recovery to the renamed live profile', async () => {
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = fakeDesktop('token', 'local')
    render(<Harness />)
    await flushAsync()
    act(() => $activeGatewayProfile.set('renamed'))

    act(() => powerResume?.({ nativeRecovery: {
      kind: 'auth', baseUrl: 'https://api.youtab.io', profiles: ['old']
    } }))
    expect($notifications.get()).toHaveLength(0)

    act(() => powerResume?.({ nativeRecovery: {
      kind: 'auth', baseUrl: 'https://api.youtab.io', profiles: ['renamed']
    } }))
    expect($notifications.get()[0]).toMatchObject({ id: 'native-provider-recovery:renamed' })
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
  })

  it('clears the sign-in recovery overlay after auth changes while the socket remains open', async () => {
    const desktop = fakeDesktop()
    let signedIn = false
    desktop.revalidateConnection = vi.fn(async () => signedIn
      ? { ok: true, rebuilt: false }
      : JSON.parse(JSON.stringify({ ok: false, needsOauthLogin: true, error: 'Native session unavailable' })))
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop

    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    act(() => powerResume?.())
    await flushAsync()
    expect($desktopBoot.get().error).toMatch(/Gateway sign-in required/i)

    signedIn = true
    act(() => powerResume?.({ authChanged: true }))
    await flushAsync()
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('ignores stale revalidation when sign-in completes during a reconnect attempt', async () => {
    const desktop = fakeDesktop()
    let finishOldRevalidation!: (result: any) => void
    desktop.revalidateConnection = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finishOldRevalidation = resolve }))
      .mockResolvedValue({ ok: true, rebuilt: false })
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop

    render(<Harness />)
    await flushAsync()
    act(() => FakeWebSocket.instances[0].drop())
    await advanceBackoff()
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)

    act(() => powerResume?.({ authChanged: true }))
    await act(async () => {
      finishOldRevalidation(JSON.parse(JSON.stringify({
        ok: false, needsOauthLogin: true, error: 'old native session expired'
      })))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(2)
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('closes a stale socket that opens after sign-in and dials again', async () => {
    const desktop = fakeDesktop()
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop
    render(<Harness />)
    await flushAsync()

    FakeWebSocket.mode = 'hold'
    act(() => FakeWebSocket.instances[0].drop())
    await advanceBackoff()
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect($gatewayState.get()).not.toBe('open')

    FakeWebSocket.mode = 'open'
    act(() => powerResume?.({ authChanged: true }))
    act(() => FakeWebSocket.instances[1].open())
    await flushAsync()

    expect(FakeWebSocket.instances[1].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances).toHaveLength(3)
    expect($gatewayState.get()).toBe('open')
  })

  it('INITIAL boot against a dead VPS: getConnection hangs (waitForYoutab) → app sits in the connecting combo, then fails', async () => {
    // The report's actual path: a fresh launch pointed at an unreachable VPS.
    // startYoutab()'s remote branch awaits waitForYoutab() for 45s before it
    // throws, so the renderer's `await desktop.getConnection()` stays pending
    // that whole window. During it: gatewayState is still 'idle' (connect was
    // never reached) and boot.error is null → connecting=true → the fullscreen
    // CONNECTING overlay, latched, blocking Settings.
    let rejectConn: (e: Error) => void = () => undefined
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(
      () =>
        new Promise((_resolve, reject) => {
          rejectConn = reject
        })
    )
    ;(window as { youtabDesktop?: unknown }).youtabDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // getConnection is still pending — the dead-VPS wait. No socket was ever
    // created, gatewayState never left idle, boot.error is null.
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // ^ connecting === true here → fullscreen CONNECTING, no Settings.

    // After ~45s waitForYoutab gives up and getConnection rejects → boot()
    // catch → failDesktopBoot → the BootFailureOverlay recovery surface.
    await act(async () => {
      rejectConn(new Error('Youtab backend did not become ready: timeout'))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('resets the old machine context before connecting an applied gateway', async () => {
    const beforeConnectionSwitch = vi.fn()
    render(<Harness beforeConnectionSwitch={beforeConnectionSwitch} />)
    await flushAsync()
    expect(connectionApplied).not.toBeNull()

    act(() => connectionApplied?.())
    expect(beforeConnectionSwitch).toHaveBeenCalledTimes(1)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')
  })

  it('a remote that drops post-boot keeps looping with NO boot.error (the dead-end CONNECTING combo)', async () => {
    render(<Harness />)
    await flushAsync()

    // Initial boot connected.
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    expect(FakeWebSocket.instances).toHaveLength(1)

    // The remote VPS goes away: drop the live socket, and make every reopen
    // fail from here on.
    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Burn a couple backoff cycles BEFORE the escalation threshold (<6 attempts,
    // ~the first ~15s). This is the window where stock and fixed behave the
    // same: socket down, hook retrying, gatewayState non-open, boot.error still
    // null → CONNECTING covers the screen with no recovery surface. (Past ~45s
    // the fix raises boot.error; that's asserted in the next test.)
    await advanceBackoff()

    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // It is actively retrying, not idle — more sockets were minted.
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
  })

  it('FIX: after the prolonged drop the hook raises a recoverable boot error (the escape hatch)', async () => {
    render(<Harness />)
    await flushAsync()
    expect($desktopBoot.get().error).toBeNull()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Walk the backoff past the >=6 attempt threshold (~45s of failures).
    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    // The hook surfaced the recoverable error → BootFailureOverlay (Use local
    // gateway / Sign in / Retry) becomes reachable instead of CONNECTING.
    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('FIX: a successful reconnect clears the recoverable error', async () => {
    render(<Harness />)
    await flushAsync()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    expect($desktopBoot.get().error).toBeTruthy()

    // The remote comes back: next reconnect attempt opens.
    FakeWebSocket.mode = 'open'
    await advanceBackoff()

    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('FIX: a failed session-list fetch during boot is non-fatal — the app still boots', async () => {
    // The version-skew report: gateway WS connects fine, but refreshSessions()
    // rejects (e.g. older backend 404s an endpoint the fallback didn't cover,
    // or a transient read error). That must NOT reject boot() into
    // failDesktopBoot's "Youtab couldn't start" overlay — the socket is open
    // and the app is fully usable with an empty sidebar.
    const refreshSessions = vi.fn(async () => {
      throw new Error('404: {"detail":"No such API endpoint: /api/profiles/sessions/sidebar"}')
    })

    render(<Harness refreshSessions={refreshSessions} />)
    await flushAsync()

    expect(refreshSessions).toHaveBeenCalled()
    expect($gatewayState.get()).toBe('open')
    // Boot completed: no error, overlay dismissed.
    expect($desktopBoot.get().error).toBeNull()
    expect($desktopBoot.get().visible).toBe(false)
    expect($desktopBoot.get().phase).toBe('renderer.ready')
  })
})
