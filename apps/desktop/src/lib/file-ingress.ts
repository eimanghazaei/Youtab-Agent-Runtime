/**
 * Gateway file-ingress consumer (frontend side).
 *
 * Codex Wave 2.3 correction (item 2): the earlier version INVENTED a gateway
 * method (`file.ingress`) and pushed a raw `ArrayBuffer` over JSON-RPC. That was
 * a fabricated contract. It is removed. This consumer now **fails closed**: it
 * transmits nothing and reports `scanner_unavailable` until the REAL integrated
 * Gateway file-ingress contract (method name, transport-safe encoding or upload
 * transport, response shape and casing) is delivered and wired in by the Master.
 * No method, response shape, or casing is invented here.
 *
 * Invariants that DO hold regardless of the transport:
 * - only `state === 'clean'` with a non-empty server-issued `fileId` is attachable;
 * - a file must carry a canonical, backend-issued `workspaceId` (never a profile,
 *   cwd, free text, or client-selected value) — see `lib/workspace-identity.ts`;
 * - no local absolute path is ever part of an outgoing payload.
 */

// Canonical scan lifecycle. `clean` == ready; ONLY it may attach.
export type FileScanState =
  | 'selected'
  | 'uploading'
  | 'scanning'
  | 'clean'
  | 'quarantined'
  | 'rejected'
  | 'oversized'
  | 'quota_exceeded'
  | 'unsupported_type'
  | 'scanner_unavailable'
  | 'workspace_denied'
  | 'interrupted'

export interface FileIngressResult {
  /** Server-issued; the ONLY reference sent onward. Empty until a real clean result. */
  fileId: string
  /** Canonical backend-issued workspace binding (opaque). Never a profile/cwd. */
  workspaceId: string
  state: FileScanState
  /** Display name; never an absolute path. */
  safeName: string
  sizeBytes: number
  /** Machine code for the non-clean states. */
  reasonCode?: string
}

export interface IngestFileInput {
  /** Bytes to stage. Held locally; NOT transmitted until the real transport exists. */
  bytes: ArrayBuffer
  /** Display name only — the caller must NOT pass an absolute path here. */
  safeName: string
  sizeBytes: number
  /** Canonical backend-issued workspace id (from resolveCanonicalWorkspaceId()).
   *  `null` means no workspace authority is available → fail closed. */
  workspaceId: null | string
  contentType?: string
}

export interface IngestOptions {
  /** Threaded for the real transport: when the integrated ingress lands, the
   *  request must honour this signal. Today nothing is transmitted, so an aborted
   *  signal simply yields `interrupted` and never produces a clean artifact. */
  signal?: AbortSignal
}

/**
 * Fail-closed ingest. Never fabricates a `clean` result and never invents a
 * gateway method:
 * - no canonical `workspaceId` → `workspace_denied`;
 * - aborted before anything happens → `interrupted`;
 * - otherwise → `scanner_unavailable` (the real Gateway ingress contract is not
 *   yet integrated). The bytes are held locally and NOT transmitted.
 */
export async function ingestFile(input: IngestFileInput, options?: IngestOptions): Promise<FileIngressResult> {
  const base: Omit<FileIngressResult, 'state'> = {
    fileId: '',
    workspaceId: input.workspaceId ?? '',
    safeName: input.safeName,
    sizeBytes: input.sizeBytes
  }

  if (options?.signal?.aborted) {
    return { ...base, reasonCode: 'aborted', state: 'interrupted' }
  }

  if (!input.workspaceId) {
    return { ...base, reasonCode: 'no_canonical_workspace', state: 'workspace_denied' }
  }

  // The real integrated Gateway ingress contract is not available. Do NOT invent
  // one and do NOT transmit bytes. Surface a truthful unavailable state.
  return { ...base, reasonCode: 'gateway_ingress_contract_unavailable', state: 'scanner_unavailable' }
}

/** Only a clean, server-identified file may be attached to Chat / Agent Run. */
export function isAttachable(result: Pick<FileIngressResult, 'state' | 'fileId'>): boolean {
  return result.state === 'clean' && !!result.fileId
}
