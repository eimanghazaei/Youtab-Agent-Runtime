import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TerminalDemo as BootstrapTerminal } from '../../../../packages/youtab-ui-bootstrap/src/ui/components/terminal-demo'
import { TerminalDemo as MainTerminal } from '../../../../packages/youtab-ui/src/ui/components/terminal-demo'

let startDemo: () => void

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: IntersectionObserverCallback) {
      startDemo = () => callback([{ isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
    }
    observe() {}
    disconnect() {}
  })
})
afterEach(() => {
  cleanup()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe.each([['main', MainTerminal], ['bootstrap', BootstrapTerminal]] as const)('%s TerminalDemo', (_name, Terminal) => {
  it('keeps nested story spans but removes active output markup and attributes', async () => {
    const { container } = render(<Terminal sequence={[
      { type: 'output', lines: [
        '<span class="opacity-70" onclick="window.synthetic=1">Saved <span class="text-midground">report.md</span></span>',
        '<img src="https://invalid.example/x" onerror="window.synthetic=1"><script>window.synthetic=1</script>',
        '<span style="color:red" class="unknown opacity-50">safe text</span>'
      ] }, { type: 'pause', ms: 5000 }
    ]} />)

    await act(async () => { startDemo(); await vi.advanceTimersByTimeAsync(150) })

    expect(container.querySelector('.opacity-70 .text-midground')?.textContent).toBe('report.md')
    expect(container.querySelector('.opacity-50')?.textContent).toBe('safe text')
    expect(container.querySelector('img,script,[onclick],[style="color:red"],.unknown')).toBeNull()
    expect(container.querySelector('.blink')).toBeTruthy()
  })

  it('renders prompt and typed tags literally and still clears the terminal', async () => {
    const text = '<img onerror="synthetic()">'

    const { container } = render(<Terminal sequence={[
      { type: 'prompt', text }, { type: 'type', text, delay: 1 },
      { type: 'pause', ms: 1000 }, { type: 'clear' }, { type: 'pause', ms: 5000 }
    ]} />)

    await act(async () => { startDemo(); await vi.advanceTimersByTimeAsync(text.length + 10) })

    expect(container.textContent).toContain(text + text)
    expect(container.querySelector('img,[onerror]')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })

    expect(container.textContent).not.toContain(text)
  })
})
