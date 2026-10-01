import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $desktopOnboarding, type DesktopOnboardingState, type OnboardingContext } from '@/store/onboarding'
import type { OAuthProvider } from '@/types/youtab'

import { FlowPanel } from './flow'

import { Picker } from '.'

function provider(id: string, name = id): OAuthProvider {
  return {
    cli_command: `youtab login ${id}`,
    docs_url: `https://example.com/${id}`,
    flow: 'pkce',
    id,
    name,
    status: { logged_in: false }
  }
}

function setProviders(providers: OAuthProvider[]) {
  $desktopOnboarding.set({
    configured: false,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  } satisfies DesktopOnboardingState)
}

const ctx: OnboardingContext = { requestGateway: async () => undefined as never }

afterEach(() => {
  cleanup()

  try {
    window.localStorage.clear()
  } catch {
    // jsdom localStorage should always be present; ignore if not.
  }

  $desktopOnboarding.set({
    configured: null,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
})

describe('onboarding Picker', () => {
  it('features Youtab Portal and hides other providers behind a disclosure', () => {
    setProviders([provider('anthropic', 'Anthropic Claude'), provider('youtab', 'Youtab Portal')])
    render(<Picker ctx={ctx} />)

    expect(screen.getByText('Youtab Portal')).toBeTruthy()
    expect(screen.getByText('Recommended')).toBeTruthy()
    // Fireworks is the always-visible #2 slot (after Youtab), even while OAuth
    // alternatives stay collapsed behind the disclosure.
    expect(screen.getByText('Fireworks AI')).toBeTruthy()
    expect(screen.queryByText('Anthropic API Key')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Other providers' }))

    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Collapse' })).toBeTruthy()
  })

  it('shows Fireworks in slot #2 ahead of other OAuth providers', () => {
    setProviders([
      provider('openai-codex', 'OpenAI Codex / ChatGPT'),
      provider('minimax-oauth', 'MiniMax'),
      provider('youtab', 'Youtab Portal')
    ])
    render(<Picker ctx={ctx} />)
    fireEvent.click(screen.getByRole('button', { name: 'Other providers' }))

    const labels = screen
      .getAllByRole('button')
      .map(el => el.textContent ?? '')
      .filter(text => /Youtab Portal|Fireworks AI|OpenAI OAuth|MiniMax|OpenRouter/.test(text))

    const indexOf = (needle: string) => labels.findIndex(text => text.includes(needle))
    expect(indexOf('Youtab Portal')).toBeGreaterThanOrEqual(0)
    expect(indexOf('Fireworks AI')).toBeGreaterThan(indexOf('Youtab Portal'))
    expect(indexOf('OpenAI OAuth')).toBeGreaterThan(indexOf('Fireworks AI'))
    expect(indexOf('MiniMax')).toBeGreaterThan(indexOf('OpenAI OAuth'))
  })

  it('shows every provider directly when Youtab Portal is absent', () => {
    setProviders([provider('anthropic', 'Anthropic Claude'), provider('openai-codex', 'OpenAI Codex / ChatGPT')])
    render(<Picker ctx={ctx} />)

    expect(screen.getByText('Fireworks AI')).toBeTruthy()
    expect(screen.getByText('Anthropic API Key')).toBeTruthy()
    expect(screen.getByText('OpenAI OAuth (ChatGPT)')).toBeTruthy()
    expect(screen.queryByText('Other sign-in options')).toBeNull()
    expect(screen.queryByText('Recommended')).toBeNull()
  })

  it('offers "choose later" on first run and persists the skip', () => {
    setProviders([provider('youtab', 'Youtab Portal')])
    render(<Picker ctx={ctx} />)

    const skip = screen.getByRole('button', { name: "I'll choose a provider later" })

    fireEvent.click(skip)

    expect($desktopOnboarding.get().firstRunSkipped).toBe(true)
    expect(window.localStorage.getItem('youtab-onboarding-skipped-v1')).toBe('1')
  })

  it('hides "choose later" in manual (add-provider) mode', () => {
    setProviders([provider('youtab', 'Youtab Portal')])
    $desktopOnboarding.set({ ...$desktopOnboarding.get(), manual: true })
    render(<Picker ctx={ctx} />)

    expect(screen.queryByRole('button', { name: "I'll choose a provider later" })).toBeNull()
  })
})

it('retries a failed provider catalog load without invoking a legacy sign-in flow', async () => {
  const api = vi.fn().mockResolvedValue({ providers: [provider('youtab', 'Youtab Portal')] })

  Object.defineProperty(window, 'youtabDesktop', { configurable: true, value: { api } })
  setProviders([])
  $desktopOnboarding.set({
    ...$desktopOnboarding.get(),
    flow: { status: 'error', message: 'Could not load sign-in providers. Retry.' }
  })

  render(<FlowPanel ctx={ctx} flow={$desktopOnboarding.get().flow} leaving={false} onBegin={() => undefined} />)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

  await waitFor(() => expect($desktopOnboarding.get().providers?.[0]?.id).toBe('youtab'))
  expect($desktopOnboarding.get().flow.status).toBe('idle')
  expect(api).toHaveBeenCalledWith(expect.objectContaining({ path: '/api/providers/oauth' }))
  expect(api).toHaveBeenCalledTimes(1)
})
