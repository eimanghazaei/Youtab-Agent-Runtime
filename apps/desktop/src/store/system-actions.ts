import { atom } from 'nanostores'

import { translateNow } from '@/i18n'
import { notifyError } from '@/store/notifications'
import type { GatewayLifecycleAccepted } from '@/types/youtab'
import { getGatewayJob, restartGateway } from '@/youtab'

const POLL_ATTEMPTS = 18
const POLL_INTERVAL_MS = 1200

// True while a gateway restart is in flight — drives the statusbar gateway
// indicator (glyph spinner) so the restart shows up where users already look,
// instead of a toast that vanishes or a generic "Agents running" counter.
export const $gatewayRestarting = atom(false)

// Poll the lifecycle job to a terminal state, throwing on failure so the
// caller can surface it.
//
// The job, not the child's exit status: a restart whose child exits 0 while
// leaving no running gateway is a failure, and reading only the exit code
// cleared this spinner and reported success for exactly that case.
async function awaitRestart(started: GatewayLifecycleAccepted): Promise<void> {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    await new Promise(resolve => window.setTimeout(resolve, POLL_INTERVAL_MS))
    const job = await getGatewayJob(started.job_id)

    if (job.state === 'failed') {
      throw new Error(translateNow('commandCenter.gatewayRestartFailed'))
    }

    if (job.state === 'succeeded') {
      return
    }
  }
}

// Restart the messaging gateway, surfacing progress in the statusbar gateway
// indicator. Self-contained and never rejects, so every trigger — Cmd+K, the
// messaging save/toggle toasts — gets identical feedback from a plain
// `void runGatewayRestart()`, and a failure is the only thing that toasts.
export async function runGatewayRestart(): Promise<void> {
  $gatewayRestarting.set(true)

  try {
    await awaitRestart(await restartGateway())
  } catch (err) {
    notifyError(err, translateNow('commandCenter.gatewayRestartFailed'))
  } finally {
    $gatewayRestarting.set(false)
  }
}
