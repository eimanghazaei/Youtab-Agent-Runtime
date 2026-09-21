/**
 * Canonical workspace identity (frontend side).
 *
 * Codex Wave 2.3 correction (item 3): the local-file ingest previously used the
 * active gateway PROFILE as the workspace id. A profile, cwd, free text, or any
 * client-selected value is NOT a workspace identity. The canonical, authenticated
 * workspace id must come from the REAL backend authority.
 *
 * That backend authority endpoint is not integrated yet, so this resolver fails
 * closed: it returns `null` and never substitutes a client-side value. Callers
 * that receive `null` must reject the operation fail-closed (e.g. `workspace_denied`),
 * never fall back to a profile/cwd.
 *
 * Expected real contract (to be wired when the backend delivers it):
 *   GET the authenticated session's canonical workspace → `{ workspaceId: string }`
 *   The frontend must also reject a response whose workspaceId is missing or does
 *   not match the workspace the file/grant was issued for.
 */

/** True only when the real backend workspace-authority is wired in (not yet). */
export function isWorkspaceAuthorityAvailable(): boolean {
  return false
}

/**
 * Resolve the canonical authenticated workspace id from the backend authority.
 * Returns `null` until that authority is integrated — never a profile/cwd/free
 * text substitute.
 */
export function resolveCanonicalWorkspaceId(): null | string {
  // No backend workspace-authority contract is integrated. Fail closed.
  return null
}

/**
 * Fail-closed match check: a file/grant is usable only when its issued workspace
 * id is non-empty AND equals the canonical authenticated workspace id. With no
 * authority available (null), nothing matches.
 */
export function workspaceIdMatches(issuedWorkspaceId: null | string | undefined): boolean {
  const canonical = resolveCanonicalWorkspaceId()

  return !!canonical && !!issuedWorkspaceId && canonical === issuedWorkspaceId
}
