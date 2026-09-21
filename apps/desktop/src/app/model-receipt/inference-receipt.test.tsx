import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import type { UsageStats } from '@/types/youtab'

import { InferenceReceiptCard } from './inference-receipt'
import { classifyInferenceError, deriveInferenceReceipt, type InferenceReceipt } from './use-inference-receipt'

afterEach(cleanup)

// A realistic per-turn usage object, exactly the shape the gateway merges into
// $currentUsage from the `message.complete` payload (gateway-event.ts).
const realUsage: Partial<UsageStats> = {
  calls: 1,
  input: 1_240,
  output: 512,
  total: 1_752,
  cost_usd: 0.0123
}

const assistantReply: ChatMessage = {
  id: 'assistant-1',
  role: 'assistant',
  parts: [{ type: 'text', text: 'Here is your answer.' }]
}

describe('InferenceReceiptCard', () => {
  it('renders real metadata for a successful round-trip', () => {
    const receipt: InferenceReceipt = {
      status: 'success',
      model: 'claude-opus-4-8',
      provider: 'anthropic',
      runId: '20260921_101500_a1b2c3',
      endpoint: null,
      usage: realUsage,
      latencyMs: null,
      error: null
    }

    render(<InferenceReceiptCard receipt={receipt} />)

    expect(screen.getByTestId('receipt-status').textContent).toContain('Completed')
    // displayModelName prettifies the real model id.
    expect(screen.getByTestId('receipt-model').textContent).toContain('Opus 4 8')
    expect(screen.getByTestId('receipt-provider').textContent).toContain('anthropic')
    expect(screen.getByTestId('receipt-run-id').textContent).toContain('20260921_101500_a1b2c3')
    expect(screen.getByTestId('receipt-tokens').textContent).toContain('1,240 in / 512 out · 1,752 total')
    expect(screen.getByTestId('receipt-cost').textContent).toContain('$0.0123')
    // Fields the backend does not carry are truthfully marked, never invented.
    expect(screen.getByTestId('receipt-endpoint').textContent).toContain('not reported by backend')
    expect(screen.getByTestId('receipt-latency').textContent).toContain('not reported by backend')
    // No fabricated error/action state on success.
    expect(screen.queryByTestId('receipt-error')).toBeNull()
    expect(screen.queryByTestId('receipt-action-hint')).toBeNull()
  })

  it('shows a custom endpoint when the surface supplies one', () => {
    const receipt: InferenceReceipt = {
      status: 'success',
      model: 'qwen3.5:9b',
      provider: 'custom',
      runId: 'run-77',
      endpoint: 'http://localhost:11434/v1',
      usage: realUsage
    }

    render(<InferenceReceiptCard receipt={receipt} />)

    expect(screen.getByTestId('receipt-endpoint').textContent).toContain('http://localhost:11434/v1')
  })

  it('renders a truthful invalid-credential state with an actionable hint', () => {
    const receipt: InferenceReceipt = {
      status: 'invalid-credential',
      model: 'gpt-4o',
      provider: 'openai',
      runId: 'run-invalid',
      usage: null,
      error: '401 Unauthorized: invalid api key'
    }

    render(<InferenceReceiptCard receipt={receipt} />)

    expect(screen.getByTestId('receipt-status').textContent).toContain('Invalid credentials')
    expect(screen.getByTestId('receipt-action-hint').textContent).toContain('Check the provider API key and try again.')
    // No usage reported for a rejected call — shown as unreported, not zeroed-as-fact.
    expect(screen.getByTestId('receipt-tokens').textContent).toContain('not reported by backend')
  })

  it('renders an actionable unavailable-provider state naming the provider', () => {
    const receipt: InferenceReceipt = {
      status: 'unavailable-provider',
      model: 'llama-3.1-70b',
      provider: 'openrouter',
      runId: null,
      usage: null,
      error: 'No inference provider configured. Run `youtab model` to choose one.'
    }

    render(<InferenceReceiptCard receipt={receipt} />)

    expect(screen.getByTestId('receipt-status').textContent).toContain('Provider not set up')
    expect(screen.getByTestId('receipt-action-hint').textContent).toContain(
      'Add credentials for openrouter to run inference.'
    )
    // Never a fabricated run id / connected state when nothing ran.
    expect(screen.getByTestId('receipt-run-id').textContent).toContain('not reported by backend')
  })

  it('renders a truthful failed-inference receipt carrying the real error', () => {
    const receipt: InferenceReceipt = {
      status: 'failed',
      model: 'claude-opus-4-8',
      provider: 'anthropic',
      runId: 'run-failed',
      usage: null,
      error: 'Upstream model returned 500 internal error'
    }

    render(<InferenceReceiptCard receipt={receipt} />)

    expect(screen.getByTestId('receipt-status').textContent).toContain('Inference failed')
    expect(screen.getByTestId('receipt-error').textContent).toContain('Upstream model returned 500 internal error')
  })

  it('renders an explicit empty state before any run', () => {
    render(<InferenceReceiptCard receipt={null} />)

    expect(screen.getByText('Run a model to see its inference summary.')).not.toBeNull()
  })
})

describe('deriveInferenceReceipt / classifyInferenceError', () => {
  const baseInput = {
    busy: false,
    awaitingResponse: false,
    model: 'claude-opus-4-8',
    provider: 'anthropic',
    runtimeId: 'run-1',
    usage: realUsage
  }

  it('returns null when nothing has run on the surface', () => {
    expect(deriveInferenceReceipt({ ...baseInput, runtimeId: null, messages: [] })).toBeNull()
  })

  it('derives success from a completed assistant reply', () => {
    const receipt = deriveInferenceReceipt({ ...baseInput, messages: [assistantReply] })

    expect(receipt?.status).toBe('success')
    expect(receipt?.usage).toBe(realUsage)
    expect(receipt?.runId).toBe('run-1')
  })

  it('derives pending while a turn is in flight', () => {
    const receipt = deriveInferenceReceipt({ ...baseInput, busy: true, messages: [] })

    expect(receipt?.status).toBe('pending')
  })

  it('classifies a real terminal error on the last assistant bubble', () => {
    const errored: ChatMessage = { ...assistantReply, error: '403 forbidden: invalid credential' }
    const receipt = deriveInferenceReceipt({ ...baseInput, messages: [errored] })

    expect(receipt?.status).toBe('invalid-credential')
    expect(receipt?.error).toBe('403 forbidden: invalid credential')
  })

  it('classifies provider-setup, credential and generic errors truthfully', () => {
    expect(classifyInferenceError('No inference provider configured. Run `youtab model` to choose one.')).toBe(
      'unavailable-provider'
    )
    expect(classifyInferenceError('401 Unauthorized: invalid api key')).toBe('invalid-credential')
    expect(classifyInferenceError('Upstream model returned 500 internal error')).toBe('failed')
    expect(classifyInferenceError('')).toBe('failed')
  })
})
