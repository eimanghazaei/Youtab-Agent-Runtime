// Bounded, ownership-safe teardown for a launched Electron app in the E2E
// harness. A graceful `app.close()` is tried first; only if it does NOT settle
// within the deadline do we terminate — and then ONLY the process tree of the
// exact pid we launched (never a broad name-based kill that could hit an
// unrelated Electron/backend). This prevents a wedged teardown from hanging the
// whole serial suite without hiding product failures behind a blanket force-kill.
//
// Pure and injectable (no electron/playwright import) so the decision logic is
// unit-testable without launching a real app.
export interface BoundedCloseDeps {
  /** The graceful close (e.g. () => app.close()). */
  close: () => Promise<unknown> | unknown
  /** The owned main-process pid to tree-kill on timeout (app.process()?.pid). */
  pid?: number
  /** Deadline for the graceful close before falling back to the owned kill. */
  timeoutMs?: number
  /** Owned-tree termination (real: taskkill /PID <pid> /T /F). */
  killTree: (pid: number) => void
  /** Injectable timer for tests. */
  setTimeoutFn?: (fn: () => void, ms: number) => unknown
  /** Injectable timer clear for tests. */
  clearTimeoutFn?: (handle: unknown) => void
}

const defaultSetTimeout = (fn: () => void, ms: number): unknown => setTimeout(fn, ms)
const defaultClearTimeout = (handle: unknown): void => clearTimeout(handle as ReturnType<typeof setTimeout>)

export type BoundedCloseOutcome = 'closed' | 'killed' | 'no-pid-timeout'

/**
 * Try a graceful close; on timeout, tree-kill only the owned pid.
 *   'closed'         — graceful close settled in time (no kill)
 *   'killed'         — close timed out; owned pid tree terminated
 *   'no-pid-timeout' — close timed out but no owned pid was known (nothing killed)
 */
export async function boundedClose({
  close,
  pid,
  timeoutMs = 20_000,
  killTree,
  setTimeoutFn = defaultSetTimeout,
  clearTimeoutFn = defaultClearTimeout
}: BoundedCloseDeps): Promise<BoundedCloseOutcome> {
  let timedOut = false
  let handle: unknown

  const graceful = Promise.resolve()
    .then(() => close())
    .catch(() => undefined)

  const timeout = new Promise<void>(resolve => {
    handle = setTimeoutFn(() => {
      timedOut = true
      resolve()
    }, timeoutMs)
  })

  await Promise.race([graceful.then(() => clearTimeoutFn(handle)), timeout])

  if (!timedOut) {
    return 'closed'
  }

  if (typeof pid === 'number' && Number.isInteger(pid)) {
    killTree(pid)

    return 'killed'
  }

  return 'no-pid-timeout'
}
