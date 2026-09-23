// Committed trust anchor for the packaged backend sidecar (A2).
//
// SECURITY: the trusted root digest lives HERE, inside the signed/trusted
// Electron code — never read from the shipped bundle itself (that would be
// circular: an attacker who can replace the sidecar tree can also replace a
// digest file sitting next to it). At spawn time the main process recomputes
// the bundle's root digest and compares it to this value; a mismatch is
// fail-closed (the sidecar is refused).
//
// The value is the reproducible `root_digest` emitted by build-sidecar.mjs
// (A1). Until a reproducible digest is pinned by the release build, it is null
// and the integrity gate REFUSES to launch an unverifiable shipped sidecar
// rather than trusting it. A staged rollout / CI can supply the anchor out of
// band via YOUTAB_AGENT_SIDECAR_TRUSTED_DIGEST.

const HEX64 = /^[0-9a-f]{64}$/

// Pinned at release time from the controlled build (build-sidecar.mjs): the
// root digest of the frozen `youtab-backend` (youtab_agent_cli.main `serve`)
// bundle shipped via extraResources. Recomputed at launch and compared; a
// mismatch is refused fail-closed. Rebuild + re-pin when the bundle changes.
//   build: python 3.12.10 (PINNED via SIDECAR_BUILD_PYTHON), uv 0.8.17,
//   pyinstaller 6.22.3 (win32/x64); entry youtab_agent_cli.main via
//   sidecar_main.py (loopback-only enforced).
//   ⚠ The PyInstaller onedir freeze is NOT bit-reproducible (clean builds on the
//   same pinned toolchain gave 54e4823b…, 0b2ba35d…, 60933699…, 83823e99…,
//   13f2ad93…, 976c5255…, 1848c7d9…, 04d255cd…, 6aa04cc1… and a794ac3b…), so this anchor is a per-RELEASE attestation of one
//   specific frozen bundle — re-pin it (and rebuild the installer) on every
//   sidecar build. Re-pinned for the Runtime lanes-integration packaged build
//   that INCLUDES the attachment-persistence fix (3eac0af5), the whole-handler
//   crash-marker ownership (_finalize_turn_ack + finally gate), the exception-path
//   marker-preservation fix (_emit_terminal_turn_error no longer retires the
//   marker), and the DETERMINISTIC per-row durable-write fault seam at the real
//   chokepoint (run_agent._flush_messages_to_session_db_unlocked; raises before
//   the assistant-row append_message, after earlier rows have committed) —
//   frozen into the backend (toolchain unchanged: python 3.12.10, uv 0.8.17,
//   pyinstaller 6.22.3).
//   Adds the durability-signal propagation: _persist_session returns the DB
//   flush result and _finalize_turn_ack treats a returned-False (silently
//   swallowed per-row write failure) as not-committed, preserving the marker.
export const TRUSTED_SIDECAR_ROOT_DIGEST: string | null =
  '7495afdef949ff9dc2e9dc1db3dc23436616ca1b0abf4aa56bb96b93fcdb562c'

/**
 * Resolve the effective trust anchor: an operator/CI env override (must be a
 * 64-hex sha256) wins over the baked constant, otherwise the baked constant
 * (which may be null).
 */
export function resolveTrustedSidecarDigest(env: NodeJS.ProcessEnv = process.env): string | null {
  const override = (env.YOUTAB_AGENT_SIDECAR_TRUSTED_DIGEST || '').trim()

  if (HEX64.test(override)) {
    return override
  }

  return TRUSTED_SIDECAR_ROOT_DIGEST
}
