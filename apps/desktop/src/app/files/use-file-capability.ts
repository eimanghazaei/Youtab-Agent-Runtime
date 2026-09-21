import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'

import { $gatewayState } from '@/store/session'
import type { StatusResponse } from '@/types/youtab'
import { getStatus } from '@/youtab'

/**
 * Runtime capability state for the local secure-file-ingest + workspace-authority
 * feature (Wave 2.5, review items 2/3/4).
 *
 * This is NOT a static flag: it is derived from a REAL authenticated Gateway
 * runtime signal — the live `$gatewayState` handshake plus the `GET /api/status`
 * response — never a constant, build flag, interface presence or mock.
 *
 * The approved Gateway file-ingress + workspace-authority capability would be
 * advertised by the Gateway status (see the minimal contract documented in the
 * Wave 2.5 addendum). It is NOT present in the current `StatusResponse` (the
 * approved Gateway SHA is not integrated), so a connected gateway resolves to
 * `unavailable` **with a reason derived from the real response**, not a hardcoded
 * `false`. It flips to `available` automatically once the Gateway advertises it.
 */
export type CapabilityState = 'available' | 'error' | 'loading' | 'unavailable'

export interface FileCapability {
  state: CapabilityState
  /** Human-readable dependency status / remediation, or null when available. */
  reason: null | string
}

export const FILE_CAPABILITY_QUERY_KEY = ['file-ingest-capability'] as const

/**
 * True only when the live Gateway status advertises BOTH the secure file-ingress
 * scanner and the workspace-authority capability. Reads optional `features` flags
 * the approved Gateway is expected to publish; absent today → false.
 */
function statusAdvertisesFileIngest(status: StatusResponse): boolean {
  const features = (status as unknown as { features?: Record<string, unknown> }).features

  return features?.file_ingress === true && features?.workspace_authority === true
}

export function useFileCapability(): FileCapability {
  const gatewayState = useStore($gatewayState)
  const connected = gatewayState === 'open'

  // Only probe the backend when a real authenticated socket is open.
  const query = useQuery({ enabled: connected, queryFn: getStatus, queryKey: FILE_CAPABILITY_QUERY_KEY, staleTime: 0 })

  if (gatewayState === 'idle') {
    return { reason: 'Connecting to the Local Runtime…', state: 'loading' }
  }

  if (!connected) {
    return {
      reason: 'The Local Runtime is not connected. Start it to enable secure file scanning.',
      state: 'unavailable'
    }
  }

  if (query.isLoading) {
    return { reason: 'Checking gateway capability…', state: 'loading' }
  }

  if (query.isError) {
    return { reason: 'Could not read the gateway status. Retry once the Local Runtime is healthy.', state: 'error' }
  }

  if (query.data && statusAdvertisesFileIngest(query.data)) {
    return { reason: null, state: 'available' }
  }

  return {
    reason:
      'The gateway does not advertise the secure file-ingress + workspace-authority capability. It activates automatically once the approved Gateway SHA is integrated.',
    state: 'unavailable'
  }
}
