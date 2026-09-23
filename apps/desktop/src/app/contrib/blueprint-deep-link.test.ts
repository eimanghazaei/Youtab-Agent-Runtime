import { describe, expect, it } from 'vitest'

import { blueprintCommandFromDeepLink } from './blueprint-deep-link'

describe('blueprintCommandFromDeepLink', () => {
  it('quotes an entire slot value for the backend shlex parser', () => {
    const command = blueprintCommandFromDeepLink({
      kind: 'blueprint',
      name: 'morning-brief',
      params: { topic: 'my "boss" \\ notes and O\'Brien' }
    })
    expect(command).toBe(`/blueprint morning-brief topic='my "boss" \\ notes and O'"'"'Brien'`)
  })

  it('rejects command and slot injection in an external deep link', () => {
    expect(blueprintCommandFromDeepLink({ kind: 'blueprint', name: 'x\n/other', params: {} })).toBeNull()
    expect(blueprintCommandFromDeepLink({ kind: 'blueprint', name: 'morning-brief', params: { 'bad key': 'x' } })).toBeNull()
    expect(blueprintCommandFromDeepLink({ kind: 'blueprint', name: 'morning-brief', params: { topic: 'x\n/other' } })).toBeNull()
  })
})
