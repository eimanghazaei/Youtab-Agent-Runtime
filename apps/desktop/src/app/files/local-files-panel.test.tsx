import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { YoutabFolderGrant } from '@/global'
import { I18nProvider } from '@/i18n/context'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $currentCwd } from '@/store/session'

import { LocalFilesPanel } from './local-files-panel'

const ABS_A = '/Users/me/secret/report.pdf'
const ABS_B = '/Users/me/secret/data.csv'

function makeGrant(overrides: Partial<YoutabFolderGrant> = {}): YoutabFolderGrant {
  return {
    grantId: 'g1',
    safeLabel: 'Projects',
    permission: 'read-only',
    status: 'active',
    workspaceId: 'default',
    createdAt: '2026-09-21T00:00:00.000Z',
    ...overrides
  }
}

interface BridgeOptions {
  selectPaths?: string[]
  folderGrants?: unknown
}

function stubDesktop({ selectPaths = [], folderGrants }: BridgeOptions) {
  const value: Record<string, unknown> = {
    selectPaths: vi.fn(async () => selectPaths),
    readFileDataUrl: vi.fn(async () => 'data:text/plain;base64,aGk='),
    getConnection: vi.fn(async () => ({ mode: 'local' })),
    getPathForFile: (file: File) => (file as unknown as { _path?: string })._path ?? '',
    api: vi.fn()
  }

  if (folderGrants) {
    value.folderGrants = folderGrants
  }

  Object.defineProperty(window, 'youtabDesktop', { configurable: true, value })
}

async function renderPanel() {
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
        <LocalFilesPanel />
      </I18nProvider>
    )
  })

  return result!
}

function fileDropEvent(paths: string[]) {
  const files = paths.map(path => {
    const file = new File(['x'], path.split('/').pop() || 'f')

    ;(file as unknown as { _path: string })._path = path

    return file
  })

  const fileList = { length: files.length, item: (i: number) => files[i] ?? null } as unknown as FileList

  return {
    dataTransfer: {
      types: ['Files'],
      files: fileList,
      items: files.map(file => ({ kind: 'file', getAsFile: () => file, webkitGetAsEntry: () => null }))
    }
  }
}

describe('LocalFilesPanel', () => {
  beforeEach(() => {
    $currentCwd.set('')
    $activeGatewayProfile.set('default')
    $connection.set(null)
  })

  afterEach(() => {
    cleanup()
    Object.defineProperty(window, 'youtabDesktop', { configurable: true, value: undefined })
    vi.restoreAllMocks()
  })

  it('adds a single selected file as a chip', async () => {
    stubDesktop({ selectPaths: [ABS_A] })
    await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })

    await waitFor(() => expect(screen.getByText('report.pdf')).toBeDefined())
  })

  it('adds multiple selected files', async () => {
    stubDesktop({ selectPaths: [ABS_A, ABS_B] })
    await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeDefined()
      expect(screen.getByText('data.csv')).toBeDefined()
    })
  })

  it('never leaks an absolute local path into the rendered panel', async () => {
    stubDesktop({ selectPaths: [ABS_A] })
    const { container } = await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })

    await waitFor(() => expect(screen.getByText('report.pdf')).toBeDefined())
    expect(container.textContent).not.toContain('/Users/me/secret')
  })

  it('deduplicates the same file added twice', async () => {
    stubDesktop({ selectPaths: [ABS_A] })
    await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })
    await waitFor(() => expect(screen.getAllByText('report.pdf')).toHaveLength(1))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })

    await waitFor(() => expect(screen.getAllByText('report.pdf')).toHaveLength(1))
  })

  it('attaches files dropped onto the drop zone', async () => {
    stubDesktop({ selectPaths: [] })
    const { container } = await renderPanel()

    const dropZone = container.querySelector(
      '[data-slot="local-files-panel"] [aria-label="Drop files here to attach"]'
    )!

    await act(async () => {
      fireEvent.drop(dropZone, fileDropEvent([ABS_B]))
    })

    await waitFor(() => expect(screen.getByText('data.csv')).toBeDefined())
  })

  it('removes a chip via its cancel control', async () => {
    stubDesktop({ selectPaths: [ABS_A] })
    await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })
    await waitFor(() => expect(screen.getByText('report.pdf')).toBeDefined())

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove report.pdf' }))
    })

    await waitFor(() => expect(screen.queryByText('report.pdf')).toBeNull())
  })

  it('clears pending attachments when the workspace changes', async () => {
    stubDesktop({ selectPaths: [ABS_A] })
    await renderPanel()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Add files' }))
    })
    await waitFor(() => expect(screen.getByText('report.pdf')).toBeDefined())

    await act(async () => {
      $activeGatewayProfile.set('other-profile')
    })

    await waitFor(() => expect(screen.queryByText('report.pdf')).toBeNull())
  })

  it('shows the truthful "Local Runtime required" state when the folder bridge is absent', async () => {
    stubDesktop({ selectPaths: [] })
    const { container } = await renderPanel()

    expect(screen.getByText('Local Runtime required')).toBeDefined()
    expect(container.querySelector('[data-slot="runtime-required"]')).not.toBeNull()
    // No grant button when the bridge is unavailable.
    expect(screen.queryByRole('button', { name: /Grant a folder/i })).toBeNull()
  })

  it('grants and revokes a folder through the real bridge when available', async () => {
    const grants: YoutabFolderGrant[] = []

    const folderGrants = {
      request: vi.fn(async () => {
        const grant = makeGrant()
        grants.push(grant)

        return grant
      }),
      list: vi.fn(async () => [...grants]),
      revoke: vi.fn(async (grantId: string) => {
        const index = grants.findIndex(g => g.grantId === grantId)

        if (index >= 0) {
          grants.splice(index, 1)
        }

        return { revoked: true as const }
      }),
      read: vi.fn(),
      write: vi.fn()
    }

    stubDesktop({ selectPaths: [], folderGrants })
    await renderPanel()

    const grantButton = screen.getByRole('button', { name: 'Grant a folder (read-only)' })

    await act(async () => {
      fireEvent.click(grantButton)
    })

    await waitFor(() => expect(screen.getByText('Projects')).toBeDefined())
    expect(folderGrants.request).toHaveBeenCalledWith({ readOnly: true })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Revoke access to Projects' }))
    })

    await waitFor(() => expect(screen.queryByText('Projects')).toBeNull())
    expect(folderGrants.revoke).toHaveBeenCalledWith('g1')
  })

  it('lists existing active grants on mount', async () => {
    const folderGrants = {
      request: vi.fn(),
      list: vi.fn(async () => [makeGrant({ safeLabel: 'Docs' }), makeGrant({ grantId: 'g2', status: 'revoked' })]),
      revoke: vi.fn(),
      read: vi.fn(),
      write: vi.fn()
    }

    stubDesktop({ selectPaths: [], folderGrants })
    await renderPanel()

    await waitFor(() => expect(screen.getByText('Docs')).toBeDefined())
    // The revoked grant is filtered out.
    expect(screen.getAllByText('Docs')).toHaveLength(1)
  })
})
