// Bounded restart policy for the backend sidecar. Pure and deterministic so the
// "restart on crash, but stop after N failures within a window" behavior is
// unit-testable without real timers or a real child.
//
// A crashed sidecar is restarted up to `maxRestarts` times inside a rolling
// `windowMs`. Exhausting the budget latches into a terminal failure (the UI
// surfaces a dead backend) instead of an unbounded respawn loop that would pile
// up orphaned processes. A crash after the window has elapsed since the first
// counted crash resets the budget (transient blips don't permanently burn it).
export interface RestartPolicyOptions {
  maxRestarts?: number
  windowMs?: number
  /** Backoff for the Nth restart (1-based). Default: capped exponential. */
  backoffMs?: (attempt: number) => number
}

export const DEFAULT_MAX_RESTARTS = 3
export const DEFAULT_WINDOW_MS = 60_000

function defaultBackoff(attempt: number): number {
  // 500ms, 1s, 2s, 4s … capped at 10s.
  return Math.min(10_000, 500 * 2 ** (attempt - 1))
}

export interface RestartDecision {
  restart: boolean
  attempt: number
  delayMs: number
  remaining: number
  reason: 'restart' | 'budget-exhausted' | 'shutting-down'
}

/**
 * Tracks restart budget across crash events. `intentionalStop()` marks a
 * deliberate teardown (app quit / superseded connection) so the next exit is
 * never treated as a crash to restart.
 */
export class SidecarRestartPolicy {
  private readonly maxRestarts: number
  private readonly windowMs: number
  private readonly backoffMs: (attempt: number) => number
  private crashTimes: number[] = []
  private stopping = false

  constructor(options: RestartPolicyOptions = {}) {
    this.maxRestarts = options.maxRestarts ?? DEFAULT_MAX_RESTARTS
    this.windowMs = options.windowMs ?? DEFAULT_WINDOW_MS
    this.backoffMs = options.backoffMs ?? defaultBackoff
  }

  /** Mark that any subsequent exit is an intentional teardown, not a crash. */
  intentionalStop(): void {
    this.stopping = true
  }

  /** Reset after a healthy, long-lived run (e.g. once ready & stable). */
  reset(): void {
    this.crashTimes = []
  }

  /**
   * Record a crash at time `now` and decide whether to restart.
   * Prunes crashes older than the window before counting.
   */
  onCrash(now: number = Date.now()): RestartDecision {
    if (this.stopping) {
      return { restart: false, attempt: 0, delayMs: 0, remaining: 0, reason: 'shutting-down' }
    }

    this.crashTimes = this.crashTimes.filter(t => now - t < this.windowMs)
    this.crashTimes.push(now)

    const attempt = this.crashTimes.length

    if (attempt > this.maxRestarts) {
      return {
        restart: false,
        attempt,
        delayMs: 0,
        remaining: 0,
        reason: 'budget-exhausted'
      }
    }

    return {
      restart: true,
      attempt,
      delayMs: this.backoffMs(attempt),
      remaining: this.maxRestarts - attempt,
      reason: 'restart'
    }
  }
}
