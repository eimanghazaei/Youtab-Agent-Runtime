import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import type { ComposerAttachment } from '@/store/composer'
import { $previewTabs } from '@/store/preview'

import { AttachmentList, hasAttachmentProblem, isAttachmentAttachable, isAttachmentPending } from './attachments'

const DATA_URL = 'data:image/png;base64,iVBORw0KGgoAAAANS'

function makeAttachment(id: string, label = 'test.pdf'): ComposerAttachment {
  return { id, kind: 'file', label }
}

async function renderWithI18n(ui: React.ReactNode) {
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
        {ui}
      </I18nProvider>
    )
  })

  return result!
}

describe('AttachmentList', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders valid attachments', async () => {
    const attachments = [makeAttachment('a', 'doc.pdf'), makeAttachment('b', 'img.png')]
    await renderWithI18n(<AttachmentList attachments={attachments} />)
    expect(screen.getByText('doc.pdf')).toBeDefined()
    expect(screen.getByText('img.png')).toBeDefined()
  })

  it('renders empty list without error', async () => {
    const { container } = await renderWithI18n(<AttachmentList attachments={[]} />)

    const attachmentList = container.querySelector('[data-slot="composer-attachments"]')

    expect(attachmentList).toBeDefined()
  })

  it('does not crash when attachments array contains undefined entries', async () => {
    // Repro: session switch can leave stale/undefined entries in the
    // attachments array, causing a TypeError at attachment.refText.
    const attachments = [
      makeAttachment('a', 'good.pdf'),
      undefined as unknown as ComposerAttachment,
      makeAttachment('b', 'also-good.png')
    ]

    await expect(renderWithI18n(<AttachmentList attachments={attachments} />)).resolves.toBeTruthy()

    // Only valid attachments should render
    expect(screen.getByText('good.pdf')).toBeDefined()
    expect(screen.getByText('also-good.png')).toBeDefined()
  })

  it('does not crash when attachments array contains null entries', async () => {
    const attachments = [null as unknown as ComposerAttachment, makeAttachment('a', 'valid.txt')]

    await expect(renderWithI18n(<AttachmentList attachments={attachments} />)).resolves.toBeTruthy()

    expect(screen.getByText('valid.txt')).toBeDefined()
  })

  it('opens an attached image in the lightbox, not the preview rail', async () => {
    $previewTabs.set([])

    const image: ComposerAttachment = {
      id: 'img',
      kind: 'image',
      label: 'shot.png',
      path: '/tmp/shot.png',
      previewUrl: DATA_URL
    }

    await renderWithI18n(<AttachmentList attachments={[image]} />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /shot\.png/ }))
    })

    // The lightbox renders the full-size image in a dialog; the rail stays empty.
    const lightbox = await screen.findByRole('dialog')

    expect(lightbox.querySelector<HTMLImageElement>('img')?.src).toBe(DATA_URL)
    expect($previewTabs.get()).toHaveLength(0)
  })

  it('still routes a non-image attachment to the preview rail', async () => {
    $previewTabs.set([])

    const file: ComposerAttachment = { id: 'doc', kind: 'file', label: 'notes.md', path: '/tmp/notes.md' }

    await renderWithI18n(<AttachmentList attachments={[file]} />)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /notes\.md/ }))
    })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect($previewTabs.get().map(tab => tab.target.path)).toEqual(['/tmp/notes.md'])
  })

  it('shows a truthful scanning state and blocks preview while pending', async () => {
    const scanning: ComposerAttachment = { id: 's', kind: 'file', label: 'report.pdf', uploadState: 'scanning' }

    await renderWithI18n(<AttachmentList attachments={[scanning]} />)

    // The pill labels the scan and is not clickable-for-preview (disabled).
    const button = screen.getByRole('button', { name: /report\.pdf — Scanning/i })
    expect(button.hasAttribute('disabled')).toBe(true)
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(screen.getByText('Scanning…')).toBeDefined()
  })

  it('surfaces a quarantined attachment with a destructive label, never attachable', async () => {
    const bad: ComposerAttachment = { id: 'q', kind: 'file', label: 'evil.exe', uploadState: 'quarantined' }

    await renderWithI18n(<AttachmentList attachments={[bad]} />)

    expect(screen.getByText('Quarantined')).toBeDefined()
    expect(isAttachmentAttachable(bad)).toBe(false)
  })

  it('renders the scanner-unavailable truthful state (no fake clean)', async () => {
    const unavailable: ComposerAttachment = {
      id: 'u',
      kind: 'file',
      label: 'doc.txt',
      uploadState: 'scanner_unavailable'
    }

    await renderWithI18n(<AttachmentList attachments={[unavailable]} />)

    expect(screen.getByText('Scanner unavailable')).toBeDefined()
    expect(isAttachmentAttachable(unavailable)).toBe(false)
  })

  it('applies the strict attachability invariant (see attachment-invariant.test.ts for the full matrix)', () => {
    // Legacy undefined state is attachable ONLY with a completed attachedSessionId.
    expect(isAttachmentAttachable({ id: 'a', kind: 'file', label: 'a' })).toBe(false)
    expect(isAttachmentAttachable({ id: 'a2', kind: 'file', label: 'a', attachedSessionId: 'sess' })).toBe(true)
    // A clean scan state without a server fileId + canonical workspace is NOT attachable.
    expect(isAttachmentAttachable({ id: 'b', kind: 'file', label: 'b', uploadState: 'clean' })).toBe(false)
    expect(isAttachmentAttachable({ id: 'c', kind: 'file', label: 'c', uploadState: 'uploading' })).toBe(false)
    expect(isAttachmentAttachable({ id: 'd', kind: 'file', label: 'd', uploadState: 'rejected' })).toBe(false)

    expect(isAttachmentPending('uploading')).toBe(true)
    expect(isAttachmentPending('scanning')).toBe(true)
    expect(isAttachmentPending('clean')).toBe(false)

    expect(hasAttachmentProblem('oversized')).toBe(true)
    expect(hasAttachmentProblem('workspace_denied')).toBe(true)
    expect(hasAttachmentProblem('scanning')).toBe(false)
  })
})
