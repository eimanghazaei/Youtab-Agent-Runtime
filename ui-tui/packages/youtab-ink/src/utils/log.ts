export function logError(error: unknown): void {
  if (!process.env.YOUTAB_AGENT_INK_DEBUG_ERRORS) {
    return
  }

  console.error(error)
}
