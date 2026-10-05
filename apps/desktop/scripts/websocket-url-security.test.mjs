import { describe, expect, it } from 'vitest'
import { buildYoutabWebSocketUrl } from '../../shared/src/websocket-url'

describe('WebSocket base path normalization', () => {
  it.each([
    [undefined, ''], ['', ''], ['/', ''], ['////', ''],
    ['gateway', '/gateway'], ['/gateway///', '/gateway'],
    ['/gateway//team/', '/gateway//team'], ['/gateway//team/X', '/gateway//team/X']
  ])('preserves URL semantics for %s', (basePath, normalized) => {
    expect(buildYoutabWebSocketUrl({
      basePath, path: 'api/ws', host: 'gateway.invalid', protocol: 'https:',
      authParam: ['ticket', 'synthetic ticket'], params: { profile: 'team' }
    })).toBe(`wss://gateway.invalid${normalized}/api/ws?profile=team&ticket=synthetic+ticket`)
  })

  it('handles long matching and nonmatching slash runs without changing the endpoint', () => {
    const slashes = '/'.repeat(100_000)
    for (const [basePath, normalized] of [[slashes, ''], [slashes + 'X', slashes + 'X']]) {
      expect(buildYoutabWebSocketUrl({ basePath, path: '/api/ws', host: 'gateway.invalid' }))
        .toBe(`ws://gateway.invalid${normalized}/api/ws`)
    }
  })
})
