/** Stage one account credential in the already authenticated local Runtime WS. */
export function isTrustedSimorghRendererUrl(actualUrl: string, expectedUrl: string): boolean {
  try {
    const actual = new URL(actualUrl)
    const expected = new URL(expectedUrl)
    return actual.protocol === expected.protocol && actual.host === expected.host
      && actual.pathname === expected.pathname
  } catch {
    return false
  }
}

export function stageSimorghAccountTurn(
  wsUrl: string,
  params: { session_id: string; prompt_sha256: string; account_bearer: string },
  WebSocketImpl: any = globalThis.WebSocket
): Promise<string> {
  return new Promise((resolve, reject) => {
    let socket: any
    let settled = false

    const finish = (nonce?: string) => {
      if (settled) { return }
      settled = true
      clearTimeout(timer)

      try { socket?.close() } catch { /* already closed */ }

      if (nonce) { resolve(nonce) }
      else { reject(new Error('Simorgh account handoff failed')) }
    }

    const timer = setTimeout(() => finish(), 8_000)

    try {
      socket = new WebSocketImpl(wsUrl)
      socket.addEventListener('open', () => {
        socket.send(JSON.stringify({
          jsonrpc: '2.0', id: 'simorgh-account-stage', method: 'simorgh.account.stage', params
        }))
      })
      socket.addEventListener('message', (event: any) => {
        let frame: any

        try { frame = JSON.parse(String(event.data)) } catch { return }

        if (frame?.id !== 'simorgh-account-stage') { return }
        const nonce = frame?.result?.nonce
        finish(typeof nonce === 'string' && /^[0-9a-f]{32}$/.test(nonce) ? nonce : undefined)
      })
      socket.addEventListener('error', () => finish())
      socket.addEventListener('close', () => finish())
    } catch {
      finish()
    }
  })
}
