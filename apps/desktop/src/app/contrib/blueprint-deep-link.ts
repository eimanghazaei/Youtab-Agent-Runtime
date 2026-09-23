interface BlueprintDeepLink {
  kind: string
  name: string
  params: Record<string, string>
}

const BLUEPRINT_NAME = /^[a-z0-9][a-z0-9-]*$/i
const SLOT_NAME = /^[a-z_][a-z0-9_-]*$/i
const hasControlCharacter = (value: string): boolean =>
  Array.from(value).some((char) => char.charCodeAt(0) < 0x20 || char.charCodeAt(0) === 0x7f)

// The backend parses /blueprint arguments with Python shlex.split.
function quoteShlex(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

export function blueprintCommandFromDeepLink(payload: BlueprintDeepLink): string | null {
  if (payload.kind !== 'blueprint' || !BLUEPRINT_NAME.test(payload.name)) {
    return null
  }

  const params = Object.entries(payload.params || {})
  if (params.some(([key, value]) =>
    !SLOT_NAME.test(key) ||
    key === '__proto__' || key === 'prototype' || key === 'constructor' ||
    typeof value !== 'string' || hasControlCharacter(value)
  )) {
    return null
  }

  const slots = params.map(([key, value]) => `${key}=${quoteShlex(value)}`).join(' ')
  return `/blueprint ${payload.name}${slots ? ` ${slots}` : ''}`
}
