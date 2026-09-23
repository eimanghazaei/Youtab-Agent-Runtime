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
// rather than trusting it. Packaged builds do not accept a process-environment
// replacement for this compiled trust anchor.

// Per-release attestation of ONE frozen Windows x64 bundle (the PyInstaller
// onedir freeze is not bit-reproducible, so rebuild and re-pin per release).
//   source: release/runtime-desktop-rc-prep 8f337b5500034fa3a20892ea420c384ec6ff5e9c
//   (no-SessionDB ack, native-target guards, lifecycle diagnostics, durable
//   prompt acceptance, no lazy installs in the frozen bundle)
//   build: win32/x64 (PE32+ AMD64), python 3.12.10, uv 0.8.17, pyinstaller 6.22.3
// This anchor is valid only for a win32-x64 package; other targets need their
// own native sidecar and anchor.
export const TRUSTED_SIDECAR_ROOT_DIGEST: string | null =
  '8af7595d595295de344b734988504fb47c44d06dc52b7b7be213ff0c62df2505'
