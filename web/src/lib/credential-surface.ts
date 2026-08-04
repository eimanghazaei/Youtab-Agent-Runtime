/**
 * Which built-in pages disappear when this deployment has no credential
 * surface, and how to remove them.
 *
 * Presentation only. The server refuses the underlying routes on its own
 * authority — see `youtab_agent_cli/credential_entitlement.py`, and the
 * refusals pinned in `tests/youtab_agent_cli/test_credential_surface_refused.py`.
 * Nothing here protects anything; a client that skipped it entirely would be
 * refused exactly the same. What it avoids is a sidebar advertising a page
 * that can only answer 403.
 *
 * These live outside `App.tsx` so the rule can be tested as a pure function
 * rather than by mounting the whole application, which is how the rest of this
 * directory is tested.
 */

/** Models (raw engine selection) and Keys (credential entry). */
export const CREDENTIAL_SURFACE_PATHS: readonly string[] = ["/models", "/env"];

/**
 * Drop the credential-surface entries from a nav list.
 *
 * `offered` is the server's answer, not a local preference. Callers default it
 * to `false` until the server has actually replied: an unreachable or
 * unrecognised server is not evidence that a surface exists, and guessing
 * "yes" produces a visible entry leading to a refusal.
 */
export function filterCredentialSurfaceNav<T extends { path: string }>(
  items: readonly T[],
  offered: boolean,
): T[] {
  if (offered) return [...items];
  return items.filter((item) => !CREDENTIAL_SURFACE_PATHS.includes(item.path));
}

/**
 * Drop the credential-surface routes from a route table.
 *
 * Unlike `/analytics` — which keeps its route and explains itself when its
 * flag is off — these pages have nothing to say without the surface behind
 * them: every call they make would be refused, so the page renders as a wall
 * of errors. Removing the route sends a deep link to the catch-all redirect
 * instead. Returns a new object; the input is not mutated.
 */
export function filterCredentialSurfaceRoutes<T>(
  routes: Readonly<Record<string, T>>,
  offered: boolean,
): Record<string, T> {
  const next: Record<string, T> = { ...routes };
  if (offered) return next;
  for (const path of CREDENTIAL_SURFACE_PATHS) delete next[path];
  return next;
}
