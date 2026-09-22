// Readiness handshake for the stdio JSON-RPC backend sidecar.
//
// Unlike the HTTP `youtab serve` backend (which prints a plaintext
// `YOUTAB_AGENT_BACKEND_READY port=<n>` line — see backend-ready.ts), the frozen
// tui_gateway sidecar announces readiness by emitting a JSON-RPC event
//   {"jsonrpc":"2.0","method":"event","params":{"type":"gateway.ready", ...}}
// on stdout, then reads JSON-RPC requests line-by-line from stdin. This module
// watches stdout for that event and resolves with its payload, rejecting if the
// child exits/errrors first or the deadline elapses. Injectable/pure enough to
// unit-test with a fake EventEmitter child.
import { EventEmitter } from 'node:events'

export const DEFAULT_GATEWAY_READY_TIMEOUT_MS = 90_000
export const MIN_GATEWAY_READY_TIMEOUT_MS = 45_000

export interface ReadyChildLike extends EventEmitter {
  stdout: EventEmitter
}

export interface GatewayReady {
  type: 'gateway.ready'
  payload?: unknown
}

/** Parse one stdout line; return the gateway.ready params, or null if not it. */
export function parseGatewayReadyLine(line: string): GatewayReady | null {
  const trimmed = line.trim()

  if (!trimmed || trimmed[0] !== '{' || !trimmed.includes('gateway.ready')) {
    return null
  }

  try {
    const msg = JSON.parse(trimmed)
    const params = msg?.params

    if (msg?.method === 'event' && params?.type === 'gateway.ready') {
      return { type: 'gateway.ready', payload: params.payload }
    }
  } catch {
    // Partial/garbled line — ignore; more data may complete a later line.
  }

  return null
}

export function resolveGatewayReadyTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
  const parsed = Number(env.YOUTAB_AGENT_SIDECAR_READY_TIMEOUT_MS)

  if (Number.isFinite(parsed) && parsed > 0) {
    return Math.max(MIN_GATEWAY_READY_TIMEOUT_MS, Math.round(parsed))
  }

  return DEFAULT_GATEWAY_READY_TIMEOUT_MS
}

/**
 * Resolve when the child emits the gateway.ready event on stdout. Rejects on
 * child exit/error before the event, or on timeout. A single cleanup tears down
 * every listener on every terminal path so repeated spawns don't leak slots.
 */
export function waitForGatewayReady(
  child: ReadyChildLike,
  timeoutMs: number = resolveGatewayReadyTimeoutMs()
): Promise<GatewayReady> {
  return new Promise((resolve, reject) => {
    let buf = ''
    let done = false

    function cleanup() {
      if (done) {
        return
      }

      done = true
      clearTimeout(timer)
      child.stdout.off('data', onData)
      child.off('exit', onExit)
      child.off('error', onError)
    }

    function onData(chunk: Buffer | string) {
      buf += chunk.toString()
      let nl

      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        const ready = parseGatewayReadyLine(line)

        if (ready) {
          cleanup()
          resolve(ready)

          return
        }
      }
    }

    function onExit(code: number | null, signal: string | null) {
      cleanup()
      reject(new Error(`Youtab sidecar: exited before gateway.ready (${signal || code})`))
    }

    function onError(err: Error) {
      cleanup()
      reject(err)
    }

    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`Timed out waiting for Youtab sidecar gateway.ready (${timeoutMs}ms)`))
    }, timeoutMs)

    child.stdout.on('data', onData)
    child.on('exit', onExit)
    child.on('error', onError)
  })
}
