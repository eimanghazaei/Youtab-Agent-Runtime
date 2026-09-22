// Binding readiness (Codex #3): when a sibling provider ships its exact-SHA
// schema, conformance must compare THIS Frontend consumer expectation against
// the provider's ACTUAL exported schema — not test the Frontend fixture against
// itself. This module is that comparison seam. It stays inert until a real
// provider schema is passed in; nothing here marks a provider verified.

/** A structural drift between a consumer expectation and a provider schema. */
export interface SchemaDrift {
  /** Dotted path to the differing node (e.g. `OperationReceipt.source`). */
  path: string
  /** Keys/members the expectation has that the provider schema lacks. */
  missingInProvider: string[]
  /** Keys/members the provider schema has that the expectation lacks. */
  extraInProvider: string[]
}

export interface SchemaComparison {
  /** True only when there is zero drift at every compared node. */
  matches: boolean
  drift: SchemaDrift[]
}

function keysOrMembers(value: unknown): string[] {
  if (Array.isArray(value)) {
    return [...value].map(String).sort()
  }

  if (value && typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>).sort()
  }

  return []
}

function diffAt(path: string, expected: unknown, provider: unknown, out: SchemaDrift[]): void {
  const e = keysOrMembers(expected)
  const p = keysOrMembers(provider)

  if (e.length === 0 && p.length === 0) {
    return
  }

  const missingInProvider = e.filter(k => !p.includes(k))
  const extraInProvider = p.filter(k => !e.includes(k))

  if (missingInProvider.length > 0 || extraInProvider.length > 0) {
    out.push({ path, missingInProvider, extraInProvider })
  }

  // Recurse into shared object children (not arrays — arrays are leaf unions).
  if (!Array.isArray(expected) && expected && typeof expected === 'object') {
    const eo = expected as Record<string, unknown>
    const po = (provider && typeof provider === 'object' ? provider : {}) as Record<string, unknown>

    for (const key of Object.keys(eo)) {
      if (key in po) {
        diffAt(path ? `${path}.${key}` : key, eo[key], po[key], out)
      }
    }
  }
}

/**
 * Compare a Frontend consumer-expectation contract body against a provider's
 * exported schema of the same shape. Returns the structural drift. A caller
 * binds a contract only when `matches` is true AND an independent exact-SHA
 * review passes — this function alone never binds or verifies anything.
 */
export function compareExpectationToProviderSchema(
  expectation: Record<string, unknown>,
  providerSchema: Record<string, unknown>
): SchemaComparison {
  const drift: SchemaDrift[] = []
  diffAt('', expectation, providerSchema, drift)

  return { matches: drift.length === 0, drift }
}
