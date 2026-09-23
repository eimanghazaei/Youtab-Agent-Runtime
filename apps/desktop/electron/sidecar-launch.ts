// Build the spawn descriptor for the self-contained backend sidecar and the
// helpers that assert its security properties. Pure and injectable (no electron
// import) so every property below is unit-testable without launching Electron.
//
// Security properties enforced/asserted here:
//   • ephemeral per-launch secret, handed over the loopback stdio channel via
//     the environment — NEVER on argv (argv is world-readable via ps/Task Mgr);
//   • loopback-only: the stdio JSON-RPC gateway opens no listening socket, and
//     the descriptor carries no non-loopback host token;
//   • a dedicated, app-owned userData home (YOUTAB_AGENT_HOME) distinct from any
//     developer checkout;
//   • USERPROFILE / HOME present so the child's Python `Path.home()` resolves.
import { randomBytes as nodeRandomBytes } from 'node:crypto'

/** Env var carrying the ephemeral per-launch handshake secret. */
export const SIDECAR_SECRET_ENV = 'YOUTAB_AGENT_SIDECAR_SECRET'
/** Explicit loopback-only bind marker (the stdio gateway must never listen externally). */
export const SIDECAR_BIND_ENV = 'YOUTAB_AGENT_SIDECAR_BIND'
export const LOOPBACK_HOST = '127.0.0.1'

/** Cryptographically-random per-launch secret (32 bytes, url-safe base64). */
export function generateEphemeralSecret(randomBytes: (n: number) => Buffer = nodeRandomBytes): string {
  return randomBytes(32).toString('base64url')
}

export interface BuildSidecarLaunchInput {
  /** Absolute path to the frozen backend executable. */
  executable: string
  /** Dedicated, app-owned home for the sidecar (config/sessions/logs). */
  userDataDir: string
  /** Ephemeral per-launch secret. */
  secret: string
  /** Home dir for USERPROFILE/HOME so Python `Path.home()` resolves. */
  homeDir: string
  /** Base env to inherit from (defaults to process.env). */
  env?: NodeJS.ProcessEnv
  platform?: NodeJS.Platform
  /** Extra env the caller wants merged (never allowed to carry the secret on argv). */
  extraEnv?: Record<string, string>
}

export interface SidecarLaunchDescriptor {
  command: string
  args: string[]
  env: Record<string, string>
  stdio: ['pipe', 'pipe', 'pipe']
}

/**
 * Build the spawn descriptor. The stdio gateway takes no network arguments, so
 * argv stays empty — the secret and all configuration travel via env.
 */
export function buildSidecarLaunch({
  executable,
  userDataDir,
  secret,
  homeDir,
  env = process.env,
  platform = process.platform,
  extraEnv = {}
}: BuildSidecarLaunchInput): SidecarLaunchDescriptor {
  const base: Record<string, string> = {}

  for (const [k, v] of Object.entries(env)) {
    if (typeof v === 'string') {
      base[k] = v
    }
  }

  const homeKeys = platform === 'win32' ? { USERPROFILE: homeDir } : { HOME: homeDir }

  const finalEnv: Record<string, string> = {
    ...base,
    ...extraEnv,
    // Dedicated app-owned home — pins config/sessions/logs away from any
    // developer checkout or the user's global ~/.youtab-agent-runtime.
    YOUTAB_AGENT_HOME: userDataDir,
    // Ephemeral per-launch handshake secret — env only, never argv.
    [SIDECAR_SECRET_ENV]: secret,
    // Loopback-only marker: the gateway must not open an external socket.
    [SIDECAR_BIND_ENV]: LOOPBACK_HOST,
    // UTF-8 mode so early child stdio decodes correctly on non-UTF-8 Windows.
    PYTHONUTF8: env.PYTHONUTF8 ?? '1',
    // Path.home() anchor.
    ...homeKeys
  }

  return { command: executable, args: [], env: finalEnv, stdio: ['pipe', 'pipe', 'pipe'] }
}

/** True if the secret leaks into argv (must always be false). */
export function secretInArgv(args: readonly string[], secret: string): boolean {
  return args.some(a => a.includes(secret))
}

/**
 * Redact the ephemeral secret (and the env var assignment form) from a log
 * line so it never reaches the desktop log or crash forensics.
 */
export function redactSecret(line: string, secret: string): string {
  if (!secret) {
    return line
  }

  const escaped = secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  return String(line)
    .replace(new RegExp(escaped, 'g'), '«redacted»')
    .replace(new RegExp(`${SIDECAR_SECRET_ENV}=\\S+`, 'g'), `${SIDECAR_SECRET_ENV}=«redacted»`)
}

const NON_LOOPBACK_HOST = /\b(0\.0\.0\.0|::|\[::\]|(?!127\.)\d{1,3}(?:\.\d{1,3}){3})\b/

/**
 * True when the descriptor exposes no non-loopback bind: no external host token
 * in argv, and the bind marker (if present) is the loopback host. A stdio
 * gateway also opens no socket at all, which this preserves by construction
 * (empty argv, no host flags).
 */
export function isLoopbackOnly(descriptor: SidecarLaunchDescriptor): boolean {
  const bind = descriptor.env[SIDECAR_BIND_ENV]

  if (bind && bind !== LOOPBACK_HOST) {
    return false
  }

  return !descriptor.args.some(a => NON_LOOPBACK_HOST.test(a))
}
